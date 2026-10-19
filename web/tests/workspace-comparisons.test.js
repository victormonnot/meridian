import assert from 'node:assert/strict';
import test from 'node:test';
import { parseRoute } from '../src/workspace-api.js';
import { comparisonPayload, terminalRuns, createComparisonDialog, renderComparisonList, comparisonRunLinks, deleteComparisonDialog, renderRunComparison } from '../src/workspace-comparisons.js';

const projectId = '12345678-abcd-abcd-abcd-123456789abc';
const firstId = '87654321-abcd-abcd-abcd-987654321abc';
const secondId = '87654321-abcd-abcd-abcd-987654321def';
const jobs = [
  { id: firstId, state: 'succeeded', parameters: { num_pts: 200 }, experiment_name: 'Reference', revision_version: 1, created_at: '2026-10-19T09:00:00Z' },
  { id: secondId, state: 'failed', parameters: { num_pts: 200 }, experiment_name: 'Reference', revision_version: 1, created_at: '2026-10-19T10:00:00Z', failure: { code: 'native_failed', message: 'No valid estimate.' } },
  { id: 'active', state: 'running', parameters: { num_pts: 100 }, experiment_name: 'Variant', revision_version: 2 },
];
const values = { name: ' Repeated reference ', description: 'Two exact runs.\nKeep failures visible.', baseline_job_id: firstId, candidate_job_id: secondId };

test('comparison selection allows distinct executions of the same specification and retains failed outcomes', () => {
  assert.deepEqual(terminalRuns(jobs), jobs.slice(0, 2));
  assert.deepEqual(comparisonPayload(values, jobs), { schema_version: 1, ...values, name: 'Repeated reference' });
  const html = createComparisonDialog(jobs);
  assert.ok(html.includes(firstId) && html.includes(secondId));
  assert.doesNotMatch(html, /value="active"/);
  assert.match(html, /Failed/);
});

test('self-comparison, active or foreign run selections and invalid labels fail before submission', () => {
  for (const change of [{ candidate_job_id: firstId }, { candidate_job_id: 'active' }, { candidate_job_id: 'foreign' }, { name: ' ' }, { name: 'a'.repeat(101) }, { description: 'x'.repeat(2001) }]) {
    assert.throws(() => comparisonPayload({ ...values, ...change }, jobs), error => error.code === 'validation_error' && Object.keys(error.fields).length > 0);
  }
});

test('saved comparison labels and private descriptions never become HTML or executable attributes', () => {
  const item = { id: secondId, name: '<img src=x onerror=alert(1)>', description: '<script>private</script>', created_at: jobs[0].created_at, summary: { kind: 'repeated_execution', baseline: jobs[0], candidate: jobs[1] } };
  const html = renderComparisonList(projectId, { comparisons: [item], limits: { comparisons: 20 } }, jobs);
  assert.match(html, /Repeated execution/);
  assert.match(html, /&lt;img/);
  assert.doesNotMatch(html, /<img|<script>|winner|best run/i);
  assert.match(html, /Succeeded → Failed/);
  assert.match(deleteComparisonDialog(item), /original runs, experiments and their retained artifacts are preserved/i);
});

test('saved analysis does not link to deleted runs or expired investigation evidence', () => {
  const deleted = comparisonRunLinks(projectId, firstId, { exists: false, investigation_available: false });
  assert.doesNotMatch(deleted, /href=/);
  assert.match(deleted, /Original run removed/);
  const expired = comparisonRunLinks(projectId, firstId, { exists: true, investigation_available: false });
  assert.match(expired, /Run overview/);
  assert.doesNotMatch(expired, /\/investigation"/);
  const available = comparisonRunLinks(projectId, firstId, { exists: true, investigation_available: true });
  assert.match(available, /Investigate run/);
});

test('comparison creation reflects project limit and needs two terminal runs', () => {
  assert.match(renderComparisonList(projectId, { comparisons: [], limits: { comparisons: 20 } }, jobs.slice(0, 1)), /data-action="new-comparison" disabled/);
  const item = { id: firstId, name: 'Existing', summary: { kind: 'repeated_execution', baseline: jobs[0], candidate: jobs[1] } };
  assert.match(renderComparisonList(projectId, { comparisons: [item], limits: { comparisons: 1 } }, jobs), /data-action="new-comparison" disabled/);
  assert.doesNotMatch(renderComparisonList(projectId, { comparisons: [], limits: { comparisons: 20 } }, jobs), /data-action="new-comparison" disabled/);
});

test('comparison deep links are distinct from runs and preserve project ownership scope', () => {
  const route = `#project/${projectId}/results/comparisons/${firstId}`;
  assert.deepEqual(parseRoute(route), { page: 'project', id: projectId, section: 'results', comparisonId: firstId });
  for (const suffix of ['/edit', '?owner=other', '/..']) assert.deepEqual(parseRoute(route + suffix), { page: 'not-found' });
  assert.deepEqual(parseRoute(`#project/${projectId}/results/${firstId}`), { page: 'project', id: projectId, section: 'results', jobId: firstId });
});

function comparisonFixture() {
  // Counterexample: the candidate drops two hard epochs. Its own RMSE is lower,
  // while its error on the two shared epochs is higher. Values are synthetic.
  const score = (rmse, count) => ({ status: 'ok', metrics: { ate_rmse_m: rmse, orientation_rmse_deg: rmse * 2 }, counts: { expected_camera_samples: 4, estimate_samples: count, reference_supported_camera_samples: 4, evaluated_camera_samples: count }, coverage_fraction: count / 4, reference_coverage_fraction: 1, evaluated_fraction: count / 4 });
  const baseline = { ...jobs[0], specification_sha256: 'baseline-spec', investigation: { available: true, run_state: 'complete' }, own_support: score(Math.sqrt(50.5), 4), recorded_full_run: score(Math.sqrt(50.5), 4), qualification: { passed: true }, execution: { native_wall_seconds: 20, native_cpu_seconds: 18, native_peak_rss_kib: 1024, container_wall_seconds: 21 }, initialization_delay_s: 0, attempt_count: 1 };
  const candidate = { ...jobs[1], state: 'succeeded', failure: null, parameters: { num_pts: 100 }, specification_sha256: 'candidate-spec', investigation: { available: true, run_state: 'complete' }, own_support: score(2, 2), recorded_full_run: score(2, 2), qualification: { passed: false }, execution: { native_wall_seconds: 10, native_cpu_seconds: 9, native_peak_rss_kib: 512, container_wall_seconds: 11 }, initialization_delay_s: .1, attempt_count: 1 };
  const analysis = { schema_version: 1, kind: 'configuration_variant', baseline, candidate, configuration_changes: [{ parameter: 'num_pts', label: 'Feature target', baseline: 200, candidate: 100, unit: 'features' }], interval: { start_time_s: 0, end_time_s: .15, first_camera_index: 0, last_camera_index: 3, start_timestamp_ns: '1520530308199447626', end_timestamp_ns: '1520530308349452374', boundary: 'closed' }, common_support: { status: 'ok', expected_camera_samples: 4, evaluated_camera_samples: 2, fraction_of_expected: .5, baseline_metrics: { ate_rmse_m: 1, orientation_rmse_deg: 2 }, candidate_metrics: { ate_rmse_m: 2, orientation_rmse_deg: 4 }, delta_candidate_minus_baseline: { ate_rmse_m: 1, orientation_rmse_deg: 2 } }, support_difference: null, observed_deltas: { native_wall_seconds: -10, native_cpu_seconds: -9, native_peak_rss_kib: -512, initialization_delay_s: .1 }, warnings: [{ code: 'different_support', message: 'The runs cover different samples.' }], protocol: { ranking: false }, provenance: {} };
  return { id: '10000000-0000-4000-8000-000000000003', project_id: projectId, name: 'Synthetic dropped-support comparison', description: 'Renderer fixture, not observed estimator performance.', created_at: '2026-10-19T11:00:00Z', baseline_job_id: firstId, candidate_job_id: secondId, analysis_sha256: 'analysis-sha', baseline_access: { exists: true, investigation_available: true }, candidate_access: { exists: true, investigation_available: true }, analysis };
}

test('lower own-support error does not hide worse shared-support error, lost coverage or failed qualification', () => {
  const html = renderRunComparison(comparisonFixture());
  const own = html.split('id="own-support-heading"')[1].split('id="common-support-heading"')[0];
  const common = html.split('id="common-support-heading"')[1].split('id="host-cost-heading"')[0];
  assert.match(own, /7\.10634/);
  assert.match(own, /2\.00000/);
  assert.match(common, /1\.00000/);
  assert.match(common, /\+1\.00000/);
  assert.match(html, /50\.00%/);
  assert.match(html, /Integration qualification: did not pass/);
  assert.match(html, /not evidence of a better configuration/);
  assert.doesNotMatch(html, /winner|best configuration|improvement guaranteed/i);
});

test('recorded full-run metrics never fill unavailable interval or common-support metrics', () => {
  const item = comparisonFixture();
  item.analysis.candidate.own_support = null;
  item.analysis.candidate.investigation = { available: false, unavailable_reason: 'expired' };
  item.candidate_access.investigation_available = false;
  item.analysis.common_support = { status: 'investigation_unavailable', reason: 'Both retained investigations are required.', baseline_metrics: null, candidate_metrics: null, delta_candidate_minus_baseline: null };
  const html = renderRunComparison(item);
  assert.match(html, /Recorded full-run summary; detailed evidence was unavailable when saved/);
  const common = html.split('id="common-support-heading"')[1].split('id="host-cost-heading"')[0];
  assert.doesNotMatch(common, /2\.00000|7\.10634/);
  assert.match(common, /Unavailable/);
  assert.match(html, /Missing evidence does not imply missing estimator output/);
});

test('repeated executions expose raw observation differences without claiming pooled uncertainty', () => {
  const item = comparisonFixture();
  item.analysis.kind = 'repeated_execution';
  item.analysis.configuration_changes = [];
  const html = renderRunComparison(item);
  assert.match(html, /Two distinct run requests share the same saved specification/);
  assert.match(html, /without aggregation or confidence bounds/);
  assert.match(html, /No supported parameter changes/);
  assert.match(html, /-10\.000/);
});

test('partial failures, unavailable numbers and user-provided failure text remain explicit and escaped', () => {
  const item = comparisonFixture();
  item.analysis.candidate.state = 'failed';
  item.analysis.candidate.failure = { code: 'no_output', message: '<img src=x onerror=alert(1)>' };
  item.analysis.candidate.investigation.run_state = 'partial';
  item.analysis.candidate.own_support.metrics = null;
  item.analysis.candidate.execution = null;
  item.analysis.candidate.qualification = null;
  item.analysis.common_support = { status: 'no_common_evaluated_samples', baseline_metrics: null, candidate_metrics: null, delta_candidate_minus_baseline: null };
  const html = renderRunComparison(item);
  assert.match(html, /Partial evidence from an unsuccessful execution/);
  assert.match(html, /Failed/);
  assert.match(html, /&lt;img/);
  assert.doesNotMatch(html, /<img|NaN|undefined/);
  const common = html.split('id="common-support-heading"')[1].split('id="host-cost-heading"')[0];
  assert.doesNotMatch(common, /0\.00000/);
});


test('contextual protocol warnings stay in provenance while support and failure warnings remain prominent', () => {
  const item = comparisonFixture();
  item.analysis.warnings.push({ code: 'global_alignment_retained', message: 'Alignment detail in provenance.' }, { code: 'candidate_not_succeeded', message: 'Candidate did not succeed.' });
  const html = renderRunComparison(item);
  const prominent = html.split('class="comparison-warnings"')[1].split('class="comparison-run-grid"')[0];
  assert.match(prominent, /different samples|Candidate did not succeed/);
  assert.doesNotMatch(prominent, /Alignment detail in provenance/);
  assert.match(html.split('class="comparison-provenance"')[1], /Alignment detail in provenance/);
});
