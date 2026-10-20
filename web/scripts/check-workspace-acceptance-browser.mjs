/**
 * Execute the complete hosted reference workflow on an authorized deployment.
 * All projects, configurations, runs, comparisons and downloads are created
 * through the actual UI. API reads retain exact evidence and test ownership.
 * Two fresh accounts admit four requests: a 200-point baseline for each account,
 * one cancelled queued request, and the owner's 100-point variant. A fifth
 * submission must be refused by the active-per-account limit without admission.
 * One export request is deliberately interrupted; no business response, image,
 * trajectory, metric, archive or job state is mocked.
 *
 * Retains exact created identities and scientific responses for independent
 * audit, export reproduction, backup/restore checks and subsequent operator
 * cleanup. Credentials and cookies remain in memory and are never written.
 */
import assert from 'node:assert/strict';
import { createHash, randomBytes } from 'node:crypto';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { arch, platform, release } from 'node:os';
import { resolve, sep } from 'node:path';
import { pathToFileURL } from 'node:url';

const checklist = {
  signup: 'Two separate browser contexts register fresh accounts through the UI.',
  signout: 'Actual server logout invalidates authenticated access.',
  signin: 'The owner can sign in again through the UI.',
  execution: 'The selected deployment explicitly enables actual VIO execution.',
  projects: 'Both accounts create their own private room1 project through the UI.',
  provenance: 'The data view exposes calibration, coordinates, time and source provenance.',
  baseline: 'The owner saves the default 200-point configuration through the UI.',
  baseline_identity: 'The baseline submission retains its exact saved version and specification.',
  running: 'The actual baseline reaches a running state.',
  peer_queue: 'A second account can submit its own run while the global worker is occupied.',
  owner_queue: 'The owner can retain a second active request in the queue.',
  queue_quota: 'A third active owner request is refused with 409 queue_full.',
  quota_feedback: 'The UI explains the refused active request and leaves the saved version accessible.',
  cancellation: 'The spare queued request cancels through confirmation without an attempt or result.',
  limits: 'The run view displays the two-active-per-account and one-running-global limits.',
  refresh: 'The exact submitted baseline remains accessible after navigation and reload.',
  baseline_result: 'The real 200-point baseline succeeds with independently evaluated results.',
  coverage: 'The result view distinguishes estimate, evaluated and reference coverage.',
  investigation: 'Full-recording investigation metrics agree with the retained job result.',
  missing: 'The initial unsupported pose is explicitly unavailable.',
  stereo: 'Both original 512-pixel stereo images decode with nonblank intensity.',
  cursor: 'Keyboard cursor movement selects the exact next camera timestamp.',
  interval: 'A selected interval uses full-resolution support and the unchanged global alignment.',
  imu: 'The cursor identifies a nearest raw IMU sample and its signed time difference.',
  plots: 'Trajectory projections and synchronized signals contain actual plot paths.',
  diagnostics: 'Unavailable internal estimator diagnostics are explicitly identified.',
  interval_error: 'Reversed interval bounds show validation without replacing the selected evidence.',
  investigation_layout: 'Investigation has no page overflow at 1440 and 390 pixels.',
  revision: 'Changing the feature budget through the UI creates immutable version 2 and preserves version 1.',
  variant_result: 'The real 100-point variant succeeds using exactly saved version 2.',
  peer_result: 'The second account completes its own real baseline with independent ownership.',
  serialization: 'The three successful job execution intervals do not overlap.',
  comparison_validation: 'Self-comparison is rejected without discarding the comparison draft.',
  comparison_identity: 'The saved comparison retains exact baseline/candidate runs and the 200-to-100 change.',
  common_support: 'The UI displays service-computed scores on shared full-resolution support.',
  interpretation: 'Coverage, initialization and host costs remain visible without an automatic winner claim.',
  comparison_reload: 'The immutable saved comparison and its analysis hash survive reload.',
  comparison_layout: 'Comparison and expanded support evidence fit 1440 and 390 pixel viewports.',
  table_keyboard: 'The wide support table can receive keyboard focus.',
  export_retry: 'An interrupted real download shows an error and permits retry.',
  export: 'Keyboard activation saves a real ZIP whose length and SHA-256 match HTTP evidence.',
  export_repeat: 'Repeated downloads of the unchanged successful run are byte-identical.',
  export_layout: 'The export detail fits desktop and mobile layouts in the tested themes.',
  ownership: 'The peer cannot access the owner project, configuration, jobs, comparison, investigation, images or export.',
  owner_isolation: 'The owner cannot read the peer project or its successful run.',
  private_route: 'A substituted private run deep link displays an unavailable state to the peer.',
  logout_private: 'Server logout removes private content and denies subsequent export requests.',
  persisted_journey: 'Signing in again reopens the saved comparison and the two original run identities.',
  storage: 'Private project, comparison and run evidence is absent from browser storage.',
  browser_errors: 'No uncaught JavaScript error or CSP violation occurs.',
};
const [baseArgument, outputArgument, ...extra] = process.argv.slice(2);
if (!baseArgument || !outputArgument || extra.length) throw new Error('Usage: node web/scripts/check-workspace-acceptance-browser.mjs WORKSPACE_URL OUTPUT_DIRECTORY');
const address = new URL(baseArgument);
if (!['http:', 'https:'].includes(address.protocol) || address.username || address.password || address.search || address.hash) throw new Error('Use an explicit HTTP(S) URL without credentials, query or fragment.');
const base = address.href;
const output = resolve(outputArgument) + sep;
const moduleUrl = process.env.MERIDIAN_PLAYWRIGHT_MODULE ? pathToFileURL(resolve(process.env.MERIDIAN_PLAYWRIGHT_MODULE)).href : new URL('../../build/browser-tools/node_modules/playwright/index.mjs', import.meta.url).href;
const { chromium } = await import(moduleUrl);
const timeout = Number(process.env.MERIDIAN_JOB_QA_TIMEOUT_MS || 1_200_000);
if (!Number.isSafeInteger(timeout) || timeout < 30_000 || timeout > 3_600_000) throw new Error('Invalid MERIDIAN_JOB_QA_TIMEOUT_MS.');
await mkdir(output, { recursive: true });
const hash = value => createHash('sha256').update(value).digest('hex');
const scriptHash = hash(await readFile(new URL(import.meta.url)));
await writeFile(`${output}predeclared-checklist.json`, JSON.stringify({ schema_version: 1, declared_at: new Date().toISOString(), script_sha256: scriptHash, checklist }, null, 2) + '\n');
const browser = await chromium.launch({ headless: true, ...(process.env.MERIDIAN_BROWSER_EXECUTABLE ? { executablePath: process.env.MERIDIAN_BROWSER_EXECUTABLE } : {}) });
const context = await browser.newContext({ viewport: { width: 1440, height: 1200 }, acceptDownloads: true });
const other = await browser.newContext({ viewport: { width: 1440, height: 1200 } });
const page = await context.newPage();
const peer = await other.newPage();
const stamp = `${Date.now().toString(36)}-${randomBytes(3).toString('hex')}`;
const usernames = [`qa-acceptance-${stamp}`, `qa-acceptance-peer-${stamp}`];
const password = `Qa!${randomBytes(24).toString('base64url')}`;
const accounts = [];
const projects = [];
const experiments = [];
const jobs = [];
const completedJobs = {};
const comparisons = [];
const checks = [];
const errors = [];
const cspErrors = [];
const transitions = [];
const downloads = [];
const timings = [];
const startedAt = new Date().toISOString();
let failure;
let investigation;
let savedComparison;
let quotaEvidence;
const check = (key, condition) => {
  assert.ok(Object.hasOwn(checklist, key), `Unknown checklist entry ${key}`);
  assert.ok(!checks.some(item => item.key === key), `Duplicate checklist entry ${key}`);
  assert.ok(condition, checklist[key]);
  checks.push({ key, description: checklist[key] });
};
async function progress(state) {
  await writeFile(`${output}progress.json`, JSON.stringify({ state, checked_at: new Date().toISOString(), accounts, projects, experiments, jobs, comparisons, checks: checks.length }, null, 2) + '\n');
  console.log(state);
}
for (const target of [page, peer]) {
  target.on('pageerror', error => errors.push(error.message));
  target.on('console', message => { if (message.type() === 'error' && /content security policy|violat.*(?:directive|policy)|refused to (?:load|execute|apply|connect)/i.test(message.text())) cspErrors.push(message.text()); });
}
async function api(target, path, method = 'GET', body) {
  return target.evaluate(async ({ path, method, body }) => {
    const session = await (await fetch('/api/v1/session', { cache: 'no-store' })).json();
    const response = await fetch(`/api/v1${path}`, { method, cache: 'no-store', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': session.csrf_token }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    const raw = await response.text();
    return { status: response.status, payload: JSON.parse(raw), raw };
  }, { path, method, body });
}
async function saveResponse(name, response) {
  const bytes = await response.body();
  await writeFile(`${output}${name}`, bytes);
  return JSON.parse(bytes.toString('utf8'));
}
async function register(target, username) {
  await target.goto(base);
  await target.getByRole('button', { name: 'Create account', exact: true }).click();
  await target.getByLabel('Username', { exact: true }).fill(username);
  await target.getByLabel('Password', { exact: true }).fill(password);
  const [response] = await Promise.all([target.waitForResponse(item => new URL(item.url()).pathname === '/api/v1/auth/register' && item.request().method() === 'POST'), target.getByRole('button', { name: 'Create account', exact: true }).click()]);
  assert.equal(response.status(), 201);
  accounts.push(username);
  await target.getByRole('heading', { name: 'Projects', exact: true }).waitFor();
  await progress(`Registered ${username}`);
}
async function signIn() {
  await page.getByLabel('Username', { exact: true }).fill(usernames[0]);
  await page.getByLabel('Password', { exact: true }).fill(password);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await page.getByRole('heading', { name: 'Projects', exact: true }).waitFor();
}
async function createProject(target, name) {
  await target.getByRole('button', { name: /New project/ }).click();
  await target.getByLabel('Project name', { exact: true }).fill(name);
  const [response] = await Promise.all([target.waitForResponse(item => new URL(item.url()).pathname === '/api/v1/projects' && item.request().method() === 'POST'), target.getByRole('button', { name: 'Create project', exact: true }).click()]);
  assert.equal(response.status(), 201);
  const project = (await response.json()).project;
  projects.push({ id: project.id, owner: target === page ? 'owner' : 'peer' });
  await target.getByRole('heading', { name: 'TUM-VI room1', exact: true }).waitFor();
  assert.equal(project.dataset_id, 'tumvi-room1-512-16-v1');
  await progress(`Created ${target === page ? 'owner' : 'peer'} project ${project.id}`);
  return project;
}
async function createExperiment(target, project, name) {
  await target.getByRole('link', { name: 'Experiments', exact: true }).click();
  await target.getByRole('button', { name: 'New experiment', exact: true }).click();
  await target.getByLabel('Experiment name', { exact: true }).fill(name);
  await target.getByLabel('Description', { exact: false }).fill('Full calibrated stereo/IMU recording; exact configuration and independently evaluated evidence.');
  const [response] = await Promise.all([target.waitForResponse(item => new URL(item.url()).pathname === `/api/v1/projects/${project.id}/experiments` && item.request().method() === 'POST'), target.getByRole('button', { name: 'Create experiment', exact: true }).click()]);
  assert.equal(response.status(), 201);
  const value = await saveResponse(`experiment-${target === page ? 'owner' : 'peer'}-v1.response.json`, response);
  experiments.push({ id: value.experiment.id, project_id: project.id, owner: target === page ? 'owner' : 'peer' });
  await target.getByRole('button', { name: 'Run version 1', exact: true }).waitFor();
  return value;
}
const jobPath = job => `/projects/${job.project_id}/jobs/${job.id}`;
async function submit(target, project, experimentId, version, expectedStatus = 201) {
  await target.goto(`${base}#project/${project.id}/experiments/${experimentId}/revisions/${version}`);
  const button = target.getByRole('button', { name: `Run version ${version}`, exact: true });
  await button.waitFor();
  const [response] = await Promise.all([target.waitForResponse(item => new URL(item.url()).pathname === `/api/v1/projects/${project.id}/jobs` && item.request().method() === 'POST'), button.click()]);
  assert.equal(response.status(), expectedStatus);
  const payload = await response.json();
  await writeFile(`${output}${payload.job ? `submission-${payload.job.id}` : 'queue-refused'}.response.json`, await response.body());
  if (payload.job) {
    jobs.push({ id: payload.job.id, project_id: project.id, owner: target === page ? 'owner' : 'peer', revision_version: version });
    await target.locator('.job-detail').waitFor();
    await progress(`Submitted ${target === page ? 'owner' : 'peer'} v${version}: ${payload.job.id}`);
  }
  return { status: response.status(), payload };
}
async function openJob(target, job) {
  await target.goto(`${base}#project/${job.project_id}/results/${job.id}`);
  await target.locator('.job-detail').waitFor();
}
async function waitJob(target, job, desired = 'succeeded') {
  const deadline = Date.now() + timeout;
  let previous;
  while (Date.now() < deadline) {
    const response = await api(target, jobPath(job));
    assert.equal(response.status, 200);
    const value = response.payload.job;
    const state = `${value.state}/${value.phase}`;
    if (state !== previous) {
      transitions.push({ job_id: job.id, state: value.state, phase: value.phase, observed_at: new Date().toISOString() });
      await progress(`${job.id}: ${state}`);
      previous = state;
    }
    if (value.state === desired) {
      if (['succeeded', 'failed', 'cancelled'].includes(desired)) {
        completedJobs[job.id] = value;
        await writeFile(`${output}job-${job.id}.response.json`, response.raw);
      }
      return value;
    }
    assert.ok(!['succeeded', 'failed', 'cancelled'].includes(value.state), `Unexpected terminal state for ${job.id}: ${JSON.stringify(value.failure || value.state)}`);
    await target.waitForTimeout(2000);
  }
  throw new Error(`Job ${job.id} did not reach ${desired} within ${timeout} ms.`);
}
async function waitImages(frame) {
  await page.waitForFunction(frame => ['cam0', 'cam1'].every(camera => {
    const image = document.querySelector(`#image-${camera}`);
    return image && !image.hidden && image.complete && image.naturalWidth === 512 && image.alt.includes(`frame ${frame},`);
  }), frame);
}
async function noOverflow(target = page) {
  return target.evaluate(() => document.documentElement.scrollWidth <= innerWidth);
}
async function layouts(prefix, { expand } = {}) {
  await page.setViewportSize({ width: 1440, height: 1200 });
  await page.getByLabel('Appearance', { exact: true }).selectOption('light');
  let fits = await noOverflow();
  await page.screenshot({ path: `${output}${prefix}-desktop-light.png`, fullPage: true });
  await page.screenshot({ path: `${output}${prefix}-viewport-light.png` });
  await page.getByLabel('Appearance', { exact: true }).selectOption('dark');
  await page.screenshot({ path: `${output}${prefix}-desktop-dark.png`, fullPage: true });
  fits &&= await noOverflow();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByLabel('Appearance', { exact: true }).selectOption('light');
  if (expand) await expand();
  fits &&= await noOverflow();
  await page.screenshot({ path: `${output}${prefix}-mobile-light.png`, fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1200 });
  return fits;
}
async function downloadArchive(job, name, keyboard = false) {
  const path = `/api/v1${jobPath(job)}/export`;
  const button = page.getByRole('button', { name: 'Download experiment', exact: true });
  let started;
  const observe = request => { if (new URL(request.url()).pathname === path) started ??= performance.now(); };
  page.on('request', observe);
  let response;
  let download;
  let cdp;
  let elapsed;
  try {
    if (keyboard) await button.focus();
    [download, response] = await Promise.all([
      page.waitForEvent('download'),
      page.waitForResponse(item => new URL(item.url()).pathname === path && item.request().resourceType() === 'fetch'),
      keyboard ? page.keyboard.press('Enter') : button.click(),
    ]);
    cdp = await response.body();
    elapsed = performance.now() - started;
  } finally { page.off('request', observe); }
  assert.equal(response.status(), 200);
  assert.equal(download.suggestedFilename(), `meridian-run-${job.id}-v1.zip`);
  await download.saveAs(`${output}${name}`);
  assert.equal(await download.failure(), null);
  const saved = await readFile(`${output}${name}`);
  const headers = response.headers();
  assert.equal(hash(saved), headers['x-content-sha256']);
  assert.equal(saved.length, Number(headers['content-length']));
  assert.match(headers['cache-control'], /no-store/);
  assert.match(headers['content-type'], /^application\/zip/);
  if (cdp.length) assert.ok(saved.equals(cdp), 'An available CDP body equals the saved attachment.');
  const direct = await context.request.get(response.url(), { headers: { Accept: 'application/zip', 'Cache-Control': 'no-cache' } });
  assert.equal(direct.status(), 200);
  const directBody = await direct.body();
  assert.ok(saved.equals(directBody), 'An independent authenticated HTTP fetch equals the browser download.');
  assert.equal(hash(directBody), direct.headers()['x-content-sha256']);
  await page.getByText(/Archive verified; download requested/).waitFor();
  const evidence = {
    file: name, job_id: job.id, bytes: saved.length, sha256: hash(saved), cdp_body_bytes: cdp.length,
    independent_http_body_bytes: directBody.length, independent_http_sha256: hash(directBody),
    elapsed_ms: elapsed,
    timing_scope: 'Node monotonic time from browser request event to download event and CDP body read attempt, including network, browser SHA verification and automation; excludes the separate HTTP verification request.',
    headers: Object.fromEntries(['content-type', 'content-disposition', 'content-length', 'x-content-sha256', 'cache-control'].map(key => [key, headers[key]])),
  };
  downloads.push(evidence);
  await writeFile(`${output}${name}.http-evidence.json`, JSON.stringify(evidence, null, 2) + '\n');
  await progress(`Verified download ${name}`);
  return evidence;
}
try {
  await register(page, usernames[0]);
  await register(peer, usernames[1]);
  check('signup', accounts.length === 2);
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  await page.getByRole('heading', { name: 'Sign in', exact: true }).waitFor();
  check('signout', (await api(page, '/projects')).status === 401);
  await signIn();
  check('signin', (await api(page, '/projects')).status === 200);
  check('execution', (await api(page, '/session')).payload.capabilities.execution === true);
  const project = await createProject(page, 'Indoor VIO acceptance');
  await page.getByText('Calibration, coordinates and timestamps', { exact: true }).click();
  await page.getByText('Source, license and reproducibility', { exact: true }).click();
  check('provenance', await page.getByText('Calibration, coordinates and timestamps', { exact: true }).isVisible() && await page.getByText('Source, license and reproducibility', { exact: true }).isVisible());
  const ownExperiment = await createExperiment(page, project, 'Room1 feature budget');
  const peerProject = await createProject(peer, 'Independent account VIO');
  const peerExperiment = await createExperiment(peer, peerProject, 'Second account baseline');
  check('projects', project.id !== peerProject.id && (await api(page, '/projects')).payload.projects.length === 1 && (await api(peer, '/projects')).payload.projects.length === 1);
  check('baseline', ownExperiment.revision.specification.parameters.num_pts === 200 && ownExperiment.revision.version === 1);
  const baseline = (await submit(page, project, ownExperiment.experiment.id, 1)).payload.job;
  check('baseline_identity', baseline.revision_version === 1 && baseline.specification_sha256 === ownExperiment.revision.specification_sha256 && baseline.parameters.num_pts === 200);
  await waitJob(page, baseline, 'running');
  check('running', true);
  const peerRun = (await submit(peer, peerProject, peerExperiment.experiment.id, 1)).payload.job;
  const peerQueuedResponse = await api(peer, jobPath(peerRun));
  await writeFile(`${output}peer-queued.response.json`, peerQueuedResponse.raw);
  const peerQueued = peerQueuedResponse.payload.job;
  check('peer_queue', peerQueued.state === 'queued' && peerQueued.started_at === null);
  await peer.screenshot({ path: `${output}peer-queued.png`, fullPage: true });
  const spare = (await submit(page, project, ownExperiment.experiment.id, 1)).payload.job;
  const spareQueuedResponse = await api(page, jobPath(spare));
  await writeFile(`${output}owner-queued.response.json`, spareQueuedResponse.raw);
  const spareQueued = spareQueuedResponse.payload.job;
  check('owner_queue', spareQueued.state === 'queued' && spareQueued.started_at === null);
  await page.screenshot({ path: `${output}owner-queued.png`, fullPage: true });
  quotaEvidence = await submit(page, project, ownExperiment.experiment.id, 1, 409);
  check('queue_quota', quotaEvidence.payload.error.code === 'queue_full' && (await api(page, `/projects/${project.id}/jobs`)).payload.jobs.length === 2);
  await page.getByText('The active job limit has been reached. Wait for completion or cancel queued work.', { exact: true }).waitFor();
  check('quota_feedback', await page.getByRole('button', { name: 'Run version 1', exact: true }).isEnabled());
  check('limits', /2 active.*1 running at a time/.test(await page.locator('.job-limits').innerText()));
  await openJob(page, spare);
  await page.getByRole('button', { name: 'Cancel run', exact: true }).click();
  await page.getByRole('button', { name: 'Confirm cancellation', exact: true }).click();
  const cancelled = await waitJob(page, spare, 'cancelled');
  check('cancellation', cancelled.attempt_count === 0 && cancelled.started_at === null && cancelled.result === null);
  await openJob(page, baseline);
  await page.reload();
  await page.locator('.job-detail').waitFor();
  check('refresh', new URL(page.url()).hash.endsWith(baseline.id));
  const baselineDone = await waitJob(page, baseline);
  check('baseline_result', baselineDone.result.qualification.passed && baselineDone.investigation_available && baselineDone.export_available && Number.isFinite(baselineDone.result.evaluation.metrics.ate_rmse_m));
  await page.locator('.job-detail .job-status').filter({ hasText: /^Succeeded$/ }).waitFor();
  check('coverage', await page.getByText('Estimate coverage', { exact: true }).isVisible() && await page.getByText('Evaluated coverage', { exact: true }).isVisible() && await page.getByText('Reference coverage', { exact: true }).isVisible());
  const investigationPath = `${jobPath(baseline)}/investigation`;
  const artifactResponse = page.waitForResponse(item => new URL(item.url()).pathname === `/api/v1${investigationPath}` && item.ok());
  const metricsResponse = page.waitForResponse(item => new URL(item.url()).pathname.endsWith('/investigation/metrics') && item.ok());
  await page.getByRole('link', { name: 'Investigate run', exact: true }).click();
  investigation = (await saveResponse(`investigation-${baseline.id}.response.json`, await artifactResponse)).investigation;
  const full = (await saveResponse('interval-full.response.json', await metricsResponse)).interval;
  await waitImages(0);
  check('investigation', Math.abs(full.metrics.ate_rmse_m - baselineDone.result.evaluation.metrics.ate_rmse_m) < 1e-12 && full.counts.expected_camera_samples === investigation.frames.length);
  check('missing', await page.locator('#frame-status').innerText() === 'No estimate · initialization' && await page.locator('#cursor-position').innerText() === 'Unavailable');
  const intensities = await page.locator('#image-cam0, #image-cam1').evaluateAll(images => images.map(image => {
    const canvas = document.createElement('canvas'); canvas.width = image.naturalWidth; canvas.height = image.naturalHeight;
    const context = canvas.getContext('2d'); context.drawImage(image, 0, 0);
    const values = context.getImageData(0, 0, canvas.width, canvas.height).data;
    let min = 255; let max = 0;
    for (let index = 0; index < values.length; index += 4) { min = Math.min(min, values[index]); max = Math.max(max, values[index]); }
    return { width: image.naturalWidth, height: image.naturalHeight, min, max };
  }));
  check('stereo', intensities.length === 2 && intensities.every(value => value.width === 512 && value.max - value.min > 20));
  await writeFile(`${output}stereo-browser-decode.json`, JSON.stringify(intensities, null, 2) + '\n');
  await page.locator('#frame-cursor').focus();
  await page.keyboard.press('ArrowRight');
  await waitImages(1);
  check('cursor', await page.locator('#cursor-timestamp').innerText() === investigation.frames[1].timestamp_ns);
  await page.getByLabel('Interval start · s', { exact: true }).fill('10');
  await page.getByLabel('Interval end · s', { exact: true }).fill('20');
  const intervalResponse = page.waitForResponse(item => new URL(item.url()).pathname.endsWith('/investigation/metrics') && item.ok());
  await page.getByRole('button', { name: 'Apply interval', exact: true }).click();
  const selected = (await saveResponse('interval-selected.response.json', await intervalResponse)).interval;
  await waitImages(selected.interval.first_camera_index);
  check('interval', selected.alignment_scope === 'full_recording_global_se3_unchanged' && selected.counts.expected_camera_samples > 100 && selected.counts.expected_camera_samples < investigation.frames.length);
  check('imu', /Δt .* ms.*nearest, not interpolated/.test(await page.locator('#cursor-imu').innerText()));
  check('plots', await page.locator('.investigation-projections svg').count() === 3 && await page.locator('.investigation-signals svg').count() === 8 && await page.locator('path.plot-line').count() > 10);
  check('diagnostics', (await page.locator('.diagnostic-list').innerText()).includes('Unavailable') && !(await page.locator('.diagnostic-list').innerText()).includes('Healthy'));
  const selectedMetricsText = await page.locator('#interval-metrics').innerText();
  await page.getByLabel('Interval start · s', { exact: true }).fill('30');
  await page.getByRole('button', { name: 'Apply interval', exact: true }).click();
  check('interval_error', Boolean(await page.locator('#interval-error').innerText()) && await page.locator('#interval-metrics').innerText() === selectedMetricsText);
  await page.getByLabel('Interval start · s', { exact: true }).fill('10');
  check('investigation_layout', await layouts('investigation'));
  await page.getByRole('link', { name: 'Experiments', exact: true }).click();
  await page.getByRole('link', { name: /Room1 feature budget/ }).click();
  await page.getByRole('button', { name: 'Edit configuration', exact: true }).click();
  const profile = (await api(page, '/configuration-profile')).payload.profile;
  await page.getByLabel(profile.parameters.num_pts.label, { exact: true }).selectOption('100');
  const revisionResponse = page.waitForResponse(item => new URL(item.url()).pathname === `/api/v1/projects/${project.id}/experiments/${ownExperiment.experiment.id}` && item.request().method() === 'PATCH');
  await page.getByRole('button', { name: 'Save new version', exact: true }).click();
  const revision = await saveResponse('experiment-owner-v2.response.json', await revisionResponse);
  await page.getByRole('button', { name: 'Run version 2', exact: true }).waitFor();
  const historical = await api(page, `/projects/${project.id}/experiments/${ownExperiment.experiment.id}/revisions/1`);
  check('revision', revision.revision.version === 2 && revision.revision.specification.parameters.num_pts === 100 && revision.revision.specification_sha256 !== ownExperiment.revision.specification_sha256 && historical.payload.revision.specification_sha256 === ownExperiment.revision.specification_sha256);
  const candidate = (await submit(page, project, ownExperiment.experiment.id, 2)).payload.job;
  const candidateDone = await waitJob(page, candidate);
  check('variant_result', candidateDone.revision_version === 2 && candidateDone.parameters.num_pts === 100 && candidateDone.specification_sha256 === revision.revision.specification_sha256 && candidateDone.investigation_available && candidateDone.export_available);
  const peerDone = await waitJob(peer, peerRun);
  check('peer_result', peerDone.parameters.num_pts === 200 && peerDone.project_id === peerProject.id && peerDone.id !== baselineDone.id && peerDone.result.qualification.passed);
  const orderedRuns = [baselineDone, peerDone, candidateDone].sort((left, right) => Date.parse(left.started_at) - Date.parse(right.started_at));
  check('serialization', orderedRuns.every((value, index) => index === 0 || Date.parse(orderedRuns[index - 1].finished_at) <= Date.parse(value.started_at)));
  await page.getByRole('link', { name: 'Results', exact: true }).click();
  await page.getByRole('heading', { name: 'Saved comparisons', exact: true }).waitFor();
  await page.getByRole('button', { name: 'New comparison', exact: true }).click();
  await page.getByLabel('Comparison name', { exact: true }).fill('Feature budget · 200 → 100');
  await page.getByLabel('Description', { exact: false }).fill('Exact successful runs from the complete hosted workflow; full and common evaluated support.');
  await page.getByRole('combobox', { name: 'Baseline run', exact: true }).selectOption(baseline.id);
  await page.getByRole('combobox', { name: 'Candidate run', exact: true }).selectOption(baseline.id);
  await page.getByRole('button', { name: 'Save comparison', exact: true }).click();
  await page.getByText(/Choose a different exact run/).waitFor();
  check('comparison_validation', await page.getByLabel('Comparison name', { exact: true }).inputValue() === 'Feature budget · 200 → 100');
  await page.getByRole('combobox', { name: 'Candidate run', exact: true }).selectOption(candidate.id);
  const comparisonResponse = page.waitForResponse(item => new URL(item.url()).pathname === `/api/v1/projects/${project.id}/comparisons` && item.request().method() === 'POST');
  const comparisonStart = performance.now();
  await page.getByRole('button', { name: 'Save comparison', exact: true }).click();
  savedComparison = (await saveResponse('comparison.response.json', await comparisonResponse)).comparison;
  timings.push({ action: 'save comparison', elapsed_ms: performance.now() - comparisonStart, scope: 'Node monotonic time before UI click to complete HTTP response body, including automation and network.' });
  comparisons.push(savedComparison.id);
  await page.getByRole('heading', { name: savedComparison.name, exact: true }).waitFor();
  check('comparison_identity', savedComparison.baseline_job_id === baseline.id && savedComparison.candidate_job_id === candidate.id && savedComparison.analysis.configuration_changes.some(change => change.parameter === 'num_pts' && change.baseline === 200 && change.candidate === 100));
  const common = savedComparison.analysis.common_support;
  const table = page.locator('table').filter({ has: page.getByText('Full-resolution errors on the common evaluated support', { exact: true }) });
  check('common_support', common.status === 'ok' && common.evaluated_camera_samples > 0 && (await table.innerText()).includes(common.baseline_metrics.ate_rmse_m.toFixed(5)) && (await table.innerText()).includes(common.candidate_metrics.ate_rmse_m.toFixed(5)));
  check('interpretation', await page.getByRole('heading', { name: 'Where the runs differ', exact: true }).isVisible() && await page.getByRole('heading', { name: 'Host observations', exact: true }).isVisible() && await page.getByRole('heading', { name: 'Outcome and coverage', exact: true }).isVisible() && !/winner|best configuration/i.test(await page.locator('.run-comparison').innerText()));
  await page.reload();
  await page.getByRole('heading', { name: savedComparison.name, exact: true }).waitFor();
  check('comparison_reload', (await api(page, `/projects/${project.id}/comparisons/${savedComparison.id}`)).payload.comparison.analysis_sha256 === savedComparison.analysis_sha256);
  check('comparison_layout', await layouts('comparison', { expand: () => page.getByText('Exact evaluated support regions', { exact: true }).click() }));
  const support = page.getByLabel('evaluated support regions', { exact: true });
  await support.focus();
  check('table_keyboard', await support.evaluate(element => document.activeElement === element));
  await page.locator('.comparison-run-card').nth(1).getByRole('link', { name: 'Run overview', exact: true }).click();
  await page.getByRole('button', { name: 'Download experiment', exact: true }).waitFor();
  const exportPattern = `**/api/v1${jobPath(candidate)}/export?schema_version=1`;
  const interrupt = route => route.abort('failed');
  await page.route(exportPattern, interrupt);
  await page.getByRole('button', { name: 'Download experiment', exact: true }).click();
  await page.locator('#export-status.error').waitFor();
  check('export_retry', await page.getByRole('button', { name: 'Download experiment', exact: true }).isEnabled());
  await page.unroute(exportPattern, interrupt);
  const exported = await downloadArchive(candidate, 'experiment.zip', true);
  check('export', exported.bytes > 0 && exported.sha256.length === 64);
  const repeated = await downloadArchive(candidate, 'experiment-repeat.zip');
  check('export_repeat', repeated.sha256 === exported.sha256 && repeated.bytes === exported.bytes);
  check('export_layout', await layouts('export'));
  const denied = [];
  for (const path of [
    `/projects/${project.id}`,
    `/projects/${project.id}/experiments/${ownExperiment.experiment.id}`,
    `/projects/${project.id}/jobs`,
    jobPath(candidate),
    `/projects/${project.id}/comparisons/${savedComparison.id}`,
    investigationPath,
    `${investigationPath}/metrics?start_ns=${investigation.frames[0].timestamp_ns}&end_ns=${investigation.frames.at(-1).timestamp_ns}`,
    `${investigationPath}/images/cam0/0`,
    `${jobPath(candidate)}/export?schema_version=1`,
  ]) denied.push({ path, status: (await api(peer, path)).status });
  await writeFile(`${output}ownership-denials.json`, JSON.stringify(denied, null, 2) + '\n');
  check('ownership', denied.every(value => value.status === 404));
  check('owner_isolation', (await api(page, `/projects/${peerProject.id}`)).status === 404 && (await api(page, jobPath(peerRun))).status === 404);
  await peer.goto(`${base}#project/${project.id}/results/${candidate.id}`);
  await peer.getByRole('heading', { name: 'Run unavailable', exact: true }).waitFor();
  check('private_route', await peer.locator('.job-detail').count() === 0);
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  await page.getByRole('heading', { name: 'Sign in', exact: true }).waitFor();
  check('logout_private', await page.locator('.job-detail, .run-comparison, .investigation-chart').count() === 0 && (await api(page, `${jobPath(candidate)}/export?schema_version=1`)).status === 401);
  await signIn();
  await page.goto(`${base}#project/${project.id}/results/comparisons/${savedComparison.id}`);
  await page.getByRole('heading', { name: savedComparison.name, exact: true }).waitFor();
  check('persisted_journey', (await api(page, `/projects/${project.id}/comparisons/${savedComparison.id}`)).payload.comparison.analysis_sha256 === savedComparison.analysis_sha256 && (await api(page, jobPath(baseline))).status === 200 && (await api(page, jobPath(candidate))).status === 200);
  const stored = await page.evaluate(() => JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage)]));
  check('storage', ![project.id, baseline.id, candidate.id, savedComparison.id, 'Indoor VIO acceptance'].some(value => stored.includes(value)));
  check('browser_errors', errors.length === 0 && cspErrors.length === 0);
  assert.equal(checks.length, Object.keys(checklist).length, 'Every predeclared check completed.');
  await progress('Complete journey passed; all source data retained for independent audit and backup qualification.');
} catch (error) {
  failure = String(error.stack || error).replaceAll(password, '[redacted]');
  process.exitCode = 1;
  await page.screenshot({ path: `${output}failure.png`, fullPage: true }).catch(() => {});
} finally {
  await writeFile(`${output}report.json`, JSON.stringify({
    schema_version: 1, started_at: startedAt, checked_at: new Date().toISOString(), passed: !failure, base,
    script_sha256: scriptHash,
    environment: { description: process.env.MERIDIAN_QA_ENVIRONMENT || 'Operator did not specify physical client/server separation.', platform: platform(), release: release(), architecture: arch(), node: process.version, browser: browser.version(), headless: true, transport: address.protocol.replace(':', ''), certificate_validation: 'Default browser validation; no ignoreHTTPSErrors override.' },
    created_accounts: accounts, projects, experiments, jobs, comparison_ids: comparisons,
    checks, expected_check_count: Object.keys(checklist).length, transitions,
    completed_jobs: completedJobs, saved_comparison: savedComparison, quota_evidence: quotaEvidence,
    downloads, timings, page_errors: errors, csp_errors: cspErrors, ...(failure ? { failure } : {}),
    limitations: [
      'One scripted engineering journey and two temporary accounts do not establish ease of use for independent engineers or general multiuser load capacity.',
      'The source dataset is the qualified room1 profile. Reserved room2 evaluation is separate and does not silently enter this hosted catalog.',
      'Execution serialization is checked from the three retained job intervals; independent operator observations establish container behavior and infrastructure isolation.',
      'One real queued request is cancelled and one active-quota submission is refused. Daily submission-budget exhaustion and infrastructure failures are separate checks.',
      'A connection interruption is injected for download retry; no scientific payload or successful business response is fabricated.',
      'CDP may expose an empty attachment body; saved bytes are verified against real HTTP headers and a separate authenticated HTTP response.',
      'Observed timings include the stated network and automation scope and are not a latency or throughput guarantee.',
      'Keyboard checks cover selected controls, and viewport checks cover page overflow; this is not a formal accessibility audit.',
      'Actual server logout is tested; no elapsed server-session expiry is claimed.',
      'The downloaded archive and all source records are retained for separate independent reproduction, backup/restore qualification and exact operator cleanup.',
    ],
  }, null, 2) + '\n');
  await progress(failure ? 'FAILED' : 'PASSED');
  console.log(JSON.stringify({ passed: !failure, checks: checks.length, expected_checks: Object.keys(checklist).length, accounts, projects, jobs, comparisons, output, ...(failure ? { failure } : {}) }, null, 2));
  await browser.close();
}
