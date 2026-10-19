/**
 * Create and inspect private comparisons on an explicitly authorized deployment.
 * Creates two random QA accounts, one project, two immutable experiment versions,
 * three real VIO executions (200, 100, repeated 100), and one cancelled request.
 * Drops one real accepted comparison response to verify an idempotent retry.
 * Results and job states are never fabricated. Credentials remain in memory;
 * exact account/job/comparison IDs are saved for operator evidence and cleanup.
 * Requires execution=true, the qualified recording/image and an available worker.
 */
import assert from 'node:assert/strict';
import { randomBytes, randomUUID } from 'node:crypto';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve, sep } from 'node:path';
import { pathToFileURL } from 'node:url';

const [baseArgument, outputArgument, ...extra] = process.argv.slice(2);
if (!baseArgument || !outputArgument || extra.length) throw new Error('Usage: node web/scripts/check-workspace-comparisons-browser.mjs WORKSPACE_URL OUTPUT_DIRECTORY');
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
const context = await browser.newContext({ viewport: { width: 1440, height: 1400 } });
const other = await browser.newContext();
const page = await context.newPage();
const peer = await other.newPage();
const stamp = `${Date.now().toString(36)}-${randomBytes(3).toString('hex')}`;
const usernames = [`qa-comparison-${stamp}`, `qa-comparison-peer-${stamp}`];
const password = `Qa!${randomBytes(24).toString('base64url')}`;
const accounts = [];
const jobs = [];
const comparisons = [];
const checks = [];
const errors = [];
const cspErrors = [];
const completedJobs = {};
const savedComparisons = {};
const comparisonRequests = [];
let projectId;
let experimentId;
let failure;
const check = (label, condition) => { assert.ok(condition, label); checks.push(label); };
async function progress(state) {
  await writeFile(`${output}progress.json`, JSON.stringify({ state, checked_at: new Date().toISOString(), accounts, project_id: projectId, experiment_id: experimentId, jobs, comparisons, checks: checks.length }, null, 2) + '\n');
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
const jobsPath = () => `/projects/${projectId}/jobs`;
const comparisonsPath = () => `/projects/${projectId}/comparisons`;
async function submitJob(version) {
  const response = await api(page, jobsPath(), 'POST', { schema_version: 1, experiment_id: experimentId, revision_version: version, idempotency_key: randomUUID() });
  assert.equal(response.status, 201);
  jobs.push(response.payload.job.id);
  await progress(`Submitted version ${version}: ${response.payload.job.id}`);
  return response.payload.job;
}
async function waitJob(id, expected = 'succeeded') {
  await page.goto(`${base}#project/${projectId}/results/${id}`);
  await page.locator('.job-detail').waitFor();
  let lastState;
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const response = await api(page, `${jobsPath()}/${id}`);
    assert.equal(response.status, 200);
    const job = response.payload.job;
    const state = `${job.state}/${job.phase}`;
    if (state !== lastState) { await progress(`${id}: ${state}`); lastState = state; }
    if (['succeeded', 'failed', 'cancelled'].includes(job.state)) {
      assert.equal(job.state, expected, `Run ${id} reached ${job.state}: ${JSON.stringify(job.failure)}`);
      await page.locator('.job-detail .job-status').filter({ hasText: /^(Succeeded|Failed|Cancelled)$/ }).waitFor();
      completedJobs[id] = job;
      await writeFile(`${output}job-${id}.response.json`, response.raw);
      return job;
    }
    await page.waitForTimeout(2500);
  }
  throw new Error(`Run ${id} did not reach a terminal state within the configured timeout.`);
}
async function newComparison(name, baseline, candidate) {
  await page.goto(`${base}#project/${projectId}/results`);
  await page.getByRole('heading', { name: 'Saved comparisons', exact: true }).waitFor();
  await page.getByRole('button', { name: 'New comparison', exact: true }).click();
  await page.getByLabel('Comparison name', { exact: true }).fill(name);
  await page.getByLabel('Description', { exact: false }).fill('Full recording; two exact run identities and their retained evaluation evidence.');
  await page.getByRole('combobox', { name: 'Baseline run', exact: true }).selectOption(baseline);
  await page.getByRole('combobox', { name: 'Candidate run', exact: true }).selectOption(candidate);
}
async function saveComparison(name) {
  let started;
  let response;
  let bytes;
  let finished;
  const observeRequest = request => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === `/api/v1${comparisonsPath()}`) started ??= performance.now();
  };
  page.on('request', observeRequest);
  try {
    [response] = await Promise.all([page.waitForResponse(item => new URL(item.url()).pathname === `/api/v1${comparisonsPath()}` && item.request().method() === 'POST'), page.getByRole('button', { name: 'Save comparison', exact: true }).click()]);
    bytes = await response.body();
    finished = performance.now();
  } finally {
    page.off('request', observeRequest);
  }
  const payload = JSON.parse(bytes.toString('utf8'));
  assert.ok(response.ok(), JSON.stringify(payload));
  assert.ok(Number.isFinite(started), 'Comparison request start was observed');
  const item = payload.comparison;
  comparisonRequests.push({ comparison_id: item.id, status: response.status(), reused: payload.reused, elapsed_ms: finished - started, response_body_bytes: bytes.length, timing_scope: 'Node monotonic time from Playwright browser request event to complete decoded response body.' });
  if (!comparisons.includes(item.id)) comparisons.push(item.id);
  savedComparisons[item.id] = item;
  await writeFile(`${output}comparison-${item.id}.response.json`, bytes);
  await page.getByRole('heading', { name, exact: true }).waitFor();
  await progress(`Saved comparison ${item.id}: ${item.analysis.kind}`);
  return { response, payload, item };
}
async function openComparison(item) {
  await page.goto(`${base}#project/${projectId}/results/comparisons/${item.id}`);
  await page.getByRole('heading', { name: item.name, exact: true }).waitFor();
}
async function noOverflow(label) { check(label, await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)); }
try {
  await register(page, usernames[0]);
  check('deployment enables actual computation', (await api(page, '/session')).payload.capabilities.execution === true);
  const project = await api(page, '/projects', 'POST', { name: 'Indoor VIO validation', description: 'Exact execution comparisons on calibrated stereo and IMU.', dataset_id: 'tumvi-room1-512-16-v1' });
  assert.equal(project.status, 201);
  projectId = project.payload.project.id;
  const experiment = await api(page, `/projects/${projectId}/experiments`, 'POST', { schema_version: 1, name: 'Room1 feature budget', description: 'Reference and feature-budget variant.', dataset_id: 'tumvi-room1-512-16-v1', parameters: { num_pts: 200 } });
  assert.equal(experiment.status, 201);
  experimentId = experiment.payload.experiment.id;
  const edit = await api(page, `/projects/${projectId}/experiments/${experimentId}`, 'PATCH', { schema_version: 1, base_version: 1, name: 'Room1 feature budget', description: 'Reference and feature-budget variant.', parameters: { num_pts: 100 } });
  assert.equal(edit.status, 200);
  check('baseline and variant are distinct immutable revisions', edit.payload.revision.version === 2 && edit.payload.revision.specification_sha256 !== experiment.payload.revision.specification_sha256);
  const baseline = await submitJob(1);
  const candidate = await submitJob(2);
  const baselineDone = await waitJob(baseline.id);
  check('real baseline completes with full-resolution retained evidence', baselineDone.parameters.num_pts === 200 && baselineDone.investigation_available && Number.isFinite(baselineDone.result?.evaluation.metrics?.ate_rmse_m));
  const cancelled = await submitJob(2);
  assert.equal((await api(page, `${jobsPath()}/${cancelled.id}/cancel`, 'POST', { schema_version: 1 })).status, 200);
  await waitJob(cancelled.id, 'cancelled');
  const repeated = await submitJob(2);
  const candidateDone = await waitJob(candidate.id);
  check('real variant completes from the saved version with its own evidence', candidateDone.parameters.num_pts === 100 && candidateDone.revision_version === 2 && candidateDone.investigation_available);

  await newComparison('Feature budget · 200 → 100', baseline.id, baseline.id);
  await page.getByRole('button', { name: 'Save comparison', exact: true }).click();
  await page.getByText(/Choose a different exact run/).waitFor();
  check('self-comparison is rejected without discarding the draft', await page.getByLabel('Comparison name', { exact: true }).inputValue() === 'Feature budget · 200 → 100');
  await page.getByRole('combobox', { name: 'Candidate run', exact: true }).selectOption(candidate.id);
  check('selection exposes the exact version, parameter and run identities', (await page.locator('#baseline-selection').innerText()).includes(baseline.id) && (await page.locator('#candidate-selection').innerText()).includes(candidate.id));
  let accepted;
  let requestBody;
  const endpoint = `**/api/v1${comparisonsPath()}`;
  const dropResponse = async route => {
    if (route.request().method() !== 'POST') { await route.continue(); return; }
    requestBody = route.request().postDataJSON();
    const started = performance.now();
    const response = await route.fetch();
    const bytes = await response.body();
    const finished = performance.now();
    assert.equal(response.status(), 201);
    const payload = JSON.parse(bytes.toString('utf8'));
    accepted = payload.comparison;
    comparisonRequests.push({ comparison_id: accepted.id, status: response.status(), reused: payload.reused, elapsed_ms: finished - started, response_body_bytes: bytes.length, timing_scope: 'Node monotonic time from Playwright route.fetch() to complete decoded response body, before intentional browser interruption.' });
    await writeFile(`${output}comparison-${accepted.id}.initial.response.json`, bytes);
    comparisons.push(accepted.id);
    await route.abort('failed');
  };
  await page.route(endpoint, dropResponse);
  await page.getByRole('button', { name: 'Save comparison', exact: true }).click();
  await page.getByText(/Saving may have reached the server/).waitFor();
  check('uncertain submission preserves the exact selection and description', Boolean(accepted?.id) && await page.getByRole('combobox', { name: 'Baseline run', exact: true }).inputValue() === baseline.id && await page.getByRole('combobox', { name: 'Candidate run', exact: true }).inputValue() === candidate.id && (await page.getByLabel('Description', { exact: false }).inputValue()).includes('Full recording'));
  await page.unroute(endpoint, dropResponse);
  const variantSaved = await saveComparison('Feature budget · 200 → 100');
  const variant = variantSaved.item;
  check('retry recovers the same immutable comparison without duplication', variantSaved.response.status() === 200 && variantSaved.payload.reused && variant.id === accepted.id && variantSaved.response.request().postDataJSON().idempotency_key === requestBody.idempotency_key && (await api(page, comparisonsPath())).payload.comparisons.length === 1);
  check('saved comparison preserves exact runs, revisions and parameter difference', variant.baseline_job_id === baseline.id && variant.candidate_job_id === candidate.id && variant.analysis.baseline.revision_version === 1 && variant.analysis.candidate.revision_version === 2 && variant.analysis.configuration_changes.some(change => change.parameter === 'num_pts' && change.baseline === 200 && change.candidate === 100));
  const common = variant.analysis.common_support;
  const commonTable = page.locator('table').filter({ has: page.getByText('Full-resolution errors on the common evaluated support', { exact: true }) });
  check('common support is scored by the service on shared full-resolution epochs', common.status === 'ok' && common.evaluated_camera_samples > 0 && common.expected_camera_samples === 2821 && (await commonTable.innerText()).includes(common.baseline_metrics.ate_rmse_m.toFixed(5)) && (await commonTable.innerText()).includes(common.candidate_metrics.ate_rmse_m.toFixed(5)));
  check('signed shared-support delta is displayed without a winner claim', (await commonTable.innerText()).includes(`${common.delta_candidate_minus_baseline.ate_rmse_m > 0 ? '+' : ''}${common.delta_candidate_minus_baseline.ate_rmse_m.toFixed(5)}`) && !/winner|best configuration/i.test(await page.locator('.run-comparison').innerText()));
  check('coverage, support regions, initialization and host cost remain present', await page.getByRole('heading', { name: 'Where the runs differ', exact: true }).isVisible() && await page.getByRole('heading', { name: 'Host observations', exact: true }).isVisible() && await page.getByRole('heading', { name: 'Outcome and coverage', exact: true }).isVisible());
  await page.reload();
  await page.getByRole('heading', { name: variant.name, exact: true }).waitFor();
  check('deep link and saved analysis survive refresh', (await api(page, `${comparisonsPath()}/${variant.id}`)).payload.comparison.analysis_sha256 === variant.analysis_sha256);
  await page.getByLabel('Appearance', { exact: true }).selectOption('light');
  await noOverflow('desktop comparison has no page overflow');
  await page.screenshot({ path: `${output}comparison-desktop-light.png`, fullPage: true });
  await page.screenshot({ path: `${output}comparison-viewport-light.png` });
  await page.locator('.comparison-run-grid').evaluate(element => window.scrollTo(0, element.getBoundingClientRect().top + scrollY - 24));
  await page.screenshot({ path: `${output}comparison-metrics-light.png` });
  await page.locator('#configuration-difference-heading').evaluate(element => window.scrollTo(0, element.closest('section').getBoundingClientRect().top + scrollY - 24));
  await page.screenshot({ path: `${output}comparison-support-metrics-light.png` });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.getByLabel('Appearance', { exact: true }).selectOption('dark');
  await page.screenshot({ path: `${output}comparison-desktop-dark.png`, fullPage: true });
  await page.getByLabel('Appearance', { exact: true }).selectOption('light');
  await page.setViewportSize({ width: 390, height: 844 });
  await noOverflow('comparison fits a 390 px viewport');
  await page.getByText('Exact evaluated support regions', { exact: true }).click();
  await noOverflow('expanded exact support regions remain within the mobile viewport');
  const scrollTable = page.getByLabel('evaluated support regions', { exact: true });
  await scrollTable.focus();
  check('wide evidence tables accept keyboard focus for horizontal scrolling', await scrollTable.evaluate(element => document.activeElement === element));
  await page.screenshot({ path: `${output}comparison-mobile-light.png`, fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1400 });
  await openComparison(variant);
  await page.locator('.comparison-run-card').first().getByRole('link', { name: 'Investigate run', exact: true }).click();
  await page.waitForFunction(() => ['cam0', 'cam1'].every(camera => { const image = document.querySelector(`#image-${camera}`); return image && image.complete && image.naturalWidth === 512; }));
  check('a retained source opens the real synchronized investigation and decoded stereo images', await page.locator('#frame-cursor').count() === 1);

  const repeatedDone = await waitJob(repeated.id);
  check('a repeated execution keeps a distinct run identity and the same saved specification', repeatedDone.id !== candidateDone.id && repeatedDone.specification_sha256 === candidateDone.specification_sha256 && repeatedDone.investigation_available);
  await newComparison('Repeated feature budget · 100', candidate.id, repeated.id);
  const repetition = (await saveComparison('Repeated feature budget · 100')).item;
  check('same-specification runs are retained as a pair with raw observed variation', repetition.analysis.kind === 'repeated_execution' && repetition.analysis.configuration_changes.length === 0 && repetition.analysis.protocol.repeated_execution_aggregation === 'none' && await page.getByText(/Two distinct run requests share the same saved specification/).isVisible());
  await page.screenshot({ path: `${output}repeated-desktop-light.png`, fullPage: true });

  await newComparison('Cancelled request evidence', candidate.id, cancelled.id);
  const unavailable = (await saveComparison('Cancelled request evidence')).item;
  check('cancelled runs remain selectable and do not invent common scores', unavailable.analysis.candidate.state === 'cancelled' && unavailable.analysis.common_support.status === 'investigation_unavailable' && unavailable.analysis.common_support.candidate_metrics === null && await page.getByRole('heading', { name: 'Support differences unavailable', exact: true }).isVisible());
  const unchangedAnalysis = structuredClone(unavailable.analysis);
  assert.equal((await api(page, `${jobsPath()}/${cancelled.id}`, 'DELETE')).status, 200);
  await page.reload();
  await page.getByRole('heading', { name: unavailable.name, exact: true }).waitFor();
  const retained = (await api(page, `${comparisonsPath()}/${unavailable.id}`)).payload.comparison;
  assert.deepEqual(retained.analysis, unchangedAnalysis, 'The saved analysis is semantically unchanged after PostgreSQL JSON serialization');
  check('source deletion preserves the saved numerical snapshot and immutable hash', retained.analysis_sha256 === unavailable.analysis_sha256 && retained.candidate_access.exists === false);
  check('a removed source has no misleading run or investigation link', await page.locator('.comparison-run-card').nth(1).getByText('Original run removed', { exact: true }).isVisible() && await page.locator('.comparison-run-card').nth(1).getByRole('link').count() === 0);
  await page.screenshot({ path: `${output}unavailable-desktop-light.png`, fullPage: true });

  await register(peer, usernames[1]);
  await peer.goto(`${base}#project/${projectId}/results/comparisons/${variant.id}`);
  await peer.getByRole('heading', { name: 'Comparison unavailable', exact: true }).waitFor();
  const denied = [];
  for (const [path, method, body] of [[comparisonsPath(), 'GET'], [`${comparisonsPath()}/${variant.id}`, 'GET'], [`${comparisonsPath()}/${variant.id}`, 'DELETE'], [comparisonsPath(), 'POST', { ...requestBody, idempotency_key: randomUUID() }]]) denied.push((await api(peer, path, method, body)).status);
  check('another account cannot list read create or delete private comparisons', denied.every(status => status === 404));
  await page.getByRole('button', { name: 'Delete comparison', exact: true }).click();
  await page.getByRole('button', { name: 'Keep comparison', exact: true }).click();
  check('comparison deletion requires an explicit confirmation', (await api(page, `${comparisonsPath()}/${unavailable.id}`)).status === 200);
  await page.getByRole('button', { name: 'Delete comparison', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Delete comparison', exact: true }).click();
  await page.getByRole('heading', { name: 'Saved comparisons', exact: true }).waitFor();
  check('deleting a comparison preserves its remaining source execution', (await api(page, `${comparisonsPath()}/${unavailable.id}`)).status === 404 && (await api(page, `${jobsPath()}/${candidate.id}`)).status === 200);
  check('saved variant and repeated pairs remain individually revisitable', (await api(page, comparisonsPath())).payload.comparisons.length === 2 && await page.getByRole('heading', { name: variant.name, exact: true }).isVisible() && await page.getByRole('heading', { name: repetition.name, exact: true }).isVisible());
  await openComparison(variant);
  await context.clearCookies();
  await page.reload();
  await page.getByRole('heading', { name: 'Sign in', exact: true }).waitFor();
  check('session expiry clears private comparison content', await page.locator('.run-comparison').count() === 0);
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
    created_job_ids: jobs, created_comparison_ids: comparisons, checks, errors, cspErrors,
    completed_jobs: completedJobs, saved_comparisons: savedComparisons, comparison_requests: comparisonRequests, ...(failure ? { failure } : {}),
    limitations: [
      'Comparison timings include transport and automation overhead within each recorded scope; they are single observed requests, not isolated server processing time or a latency guarantee. Byte counts describe exact decoded response bodies, not compressed wire traffic.',
      'Parsed report objects preserve JSON values, not numeric spelling or canonical hash bytes; separate raw response files preserve the exact HTTP bodies.',
      'Three real executions establish observed behavior for this recording and supported parameter pair, without statistical confidence or automatic ranking.',
      'One accepted comparison response is intentionally interrupted; result data and execution states are never mocked.',
      'Browser checks do not independently establish process isolation, database durability, physical cleanup or artifact retention duration.',
      'A cancelled source and its comparison are deleted; other accounts, runs and comparisons remain for exact operator evidence extraction and cleanup.',
    ],
  };
  await writeFile(`${output}report.json`, JSON.stringify(report, null, 2) + '\n');
  await progress(failure ? 'FAILED' : 'PASSED');
  console.log(JSON.stringify({ passed: !failure, checks: checks.length, accounts, jobs, comparisons, output, ...(failure ? { failure } : {}) }, null, 2));
  await browser.close();
}
