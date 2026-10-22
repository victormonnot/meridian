/**
 * Run the predeclared first 30 s of the already-consumed real room1 recording
 * through an actual private upload, supplied calibration, two preparations and
 * two VIO jobs. Both use num_pts=200; no setting is retuned after observation.
 * Preserve QA identities and successful inputs/results for operator restoration.
 * Credentials stay in memory; reports contain identities and aggregate evidence.
 */
import assert from 'node:assert/strict';
import { createHash, randomBytes } from 'node:crypto';
import { createReadStream } from 'node:fs';
import { mkdir, readFile, writeFile, stat } from 'node:fs/promises';
import { resolve, sep, posix } from 'node:path';
import { pathToFileURL } from 'node:url';

const [baseArgument, outputArgument, fixtureArgument, ...extra] = process.argv.slice(2);
if (!baseArgument || !outputArgument || !fixtureArgument || extra.length) {
  throw new Error('Usage: node web/scripts/check-workspace-private-vio-browser.mjs WORKSPACE_URL OUTPUT_DIRECTORY FIXTURE_DIRECTORY');
}
const address = new URL(baseArgument);
if (!['http:', 'https:'].includes(address.protocol) || address.username || address.password || address.search || address.hash) {
  throw new Error('Use an explicit HTTP(S) workspace URL without credentials, query or fragment.');
}
const base = address.href, output = resolve(outputArgument) + sep, fixtures = resolve(fixtureArgument);
// Optional operator-assisted continuation preserves an interrupted QA upload.
// The supplied password is ephemeral; this script never resets credentials.
const resumeBytes = process.env.MERIDIAN_QA_RESUME_REPORT
  ? await readFile(resolve(process.env.MERIDIAN_QA_RESUME_REPORT)) : null;
const resume = resumeBytes ? JSON.parse(resumeBytes.toString('utf8')) : null;
if (resume) {
  assert.equal(resume.base, base);
  assert.equal(resume.created_accounts.length, 1);
  assert.match(resume.created_accounts[0], /^qa-private-vio-[a-z0-9-]+$/);
  assert.ok(Object.keys(resume.created_jobs || {}).length <= 2);
  assert.ok(process.env.MERIDIAN_QA_PASSWORD?.length >= 24, 'Resuming requires an ephemeral QA credential.');
}
const manifest = JSON.parse(await readFile(`${fixtures}/manifest.json`, 'utf8'));
assert.equal(manifest.synthetic, false);
assert.equal(manifest.recording_group, 'tumvi-room1');
assert.equal(manifest.counts.cam0, 600);
const archive = resolve(fixtures, manifest.package.file);
assert.equal((await stat(archive)).size, manifest.package.size_bytes);
const archiveHash = createHash('sha256');
for await (const chunk of createReadStream(archive)) archiveHash.update(chunk);
assert.equal(archiveHash.digest('hex'), manifest.package.sha256);
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const calibrationText = await readFile(resolve(fixtures, manifest.calibration.file));
assert.equal(hash(calibrationText), manifest.calibration.sha256);
const timeout = Number(process.env.MERIDIAN_JOB_QA_TIMEOUT_MS || 1_200_000);
assert.ok(Number.isSafeInteger(timeout) && timeout >= 30_000 && timeout <= 3_600_000);
await mkdir(output, { recursive: true });
const moduleUrl = process.env.MERIDIAN_PLAYWRIGHT_MODULE
  ? pathToFileURL(resolve(process.env.MERIDIAN_PLAYWRIGHT_MODULE)).href
  : new URL('../../build/browser-tools/node_modules/playwright/index.mjs', import.meta.url).href;
const { chromium } = await import(moduleUrl);
const browser = await chromium.launch({ headless: true,
  ...(process.env.MERIDIAN_BROWSER_EXECUTABLE ? { executablePath: process.env.MERIDIAN_BROWSER_EXECUTABLE } : {}) });
const context = await browser.newContext({ viewport: { width: 1440, height: 1100 } });
const peerContext = await browser.newContext();
const page = await context.newPage(), peer = await peerContext.newPage();
const stamp = `${Date.now().toString(36)}-${randomBytes(3).toString('hex')}`;
const usernames = [`qa-private-vio-${stamp}`, `qa-private-vio-peer-${stamp}`];
if (resume) usernames[0] = resume.created_accounts[0];
const password = resume ? process.env.MERIDIAN_QA_PASSWORD : `Qa!${randomBytes(24).toString('base64url')}`;
const accounts = [], revisions = {}, experiments = {}, jobs = {}, checks = [], errors = [], cspErrors = [];
const deletedRevisionFiles = [...(resume?.deleted_revision_files || [])];
const priorAttempts = [...(resume?.prior_attempts || [])];
const evidence = { predeclared_feature_budget: 200, runs: { ...(resume?.evidence?.runs || {}) } };
let projectId, acquisitionId, artifactId, failure;
if (resume) {
  accounts.push(...resume.created_accounts);
  Object.assign(revisions, resume.created_revisions);
  Object.assign(experiments, resume.created_experiments);
  Object.assign(jobs, resume.created_jobs);
  checks.push(...resume.checks);
  projectId = resume.project_id; acquisitionId = resume.retained_acquisition_id; artifactId = resume.created_artifact_ids[0];
  assert.equal(resume.source_sha256, manifest.package.sha256);
}
const check = (label, condition) => { assert.ok(condition, label); checks.push(label); };
const acqBase = () => `/projects/${projectId}/acquisitions/${acquisitionId}`;
const revisionsBase = () => `${acqBase()}/input-revisions`;
const artifactsBase = () => `/projects/${projectId}/sensor-artifacts`;
const experimentsBase = () => `/projects/${projectId}/experiments`;
const jobsBase = () => `/projects/${projectId}/jobs`;
const calibrationRoute = revision => `${base}#project/${projectId}/data/acquisitions/${acquisitionId}/calibration${revision ? `/revisions/${revision}` : ''}`;
for (const target of [page, peer]) {
  target.on('pageerror', error => errors.push({ message: error.message, stack: error.stack, after_check: checks.at(-1) }));
  target.on('dialog', dialog => dialog.accept());
  target.on('console', message => {
    if (message.type() === 'error' && /content security policy|violat.*(?:directive|policy)|refused to (?:load|execute|apply|connect)/i.test(message.text())) cspErrors.push(message.text());
  });
}
async function progress(state) {
  await writeFile(`${output}progress.json`, JSON.stringify({ state, accounts, project_id: projectId,
    acquisition_id: acquisitionId, artifact_id: artifactId, revisions, experiments, jobs, checks: checks.length }, null, 2) + '\n');
  console.log(state);
}
async function api(target, path, method = 'GET', body) {
  return target.evaluate(async ({ path, method, body }) => {
    const session = await (await fetch('/api/v1/session', { cache: 'no-store' })).json();
    const response = await fetch(`/api/v1${path}`, { method, cache: 'no-store',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': session.csrf_token },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    const bytes = await response.arrayBuffer();
    const sha256 = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), value => value.toString(16).padStart(2, '0')).join('');
    let payload;
    try { payload = JSON.parse(new TextDecoder().decode(bytes)); } catch { /* Binary endpoints use the request context. */ }
    return { status: response.status, payload, sha256, size_bytes: bytes.byteLength };
  }, { path, method, body });
}
async function register(target, name) {
  await target.goto(base);
  await target.getByRole('button', { name: 'Create account', exact: true }).click();
  await target.getByLabel('Username', { exact: true }).fill(name);
  await target.getByLabel('Password', { exact: true }).fill(password);
  const [response] = await Promise.all([
    target.waitForResponse(item => new URL(item.url()).pathname === '/api/v1/auth/register' && item.request().method() === 'POST'),
    target.getByRole('button', { name: 'Create account', exact: true }).click(),
  ]);
  assert.ok(response.ok()); accounts.push(name);
  await target.getByRole('heading', { name: 'Projects', exact: true }).waitFor();
}
async function calibrationOverview() {
  await page.goto(calibrationRoute());
  await page.getByRole('heading', { name: 'Saved artifacts', exact: true }).waitFor();
}
async function prepare(key, includeReference, withCalibration = true) {
  if (revisions[key]) {
    const result = await api(page, `${revisionsBase()}/${revisions[key]}`);
    assert.equal(result.status, 200);
    assert.equal(result.payload.revision.state, 'ready');
    assert.equal(result.payload.revision.selection.calibration_id, artifactId);
    assert.equal(result.payload.revision.selection.include_reference, includeReference);
    return result.payload.revision;
  }
  await calibrationOverview();
  await page.getByRole('combobox', { name: 'Calibration artifact', exact: true }).selectOption(withCalibration ? artifactId : '');
  if (withCalibration) await page.locator(`[data-artifact-id="${artifactId}"]`).waitFor();
  await page.getByLabel('Include independent reference observations', { exact: true }).setChecked(includeReference);
  await page.getByLabel('Revision name', { exact: true }).fill(`Room1 ${key}`);
  check('preparation never enables an unrequested IMU correction', !await page.getByLabel('Apply supplied IMU bias and scale', { exact: true }).isChecked());
  const [assessment] = await Promise.all([
    page.waitForResponse(item => new URL(item.url()).pathname === `/api/v1${revisionsBase()}/assess` && item.request().method() === 'POST'),
    page.getByRole('button', { name: 'Check revision', exact: true }).click(),
  ]);
  assert.equal(assessment.status(), 200);
  const [response] = await Promise.all([
    page.waitForResponse(item => new URL(item.url()).pathname === `/api/v1${revisionsBase()}` && item.request().method() === 'POST'),
    page.getByRole('button', { name: 'Create input revision', exact: true }).click(),
  ]);
  assert.equal(response.status(), 201); revisions[key] = (await response.json()).revision.id;
  await progress(`PREPARING_${key.toUpperCase()}`);
  await page.waitForURL(url => url.hash === new URL(calibrationRoute(revisions[key])).hash);
  await page.locator('.calibration-revision-detail .acquisition-status').filter({ hasText: /^Prepared input available$/ }).waitFor({ timeout });
  const saved = (await api(page, `${revisionsBase()}/${revisions[key]}`)).payload.revision;
  check(`${key} prepared input retains selected supplied metadata and reference policy`,
    saved.state === 'ready' && saved.files_available && saved.selection.include_reference === includeReference
    && saved.selection.calibration_id === (withCalibration ? artifactId : null));
  return saved;
}
async function experimentDialog(revisionId, name) {
  await page.goto(`${base}#project/${projectId}/experiments`);
  await page.getByRole('button', { name: 'New experiment', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('Experiment name', { exact: true }).fill(name);
  await dialog.getByRole('combobox', { name: 'Input profile', exact: true }).selectOption('openvins-private-stereo-v1');
  const [response] = await Promise.all([
    page.waitForResponse(item => new URL(item.url()).pathname === `/api/v1/projects/${projectId}/execution-inputs/assess` && item.request().method() === 'POST'),
    dialog.getByRole('combobox', { name: 'Prepared input revision', exact: true }).selectOption(revisionId),
  ]);
  assert.equal(response.status(), 200);
  return { dialog, assessment: await response.json() };
}
async function createExperiment(key) {
  if (experiments[key]) {
    const detail = await api(page, `${experimentsBase()}/${experiments[key]}`);
    assert.equal(detail.status, 200);
    assert.equal(detail.payload.revision.input_selection.input_revision_id, revisions[key]);
    assert.equal(detail.payload.revision.specification.parameters.num_pts, 200);
    await page.goto(`${base}#project/${projectId}/experiments/${experiments[key]}`);
    await page.getByRole('button', { name: 'Run version 1', exact: true }).waitFor();
    return detail.payload.revision;
  }
  const { dialog, assessment } = await experimentDialog(revisions[key], `Private room1 ${key}`);
  check(`${key} is admitted with the exact owned revision and predeclared bounds`,
    assessment.eligible && assessment.selection.input_revision_id === revisions[key]
    && assessment.selection.interval.first_ns === manifest.interval.start_ns
    && assessment.selection.interval.last_ns === manifest.interval.last_camera_ns);
  check('private policy fixes supplied calibration, masks and gravity before execution',
    assessment.specification.policy.calibration === 'fixed_supplied_parameters_no_online_refinement'
    && assessment.specification.policy.masks === 'disabled' && assessment.specification.policy.gravity_m_s2 === 9.80766);
  assert.equal(await dialog.getByLabel('Feature budget', { exact: true }).inputValue(), '200');
  await dialog.getByLabel('First camera timestamp · ns', { exact: true }).fill(String(BigInt(manifest.interval.start_ns) + 1n));
  check('editing a single nanosecond invalidates the checked input', await dialog.getByRole('button', { name: 'Create experiment', exact: true }).isDisabled());
  await dialog.getByLabel('First camera timestamp · ns', { exact: true }).fill(manifest.interval.start_ns);
  const [rechecked] = await Promise.all([
    page.waitForResponse(item => new URL(item.url()).pathname.endsWith('/execution-inputs/assess') && item.request().method() === 'POST'),
    dialog.getByRole('button', { name: 'Check input', exact: true }).click(),
  ]);
  assert.equal(rechecked.status(), 200); assert.equal((await rechecked.json()).eligible, true);
  const [created] = await Promise.all([
    page.waitForResponse(item => new URL(item.url()).pathname === `/api/v1${experimentsBase()}` && item.request().method() === 'POST'),
    dialog.getByRole('button', { name: 'Create experiment', exact: true }).click(),
  ]);
  assert.equal(created.status(), 201);
  const payload = await created.json(); experiments[key] = payload.experiment.id;
  const detail = await api(page, `${experimentsBase()}/${experiments[key]}`);
  check('saved experiment preserves the server-frozen prepared input and feature count',
    detail.payload.revision.input_selection.input_revision_id === revisions[key]
    && detail.payload.revision.specification.parameters.num_pts === 200
    && detail.payload.revision.specification.input.prepared_input_sha256 === assessment.specification.input.prepared_input_sha256);
  await page.getByRole('button', { name: 'Run version 1', exact: true }).waitFor();
  return detail.payload.revision;
}
async function execute(key, revision) {
  if (jobs[key]) {
    const existing = (await api(page, `${jobsBase()}/${jobs[key]}`)).payload.job;
    assert.equal(existing.specification_sha256, revision.specification_sha256);
    if (existing.state === 'failed') {
      assert.equal(process.env.MERIDIAN_QA_RETRY_INFRASTRUCTURE_JOB, existing.id,
        'Retrying a failed execution requires its explicit operator-approved UUID.');
      assert.equal(existing.failure?.code, 'infrastructure_failure');
      assert.equal(existing.result, null);
      priorAttempts.push({ job_id: existing.id, specification_sha256: existing.specification_sha256,
        state: existing.state, failure: existing.failure, reason: 'Explicit retry after infrastructure repair; scientific input unchanged.' });
      delete jobs[key];
    } else {
      assert.notEqual(existing.state, 'cancelled');
      await page.goto(`${base}#project/${projectId}/results/${existing.id}`);
    }
  }
  if (!jobs[key]) {
  const [submitted] = await Promise.all([
    page.waitForResponse(item => new URL(item.url()).pathname === `/api/v1${jobsBase()}` && item.request().method() === 'POST'),
    page.getByRole('button', { name: 'Run version 1', exact: true }).click(),
  ]);
  assert.equal(submitted.status(), 201);
  const submission = await submitted.json(); jobs[key] = submission.job.id;
  await progress(`RUNNING_${key.toUpperCase()}`);
  const replay = await api(page, jobsBase(), 'POST', submitted.request().postDataJSON());
  check('retrying the exact job submission recovers one execution identity', replay.payload.job.id === jobs[key] && replay.payload.reused === true);
  }
  await page.reload();
  let saved;
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    saved = (await api(page, `${jobsBase()}/${jobs[key]}`)).payload.job;
    if (['succeeded', 'failed', 'cancelled'].includes(saved.state)) break;
    await page.waitForTimeout(2000);
  }
  assert.equal(saved?.state, 'succeeded', `${key} terminal state: ${saved?.state}; ${saved?.failure?.code || ''}`);
  await page.locator('.job-detail .job-status').filter({ hasText: /^Succeeded$/ }).waitFor();
  check(`${key} actual execution succeeds without claiming room1 qualification`,
    saved.result.qualification.passed === null && saved.result.qualification.status === 'not_assessed'
    && saved.specification_sha256 === revision.specification_sha256 && saved.result.evaluation.counts.estimate_samples > 0);
  const availability = saved.result.evaluation.availability;
  check(`${key} reference availability matches the selected input policy`, key === 'with_reference'
    ? ['available', 'partial'].includes(availability.status) && saved.result.evaluation.metrics !== null
    : availability.status === 'unavailable' && saved.result.evaluation.metrics === null);
  check('private run does not offer the public-recording export recipe', await page.getByRole('button', { name: 'Download experiment', exact: true }).count() === 0);
  const exported = await context.request.get(`${address.origin}/api/v1${jobsBase()}/${jobs[key]}/export?schema_version=1`);
  check('private export is explicitly unavailable', !exported.ok() && (await exported.json()).error.code === 'private_export_unavailable');
  return saved;
}
async function investigate(key, job, prepared, revision) {
  await page.getByRole('link', { name: 'Investigate run', exact: true }).click();
  await page.getByRole('heading', { name: 'Shared timeline', exact: true }).waitFor({ timeout: 30_000 });
  const path = `${jobsBase()}/${job.id}/investigation`;
  const response = await api(page, path);
  assert.equal(response.status, 200);
  const artifact = response.payload.investigation;
  check('private investigation binds exact prepared and experiment identities',
    artifact.schema_version === 2 && artifact.specification_sha256 === job.specification_sha256
    && artifact.input.input_revision_id === revisions[key]
    && artifact.input.prepared_input_sha256 === prepared.result.prepared_input.sha256);
  check('every predeclared camera epoch and prepared IMU sample remains accounted for',
    artifact.frames.length === manifest.counts.cam0 && artifact.imu.samples.length === manifest.counts.imu0);
  const selected = artifact.frames.find(frame => frame.estimated);
  assert.ok(selected);
  await page.locator('#frame-cursor').evaluate((input, value) => {
    input.value = value;
    input.dispatchEvent(new Event('input', { bubbles: true }));
  }, String(selected.index));
  await page.locator('#image-cam0').waitFor({ state: 'visible' });
  await page.waitForFunction(() => document.querySelector('#image-cam0')?.complete && document.querySelector('#image-cam0')?.naturalWidth > 0);
  check('trajectory is actually plotted from retained numerical evidence',
    await page.locator('svg[aria-label="X–Y trajectory"] path.series-estimate').evaluateAll(paths => paths.some(path => Boolean(path.getAttribute('d')) && !/NaN|Infinity/.test(path.getAttribute('d')))));
  if (key === 'without_reference') {
    check('no-reference investigation shows estimator frame and explicit unavailable accuracy',
      (await page.locator('#investigation-content').innerText()).includes('estimator’s own world frame')
      && await page.getByRole('heading', { name: 'Position error', exact: true }).count() === 0
      && artifact.alignment === null && artifact.frames.every(frame => frame.position_error_m === null && frame.estimate === null));
    check('no unaligned reference path is overlaid',
      await page.locator('.investigation-projections path.series-reference').evaluateAll(paths => paths.every(path => !path.getAttribute('d'))));
  }
  const cameraOutput = prepared.prepared_input.outputs.find(item => item.stream_id === artifact.cameras[0].stream_id);
  const csvResponse = await context.request.get(`${address.origin}/api/v1${revisionsBase()}/${revisions[key]}/files?${new URLSearchParams({ path: cameraOutput.index_path })}`);
  assert.equal(csvResponse.status(), 200);
  const rows = (await csvResponse.text()).trim().split('\n').slice(1).map(line => line.split(','));
  const sourceRow = rows.find(row => row[2] === selected.timestamp_ns);
  assert.ok(sourceRow);
  const imagePath = posix.join(posix.dirname(cameraOutput.index_path), sourceRow[3]);
  const identity = prepared.manifest.files[imagePath];
  const imageResponse = await context.request.get(`${address.origin}/api/v1${path}/images/cam0/${selected.index}`);
  const imageBytes = await imageResponse.body();
  check('owned investigation image is the exact retained prepared observation',
    imageResponse.status() === 200 && hash(imageBytes) === identity.sha256 && imageBytes.length === identity.size_bytes);
  await page.getByLabel('Interval start · s', { exact: true }).fill(String(selected.time_s));
  const [intervalResponse] = await Promise.all([
    page.waitForResponse(item => new URL(item.url()).pathname === `/api/v1${path}/metrics` && new URL(item.url()).searchParams.get('start_ns') === selected.timestamp_ns),
    page.getByRole('button', { name: 'Apply interval', exact: true }).click(),
  ]);
  const intervalResult = (await intervalResponse.json()).interval;
  check('interval selection preserves exact sample timestamps and reference availability',
    intervalResponse.status() === 200 && intervalResult.evaluation
    && (key === 'with_reference' || intervalResult.evaluation.status === 'unavailable'));
  check('range controls do not trigger unrelated workspace forms', errors.length === 0);
  await page.getByRole('button', { name: 'Full recording', exact: true }).click();
  await page.reload();
  await page.getByRole('heading', { name: 'Shared timeline', exact: true }).waitFor();
  check('investigation survives refresh with its immutable source selection', (await api(page, path)).sha256 === response.sha256);
  for (const theme of ['dark', 'light']) {
    await page.getByLabel('Appearance', { exact: true }).selectOption(theme);
    await page.screenshot({ path: `${output}${key}-${theme}.png`, fullPage: true });
  }
  await page.setViewportSize({ width: 390, height: 844 });
  check('private investigation fits the mobile viewport', await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.screenshot({ path: `${output}${key}-mobile.png`, fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1100 });
  evidence.runs[key] = { job_id: job.id, experiment_id: experiments[key], input_revision_id: revisions[key],
    specification_sha256: job.specification_sha256, specification: revision.specification,
    prepared_input: prepared.result.prepared_input, profile_id: job.profile_id, qualification: job.result.qualification,
    evaluation: job.result.evaluation, investigation_response_sha256: response.sha256,
    investigation_response_bytes: response.size_bytes, expected_camera_epochs: artifact.frames.length,
    imu_samples: artifact.imu.samples.length,
    selected_image: { camera: 'cam0', frame_index: selected.index, timestamp_ns: selected.timestamp_ns,
      relative_path: imagePath, sha256: hash(imageBytes), size_bytes: imageBytes.length } };
}

try {
  if (resume) {
    await page.goto(base);
    await page.getByLabel('Username', { exact: true }).fill(usernames[0]);
    await page.getByLabel('Password', { exact: true }).fill(password);
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
    await page.getByRole('heading', { name: 'Projects', exact: true }).waitFor();
    check('operator-assisted resume retains the exact uploaded source identity',
      (await api(page, acqBase())).payload.acquisition.sha256 === manifest.package.sha256);
  } else {
  await register(page, usernames[0]);
  check('deployment enables the authorized real computation', (await api(page, '/session')).payload.capabilities.execution === true);
  await page.getByRole('button', { name: 'New project', exact: true }).click();
  await page.getByLabel('Project name', { exact: true }).fill('Private room1 execution acceptance');
  const [createdProject] = await Promise.all([
    page.waitForResponse(item => new URL(item.url()).pathname === '/api/v1/projects' && item.request().method() === 'POST'),
    page.getByRole('button', { name: 'Create project', exact: true }).click(),
  ]);
  assert.equal(createdProject.status(), 201); projectId = (await createdProject.json()).project.id;
  await page.getByRole('button', { name: 'Upload recording', exact: true }).click();
  await page.getByLabel('Acquisition name', { exact: true }).fill('Consumed real room1 · first 30 seconds');
  await page.getByLabel('Recording archive', { exact: true }).setInputFiles(archive);
  const [uploaded] = await Promise.all([
    page.waitForResponse(item => new URL(item.url()).pathname === `/api/v1/projects/${projectId}/acquisitions` && item.request().method() === 'POST'),
    page.getByRole('button', { name: 'Start upload', exact: true }).click(),
  ]);
  assert.equal(uploaded.status(), 201); acquisitionId = (await uploaded.json()).acquisition.id;
  await progress('UPLOADING_REAL_ROOM1_CROP');
  await page.waitForURL(url => url.hash === `#project/${projectId}/data/acquisitions/${acquisitionId}`);
  await page.locator('.acquisition-detail .acquisition-status').filter({ hasText: /^Ready to inspect$/ }).waitFor({ timeout });
  const acquired = (await api(page, acqBase())).payload.acquisition;
  check('real uploaded archive retains its independently verified identity', acquired.sha256 === manifest.package.sha256 && acquired.size_bytes === manifest.package.size_bytes);
  await calibrationOverview();
  await page.getByRole('button', { name: 'Import artifact', exact: true }).click();
  const artifactDialog = page.getByRole('dialog');
  await artifactDialog.getByLabel('Artifact name', { exact: true }).fill('Supplied room1 calibration · fixed');
  await artifactDialog.getByLabel('Descriptor JSON', { exact: true }).setInputFiles(resolve(fixtures, manifest.calibration.file));
  for (const box of await artifactDialog.locator('input[name="acquisition_ids"]').all()) await box.setChecked(await box.getAttribute('value') === acquisitionId);
  const [imported] = await Promise.all([
    page.waitForResponse(item => new URL(item.url()).pathname === `/api/v1${artifactsBase()}` && item.request().method() === 'POST'),
    artifactDialog.getByRole('button', { name: 'Import artifact', exact: true }).click(),
  ]);
  assert.equal(imported.status(), 201);
  const artifact = (await imported.json()).artifact; artifactId = artifact.id;
  await artifactDialog.waitFor({ state: 'detached' });
  await page.locator(`[data-artifact-id="${artifactId}"]`).waitFor();
  check('supplied artifact retains its declared calibration identity', artifact.contract_sha256 === manifest.calibration.contract.sha256);
  }
  const withReference = await prepare('with_reference', true);
  const withoutReference = await prepare('without_reference', false);
  const unsupported = await prepare('unsupported', false);
  check('reference selection preserves identical sensor payload identities',
    Object.entries(withReference.manifest.files).filter(([name]) => name.startsWith('sensors/')).every(([name, identity]) =>
      JSON.stringify(identity) === JSON.stringify(withoutReference.manifest.files[name])));
  if (!deletedRevisionFiles.includes(unsupported.id)) {
  const rejected = await experimentDialog(revisions.unsupported, 'Unsupported clock segment');
  assert.equal(rejected.assessment.eligible, true);
  await rejected.dialog.getByLabel('Clock segment', { exact: true }).fill('undeclared-segment');
  const [rejectedResponse] = await Promise.all([
    page.waitForResponse(item => new URL(item.url()).pathname.endsWith('/execution-inputs/assess') && item.request().method() === 'POST'),
    rejected.dialog.getByRole('button', { name: 'Check input', exact: true }).click(),
  ]);
  assert.equal(rejectedResponse.status(), 200);
  const rejectedAssessment = await rejectedResponse.json();
  check('unsupported prepared input is explicitly refused before creating an experiment',
    rejectedAssessment.eligible === false && rejectedAssessment.reasons.length > 0
    && await rejected.dialog.getByRole('button', { name: 'Create experiment', exact: true }).isDisabled());
  await rejected.dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
  await page.goto(calibrationRoute(unsupported.id));
  await page.getByRole('button', { name: 'Delete revision files', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Delete revision files', exact: true }).click();
  await page.locator('.calibration-revision-detail .acquisition-status').filter({ hasText: /^Files deleted$/ }).waitFor();
  deletedRevisionFiles.push(unsupported.id);
  }
  const missing = await api(page, `/projects/${projectId}/execution-inputs/assess`, 'POST',
    { schema_version: 1, input_revision_id: unsupported.id, interval: null, parameters: { num_pts: 200 } });
  check('deleted test-revision files cannot be silently replaced by current source data',
    missing.status === 200 && missing.payload.eligible === false && missing.payload.reasons.length > 0);
  for (const [key, prepared] of [['with_reference', withReference], ['without_reference', withoutReference]]) {
    const revision = await createExperiment(key);
    const job = await execute(key, revision);
    await investigate(key, job, prepared, revision);
  }
  const comparison = await api(page, `/projects/${projectId}/comparisons`, 'POST',
    { schema_version: 1, name: 'Unsupported private comparison', description: '',
      baseline_job_id: jobs.with_reference, candidate_job_id: jobs.without_reference, idempotency_key: crypto.randomUUID() });
  check('private comparisons are explicitly refused without misrepresenting evaluation support',
    comparison.status === 409 && comparison.payload.error.code === 'private_comparison_unavailable');
  await progress('CHECKING_PRIVATE_OWNER_BOUNDARIES');
  const paths = [`/projects/${projectId}/execution-inputs`, `${artifactsBase()}/${artifactId}`,
    `${revisionsBase()}/${revisions.with_reference}`, `${jobsBase()}/${jobs.without_reference}`,
    `${jobsBase()}/${jobs.without_reference}/investigation`,
    `${jobsBase()}/${jobs.without_reference}/investigation/images/cam0/0`];
  for (const path of paths) check('anonymous private execution evidence returns401',
    (await peerContext.request.get(`${address.origin}/api/v1${path}`)).status() === 401);
  await register(peer, usernames[1]);
  for (const path of paths) check('foreign-owner private execution evidence returns404',
    (await peerContext.request.get(`${address.origin}/api/v1${path}`)).status() === 404);
  const foreignAssess = await api(peer, `/projects/${projectId}/execution-inputs/assess`, 'POST',
    { schema_version: 1, input_revision_id: revisions.with_reference, interval: null, parameters: { num_pts: 200 } });
  check('foreign input assessment cannot resolve owned source metadata', foreignAssess.status === 404);
  check('private execution data is absent from persistent browser storage', await page.evaluate(() =>
    Object.keys(localStorage).every(key => key === 'meridian-theme') && sessionStorage.length === 0));
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  await page.getByRole('heading', { name: 'Sign in', exact: true }).waitFor();
  check('logout clears private investigation and revokes subsequent reads', await page.locator('#investigation-content').count() === 0
    && (await context.request.get(`${address.origin}/api/v1${jobsBase()}/${jobs.without_reference}/investigation`)).status() === 401);
  check('no uncaught browser or CSP errors occur', errors.length === 0 && cspErrors.length === 0);
} catch (error) {
  failure = String(error.stack || error).replaceAll(password, '[redacted]');
  process.exitCode = 1;
  try { await page.screenshot({ path: `${output}failure.png`, fullPage: true }); } catch { /* Retain the original error. */ }
} finally {
  const report = { schema_version: 1, base, checked_at: new Date().toISOString(), passed: !failure,
    ...(resume ? { resumed_from_report_sha256: hash(resumeBytes), resumed_from_checked_at: resume.checked_at } : {}),
    created_accounts: accounts, project_id: projectId, created_acquisition_ids: acquisitionId ? [acquisitionId] : [],
    deleted_acquisition_ids: [], retained_acquisition_id: acquisitionId,
    source_sha256: manifest.package.sha256, source_size_bytes: manifest.package.size_bytes, source_synthetic: false,
    created_artifact_ids: artifactId ? [artifactId] : [], artifact_identity: manifest.calibration,
    created_revisions: revisions, retained_revision_ids: [revisions.with_reference, revisions.without_reference].filter(Boolean),
    deleted_revision_files: deletedRevisionFiles, created_experiments: experiments,
    created_jobs: jobs, retained_job_ids: Object.values(jobs), prior_attempts: priorAttempts, evidence, checks, errors, cspErrors,
    ...(failure ? { failure } : {}),
    limitations: ['First30s of previously consumed TUM-VI room1; no new reserved recording or held-out evaluation.',
      'Both real runs use the predeclared200-feature fixed-calibration private profile; no result-driven retuning.',
      'No physical calibration, embedded or flight qualification. QA identities and retained bytes require exact cleanup after restoration.'] };
  await writeFile(`${output}report.json`, JSON.stringify(report, null, 2) + '\n');
  await progress(failure ? 'FAILED' : 'PASSED');
  console.log(JSON.stringify({ passed: !failure, checks: checks.length, accounts, projectId, acquisitionId,
    artifactId, revisions, experiments, jobs, output, ...(failure ? { failure } : {}) }, null, 2));
  await browser.close();
}
