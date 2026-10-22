/**
 * Exercise supplied artifacts and immutable input revisions on an authorized
 * workspace. Fixtures come from scripts/make-calibration-fixture.py.
 * Retain exact QA identities and prepared inputs for backup/restore; credentials
 * remain in memory. This runs no estimator and claims no physical validation.
 */
import assert from 'node:assert/strict';
import { createHash, randomBytes } from 'node:crypto';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { resolve, sep } from 'node:path';
import { pathToFileURL } from 'node:url';

const [baseArgument, outputArgument, fixtureArgument, ...extra] = process.argv.slice(2);
if (!baseArgument || !outputArgument || !fixtureArgument || extra.length) {
  throw new Error('Usage: node web/scripts/check-workspace-calibration-browser.mjs WORKSPACE_URL OUTPUT_DIRECTORY FIXTURE_DIRECTORY');
}
const address = new URL(baseArgument);
if (!['http:', 'https:'].includes(address.protocol) || address.username || address.password || address.search || address.hash) {
  throw new Error('Use an explicit HTTP(S) workspace URL without credentials, query or fragment.');
}
const base = address.href, output = resolve(outputArgument) + sep, fixtures = resolve(fixtureArgument);
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const manifest = JSON.parse(await readFile(`${fixtures}/manifest.json`, 'utf8'));
assert.equal(manifest.synthetic, true);
for (const item of Object.values(manifest.packages)) {
  const bytes = await readFile(`${fixtures}/${item.file}`);
  assert.ok(bytes.length < 1024 * 1024);
  assert.equal(hash(bytes), item.sha256);
}
await mkdir(output, { recursive: true });
const moduleUrl = process.env.MERIDIAN_PLAYWRIGHT_MODULE
  ? pathToFileURL(resolve(process.env.MERIDIAN_PLAYWRIGHT_MODULE)).href
  : new URL('../../build/browser-tools/node_modules/playwright/index.mjs', import.meta.url).href;
const { chromium } = await import(moduleUrl);
const browser = await chromium.launch({ headless: true,
  ...(process.env.MERIDIAN_BROWSER_EXECUTABLE ? { executablePath: process.env.MERIDIAN_BROWSER_EXECUTABLE } : {}) });
const context = await browser.newContext({ viewport: { width: 1440, height: 1100 }, acceptDownloads: true });
const peerContext = await browser.newContext();
const page = await context.newPage(), peer = await peerContext.newPage();
const stamp = `${Date.now().toString(36)}-${randomBytes(3).toString('hex')}`;
const usernames = [`qa-calibration-${stamp}`, `qa-calibration-peer-${stamp}`];
const password = `Qa!${randomBytes(24).toString('base64url')}`;
const accounts = [], acquisitions = {}, artifacts = {}, revisions = {}, deletedRevisionFiles = [];
const checks = [], errors = [], cspErrors = [], evidence = {};
let projectId, failure, contentSecurityPolicy;
const check = (label, condition) => { assert.ok(condition, label); checks.push(label); };
const sourceBase = () => `/projects/${projectId}/acquisitions/${acquisitions.replay}`;
const artifactBase = () => `/projects/${projectId}/sensor-artifacts`;
const revisionBase = () => `${sourceBase()}/input-revisions`;
const route = revision => `${base}#project/${projectId}/data/acquisitions/${acquisitions.replay}/calibration${revision ? `/revisions/${revision}` : ''}`;
for (const target of [page, peer]) {
  target.on('pageerror', error => errors.push({ message: error.message, stack: error.stack, after_check: checks.at(-1) ?? null }));
  target.on('console', message => {
    if (message.type() === 'error' && /content security policy|violat.*(?:directive|policy)|refused to (?:load|execute|apply|connect)/i.test(message.text())) cspErrors.push(message.text());
  });
}
async function progress(state) {
  await writeFile(`${output}progress.json`, JSON.stringify({ state, accounts, project_id: projectId,
    acquisitions, artifacts, revisions, checks: checks.length }, null, 2) + '\n');
  console.log(state);
}
async function api(target, path, method = 'GET', body) {
  return target.evaluate(async ({ path, method, body }) => {
    const session = await (await fetch('/api/v1/session', { cache: 'no-store' })).json();
    const response = await fetch(`/api/v1${path}`, { method, cache: 'no-store',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': session.csrf_token },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    const bytes = await response.arrayBuffer();
    const sha256 = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), item => item.toString(16).padStart(2, '0')).join('');
    let payload;
    try { payload = JSON.parse(new TextDecoder().decode(bytes)); } catch { /* Binary reads use the request context. */ }
    return { status: response.status, payload, sha256, size_bytes: bytes.byteLength, headers: Object.fromEntries(response.headers) };
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
  await page.getByLabel('Acquisition name', { exact: true }).fill(`Analytic calibration ${name}`);
  await page.getByLabel('Recording archive', { exact: true }).setInputFiles(`${fixtures}/${name}.zip`);
  const [response] = await Promise.all([
    page.waitForResponse(item => new URL(item.url()).pathname === `/api/v1/projects/${projectId}/acquisitions` && item.request().method() === 'POST'),
    page.getByRole('button', { name: 'Start upload', exact: true }).click(),
  ]);
  assert.equal(response.status(), 201);
  acquisitions[name] = (await response.json()).acquisition.id;
  await progress(`IMPORT_${name.toUpperCase()}_CREATED`);
  await page.waitForURL(url => url.hash === `#project/${projectId}/data/acquisitions/${acquisitions[name]}`);
  await page.locator('.acquisition-detail .acquisition-status').filter({ hasText: /^Ready to inspect$/ }).waitFor({ timeout: 300_000 });
  const saved = (await api(page, `/projects/${projectId}/acquisitions/${acquisitions[name]}`)).payload.acquisition;
  check(`${name} source archive retains its independently recorded hash and size`,
    saved.sha256 === manifest.packages[name].sha256 && saved.size_bytes === manifest.packages[name].size_bytes);
}
async function overview() {
  await page.goto(route());
  await page.getByRole('heading', { name: 'Saved artifacts', exact: true }).waitFor();
}
async function importArtifact(file, name, bindings, valid = true) {
  await page.getByRole('button', { name: 'Import artifact', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('Artifact name', { exact: true }).fill(name);
  await dialog.getByLabel('Descriptor JSON', { exact: true }).setInputFiles(`${fixtures}/${file}`);
  const boxes = dialog.locator('input[name="acquisition_ids"]');
  for (const box of await boxes.all()) await box.setChecked(bindings.includes(await box.getAttribute('value')));
  const [response] = await Promise.all([
    page.waitForResponse(item => new URL(item.url()).pathname === `/api/v1${artifactBase()}` && item.request().method() === 'POST'),
    dialog.getByRole('button', { name: 'Import artifact', exact: true }).click(),
  ]);
  if (!valid) {
    check(`${file} is rejected through strict server parsing or compatibility validation`, response.status() === 400);
    await dialog.locator('.form-error').filter({ hasText: /supported bounded contract/ }).waitFor();
    await dialog.getByRole('button', { name: 'Close', exact: true }).click();
    return;
  }
  assert.equal(response.status(), 201);
  const artifact = (await response.json()).artifact;
  artifacts[file] = artifact.id;
  await dialog.waitFor({ state: 'detached' });
  await page.locator(`[data-artifact-id="${artifact.id}"]`).waitFor();
  check(`${file} retains its canonical contract identity`, artifact.contract_sha256 === manifest.artifacts[file].contract.sha256);
  const original = await context.request.get(`${address.origin}/api/v1${artifactBase()}/${artifact.id}/contract`);
  const bytes = await original.body();
  check(`${file} download preserves the exact supplied JSON bytes`,
    original.status() === 200 && hash(bytes) === manifest.artifacts[file].sha256 && bytes.length === manifest.artifacts[file].size_bytes);
  await progress('ARTIFACT_IMPORTED');
  return { artifact, request: response.request().postDataJSON() };
}
async function choose(calibration, { intrinsic = false, frame = '', clocks = [] } = {}) {
  await page.getByRole('combobox', { name: 'Calibration artifact', exact: true }).selectOption(calibration || '');
  if (calibration) {
    await page.locator(`[data-artifact-id="${calibration}"]`).waitFor();
    await page.waitForFunction(() => !document.querySelector('[name="apply_imu_intrinsics"]').disabled);
  }
  await page.getByLabel('Apply supplied IMU bias and scale', { exact: true }).setChecked(intrinsic);
  await page.getByRole('combobox', { name: 'IMU output frame', exact: true }).selectOption(frame);
  for (const box of await page.locator('input[name="clock_mapping_ids"]').all()) {
    await box.setChecked(clocks.includes(await box.getAttribute('value')));
  }
}
async function assess(expectedStatus = 200) {
  const [response] = await Promise.all([
    page.waitForResponse(item => new URL(item.url()).pathname === `/api/v1${revisionBase()}/assess` && item.request().method() === 'POST'),
    page.getByRole('button', { name: 'Check revision', exact: true }).click(),
  ]);
  assert.equal(response.status(), expectedStatus);
  if (expectedStatus === 200) {
    await page.getByRole('heading', { name: 'Checked preparation', exact: true }).waitFor();
    check('revision assessment does not leak into unrelated workspace forms', errors.length === 0);
  }
  return response.json();
}
async function createRevision(name, key) {
  await page.getByLabel('Revision name', { exact: true }).fill(name);
  const assessment = await assess();
  assert.equal(assessment.eligible, true);
  const button = page.getByRole('button', { name: 'Create input revision', exact: true });
  await button.focus();
  const [response] = await Promise.all([
    page.waitForResponse(item => new URL(item.url()).pathname === `/api/v1${revisionBase()}` && item.request().method() === 'POST'),
    page.keyboard.press('Enter'),
  ]);
  assert.equal(response.status(), 201);
  const created = (await response.json()).revision;
  revisions[key] = created.id;
  await progress(`REVISION_${key.toUpperCase()}_CREATED`);
  await page.waitForURL(url => url.hash === new URL(route(created.id)).hash);
  await page.locator('.calibration-revision-detail .acquisition-status').filter({ hasText: /^Prepared input available$/ }).waitFor({ timeout: 300_000 });
  const saved = (await api(page, `${revisionBase()}/${created.id}`)).payload.revision;
  check(`${key} revision reaches a retained ready state without estimator execution`,
    saved.state === 'ready' && saved.files_available && saved.execution_readiness === 'not_assessed');
  return { saved, request: response.request().postDataJSON() };
}
async function imuFile(saved) {
  const output = saved.prepared_input.outputs.find(item => item.stream_id === 'imu0');
  const path = output.index_path;
  const response = await context.request.get(`${address.origin}/api/v1${revisionBase()}/${saved.id}/files?${new URLSearchParams({ path })}`);
  const bytes = await response.body();
  check('owned corrected CSV download matches its published inventory and no-store boundary',
    response.status() === 200 && hash(bytes) === saved.manifest.files[path].sha256
    && response.headers()['cache-control'].includes('no-store'));
  return { path, sha256: hash(bytes), size_bytes: bytes.length,
    rows: bytes.toString('utf8').trim().split(/\r?\n/).slice(1).map(line => line.split(',')), output };
}

try {
  contentSecurityPolicy = await (await page.goto(base)).headerValue('content-security-policy');
  await register(page, usernames[0]);
  await page.getByRole('button', { name: /New project/ }).click();
  await page.getByLabel('Project name', { exact: true }).fill('Supplied calibration acceptance');
  await page.getByRole('button', { name: 'Create project', exact: true }).click();
  await page.getByRole('heading', { name: 'Acquisitions', exact: true }).waitFor();
  projectId = new URL(page.url()).hash.split('/')[1];
  await progress('IMPORTING_ANALYTIC_RECORDINGS');
  for (const name of ['source', 'replay', 'validation']) await upload(name);
  const beforeInspection = await api(page, `${sourceBase()}/inspection`);
  await overview();
  for (const file of ['invalid-duplicate-key.json', 'invalid-rotation.json', 'invalid-resolution.json', 'invalid-noise-unit.json']) {
    await importArtifact(file, 'Invalid candidate', [acquisitions.source, acquisitions.validation], false);
  }
  check('invalid imports create no stored artifact', (await api(page, artifactBase())).payload.artifacts.length === 0);
  const imported = await importArtifact('calibration.json', 'Analytic calibration A', [acquisitions.source, acquisitions.validation]);
  check('reported calibration evidence remains explicitly uncertified', (await page.locator('.calibration-artifact').textContent()).includes('physical conclusions are not certified'));
  check('structured geometry and noise retain frame direction and distinct units',
    (await page.locator('.calibration-artifact').textContent()).includes('imu → imu-corrected')
    && (await page.locator('.calibration-artifact').textContent()).includes('rad/s/√Hz')
    && (await page.locator('.calibration-artifact').textContent()).includes('(rad/s)/√s'));
  const repeated = await api(page, artifactBase(), 'POST', imported.request);
  check('retrying the same artifact import returns its existing identity', repeated.status === 200 && repeated.payload.artifact.id === imported.artifact.id);
  check('artifact import does not select or apply it automatically', await page.getByRole('combobox', { name: 'Calibration artifact', exact: true }).inputValue() === ''
    && (await api(page, revisionBase())).payload.revisions.length === 0);
  await importArtifact('clock-domain.json', 'Clock domain +2 ns', [acquisitions.replay]);
  check('clock-domain mapping displays exact epoch and positive offset', (await page.locator('.calibration-artifact').textContent()).includes('Clock-domain mapping')
    && (await page.locator('.calibration-artifact').textContent()).includes('1520530308199447626 ns'));
  await importArtifact('sensor-offset.json', 'Sensor offset -2 ns', [acquisitions.replay]);
  check('sensor offset retains its distinct purpose and negative sign', (await page.locator('.calibration-artifact').textContent()).includes('Sensor time offset')
    && (await page.locator('.calibration-artifact').textContent()).includes('-2 ns'));
  await choose(artifacts['calibration.json']);
  const metadata = await createRevision('Calibration metadata only', 'metadata_only');
  check('binding calibration alone applies no intrinsic, frame or clock correction',
    !metadata.saved.prepared_input.operations.some(item => ['imu_calibration', 'frame_transform', 'clock_mapping'].includes(item.kind)));
  await overview();
  await choose(artifacts['calibration.json'], { intrinsic: true, frame: 'imu-corrected',
    clocks: [artifacts['clock-domain.json'], artifacts['sensor-offset.json']] });
  await page.getByLabel('Revision name', { exact: true }).fill('Overlapping mappings');
  await assess(400);
  check('ambiguous overlapping clock operations cannot create a revision', await page.getByRole('button', { name: 'Create input revision', exact: true }).isDisabled());
  await choose(artifacts['calibration.json'], { intrinsic: true, frame: 'imu-corrected', clocks: [artifacts['clock-domain.json']] });
  await page.getByLabel('Revision name', { exact: true }).fill('Analytic correction A');
  await assess();
  await page.getByLabel('Apply supplied IMU bias and scale', { exact: true }).uncheck();
  check('changing a checked selection requires a new compatibility assessment', await page.getByRole('button', { name: 'Create input revision', exact: true }).isDisabled());
  await page.getByLabel('Apply supplied IMU bias and scale', { exact: true }).check();
  await assess();
  for (const theme of ['dark', 'light']) {
    await page.getByLabel('Appearance', { exact: true }).selectOption(theme);
    await page.screenshot({ path: `${output}calibration-${theme}.png`, fullPage: true });
  }
  await page.setViewportSize({ width: 390, height: 844 });
  check('calibration parameters and preparation controls fit the mobile viewport', await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  check('mobile revision history keeps names above status labels', await page.locator('.calibration-revision-list > a').first().evaluate(row => {
    const name = row.querySelector('div').getBoundingClientRect();
    const status = row.querySelector('.acquisition-status').getBoundingClientRect();
    return name.width > row.clientWidth * .8 && name.bottom <= status.top;
  }));
  await page.screenshot({ path: `${output}calibration-mobile.png`, fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1100 });
  const first = await createRevision('Analytic correction A', 'corrected_a');
  const csvA = await imuFile(first.saved);
  check('bias/scale then positive quarter-turn and +2 ns match independent analytic rows',
    JSON.stringify(csvA.rows) === JSON.stringify(manifest.expected_revision_a.rows)
    && csvA.sha256 === manifest.expected_revision_a.imu_csv.sha256
    && csvA.output.frame_id === manifest.expected_revision_a.output_frame_id
    && csvA.output.clock_id === manifest.expected_revision_a.output_clock_id);
  const repeatedRevision = await api(page, revisionBase(), 'POST', first.request);
  check('retrying a revision request does not duplicate preparation', repeatedRevision.status === 200 && repeatedRevision.payload.revision.id === first.saved.id);
  await overview();
  await importArtifact('calibration-alternative.json', 'Analytic calibration B', [acquisitions.source, acquisitions.validation]);
  await choose(artifacts['calibration-alternative.json'], { intrinsic: true, frame: 'imu-corrected', clocks: [artifacts['clock-domain.json']] });
  const second = await createRevision('Analytic correction B', 'corrected_b');
  const csvB = await imuFile(second.saved);
  check('a changed supplied candidate produces the second independent expected CSV',
    JSON.stringify(csvB.rows) === JSON.stringify(manifest.expected_revision_b.rows)
    && csvB.sha256 === manifest.expected_revision_b.imu_csv.sha256 && csvB.sha256 !== csvA.sha256);
  const firstAgain = (await api(page, `${revisionBase()}/${first.saved.id}`)).payload.revision;
  const csvAgain = await imuFile(firstAgain);
  check('later candidates preserve the first revision request, contract and output identity',
    firstAgain.request_sha256 === first.saved.request_sha256 && csvAgain.sha256 === csvA.sha256
    && JSON.stringify(firstAgain.prepared_input) === JSON.stringify(first.saved.prepared_input));
  check('corrected revisions leave the original sensor inspection unchanged',
    (await api(page, `${sourceBase()}/inspection`)).sha256 === beforeInspection.sha256);
  await page.reload();
  await page.getByRole('heading', { name: 'Analytic correction B', exact: true }).waitFor();
  check('immutable revision selection and output downloads survive refresh', await page.getByRole('link', { name: /Download imu0/ }).count() === 1);
  await progress('CHECKING_OWNER_BOUNDARIES');
  const privatePaths = [artifactBase(), `${artifactBase()}/${artifacts['calibration.json']}`,
    `${artifactBase()}/${artifacts['calibration.json']}/contract`, `${revisionBase()}/${first.saved.id}`,
    `${revisionBase()}/${first.saved.id}/contract`, `${revisionBase()}/${first.saved.id}/report`,
    `${revisionBase()}/${first.saved.id}/files?${new URLSearchParams({ path: csvA.path })}`];
  for (const path of privatePaths) check('anonymous artifact or revision read returns 401',
    (await peerContext.request.get(`${address.origin}/api/v1${path}`)).status() === 401);
  await register(peer, usernames[1]);
  for (const path of privatePaths) check('foreign-owner artifact or revision read returns 404',
    (await peerContext.request.get(`${address.origin}/api/v1${path}`)).status() === 404);
  await peer.goto(route(first.saved.id));
  await peer.getByRole('heading', { name: 'Acquisition unavailable', exact: true }).waitFor();
  check('foreign deep links expose no artifact values or corrected outputs', await peer.locator('#calibration-content').count() === 0);
  await progress('CHECKING_RETAINED_LINEAGE_AND_REVOCATION');
  await overview();
  await page.getByRole('button', { name: 'Archive Analytic calibration B', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Archive artifact', exact: true }).click();
  await page.getByRole('dialog').waitFor({ state: 'detached' });
  await page.waitForFunction(id => ![...document.querySelectorAll('[name="calibration_id"] option')].some(option => option.value === id), artifacts['calibration-alternative.json']);
  check('archiving removes a candidate from new selections while preserving existing revision files',
    (await api(page, `${revisionBase()}/${second.saved.id}`)).payload.revision.files_available);
  await page.goto(route(metadata.saved.id));
  await page.getByRole('heading', { name: 'Calibration metadata only', exact: true }).waitFor();
  await page.getByRole('button', { name: 'Delete revision files', exact: true }).click();
  const [deleted] = await Promise.all([
    page.waitForResponse(item => new URL(item.url()).pathname === `/api/v1${revisionBase()}/${metadata.saved.id}/files` && item.request().method() === 'DELETE'),
    page.getByRole('dialog').getByRole('button', { name: 'Delete revision files', exact: true }).click(),
  ]);
  assert.equal(deleted.status(), 200); deletedRevisionFiles.push(metadata.saved.id);
  await page.locator('.calibration-revision-detail .acquisition-status').filter({ hasText: /^Files deleted$/ }).waitFor();
  check('deleting revision files retains immutable metadata and revokes downloads',
    (await api(page, `${revisionBase()}/${metadata.saved.id}`)).payload.revision.selection.calibration_id === artifacts['calibration.json']
    && (await context.request.get(`${address.origin}/api/v1${revisionBase()}/${metadata.saved.id}/files?path=prepared-input.json`)).status() === 410);
  check('revision file deletion preserves the source and other corrected revisions',
    (await api(page, sourceBase())).payload.acquisition.files_available
    && (await api(page, `${revisionBase()}/${first.saved.id}`)).payload.revision.files_available
    && (await api(page, `${revisionBase()}/${second.saved.id}`)).payload.revision.files_available);
  evidence.revisions = [first.saved, second.saved].map((item, index) => ({
    id: item.id, request_sha256: item.request_sha256, source_identity: item.source_identity,
    prepared_input: item.result.prepared_input,
    files: item.manifest.files, imu: { path: [csvA, csvB][index].path,
      sha256: [csvA, csvB][index].sha256, size_bytes: [csvA, csvB][index].size_bytes, samples: [csvA, csvB][index].rows.length },
  }));
  check('artifact and sensor values are absent from browser persistent storage', await page.evaluate(() =>
    Object.keys(localStorage).every(key => key === 'meridian-theme') && sessionStorage.length === 0));
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  await page.getByRole('heading', { name: 'Sign in', exact: true }).waitFor();
  check('logout removes private calibration content and revokes revision reads',
    await page.locator('#calibration-content').count() === 0
    && (await context.request.get(`${address.origin}/api/v1${revisionBase()}/${first.saved.id}`)).status() === 401);
  check('no uncaught browser or CSP errors occur', errors.length === 0 && cspErrors.length === 0);
} catch (error) {
  failure = String(error.stack || error).replaceAll(password, '[redacted]');
  process.exitCode = 1;
  try { await page.screenshot({ path: `${output}failure.png`, fullPage: true }); } catch { /* Keep the original failure. */ }
} finally {
  const report = { schema_version: 1, base, checked_at: new Date().toISOString(), passed: !failure,
    source_synthetic: true, created_accounts: accounts, project_id: projectId, created_acquisitions: acquisitions,
    created_acquisition_ids: Object.values(acquisitions), deleted_acquisition_ids: [],
    created_artifacts: artifacts, created_artifact_ids: Object.values(artifacts),
    artifact_identities: Object.entries(artifacts).map(([file, id]) => ({ id, contract: manifest.artifacts[file].contract,
      descriptor_sha256: manifest.artifacts[file].sha256, descriptor_size_bytes: manifest.artifacts[file].size_bytes })),
    created_revisions: revisions,
    deleted_revision_files: deletedRevisionFiles, retained_revision_ids: [revisions.corrected_a, revisions.corrected_b].filter(Boolean),
    retained_acquisition_id: acquisitions.replay, source_sha256: manifest.packages.replay.sha256,
    source_size_bytes: manifest.packages.replay.size_bytes, content_security_policy: contentSecurityPolicy,
    evidence, checks, errors, cspErrors, ...(failure ? { failure } : {}),
    limitations: ['Analytic supplied parameters and observations; no calibration fitting or physical validation.',
      'No estimator, corrected-input plot, clock drift estimation or flight result.',
      'QA accounts and retained sources/revisions require exact operator cleanup after restoration. Credentials are not saved.'] };
  await writeFile(`${output}report.json`, JSON.stringify(report, null, 2) + '\n');
  await progress(failure ? 'FAILED' : 'PASSED');
  console.log(JSON.stringify({ passed: !failure, checks: checks.length, accounts, projectId, acquisitions, artifacts, revisions, output,
    ...(failure ? { failure } : {}) }, null, 2));
  await browser.close();
}
