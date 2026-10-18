import assert from 'node:assert/strict';
import test from 'node:test';
import { activeJob, jobStatus, renderJobDetail, renderRevisionJobs, renderLimits, renderService, createJobPoller } from '../src/workspace-jobs.js';
import { parseRoute } from '../src/workspace-api.js';

const projectId = '12345678-abcd-abcd-abcd-123456789abc';
const jobId = '87654321-abcd-abcd-abcd-987654321abc';
const base = { id: jobId, project_id: projectId, experiment_id: 'experiment', experiment_name: 'Room1 reference', revision_version: 2, specification_sha256: 'pinned', parameters: { num_pts: 200 }, state: 'queued', phase: 'queued', attempt_count: 0, max_attempts: 2, created_at: '2026-10-18T10:00:00Z', attempts: [], result: null, failure: null };
const result = {
  schema_version: 1,
  evaluation: { status: 'evaluated', metrics: { ate_rmse_m: 0.062, orientation_rmse_deg: 1.3 }, coverage_fraction: .95, evaluated_fraction: .93, reference_coverage_fraction: .98, initialization_delay_s: 6.6, protocol: { name: 'meridian-vio-se3', version: 1 }, counts: { expected_camera_samples: 100, estimate_samples: 95, evaluated_camera_samples: 93, reference_supported_camera_samples: 98 } },
  qualification: { passed: false, checks: { ate: false } }, execution: { native_peak_rss_kib: 80000 }, artifacts: { file_count: 4, total_bytes: 1000, manifest_sha256: 'manifest' },
};

test('run detail routes retain project ownership scope and reject ambiguous suffixes', () => {
  const route = `#project/${projectId}/results/${jobId}`;
  assert.deepEqual(parseRoute(route), { page: 'project', id: projectId, section: 'results', jobId });
  for (const suffix of ['/extra', '?owner=other', '/..']) assert.deepEqual(parseRoute(route + suffix), { page: 'not-found' });
});

test('cancellation requested remains active until a terminal server state arrives', () => {
  for (const state of ['queued', 'running', 'evaluating']) {
    assert.equal(activeJob({ state }), true);
    assert.equal(jobStatus({ state, cancel_requested: true }), 'Cancellation requested');
  }
  for (const state of ['succeeded', 'failed', 'cancelled']) assert.equal(activeJob({ state }), false);
  assert.equal(jobStatus({ state: 'cancelled', cancel_requested: true }), 'Cancelled');
  const html = renderJobDetail({ ...base, state: 'running', cancel_requested: true });
  assert.doesNotMatch(html, /data-action="cancel-job"/);
  assert.match(html, /acknowledgement/);
});

test('published success and numerical qualification are separate, with distinct coverage denominators', () => {
  const html = renderJobDetail({ ...base, state: 'succeeded', phase: 'complete', result });
  assert.match(html, /Succeeded/);
  assert.match(html, /qualification did not pass/);
  for (const value of ['0.06200', '95.00%', '93.00%', '98.00%', 'all 100 expected camera samples', 'without fitted scale']) assert.ok(html.includes(value), value);
  assert.doesNotMatch(html, /NaN|undefined/);
});

test('unsuccessful runs never expose supplied result metrics and failure text stays escaped', () => {
  for (const state of ['queued', 'running', 'evaluating', 'failed', 'cancelled']) {
    const html = renderJobDetail({ ...base, state, result, failure: { code: '<script>', message: '<img src=x onerror=alert(1)>' } });
    assert.doesNotMatch(html, /ATE RMSE|0\.06200|<script>|<img/);
    assert.match(html, /&lt;img/);
  }
});

test('artifact expiry does not erase saved metrics or claim files remain available', () => {
  const expired = renderJobDetail({ ...base, state: 'succeeded', result, artifacts_available: false });
  assert.match(expired, /Private artifact files are no longer retained/);
  assert.match(expired, /0\.06200/);
  assert.doesNotMatch(expired, /files are retained until|download/i);
  const retained = renderJobDetail({ ...base, state: 'succeeded', result, artifacts_available: true, artifacts_expire_at: '2026-10-25T10:00:00Z' });
  assert.match(retained, /Private artifact files are retained until/);
  assert.match(retained, /Saved metrics remain after file expiry/);
});

test('quotas and retention reflect API values and describe fixed windows without a false reset', () => {
  const html = renderLimits({ active_per_account: 2, retained_per_account: 20, concurrency_global: 1, submissions_per_account_24h: 10, submissions_global_24h: 100, artifact_retention_days: 7 });
  for (const text of ['10 new runs per account', '100 across the deployment', 'fixed 24-hour window', 'deleting runs does not reset', 'retained for 7 days']) assert.ok(html.includes(text), text);
  assert.match(renderLimits({ submissions_per_account_24h: 3, artifact_retention_days: 2 }), /3 new runs per account/);
  assert.match(renderLimits({ submissions_per_account_24h: 3, artifact_retention_days: 2 }), /retained for 2 days/);
});

test('service availability distinguishes missing workers and resource admission without inventing a queue ETA', () => {
  assert.match(renderService({ enabled: true, worker_available: false, capacity_available: false }), /Waiting for the execution service/);
  assert.match(renderService({ enabled: true, worker_available: true, capacity_available: false }), /Waiting for available host resources/);
  assert.match(renderService({ enabled: true, worker_available: true, capacity_available: true }), /execution service is available/);
  assert.match(renderService({ enabled: false }), /disabled/);
  assert.equal(renderService(undefined), '');
});

test('revision history filters exact experiment and version, and launching requires enabled saved state', () => {
  const view = { experiment: { id: 'experiment' }, revision: { version: 2 }, jobs: [base, { ...base, id: 'foreign', experiment_id: 'other' }, { ...base, id: 'old', revision_version: 1 }] };
  const html = renderRevisionJobs(view, true);
  assert.match(html, /Run version 2/);
  assert.match(html, new RegExp(jobId));
  assert.doesNotMatch(html, /results\/foreign|results\/old/);
  assert.doesNotMatch(renderRevisionJobs(view, false), /data-action="launch-job"/);
  assert.doesNotMatch(renderRevisionJobs(view, true, true), /data-action="launch-job"/);
});

function harness(options = {}) {
  const scheduled = [];
  const values = [];
  let signal;
  const poller = createJobPoller({
    read: async value => { signal = value; return { state: 'succeeded' }; },
    onValue: value => values.push(value), onError: () => true,
    shouldContinue: activeJob,
    setTimer: callback => { scheduled.push(callback); return callback; },
    clearTimer: callback => { const index = scheduled.indexOf(callback); if (index >= 0) scheduled.splice(index, 1); },
    ...options,
  });
  return { poller, scheduled, values, signal: () => signal };
}

test('polling waits for each request, stops at terminal state and aborts its signal', async () => {
  let finish;
  const run = harness({ read: () => new Promise(resolve => { finish = resolve; }) });
  const pending = run.scheduled.shift()();
  assert.equal(run.scheduled.length, 0);
  finish({ state: 'succeeded' });
  await pending;
  assert.equal(run.values.length, 1);
  assert.equal(run.scheduled.length, 0);
  const complete = harness();
  await complete.scheduled.shift()();
  assert.equal(complete.signal().aborted, true);
});

test('navigation or session cleanup suppresses a late response even if transport ignores abort', async () => {
  let finish;
  const run = harness({ read: () => new Promise(resolve => { finish = resolve; }) });
  const pending = run.scheduled.shift()();
  run.poller.stop();
  finish({ state: 'running' });
  await pending;
  assert.equal(run.values.length, 0);
  assert.equal(run.scheduled.length, 0);
});

test('hidden tabs pause requests and recoverable transport errors retain polling', async () => {
  let visible = false;
  let reads = 0;
  const errors = [];
  const run = harness({ isVisible: () => visible, read: async () => { reads += 1; throw new TypeError('offline'); }, onError: error => { errors.push(error.message); return true; } });
  await run.scheduled.shift()();
  assert.equal(reads, 0);
  visible = true;
  await run.scheduled.shift()();
  assert.equal(reads, 1);
  assert.deepEqual(errors, ['offline']);
  assert.equal(run.scheduled.length, 1);
  run.poller.stop();
  assert.equal(run.scheduled.length, 0);
});

test('an authorization failure can stop polling without repeated private requests', async () => {
  const run = harness({ read: async () => { throw { status: 401 }; }, onError: error => error.status !== 401 });
  await run.scheduled.shift()();
  assert.equal(run.scheduled.length, 0);
  assert.equal(run.values.length, 0);
});
