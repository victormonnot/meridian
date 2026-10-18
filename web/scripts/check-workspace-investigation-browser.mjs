/**
 * Run one real VIO computation and inspect its retained, private investigation.
 * Mutates only an explicitly supplied authorized deployment: creates two random
 * QA accounts, one project, an experiment, and a successful/cancelled run pair.
 * Credentials and cookies are never written. Exact created identities are saved
 * for operator cleanup. No trajectory, image, metric or job state is fabricated.
 * Controlled delayed responses test stale-request handling without changing data.
 */
import assert from 'node:assert/strict';
import { randomBytes, randomUUID, createHash } from 'node:crypto';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve, sep } from 'node:path';
import { pathToFileURL } from 'node:url';

const [baseArgument, outputArgument, ...extra] = process.argv.slice(2);
if (!baseArgument || !outputArgument || extra.length) throw new Error('Usage: node web/scripts/check-workspace-investigation-browser.mjs WORKSPACE_URL OUTPUT_DIRECTORY');
const address = new URL(baseArgument);
if (!['http:', 'https:'].includes(address.protocol) || address.username || address.password || address.search || address.hash) throw new Error('An explicit HTTP(S) URL without credentials, query or fragment is required.');
const base = address.href;
const output = resolve(outputArgument) + sep;
const moduleUrl = process.env.MERIDIAN_PLAYWRIGHT_MODULE ? pathToFileURL(resolve(process.env.MERIDIAN_PLAYWRIGHT_MODULE)).href : new URL('../../build/browser-tools/node_modules/playwright/index.mjs', import.meta.url).href;
const { chromium } = await import(moduleUrl);
const timeout = Number(process.env.MERIDIAN_JOB_QA_TIMEOUT_MS || 1_200_000);
if (!Number.isSafeInteger(timeout) || timeout < 30_000 || timeout > 3_600_000) throw new Error('Invalid MERIDIAN_JOB_QA_TIMEOUT_MS.');
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true, ...(process.env.MERIDIAN_BROWSER_EXECUTABLE ? { executablePath: process.env.MERIDIAN_BROWSER_EXECUTABLE } : {}) });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
const other = await browser.newContext();
const page = await context.newPage();
const peer = await other.newPage();
const stamp = `${Date.now().toString(36)}-${randomBytes(3).toString('hex')}`;
const usernames = [`qa-investigation-${stamp}`, `qa-investigation-peer-${stamp}`];
const password = `Qa!${randomBytes(24).toString('base64url')}`;
const accounts = [];
const jobs = [];
const checks = [];
const errors = [];
const cspErrors = [];
const timings = [];
const imageRequests = [];
const requestTimes = new Map();
let projectId;
let experimentId;
let job;
let artifact;
let failure;
let artifactEvidence;
const check = (label, condition) => { assert.ok(condition, label); checks.push(label); };
async function progress(state) {
  await writeFile(`${output}progress.json`, JSON.stringify({ state, accounts, project_id: projectId, experiment_id: experimentId, jobs }, null, 2) + '\n');
  console.log(state);
}
for (const target of [page, peer]) {
  target.on('pageerror', error => errors.push(error.message));
  target.on('console', message => { if (message.type() === 'error' && /content security policy|violat.*(?:directive|policy)|refused to (?:load|execute|apply|connect)/i.test(message.text())) cspErrors.push(message.text()); });
}
page.on('request', request => {
  if (!request.url().includes('/investigation')) return;
  requestTimes.set(request, performance.now());
  if (/\/images\//.test(request.url())) imageRequests.push(new URL(request.url()).pathname);
});
page.on('requestfinished', async request => {
  if (!requestTimes.has(request)) return;
  const elapsed = performance.now() - requestTimes.get(request);
  const response = await request.response();
  const sizes = await request.sizes().catch(() => ({}));
  timings.push({ path: new URL(request.url()).pathname, query: new URL(request.url()).search, status: response?.status(), elapsed_ms: elapsed, response_body_bytes: sizes.responseBodySize });
});
async function api(target, path, method = 'GET', body) {
  return target.evaluate(async ({ path, method, body }) => {
    const session = await (await fetch('/api/v1/session', { cache: 'no-store' })).json();
    const response = await fetch(`/api/v1${path}`, { method, cache: 'no-store', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': session.csrf_token }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    return { status: response.status, payload: await response.json() };
  }, { path, method, body });
}
async function register(target, username) {
  await target.goto(base);
  await target.getByRole('button', { name: 'Create account', exact: true }).click();
  await target.getByLabel('Username', { exact: true }).fill(username);
  await target.getByLabel('Password', { exact: true }).fill(password);
  const [response] = await Promise.all([target.waitForResponse(item => new URL(item.url()).pathname === '/api/v1/auth/register' && item.request().method() === 'POST'), target.getByRole('button', { name: 'Create account', exact: true }).click()]);
  assert.ok(response.ok(), 'QA registration succeeds');
  accounts.push(username);
  await target.getByRole('heading', { name: 'Projects', exact: true }).waitFor();
}
async function waitImages(frame) {
  await page.waitForFunction(frame => ['cam0', 'cam1'].every(camera => {
    const image = document.querySelector(`#image-${camera}`);
    return image && !image.hidden && image.complete && image.naturalWidth === 512 && image.alt.includes(`frame ${frame},`);
  }), frame);
}
async function move(frame) {
  await page.locator('#frame-cursor').evaluate((input, value) => { input.value = value; input.dispatchEvent(new Event('input', { bubbles: true })); }, frame);
  await waitImages(frame);
}
async function interval(start, end) {
  await page.getByLabel('Interval start · s', { exact: true }).fill(String(start));
  await page.getByLabel('Interval end · s', { exact: true }).fill(String(end));
  const response = page.waitForResponse(item => new URL(item.url()).pathname.endsWith('/investigation/metrics') && item.ok());
  await page.getByRole('button', { name: 'Apply interval', exact: true }).click();
  return (await (await response).json()).interval;
}
try {
  await register(page, usernames[0]);
  const capabilities = (await api(page, '/session')).payload.capabilities;
  check('deployment enables actual computation', capabilities.execution === true);
  const project = await api(page, '/projects', 'POST', { name: 'Indoor VIO investigation', description: 'Synchronized investigation of an immutable stereo-inertial run.', dataset_id: 'tumvi-room1-512-16-v1' });
  assert.equal(project.status, 201);
  projectId = project.payload.project.id;
  const experiment = await api(page, `/projects/${projectId}/experiments`, 'POST', { schema_version: 1, name: 'Room1 reference investigation', description: 'Full recording, calibrated stereo and raw IMU.', dataset_id: 'tumvi-room1-512-16-v1', parameters: { num_pts: 200 } });
  assert.equal(experiment.status, 201);
  experimentId = experiment.payload.experiment.id;
  const submitted = await api(page, `/projects/${projectId}/jobs`, 'POST', { schema_version: 1, experiment_id: experimentId, revision_version: 1, idempotency_key: randomUUID() });
  assert.equal(submitted.status, 201);
  job = submitted.payload.job;
  jobs.push(job.id);
  await progress(`Real run submitted: ${job.id}`);
  const cancelled = await api(page, `/projects/${projectId}/jobs`, 'POST', { schema_version: 1, experiment_id: experimentId, revision_version: 1, idempotency_key: randomUUID() });
  assert.equal(cancelled.status, 201);
  jobs.push(cancelled.payload.job.id);
  assert.equal((await api(page, `/projects/${projectId}/jobs/${jobs[1]}/cancel`, 'POST', { schema_version: 1 })).status, 200);
  await page.goto(`${base}#project/${projectId}/results/${job.id}`);
  await page.locator('.job-detail .job-status').filter({ hasText: /^(Succeeded|Failed|Cancelled)$/ }).waitFor({ timeout });
  job = (await api(page, `/projects/${projectId}/jobs/${job.id}`)).payload.job;
  check('run succeeded with an available fresh investigation', job.state === 'succeeded' && job.investigation_available === true);
  await progress(`Real run complete; opening investigation: ${job.id}`);
  const investigationPath = `/projects/${projectId}/jobs/${job.id}/investigation`;
  const investigationUrl = `${base}#project/${projectId}/results/${job.id}/investigation`;
  const artifactResponse = page.waitForResponse(item => new URL(item.url()).pathname === `/api/v1${investigationPath}` && item.ok());
  const initialMetrics = page.waitForResponse(item => new URL(item.url()).pathname.endsWith('/investigation/metrics') && item.ok());
  await page.getByRole('link', { name: 'Investigate run', exact: true }).click();
  const response = await artifactResponse;
  const bytes = await response.body();
  artifact = JSON.parse(bytes).investigation;
  artifactEvidence = { response_sha256: createHash('sha256').update(bytes).digest('hex'), response_bytes: bytes.length, specification_sha256: artifact.specification_sha256, frames: artifact.frames.length, imu_samples: artifact.imu.samples.length, metrics: artifact.summary.metrics, first_timestamp_ns: artifact.frames[0].timestamp_ns, last_timestamp_ns: artifact.frames.at(-1).timestamp_ns };
  await waitImages(0);
  const full = (await (await initialMetrics).json()).interval;
  check('full interval preserves run metrics and full-resolution support', Math.abs(full.metrics.ate_rmse_m - job.result.evaluation.metrics.ate_rmse_m) < 1e-12 && full.counts.expected_camera_samples === artifact.frames.length);
  check('initial missing estimate is explicit and does not invent a pose', await page.locator('#frame-status').innerText() === 'No estimate · initialization' && await page.locator('#cursor-position').innerText() === 'Unavailable');
  check('only the selected stereo pair is requested initially', imageRequests.length === 2 && imageRequests.every(path => path.endsWith('/0')));
  const intensity = await page.locator('#image-cam0').evaluate(image => {
    const canvas = document.createElement('canvas'); canvas.width = image.naturalWidth; canvas.height = image.naturalHeight;
    const context = canvas.getContext('2d'); context.drawImage(image, 0, 0);
    const values = context.getImageData(0, 0, canvas.width, canvas.height).data;
    let min = 255; let max = 0;
    for (let i = 0; i < values.length; i += 4) { min = Math.min(min, values[i]); max = Math.max(max, values[i]); }
    return { min, max };
  });
  check('original mono16 PNG decodes with a nonblank intensity range', intensity.max - intensity.min > 20);
  artifactEvidence.browser_intensity_range = intensity;
  await page.locator('#frame-cursor').focus();
  await page.keyboard.press('ArrowRight');
  await waitImages(1);
  check('keyboard cursor selects exact camera timestamp', await page.locator('#cursor-timestamp').innerText() === artifact.frames[1].timestamp_ns);
  await page.keyboard.press('End');
  await waitImages(artifact.frames.length - 1);
  check('keyboard End reaches the final camera epoch', await page.locator('#frame-cursor').inputValue() === String(artifact.frames.length - 1));
  const missing = await interval(0, 1);
  check('initialization-only interval has support counts and null error metrics', missing.metrics === null && missing.counts.estimate_samples === 0 && missing.counts.expected_camera_samples > 0);
  const selected = await interval(10, 20);
  check('selected interval metrics use server support and fixed global alignment', selected.alignment_scope === 'full_recording_global_se3_unchanged' && selected.counts.expected_camera_samples > 100 && selected.counts.expected_camera_samples < artifact.frames.length);
  const selectedFrame = Number(await page.locator('#frame-cursor').inputValue());
  await waitImages(selectedFrame);
  check('cursor is clamped to selected interval', selectedFrame === selected.interval.first_camera_index);
  check('nearest raw IMU sample reports signed timing difference without interpolation', /Δt .* ms.*nearest, not interpolated/.test(await page.locator('#cursor-imu').innerText()));
  check('trajectory uses three projections and signals retain actual SVG paths', await page.locator('.investigation-projections svg').count() === 3 && await page.locator('.investigation-signals svg').count() === 8 && await page.locator('path.plot-line').count() > 10);
  check('unsupported internal diagnostics are explicitly unavailable', (await page.locator('.diagnostic-list').innerText()).includes('Unavailable') && !(await page.locator('.diagnostic-list').innerText()).includes('Healthy'));
  let heldResponse;
  let releaseReady;
  let holdFirst = true;
  const heldReady = new Promise(resolve => { releaseReady = resolve; });
  await page.route(`**/api/v1${investigationPath}/metrics?*`, async route => {
    if (!holdFirst) { await route.continue(); return; }
    holdFirst = false;
    const response = await route.fetch();
    heldResponse = () => route.fulfill({ response }).catch(() => {});
    releaseReady();
  });
  await page.getByLabel('Interval start · s', { exact: true }).fill('30');
  await page.getByLabel('Interval end · s', { exact: true }).fill('40');
  await page.getByRole('button', { name: 'Apply interval', exact: true }).click();
  await heldReady;
  const latest = await interval(50, 60);
  await page.locator('#interval-metrics .interval-status').waitFor();
  const latestText = await page.locator('#interval-metrics').innerText();
  await heldResponse();
  await page.unroute(`**/api/v1${investigationPath}/metrics?*`);
  await page.waitForTimeout(100);
  check('late interval response cannot replace newer authoritative metrics', latest.interval.start_time_s > 49 && await page.locator('#interval-metrics').innerText() === latestText);
  await page.getByLabel('Interval start · s', { exact: true }).fill('70');
  await page.getByRole('button', { name: 'Apply interval', exact: true }).click();
  check('invalid reversed bounds preserve the selected plots and show validation', Boolean(await page.locator('#interval-error').innerText()));
  const resetResponse = page.waitForResponse(item => new URL(item.url()).pathname.endsWith('/investigation/metrics') && item.ok());
  await page.getByRole('button', { name: 'Full recording', exact: true }).click();
  await resetResponse;
  await move(1000);
  const beforeRapid = imageRequests.length;
  await page.locator('#frame-cursor').evaluate(input => { for (let value = 1001; value <= 1010; value += 1) { input.value = value; input.dispatchEvent(new Event('input', { bubbles: true })); } });
  await waitImages(1010);
  check('rapid cursor changes coalesce image loads to the final selected stereo pair', imageRequests.length - beforeRapid === 2);
  await page.selectOption('#appearance', 'light');
  await page.screenshot({ path: `${output}investigation-desktop-light.png`, fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1400 });
  await page.locator('.investigation-controls').scrollIntoViewIfNeeded();
  await page.evaluate(() => window.scrollTo(0, document.querySelector('.investigation-controls').getBoundingClientRect().top + window.scrollY - 18));
  await page.screenshot({ path: `${output}investigation-workspace-light.png` });
  check('desktop has no horizontal overflow', await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: `${output}investigation-mobile-light.png`, fullPage: true });
  check('390px mobile has no horizontal overflow', await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.selectOption('#appearance', 'dark');
  await page.screenshot({ path: `${output}investigation-desktop-dark.png`, fullPage: true });
  await page.reload();
  await waitImages(0);
  check('deep-link reload loads the same private run without browser-persisted run data', await page.locator('#cursor-timestamp').innerText() === artifact.frames[0].timestamp_ns);
  const pending = [];
  await page.route(`**/api/v1${investigationPath}/images/**`, async route => { pending.push(route); });
  await page.locator('#frame-cursor').evaluate(input => { input.value = 10; input.dispatchEvent(new Event('input', { bubbles: true })); });
  await page.waitForFunction(() => document.querySelector('#cursor-time').textContent.includes('frame 10 /'));
  await page.waitForTimeout(200);
  await page.getByRole('link', { name: 'Data', exact: true }).click();
  await page.getByRole('heading', { name: 'TUM-VI room1', exact: true }).waitFor();
  for (const route of pending) await route.continue().catch(() => {});
  await page.unroute(`**/api/v1${investigationPath}/images/**`);
  check('leaving investigation discards pending images and private plot DOM', await page.locator('.investigation-chart, img[src^="blob:"]').count() === 0);
  await page.goto(`${base}#project/${projectId}/results/${jobs[1]}`);
  await page.getByRole('link', { name: 'Investigate run', exact: true }).click();
  await page.getByRole('heading', { name: 'Investigation unavailable', exact: true }).waitFor();
  check('cancelled run exposes an honest unavailable investigation state', await page.locator('.investigation-chart').count() === 0);
  await register(peer, usernames[1]);
  for (const suffix of ['', `/metrics?start_ns=${artifact.frames[0].timestamp_ns}&end_ns=${artifact.frames.at(-1).timestamp_ns}`, '/images/cam0/0']) {
    const denied = await api(peer, investigationPath + suffix);
    check(`other account cannot read investigation${suffix.includes('/metrics') ? ' interval metrics' : suffix.includes('/images') ? ' images' : ' artifact'}`, denied.status === 404);
  }
  await page.goto(investigationUrl);
  await waitImages(0);
  await context.clearCookies();
  // A pointer click may focus this tab and trigger the separate focus/session
  // check first. Dispatch the actual control event without changing tab focus.
  await page.locator('#frame-next').evaluate(button => button.click());
  await page.getByRole('heading', { name: 'Sign in', exact: true }).waitFor();
  check('session expiry on image request clears images, charts and private run text', await page.locator('img[src^="blob:"], .investigation-chart, #investigation-heading').count() === 0);
  const anonymous = await api(page, `${investigationPath}/images/cam0/0`);
  check('anonymous camera access is rejected', anonymous.status === 401);
  check('no unhandled browser errors', errors.length === 0);
  check('no CSP violations', cspErrors.length === 0);
  await progress('Investigation browser checks complete');
} catch (error) {
  failure = { name: error.name, message: error.message };
  await page.screenshot({ path: `${output}failure.png`, fullPage: true }).catch(() => {});
} finally {
  await writeFile(`${output}report.json`, JSON.stringify({ schema_version: 1, generated_at: new Date().toISOString(), base_url: base, environment: process.env.MERIDIAN_QA_ENVIRONMENT || `${address.protocol === 'https:' ? 'HTTPS with normal browser certificate validation' : 'HTTP'}; database and infrastructure identity not asserted by this script`, passed: !failure, checks, accounts, project_id: projectId, experiment_id: experimentId, jobs, artifact: artifactEvidence, timings, page_errors: errors, csp_errors: cspErrors, failure, limits: ['One calibrated handheld room1 run; no flight or embedded validation.', 'Failed/expired/tampered artifact security paths are covered separately by API tests; this browser run uses actual retained artifacts.', 'Browser timing is an observation of this deployment and request, not a performance guarantee.'] }, null, 2) + '\n');
  await browser.close();
}
if (failure) throw new Error(failure.message);
console.log(`${checks.length} investigation browser checks passed.`);
