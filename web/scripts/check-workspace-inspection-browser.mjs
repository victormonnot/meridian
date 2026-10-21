/**
 * Exercise pre-estimation inspection through real imported synthetic packages.
 * Generate faulty.zip, no-reference.zip, reset.zip and foreign-clock.zip with
 * scripts/make-inspection-fixture.py first. Run only on an authorized deployment.
 * Keep exact QA identities for backup/restore and operator cleanup; never save
 * credentials, cookies, physical sensor payloads or fabricated API responses.
 */
import assert from 'node:assert/strict';
import { createHash, randomBytes } from 'node:crypto';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { resolve, sep } from 'node:path';
import { pathToFileURL } from 'node:url';

const [baseArgument, outputArgument, fixtureArgument, ...extra] = process.argv.slice(2);
if (!baseArgument || !outputArgument || !fixtureArgument || extra.length) {
  throw new Error('Usage: node web/scripts/check-workspace-inspection-browser.mjs WORKSPACE_URL OUTPUT_DIRECTORY FIXTURE_DIRECTORY');
}
const address = new URL(baseArgument);
if (!['http:', 'https:'].includes(address.protocol) || address.username || address.password || address.search || address.hash) {
  throw new Error('Use an explicit HTTP(S) workspace URL without credentials, query or fragment.');
}
const base = address.href, output = resolve(outputArgument) + sep, fixtures = resolve(fixtureArgument);
const hash = data => createHash('sha256').update(data).digest('hex');
const fixtureInfo = {};
for (const name of ['faulty', 'no-reference', 'reset', 'foreign-clock']) {
  const bytes = await readFile(`${fixtures}/${name}.zip`);
  assert.ok(bytes.length < 1024 * 1024, 'Use the small analytic inspection fixtures.');
  fixtureInfo[name] = { sha256: hash(bytes), size_bytes: bytes.length };
}
await mkdir(output, { recursive: true });
const moduleUrl = process.env.MERIDIAN_PLAYWRIGHT_MODULE ? pathToFileURL(resolve(process.env.MERIDIAN_PLAYWRIGHT_MODULE)).href
  : new URL('../../build/browser-tools/node_modules/playwright/index.mjs', import.meta.url).href;
const { chromium } = await import(moduleUrl);
const browser = await chromium.launch({ headless: true,
  ...(process.env.MERIDIAN_BROWSER_EXECUTABLE ? { executablePath: process.env.MERIDIAN_BROWSER_EXECUTABLE } : {}) });
const context = await browser.newContext({ viewport: { width: 1440, height: 1100 } });
const peerContext = await browser.newContext();
const page = await context.newPage(), peer = await peerContext.newPage();
const stamp = `${Date.now().toString(36)}-${randomBytes(3).toString('hex')}`;
const usernames = [`qa-inspect-${stamp}`, `qa-inspect-peer-${stamp}`];
const password = `Qa!${randomBytes(24).toString('base64url')}`;
const accounts = [], checks = [], errors = [], errorDetails = [], cspErrors = [], created = {}, deleted = [];
let projectId, failure, contentSecurityPolicy, inspectionIdentity;
const check = (label, condition) => { assert.ok(condition, label); checks.push(label); };
const collection = () => `/projects/${projectId}/acquisitions`;
const path = id => `${collection()}/${id}/inspection`;
const route = id => `${base}#project/${projectId}/data/acquisitions/${id}/inspection`;
for (const target of [page, peer]) {
  target.on('pageerror', error => {
    errors.push(error.message);
    errorDetails.push({ message: error.message, stack: error.stack, after_check: checks.at(-1) ?? null });
  });
  target.on('console', message => {
    if (message.type() === 'error' && /content security policy|violat.*(?:directive|policy)|refused to (?:load|execute|apply|connect)/i.test(message.text())) cspErrors.push(message.text());
  });
}
async function progress(state) {
  await writeFile(`${output}progress.json`, JSON.stringify({ state, accounts, project_id: projectId, acquisitions: created, checks: checks.length }, null, 2) + '\n');
  console.log(state);
}
async function api(target, path, method = 'GET', body) {
  return target.evaluate(async ({ path, method, body }) => {
    const session = await (await fetch('/api/v1/session', { cache: 'no-store' })).json();
    const response = await fetch(`/api/v1${path}`, { method, cache: 'no-store',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': session.csrf_token },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    const bytes = await response.arrayBuffer();
    const text = new TextDecoder().decode(bytes);
    const response_sha256 = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)),
      value => value.toString(16).padStart(2, '0')).join('');
    let payload;
    try { payload = JSON.parse(text); } catch { /* No successful binary bodies use this helper. */ }
    return { status: response.status, payload, response_sha256, headers: Object.fromEntries(response.headers) };
  }, { path, method, body });
}
async function register(target, username) {
  await target.goto(base);
  await target.getByRole('button', { name: 'Create account', exact: true }).click();
  await target.getByLabel('Username', { exact: true }).fill(username);
  await target.getByLabel('Password', { exact: true }).fill(password);
  const [response] = await Promise.all([
    target.waitForResponse(item => new URL(item.url()).pathname === '/api/v1/auth/register' && item.request().method() === 'POST'),
    target.getByRole('button', { name: 'Create account', exact: true }).click(),
  ]);
  if (response.ok()) accounts.push(username);
  await target.getByRole('heading', { name: 'Projects', exact: true }).waitFor();
}
async function upload(name) {
  await page.goto(`${base}#project/${projectId}/data`);
  await page.getByRole('button', { name: 'Upload recording', exact: true }).click();
  await page.getByLabel('Acquisition name', { exact: true }).fill(`Synthetic inspection · ${name}`);
  await page.getByLabel('Recording archive', { exact: true }).setInputFiles(`${fixtures}/${name}.zip`);
  const [response] = await Promise.all([
    page.waitForResponse(item => new URL(item.url()).pathname === `/api/v1${collection()}` && item.request().method() === 'POST'),
    page.getByRole('button', { name: 'Start upload', exact: true }).click(),
  ]);
  assert.equal(response.status(), 201);
  const value = await response.json();
  created[name] = value.acquisition.id;
  await page.waitForURL(url => url.hash === `#project/${projectId}/data/acquisitions/${created[name]}`, { timeout: 300_000 });
  await page.locator('.acquisition-detail .acquisition-status').filter({ hasText: /^Ready to inspect$/ }).waitFor({ timeout: 300_000 });
  const saved = (await api(page, `${collection()}/${created[name]}`)).payload.acquisition;
  check(`${name} original retains its independent source SHA and byte count`,
    saved.sha256 === fixtureInfo[name].sha256 && saved.size_bytes === fixtureInfo[name].size_bytes);
  await page.getByRole('link', { name: 'Inspect sensor data', exact: true }).click();
  await loaded();
  return created[name];
}
async function loaded() {
  await page.locator('.inspection-statistics').waitFor();
  await page.waitForFunction(() => !document.querySelector('[data-inspection-error]')?.textContent
    && Boolean(document.querySelector('#inspection-cursor')));
}
async function chooseStream(id) {
  await page.getByRole('combobox', { name: 'Sensor stream', exact: true }).selectOption(id);
  await page.waitForFunction(id => document.querySelector('.inspection-statistics .section-heading')?.textContent.includes(`· ${id} /`), id);
}
async function interval(first, last) {
  await page.getByLabel('Interval start · s', { exact: true }).fill(first);
  await page.getByLabel('Interval end · s', { exact: true }).fill(last);
  const response = page.waitForResponse(item => new URL(item.url()).pathname.endsWith('/inspection/window')
    && new URL(item.url()).searchParams.get('stream_id') === 'imu0');
  await page.getByRole('button', { name: 'Apply interval', exact: true }).click();
  await response;
  await page.locator('.inspection-statistics').waitFor();
}

try {
  contentSecurityPolicy = await (await page.goto(base)).headerValue('content-security-policy');
  await register(page, usernames[0]);
  await page.getByRole('button', { name: /New project/ }).click();
  await page.getByLabel('Project name', { exact: true }).fill('Sensor inspection acceptance');
  await page.getByRole('button', { name: 'Create project', exact: true }).click();
  await page.getByRole('heading', { name: 'Acquisitions', exact: true }).waitFor();
  projectId = new URL(page.url()).hash.split('/')[1];
  await progress('IMPORTING_FAULTY_BUT_VALID_OBSERVATIONS');
  const id = await upload('faulty');
  await page.waitForFunction(() => [...document.querySelectorAll('[data-inspection-images] img')].length === 2
    && [...document.querySelectorAll('[data-inspection-images] img')].every(image => image.complete && image.naturalWidth > 0));
  await page.locator('[data-inspection-linked-status]').filter({ hasText: 'Linked IMU: imu0' }).waitFor();
  const summaryResponse = await api(page, path(id));
  const summary = summaryResponse.payload.inspection;
  check('inspection is available before any estimator output exists', summary.status === 'available'
    && summary.evidence.estimate_support === 'not_generated'
    && summary.evidence.calibration_suitability === 'not_assessed'
    && summary.evidence.estimator_compatibility === 'not_assessed');
  check('full source counts are independent of display points', summary.streams.find(item => item.id === 'imu0').sample_count === 361);
  const camera = summary.streams.find(item => item.id === 'cam0'), segment = camera.segments[0];
  const query = new URLSearchParams({ stream_id: camera.id, segment_id: segment.id, first_ns: segment.first_ns, last_ns: segment.last_ns });
  const cameraWindowResponse = await api(page, `${path(id)}/window?${query}`);
  const cameraWindow = cameraWindowResponse.payload.window;
  check('partial reference supports exactly 13 of 17 camera observations', cameraWindow.support.find(item => item.stream_id === 'pose0').supported_samples === 13);
  check('IMU cadence gap independently removes support at exactly two camera observations', cameraWindow.support.find(item => item.stream_id === 'imu0').supported_samples === 15);
  check('private summary and window responses prohibit caching', [summaryResponse, cameraWindowResponse].every(item => item.headers['cache-control'].includes('no-store')));
  check('both preview images decode within their display bound', await page.locator('[data-inspection-images] img').evaluateAll(images =>
    images.length === 2 && images.every(image => image.naturalWidth <= 320 && image.naturalHeight <= 320 && image.src.startsWith('blob:'))));
  const preview = await context.request.get(`${address.origin}/api/v1${path(id)}/images/cam0/0`);
  const previewBytes = await preview.body(), headers = preview.headers();
  check('owned preview bytes and headers identify the exact source image', preview.status() === 200
    && headers['x-source-sha256'] === cameraWindow.display.points[0].image.source_sha256
    && headers['x-sample-id'] === '0' && headers['x-timestamp-ns'] === segment.first_ns
    && headers['x-content-sha256'] === hash(previewBytes) && headers['cache-control'].includes('no-store'));
  inspectionIdentity = { summary_response_sha256: summaryResponse.response_sha256,
    camera_window_response_sha256: cameraWindowResponse.response_sha256,
    camera_window_query: Object.fromEntries(query), preview: { stream_id: 'cam0', sample_id: '0',
      sha256: hash(previewBytes), size_bytes: previewBytes.length, source_sha256: headers['x-source-sha256'] } };
  await page.getByRole('button', { name: 'Next displayed observation', exact: true }).click();
  check('linked cursor advances exact camera sample identity without epoch rounding',
    (await page.locator('[data-inspection-cursor-detail]').textContent()).includes('sample 1')
    && (await page.locator('[data-inspection-cursor-detail]').textContent()).includes(cameraWindow.display.points[1].timestamp_ns));
  await chooseStream('imu0');
  check('all six IMU axis traces are present', await page.locator('.inspection-chart .plot-legend').allTextContents()
    .then(labels => labels.filter(label => label === 'XYZ').length === 2));
  check('each IMU axis is split at the cross-chunk source gap', await page.locator('.inspection-chart .plot-line').count() === 12);
  check('full-resolution statistics retain the 361 source observations', (await page.locator('.inspection-statistics').textContent()).includes('361 source observations'));
  check('source gap endpoints remain exact and visible', (await page.locator('.inspection-events').textContent()).includes('255 → 256')
    && (await page.locator('.inspection-events').textContent()).includes('102.5 ms'));
  await interval('0.65', '0.7');
  check('range submission remains isolated from workspace mutation forms', errors.length === 0);
  check('an interval entirely inside a gap does not conceal the gap or invent samples',
    (await page.locator('.inspection-statistics').textContent()).includes('0 source observations')
    && (await page.locator('.inspection-events').textContent()).includes('102.5 ms'));
  await page.getByRole('button', { name: 'Full segment', exact: true }).click();
  await loaded();
  await chooseStream('cam0');
  await page.locator('[data-inspection-linked-status]').filter({ hasText: 'Linked IMU: imu0' }).waitFor();
  await page.waitForFunction(() => [...document.querySelectorAll('[data-inspection-images] img')].length === 2
    && [...document.querySelectorAll('[data-inspection-images] img')].every(image => image.complete && image.naturalWidth > 0));
  await page.getByLabel('Appearance', { exact: true }).selectOption('dark');
  await page.screenshot({ path: `${output}inspection-dark.png`, fullPage: true });
  await page.getByLabel('Appearance', { exact: true }).selectOption('light');
  await page.screenshot({ path: `${output}inspection-light.png`, fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  check('inspection remains within the mobile viewport', await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.screenshot({ path: `${output}inspection-mobile.png`, fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1100 });
  await progress('CHECKING_PRIVATE_READ_BOUNDARIES');
  await peer.goto(base);
  const privatePaths = [path(id), `${path(id)}/window?${query}`, `${path(id)}/images/cam0/0`];
  for (const privatePath of privatePaths) check('anonymous inspection endpoint returns 401',
    (await peerContext.request.get(`${address.origin}/api/v1${privatePath}`)).status() === 401);
  await register(peer, usernames[1]);
  for (const privatePath of privatePaths) check('foreign inspection endpoint returns 404',
    (await peerContext.request.get(`${address.origin}/api/v1${privatePath}`)).status() === 404);
  await peer.goto(route(id));
  await peer.getByRole('heading', { name: 'Acquisition unavailable', exact: true }).waitFor();
  check('foreign deep links display neither observations nor previews', await peer.locator('#acquisition-inspection-content').count() === 0);
  await progress('CHECKING_ABSENT_REFERENCE_AND_CLOCK_SEGMENTS');
  await upload('no-reference');
  check('missing reference leaves inspection available without fabricated accuracy',
    (await page.locator('.inspection-evidence').textContent()).includes('No reference included')
    && (await page.locator('.inspection-reference-absent').textContent()).includes('unavailable'));
  await upload('reset');
  check('reset ambiguity prevents automatic cross-stream IMU alignment',
    await page.locator('[data-inspection-linked] option').count() === 1);
  await chooseStream('imu0');
  const choices = await page.locator('[data-inspection-segment] option').evaluateAll(options => options.map(option => option.value));
  check('a reset remains a separate selectable clock segment', choices.length === 2);
  await page.getByRole('combobox', { name: 'Clock segment', exact: true }).selectOption(choices[1]);
  await page.locator('[data-inspection-conventions] .inspection-boundary').waitFor();
  check('reset segment is explicitly identified without stitching its timeline', (await page.locator('[data-inspection-conventions]').textContent()).includes('reset or overlap')
    && await page.getByLabel('Interval start · s', { exact: true }).inputValue() === '0');
  const foreignId = await upload('foreign-clock');
  check('independent clock declarations remain non-comparable despite similar timestamp values',
    await page.locator('[data-inspection-linked] option').count() === 1
    && (await page.locator('.inspection-comparisons').textContent()).includes('not comparable'));
  const deletion = await api(page, `${collection()}/${foreignId}`, 'DELETE');
  assert.equal(deletion.status, 200);
  deleted.push(foreignId);
  check('deleting retained inputs revokes new inspection reads', (await api(page, path(foreignId))).payload.inspection === null);
  const deletedImage = await context.request.get(`${address.origin}/api/v1${path(foreignId)}/images/cam0/0`);
  check('deleted previews are unavailable', deletedImage.status() === 410);
  await page.goto(route(id)); await loaded();
  check('inspection stores no private sensor data in browser storage', await page.evaluate(() =>
    Object.keys(localStorage).every(key => key === 'meridian-theme') && sessionStorage.length === 0));
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  await page.getByRole('heading', { name: 'Sign in', exact: true }).waitFor();
  check('logout removes private observation content and revokes further reads',
    await page.locator('#acquisition-inspection-content').count() === 0
    && (await context.request.get(`${address.origin}/api/v1${path(id)}`)).status() === 401);
  check('no uncaught browser or CSP error occurs', errors.length === 0 && cspErrors.length === 0);
} catch (error) {
  failure = String(error.stack || error).replaceAll(password, '[redacted]');
  process.exitCode = 1;
  try { await page.screenshot({ path: `${output}failure.png`, fullPage: true }); } catch { /* Preserve the original error. */ }
} finally {
  const report = { schema_version: 1, base, checked_at: new Date().toISOString(), passed: !failure,
    source_synthetic: true, created_accounts: accounts, project_id: projectId, created_acquisitions: created,
    created_acquisition_ids: Object.values(created), deleted_acquisition_ids: deleted,
    retained_acquisition_id: created.faulty, source_sha256: fixtureInfo.faulty.sha256,
    source_size_bytes: fixtureInfo.faulty.size_bytes, fixtures: fixtureInfo, content_security_policy: contentSecurityPolicy,
    ...inspectionIdentity, checks, errors, error_details: errorDetails, cspErrors, ...(failure ? { failure } : {}),
    limitations: ['Analytic accepted packages exercise observations and recorded policy; no new physical acquisition or calibration fit.',
      'No estimator output, trajectory accuracy, saturation verdict or flight validation is produced.',
      'Retained QA accounts and original inputs require exact operator cleanup after restoration checks. Credentials are never saved.'] };
  await writeFile(`${output}report.json`, JSON.stringify(report, null, 2) + '\n');
  await progress(failure ? 'FAILED' : 'PASSED');
  console.log(JSON.stringify({ passed: !failure, checks: checks.length, accounts, projectId, acquisitions: created,
    output, ...(failure ? { failure } : {}) }, null, 2));
  await browser.close();
}
