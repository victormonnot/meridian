/**
 * Qualify a real private experiment download on an authorized deployment.
 * Creates two temporary accounts, one project, a nondefault 100-point experiment,
 * one actual VIO run and one cancelled request. Saves the exact HTTP evidence and
 * downloaded ZIP before deleting the successful source through the UI. The ZIP
 * remains available for independent reproduction after hosted access is revoked.
 * No run state, scientific result or archive body is fabricated. One connection
 * is intentionally interrupted, and two requests are held during navigation or
 * logout. Account credentials stay in memory; exact cleanup identities are saved.
 */
import assert from 'node:assert/strict';
import { createHash, randomBytes, randomUUID } from 'node:crypto';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { resolve, sep } from 'node:path';
import { pathToFileURL } from 'node:url';

const [baseArgument, outputArgument, ...extra] = process.argv.slice(2);
if (!baseArgument || !outputArgument || extra.length) throw new Error('Usage: node web/scripts/check-workspace-exports-browser.mjs WORKSPACE_URL OUTPUT_DIRECTORY');
const address = new URL(baseArgument);
if (!['http:', 'https:'].includes(address.protocol) || address.username || address.password || address.search || address.hash) throw new Error('Use an explicit HTTP(S) URL without credentials, query or fragment.');
const base = address.href;
const output = resolve(outputArgument) + sep;
const moduleUrl = process.env.MERIDIAN_PLAYWRIGHT_MODULE ? pathToFileURL(resolve(process.env.MERIDIAN_PLAYWRIGHT_MODULE)).href : new URL('../../build/browser-tools/node_modules/playwright/index.mjs', import.meta.url).href;
const { chromium } = await import(moduleUrl);
const timeout = Number(process.env.MERIDIAN_JOB_QA_TIMEOUT_MS || 1_200_000);
if (!Number.isSafeInteger(timeout) || timeout < 30_000 || timeout > 3_600_000) throw new Error('Invalid MERIDIAN_JOB_QA_TIMEOUT_MS.');
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true, ...(process.env.MERIDIAN_BROWSER_EXECUTABLE ? { executablePath: process.env.MERIDIAN_BROWSER_EXECUTABLE } : {}) });
const context = await browser.newContext({ viewport: { width: 1440, height: 1200 }, acceptDownloads: true });
const other = await browser.newContext();
const page = await context.newPage();
const peer = await other.newPage();
const stamp = `${Date.now().toString(36)}-${randomBytes(3).toString('hex')}`;
const usernames = [`qa-export-${stamp}`, `qa-export-peer-${stamp}`];
const password = `Qa!${randomBytes(24).toString('base64url')}`;
const accounts = [];
const jobs = [];
const deletedJobs = [];
const completedJobs = {};
const checks = [];
const errors = [];
const cspErrors = [];
const downloadEvents = [];
const downloads = [];
let projectId;
let experimentId;
let failure;
const check = (label, condition) => { assert.ok(condition, label); checks.push(label); };
const hash = value => createHash('sha256').update(value).digest('hex');
const jobsPath = () => `/projects/${projectId}/jobs`;
const exportPath = id => `${jobsPath()}/${id}/export`;
const exportUrl = id => `/api/v1${exportPath(id)}?schema_version=1`;
async function progress(state) {
  await writeFile(`${output}progress.json`, JSON.stringify({ state, checked_at: new Date().toISOString(), accounts, project_id: projectId, experiment_id: experimentId, jobs, deleted_job_ids: deletedJobs, checks: checks.length }, null, 2) + '\n');
  console.log(state);
}
for (const target of [page, peer]) {
  target.on('pageerror', error => errors.push(error.message));
  target.on('console', message => { if (message.type() === 'error' && /content security policy|violat.*(?:directive|policy)|refused to (?:load|execute|apply|connect)/i.test(message.text())) cspErrors.push(message.text()); });
}
page.on('download', download => downloadEvents.push(download.suggestedFilename()));
async function api(target, path, method = 'GET', body) {
  return target.evaluate(async ({ path, method, body }) => {
    const session = await (await fetch('/api/v1/session', { cache: 'no-store' })).json();
    const response = await fetch(`/api/v1${path}`, { method, cache: 'no-store', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': session.csrf_token }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    const raw = await response.text();
    return { status: response.status, payload: JSON.parse(raw), raw };
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
  await progress(`Registered ${username}`);
}
async function login() {
  await page.getByLabel('Username', { exact: true }).fill(usernames[0]);
  await page.getByLabel('Password', { exact: true }).fill(password);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await page.getByRole('heading', { name: 'Projects', exact: true }).waitFor();
}
async function submitJob() {
  const response = await api(page, jobsPath(), 'POST', { schema_version: 1, experiment_id: experimentId, revision_version: 1, idempotency_key: randomUUID() });
  assert.equal(response.status, 201);
  jobs.push(response.payload.job.id);
  await progress(`Submitted ${response.payload.job.id}`);
  return response.payload.job;
}
async function openJob(id) {
  await page.goto(`${base}#project/${projectId}/results/${id}`);
  await page.locator('.job-detail').waitFor();
}
async function waitJob(id, expected = 'succeeded') {
  await openJob(id);
  let lastState;
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const response = await api(page, `${jobsPath()}/${id}`);
    assert.equal(response.status, 200);
    const job = response.payload.job;
    const state = `${job.state}/${job.phase}`;
    if (state !== lastState) { await progress(`${id}: ${state}`); lastState = state; }
    if (['succeeded', 'failed', 'cancelled'].includes(job.state)) {
      assert.equal(job.state, expected, `Run ${id}: ${JSON.stringify(job.failure)}`);
      await page.locator('.job-detail .job-status').filter({ hasText: /^(Succeeded|Failed|Cancelled)$/ }).waitFor();
      completedJobs[id] = job;
      await writeFile(`${output}job-${id}.response.json`, response.raw);
      return job;
    }
    await page.waitForTimeout(2500);
  }
  throw new Error(`Run ${id} did not reach a terminal state within the configured timeout.`);
}
async function deniedExport(target, id, suffix = '?schema_version=1') {
  return target.evaluate(async url => {
    const response = await fetch(url, { cache: 'no-store' });
    return { status: response.status, content_type: response.headers.get('Content-Type'), payload: await response.json() };
  }, `/api/v1${exportPath(id)}${suffix}`);
}
async function downloadArchive(id, name, keyboard = false) {
  const button = page.getByRole('button', { name: 'Download experiment', exact: true });
  let started;
  const observe = request => { if (new URL(request.url()).pathname === `/api/v1${exportPath(id)}`) started ??= performance.now(); };
  page.on('request', observe);
  let response;
  let download;
  let body;
  let elapsed;
  try {
    if (keyboard) await button.focus();
    [download, response] = await Promise.all([
      page.waitForEvent('download'),
      page.waitForResponse(item => new URL(item.url()).pathname === `/api/v1${exportPath(id)}` && item.request().resourceType() === 'fetch'),
      keyboard ? page.keyboard.press('Enter') : button.click(),
    ]);
    body = await response.body();
    elapsed = performance.now() - started;
  } finally { page.off('request', observe); }
  assert.equal(response.status(), 200);
  assert.equal(download.suggestedFilename(), `meridian-run-${id}-v1.zip`);
  await download.saveAs(`${output}${name}`);
  assert.equal(await download.failure(), null);
  const saved = await readFile(`${output}${name}`);
  const headers = response.headers();
  await writeFile(`${output}${name}.http-body`, body);
  await writeFile(`${output}${name}.http-evidence.json`, JSON.stringify({ url: response.url(), request_resource_type: response.request().resourceType(), headers, response_body_bytes: body.length, response_body_sha256: hash(body), downloaded_bytes: saved.length, downloaded_sha256: hash(saved) }, null, 2) + '\n');
  assert.equal(hash(saved), headers['x-content-sha256']);
  assert.equal(saved.length, Number(headers['content-length']));
  assert.match(headers['cache-control'], /no-store/);
  // Some Chrome/CDP versions expose an empty response.body() for attachments.
  // Preserve that observation; compare the saved bytes with a separate real
  // authenticated HTTP response, in addition to the browser response headers.
  if (body.length) assert.ok(saved.equals(body), 'An available CDP body equals the saved attachment');
  const direct = await context.request.get(response.url(), { headers: { Accept: 'application/zip', 'Cache-Control': 'no-cache' } });
  assert.equal(direct.status(), 200);
  const directBody = await direct.body();
  await writeFile(`${output}${name}.direct-http-body`, directBody);
  assert.ok(saved.equals(directBody), 'A separate authenticated HTTP response equals the saved attachment');
  assert.equal(hash(directBody), direct.headers()['x-content-sha256']);
  await page.getByText(/Archive verified; download requested/).waitFor();
  const evidence = { file: name, suggested_filename: download.suggestedFilename(), bytes: saved.length, sha256: hash(saved), cdp_body_bytes: body.length, cdp_body_available: body.length > 0, independent_http_body_bytes: directBody.length, independent_http_sha256: hash(directBody), elapsed_ms: elapsed, timing_scope: 'Node monotonic time from browser request event to the browser download event and CDP body read attempt; includes network, browser SHA-256 verification and automation overhead, and excludes the separate HTTP verification request.', response_headers: Object.fromEntries(['content-type', 'content-disposition', 'content-length', 'x-content-sha256', 'cache-control'].map(key => [key, headers[key]])) };
  downloads.push(evidence);
  await progress(`Downloaded and verified ${name}`);
  return evidence;
}
async function delayUntilAction(id, action) {
  let signalRequest;
  let release;
  const observed = new Promise(resolve => { signalRequest = resolve; });
  const gate = new Promise(resolve => { release = resolve; });
  const pattern = `**${exportUrl(id)}`;
  const delay = async route => { signalRequest(); await gate; try { await route.continue(); } catch { /* Navigation can abort the held request. */ } };
  const before = downloadEvents.length;
  await page.route(pattern, delay);
  try {
    await page.getByRole('button', { name: 'Download experiment', exact: true }).click();
    await observed;
    await action();
  } finally {
    release();
    await page.unroute(pattern, delay);
  }
  await page.waitForTimeout(500);
  assert.equal(downloadEvents.length, before, 'A cancelled private response must not create a browser download');
}
try {
  await register(page, usernames[0]);
  check('deployment enables actual VIO execution', (await api(page, '/session')).payload.capabilities.execution === true);
  const project = await api(page, '/projects', 'POST', { name: 'Portable VIO experiment', description: 'Independent reproduction of an exact retained nondefault execution.', dataset_id: 'tumvi-room1-512-16-v1' });
  assert.equal(project.status, 201);
  projectId = project.payload.project.id;
  const experiment = await api(page, `/projects/${projectId}/experiments`, 'POST', { schema_version: 1, name: 'Room1 · 100 visual features', description: 'An exact nondefault specification for independent reproduction.', dataset_id: 'tumvi-room1-512-16-v1', parameters: { num_pts: 100 } });
  assert.equal(experiment.status, 201);
  experimentId = experiment.payload.experiment.id;
  await writeFile(`${output}experiment.response.json`, experiment.raw);
  const run = await submitJob();
  await openJob(run.id);
  check('active run exposes a reason without offering an archive', await page.getByRole('button', { name: 'Download experiment', exact: true }).count() === 0 && await page.locator('.export-unavailable').isVisible());
  check('active run export is rejected by the API', (await deniedExport(page, run.id)).status === 409);
  const cancelled = await submitJob();
  assert.equal((await api(page, `${jobsPath()}/${cancelled.id}/cancel`, 'POST', { schema_version: 1 })).status, 200);
  const cancelledDone = await waitJob(cancelled.id, 'cancelled');
  check('cancelled request has an explicit unavailable state and no archive button', cancelledDone.export_available === false && Boolean(cancelledDone.export_unavailable_reason) && await page.getByRole('button', { name: 'Download experiment', exact: true }).count() === 0);
  check('cancelled request cannot obtain a reproduction archive', (await deniedExport(page, cancelled.id)).status === 409);
  const done = await waitJob(run.id);
  check('actual nondefault computation has complete retained export evidence', done.parameters.num_pts === 100 && done.export_available === true && done.investigation_available && Number.isFinite(done.result.evaluation.metrics.ate_rmse_m));
  await page.getByRole('button', { name: 'Download experiment', exact: true }).waitFor();
  check('run detail identifies external data and runtime requirements', /Linux.*Docker/.test(await page.locator('.job-export').innerText()) && /no Meridian account/.test(await page.locator('.job-export').innerText()));
  const invalid = [];
  for (const suffix of ['', '?schema_version=2', '?schema_version=1&schema_version=1', '?schema_version=1&path=other']) invalid.push((await deniedExport(page, run.id, suffix)).status);
  check('unsupported or ambiguous export format requests fail explicitly', invalid.every(status => status === 400));

  const pattern = `**${exportUrl(run.id)}`;
  const interrupt = route => route.abort('failed');
  await page.route(pattern, interrupt);
  await page.getByRole('button', { name: 'Download experiment', exact: true }).click();
  await page.locator('#export-status.error').waitFor();
  check('interrupted download preserves the run and enables retry', await page.getByRole('button', { name: 'Download experiment', exact: true }).isEnabled() && downloadEvents.length === 0);
  await page.unroute(pattern, interrupt);
  const first = await downloadArchive(run.id, 'experiment.zip', true);
  check('keyboard activation saves a verified real ZIP attachment', first.bytes > 0 && await page.getByText(/Archive verified; download requested/).isVisible());
  const repeated = await downloadArchive(run.id, 'experiment-repeat.zip');
  check('repeated downloads of the unchanged source have identical bytes', repeated.sha256 === first.sha256 && repeated.bytes === first.bytes);

  await page.getByLabel('Appearance', { exact: true }).selectOption('light');
  await page.locator('.job-export').scrollIntoViewIfNeeded();
  await page.screenshot({ path: `${output}export-desktop-light.png`, fullPage: true });
  await page.screenshot({ path: `${output}export-viewport-light.png` });
  check('desktop export detail has no horizontal page overflow', await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.getByLabel('Appearance', { exact: true }).selectOption('dark');
  await page.screenshot({ path: `${output}export-desktop-dark.png`, fullPage: true });
  await page.getByLabel('Appearance', { exact: true }).selectOption('light');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator('.job-export').scrollIntoViewIfNeeded();
  await page.screenshot({ path: `${output}export-mobile-light.png`, fullPage: true });
  check('archive identity and controls fit a 390 px viewport', await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.setViewportSize({ width: 1440, height: 1200 });
  await page.reload();
  await page.getByRole('button', { name: 'Download experiment', exact: true }).waitFor();
  check('the exact run export remains available after refresh', (await api(page, `${jobsPath()}/${run.id}`)).payload.job.export_available === true);
  await delayUntilAction(run.id, async () => {
    await page.getByRole('link', { name: 'All project runs', exact: false }).click();
    await page.getByRole('heading', { name: 'Project runs', exact: true }).waitFor();
  });
  check('navigation cancels a pending export without a late download', await page.locator('.job-export').count() === 0);

  await peer.goto(base);
  check('unauthenticated clients cannot download another user’s archive', (await deniedExport(peer, run.id)).status === 401);
  await register(peer, usernames[1]);
  check('another account cannot download the retained successful export', (await deniedExport(peer, run.id)).status === 404);
  await peer.goto(`${base}#project/${projectId}/results/${run.id}`);
  await peer.getByRole('heading', { name: 'Run unavailable', exact: true }).waitFor();
  check('another account sees no private run or export controls', await peer.locator('.job-export').count() === 0);

  await openJob(run.id);
  await delayUntilAction(run.id, async () => {
    await page.getByRole('button', { name: 'Sign out', exact: true }).click();
    await page.getByRole('heading', { name: 'Sign in', exact: true }).waitFor();
  });
  check('server logout clears private content and prevents a pending download', await page.locator('.job-detail').count() === 0 && (await deniedExport(page, run.id)).status === 401);
  await login();
  await openJob(run.id);
  await page.getByRole('button', { name: 'Delete run', exact: true }).click();
  await page.getByRole('button', { name: 'Keep run', exact: true }).click();
  check('cancelling the delete confirmation preserves export access', (await api(page, `${jobsPath()}/${run.id}`)).payload.job.export_available === true);
  await page.getByRole('button', { name: 'Delete run', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Delete run', exact: true }).click();
  await page.getByRole('heading', { name: 'Project runs', exact: true }).waitFor();
  deletedJobs.push(run.id);
  check('deleting the successful source revokes subsequent hosted export access', (await deniedExport(page, run.id)).status === 404 && (await api(page, `${jobsPath()}/${run.id}`)).status === 404);
  check('the already downloaded experiment remains intact after source deletion', hash(await readFile(`${output}experiment.zip`)) === first.sha256);
  check('deleting the run preserves its saved configuration', (await api(page, `/projects/${projectId}/experiments/${experimentId}`)).status === 200);
  check('no uncaught browser or CSP errors occurred', errors.length === 0 && cspErrors.length === 0);
} catch (error) {
  failure = String(error.stack || error).replaceAll(password, '[redacted]');
  process.exitCode = 1;
  try { await page.screenshot({ path: `${output}failure.png`, fullPage: true }); } catch { /* Preserve the original failure. */ }
} finally {
  const report = {
    schema_version: 1, base, checked_at: new Date().toISOString(), passed: !failure,
    environment_description: process.env.MERIDIAN_QA_ENVIRONMENT || 'Not specified by the operator.',
    transport: address.protocol.replace(':', ''), created_accounts: accounts, project_id: projectId, experiment_id: experimentId,
    created_job_ids: jobs, deleted_job_ids: deletedJobs, completed_jobs: completedJobs,
    checks, errors, cspErrors, downloads, observed_download_filenames: downloadEvents, ...(failure ? { failure } : {}),
    limitations: [
      'The archive and scientific outputs come from one actual successful nondefault execution. A separate cancelled request demonstrates unavailable evidence; failed or expired retained files require separate backend tests.',
      'Timing includes network, browser hashing and automation within the stated scope; it is not isolated server CPU time, sustained throughput or a latency guarantee.',
      'CDP may expose an empty body for a browser attachment; that absence is recorded, not treated as received file bytes. Saved downloads are checked against actual response SHA-256/length headers and a separate authenticated HTTP response.',
      'A transport interruption and request delays are injected deliberately; no scientific data, archive bytes or job states are mocked.',
      'Successful deletion revokes subsequent hosted requests. It cannot erase bytes already downloaded, and this browser script does not establish asynchronous physical cleanup.',
      'A real server logout is performed; no elapsed session-expiry duration is claimed.',
      'The downloaded archive is retained for a separate independent reproduction check after source deletion; browser checks alone do not establish that reproduction succeeds.',
      'Created accounts, project, experiment and cancelled request remain for exact operator cleanup.',
    ],
  };
  await writeFile(`${output}report.json`, JSON.stringify(report, null, 2) + '\n');
  await progress(failure ? 'FAILED' : 'PASSED');
  console.log(JSON.stringify({ passed: !failure, checks: checks.length, accounts, jobs, deletedJobs, output, ...(failure ? { failure } : {}) }, null, 2));
  await browser.close();
}
