/**
 * Exercise real VIO computation on an explicitly selected, authorized deployment.
 * Creates two QA accounts, a private project, immutable revisions and three jobs.
 * One real baseline must complete; the others exercise queued/active cancellation.
 * One submission response is deliberately dropped after the real server accepts it
 * to verify an idempotent browser retry. No job state or result is fabricated.
 * Account/job IDs are reported for exact cleanup; credentials are never saved.
 * Requires execution=true, the qualified recording/image and a running worker.
 */
import assert from 'node:assert/strict';
import { randomBytes, randomUUID } from 'node:crypto';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve, sep } from 'node:path';
import { pathToFileURL } from 'node:url';

const [baseArgument, outputArgument, ...extraArguments] = process.argv.slice(2);
if (!baseArgument || !outputArgument || extraArguments.length) throw new Error('Usage: node web/scripts/check-workspace-jobs-browser.mjs WORKSPACE_URL OUTPUT_DIRECTORY');
const url = new URL(baseArgument);
if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error('Use an explicit HTTP(S) workspace URL without credentials, query or fragment.');
const base = url.href;
const output = resolve(outputArgument) + sep;
const moduleUrl = process.env.MERIDIAN_PLAYWRIGHT_MODULE ? pathToFileURL(resolve(process.env.MERIDIAN_PLAYWRIGHT_MODULE)).href : new URL('../../build/browser-tools/node_modules/playwright/index.mjs', import.meta.url).href;
const { chromium } = await import(moduleUrl);
const timeout = Number(process.env.MERIDIAN_JOB_QA_TIMEOUT_MS || 1_200_000);
if (!Number.isSafeInteger(timeout) || timeout < 30_000 || timeout > 3_600_000) throw new Error('MERIDIAN_JOB_QA_TIMEOUT_MS must be 30000–3600000.');
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true, ...(process.env.MERIDIAN_BROWSER_EXECUTABLE ? { executablePath: process.env.MERIDIAN_BROWSER_EXECUTABLE } : {}) });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
const other = await browser.newContext();
const page = await context.newPage();
const peer = await other.newPage();
const stamp = `${Date.now().toString(36)}-${randomBytes(3).toString('hex')}`;
const ownerName = `qa-jobs-${stamp}`;
const peerName = `qa-jobs-peer-${stamp}`;
const password = `Qa!${randomBytes(24).toString('base64url')}`;
const createdAccounts = [];
const createdJobs = [];
const checks = [];
const pageErrors = [];
const cspErrors = [];
const observedStates = new Set();
let projectId;
let completedJob;
let failure;
let jobReads = 0;
const check = (label, condition) => { assert.ok(condition, label); checks.push(label); };
for (const target of [page, peer]) {
  target.on('pageerror', error => pageErrors.push(error.message));
  target.on('console', message => { if (message.type() === 'error' && /content security policy|violat.*(?:directive|policy)|refused to (?:load|execute|apply|connect)/i.test(message.text())) cspErrors.push(message.text()); });
}
page.on('request', request => { if (request.method() === 'GET' && /\/api\/v1\/projects\/[^/]+\/jobs(?:\/[^/]+)?$/.test(new URL(request.url()).pathname)) jobReads += 1; });
page.on('response', async response => {
  if (response.request().method() !== 'GET' || !/\/jobs\/[^/]+$/.test(new URL(response.url()).pathname) || !response.ok()) return;
  try { const payload = await response.json(); if (payload.job) observedStates.add(payload.job.state); } catch { /* A navigation may discard the response body. */ }
});
async function api(target, path, method = 'GET', body) {
  return target.evaluate(async ({ path, method, body }) => {
    const session = await (await fetch('/api/v1/session')).json();
    const response = await fetch(`/api/v1${path}`, { method, headers: { 'Content-Type': 'application/json', 'X-CSRFToken': session.csrf_token }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    return { status: response.status, payload: await response.json() };
  }, { path, method, body });
}
async function register(target, username) {
  await target.goto(base);
  await target.getByRole('button', { name: 'Create account', exact: true }).click();
  await target.getByLabel('Username', { exact: true }).fill(username);
  await target.getByLabel('Password', { exact: true }).fill(password);
  const [response] = await Promise.all([target.waitForResponse(item => new URL(item.url()).pathname === '/api/v1/auth/register' && item.request().method() === 'POST'), target.getByRole('button', { name: 'Create account', exact: true }).click()]);
  if (response.ok()) createdAccounts.push(username);
  await target.getByRole('heading', { name: 'Projects', exact: true }).waitFor();
}
async function overflow(label) { check(label, await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)); }
async function waitState(state) { await page.locator('.job-detail .job-status').filter({ hasText: new RegExp(`^${state}$`) }).waitFor({ timeout }); }
async function openJob(id) {
  await page.goto(`${base}#project/${projectId}/results/${id}`);
  await page.locator('.job-detail').waitFor();
}
try {
  await register(page, ownerName);
  check('deployment explicitly enables execution', (await api(page, '/session')).payload.capabilities.execution === true);
  await page.getByRole('button', { name: /New project/ }).click();
  await page.getByLabel('Project name', { exact: true }).fill('Hosted VIO validation');
  await page.getByRole('button', { name: 'Create project', exact: true }).click();
  await page.getByRole('heading', { name: 'TUM-VI room1', exact: true }).waitFor();
  projectId = new URL(page.url()).hash.split('/')[1];
  const jobsApi = `/projects/${projectId}/jobs`;
  await page.getByRole('link', { name: 'Experiments', exact: true }).click();
  await page.getByRole('button', { name: 'New experiment', exact: true }).click();
  await page.getByLabel('Experiment name', { exact: true }).fill('Room1 baseline');
  await page.getByLabel('Description', { exact: false }).fill('Immutable baseline for a hosted execution check.');
  await page.getByRole('button', { name: 'Create experiment', exact: true }).click();
  await page.getByRole('button', { name: 'Run version 1', exact: true }).waitFor();
  const experimentId = new URL(page.url()).hash.split('/')[3];
  const experimentApi = `/projects/${projectId}/experiments/${experimentId}`;
  const baseline = (await api(page, experimentApi)).payload.revision;
  const profile = (await api(page, '/configuration-profile')).payload.profile;
  await page.getByRole('button', { name: 'Edit configuration', exact: true }).click();
  check('unsaved configuration cannot be submitted as a run', await page.getByRole('button', { name: /^Run version / }).count() === 0);
  await page.getByLabel(profile.parameters.num_pts.label, { exact: true }).selectOption('100');
  await page.getByRole('button', { name: 'Save new version', exact: true }).click();
  await page.getByRole('button', { name: 'Run version 2', exact: true }).waitFor();
  await page.getByRole('navigation', { name: 'Saved versions' }).getByRole('link', { name: /^Version 1/ }).click();
  await page.getByRole('button', { name: 'Run version 1', exact: true }).waitFor();

  let submittedBody;
  let accepted;
  const endpoint = `**/api/v1${jobsApi}`;
  const dropAcceptedResponse = async route => {
    if (route.request().method() !== 'POST') { await route.continue(); return; }
    submittedBody = route.request().postDataJSON();
    const response = await route.fetch();
    assert.equal(response.status(), 201);
    accepted = (await response.json()).job;
    createdJobs.push(accepted.id);
    await route.abort('failed');
  };
  await page.route(endpoint, dropAcceptedResponse);
  await page.getByRole('button', { name: 'Run version 1', exact: true }).click();
  await page.getByText(/Submission may have reached the server/).waitFor();
  check('a lost response preserves an explicit retry path', Boolean(accepted?.id));
  await page.unroute(endpoint, dropAcceptedResponse);
  const [retryResponse] = await Promise.all([page.waitForResponse(response => new URL(response.url()).pathname === `/api/v1${jobsApi}` && response.request().method() === 'POST'), page.getByRole('button', { name: 'Run version 1', exact: true }).click()]);
  const retried = await retryResponse.json();
  check('browser retry reuses the exact idempotency identity without duplicate work', retryResponse.status() === 200 && retried.reused === true && retried.job.id === accepted.id && retryResponse.request().postDataJSON().idempotency_key === submittedBody.idempotency_key);
  await page.locator('.job-detail').waitFor();
  check('historical launch preserves its exact immutable version and specification', retried.job.revision_version === 1 && retried.job.parameters.num_pts === 200 && retried.job.specification_sha256 === baseline.specification_sha256);
  check('only one job exists after uncertain submission and retry', (await api(page, jobsApi)).payload.jobs.length === 1);
  await page.getByRole('link', { name: 'Data', exact: true }).click();
  await page.getByRole('heading', { name: 'TUM-VI room1', exact: true }).waitFor();
  const readsBefore = jobReads;
  await page.waitForTimeout(3200);
  check('leaving the execution view stops its polling requests', jobReads === readsBefore);
  await openJob(accepted.id);
  await page.reload();
  await page.locator('.job-detail').waitFor();
  check('a submitted run survives browser navigation and refresh', new URL(page.url()).hash.endsWith(accepted.id));
  await waitState('Succeeded');
  completedJob = (await api(page, `${jobsApi}/${accepted.id}`)).payload.job;
  check('a real worker published evaluated results for the selected specification', completedJob.state === 'succeeded' && completedJob.result?.schema_version === 1 && Number.isFinite(completedJob.result.evaluation.metrics.ate_rmse_m) && completedJob.result.artifacts.manifest_sha256.length === 64);
  check('the qualified baseline passes its independently evaluated numerical gates', completedJob.result.qualification.passed === true);
  check('the UI distinguishes estimate evaluation and reference coverage', await page.getByText('Estimate coverage', { exact: true }).isVisible() && await page.getByText('Evaluated coverage', { exact: true }).isVisible() && await page.getByText('Reference coverage', { exact: true }).isVisible());
  await page.getByLabel('Appearance', { exact: true }).selectOption('light');
  await page.getByRole('heading', { name: 'Room1 baseline', exact: true }).click();
  await page.screenshot({ path: `${output}run-desktop-light.png`, fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await overflow('completed metrics fit a 390 px viewport');
  await page.getByText('Qualification, protocol and resource evidence', { exact: true }).click();
  await overflow('expanded result evidence fits a 390 px viewport');
  await page.screenshot({ path: `${output}run-mobile-light.png`, fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  const terminalReads = jobReads;
  await page.waitForTimeout(3200);
  check('terminal runs stop automatic polling', terminalReads === jobReads);

  await register(peer, peerName);
  await peer.goto(`${base}#project/${projectId}/results/${accepted.id}`);
  await peer.getByRole('heading', { name: 'Run unavailable', exact: true }).waitFor();
  const denied = [];
  for (const [path, method, body] of [[jobsApi, 'GET'], [`${jobsApi}/${accepted.id}`, 'GET'], [`${jobsApi}/${accepted.id}/cancel`, 'POST', { schema_version: 1 }], [`${jobsApi}/${accepted.id}`, 'DELETE'], [jobsApi, 'POST', { ...submittedBody, idempotency_key: randomUUID() }]]) denied.push((await api(peer, path, method, body)).status);
  check('another account cannot list read submit cancel or delete these jobs', denied.every(status => status === 404));

  const active = await api(page, jobsApi, 'POST', { ...submittedBody, idempotency_key: randomUUID() });
  assert.equal(active.status, 201);
  createdJobs.push(active.payload.job.id);
  await openJob(active.payload.job.id);
  await waitState('Running');
  const queued = await api(page, jobsApi, 'POST', { ...submittedBody, idempotency_key: randomUUID() });
  assert.equal(queued.status, 201);
  createdJobs.push(queued.payload.job.id);
  await openJob(queued.payload.job.id);
  await waitState('Queued');
  await page.getByRole('button', { name: 'Cancel run', exact: true }).click();
  await page.getByRole('button', { name: 'Keep run', exact: true }).click();
  check('cancellation requires a confirmation', (await api(page, `${jobsApi}/${queued.payload.job.id}`)).payload.job.cancel_requested === false);
  await page.getByRole('button', { name: 'Cancel run', exact: true }).click();
  await page.getByRole('button', { name: 'Confirm cancellation', exact: true }).click();
  await waitState('Cancelled');
  check('queued work cancels without publishing results', (await api(page, `${jobsApi}/${queued.payload.job.id}`)).payload.job.result === null);
  const cancelledAgain = await api(page, `${jobsApi}/${queued.payload.job.id}/cancel`, 'POST', { schema_version: 1 });
  check('repeated cancellation is idempotent', cancelledAgain.status === 200 && cancelledAgain.payload.job.state === 'cancelled');
  await page.getByRole('button', { name: 'Delete run', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Delete run', exact: true }).click();
  await page.getByRole('heading', { name: 'Project runs', exact: true }).waitFor();
  check('terminal deletion removes access to that run', (await api(page, `${jobsApi}/${queued.payload.job.id}`)).status === 404);
  await openJob(active.payload.job.id);
  await context.clearCookies();
  await page.getByRole('heading', { name: 'Sign in', exact: true }).waitFor({ timeout: 15_000 });
  check('an unauthorized polling response clears private run content', await page.locator('.job-detail').count() === 0);
  await page.getByLabel('Username', { exact: true }).fill(ownerName);
  await page.getByLabel('Password', { exact: true }).fill(password);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await page.getByRole('button', { name: 'Cancel run', exact: true }).waitFor();
  await page.getByRole('button', { name: 'Cancel run', exact: true }).click();
  await page.getByRole('button', { name: 'Confirm cancellation', exact: true }).click();
  await waitState('Cancelled');
  check('active cancellation reaches worker acknowledgement with no published result', (await api(page, `${jobsApi}/${active.payload.job.id}`)).payload.job.result === null);
  check('no uncaught browser or CSP errors occurred', pageErrors.length === 0 && cspErrors.length === 0);
} catch (error) {
  failure = String(error.message || error).replaceAll(password, '[redacted]');
  process.exitCode = 1;
  try { await page.screenshot({ path: `${output}failure.png`, fullPage: true }); } catch { /* Preserve the original failure. */ }
} finally {
  const report = {
    schema_version: 1, base, checked_at: new Date().toISOString(), passed: !failure,
    environment_description: process.env.MERIDIAN_QA_ENVIRONMENT || 'Not specified by the operator.',
    transport: url.protocol.replace(':', ''), created_accounts: createdAccounts, project_id: projectId, created_job_ids: createdJobs,
    checks, observed_states: [...observedStates], pageErrors, cspErrors, completed_job: completedJob,
    ...(failure ? { failure } : {}),
    limitations: [
      'Browser checks do not independently establish container isolation, database durability, worker restart recovery or filesystem cleanup.',
      'Observed states are sampled from real API responses; brief intermediate phases may not be observed.',
      'The first submission response is deliberately interrupted after a real server mutation; no execution result is mocked.',
      'Created accounts, the project and retained jobs remain for exact administrator cleanup. Inspect active jobs after a failed check.',
    ],
  };
  await writeFile(`${output}report.json`, JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ passed: !failure, checks: checks.length, created_accounts: createdAccounts, created_job_ids: createdJobs, output, ...(failure ? { failure } : {}) }, null, 2));
  await browser.close();
}
