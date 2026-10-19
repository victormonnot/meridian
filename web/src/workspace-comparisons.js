import { ApiError, escapeHtml as e } from './workspace-api.js';
import { jobPath, jobStatus } from './workspace-jobs.js';

export const comparisonPath = (projectId, id) => `#project/${projectId}/results/comparisons/${id}`;
export const terminalRuns = jobs => jobs.filter(job => ['succeeded', 'failed', 'cancelled'].includes(job.state));
const time = value => {
  const parsed = new Date(value);
  return value && !Number.isNaN(parsed.getTime()) ? new Intl.DateTimeFormat('en', { dateStyle: 'medium', timeStyle: 'medium' }).format(parsed) : '—';
};
const kindLabel = kind => ({ repeated_execution: 'Repeated execution', configuration_variant: 'Configuration variant', incompatible: 'Incompatible inputs' })[kind] || 'Saved comparison';
const parameterText = parameters => Object.entries(parameters || {}).map(([key, value]) => `${key}: ${value}`).join(' · ') || 'No parameters';

export function comparisonPayload(values, jobs) {
  const available = terminalRuns(jobs);
  const fields = {};
  if (!values.name?.trim() || values.name.trim().length > 100) fields.name = ['Use a name between 1 and 100 characters.'];
  if ((values.description || '').length > 2000) fields.description = ['Use at most 2000 characters.'];
  for (const key of ['baseline_job_id', 'candidate_job_id']) if (!available.some(job => job.id === values[key])) fields[key] = ['Choose a terminal run in this project.'];
  if (values.baseline_job_id && values.baseline_job_id === values.candidate_job_id) fields.candidate_job_id = ['Choose a different exact run. Repeated executions may use the same configuration.'];
  if (Object.keys(fields).length) throw new ApiError(400, { error: { code: 'validation_error', message: 'Check the comparison selection.', fields } });
  return { schema_version: 1, name: values.name.trim(), description: values.description || '', baseline_job_id: values.baseline_job_id, candidate_job_id: values.candidate_job_id };
}

export function selectionSummary(job) {
  if (!job) return '<p class="muted">Select an exact run.</p>';
  return `<p><strong>${e(job.experiment_name)}</strong> · version ${e(job.revision_version)}</p><p class="muted">${e(jobStatus(job))} · ${e(parameterText(job.parameters))}</p><p class="muted">${e(time(job.created_at))}</p><p class="mono">${e(job.id)}</p>${job.failure ? `<p class="error">${e(job.failure.code)}: ${e(job.failure.message)}</p>` : ''}`;
}

export function createComparisonDialog(jobs) {
  const runs = terminalRuns(jobs);
  const options = runs.map(job => `<option value="${e(job.id)}">${e(job.experiment_name)} · v${e(job.revision_version)} · ${e(parameterText(job.parameters))} · ${e(jobStatus(job))} · ${e(time(job.created_at))} · ${e(job.id)}</option>`).join('');
  return `<dialog id="run-comparison-dialog" class="run-comparison-dialog" aria-labelledby="new-comparison-heading"><h2 id="new-comparison-heading">New comparison</h2><p>Save two exact executions and their evaluation evidence. The baseline establishes the direction of the differences; it is not a claim of superiority.</p><form id="create-comparison-form"><fieldset><label for="comparison-name">Comparison name<input id="comparison-name" name="name" maxlength="100" autocomplete="off" required></label><label for="comparison-description">Description <span class="muted">Optional</span><textarea id="comparison-description" name="description" maxlength="2000" rows="2"></textarea></label><div class="run-selection-grid">${[['baseline', 'Baseline'], ['candidate', 'Candidate']].map(([side, label]) => `<div><label for="${side}-job">${label} run<select id="${side}-job" name="${side}_job_id" required><option value="">Choose a terminal run</option>${options}</select></label><div class="run-selection-summary" id="${side}-selection">${selectionSummary(null)}</div></div>`).join('')}</div><p class="muted">Includes the full recording. Identical configurations remain separate runs. Compatibility and common evaluated support are checked by the service; missing evidence remains explicit.</p><p class="form-error" role="alert"></p><div class="form-actions"><button type="button" data-action="close-dialog">Cancel</button><button class="primary" type="submit">Save comparison</button></div></fieldset></form></dialog>`;
}

export function renderComparisonList(projectId, view, jobs) {
  const items = view.comparisons;
  const atLimit = items.length >= view.limits.comparisons;
  const enoughRuns = terminalRuns(jobs).length >= 2;
  return `<section id="saved-comparisons" class="saved-comparisons" aria-labelledby="saved-comparisons-heading"><div class="section-heading"><div><p class="eyebrow">Saved execution evidence</p><h2 id="saved-comparisons-heading">Saved comparisons</h2><p class="muted">Exact runs, configuration differences and evaluated support.</p></div><button type="button" class="primary" data-action="new-comparison" ${atLimit || !enoughRuns ? 'disabled' : ''}>New comparison</button></div>${items.length ? `<div class="saved-comparison-list">${items.map(item => `<a class="saved-comparison-row" href="${e(comparisonPath(projectId, item.id))}"><div><h3>${e(item.name)}</h3><p class="muted">${e(kindLabel(item.summary.kind))} · ${e(time(item.created_at))}</p>${item.description ? `<p class="muted preserve-lines">${e(item.description)}</p>` : ''}</div><div><span class="eyebrow">Baseline → candidate</span><p class="mono">${e(parameterText(item.summary.baseline.parameters))} → ${e(parameterText(item.summary.candidate.parameters))}</p><p class="muted">${e(jobStatus(item.summary.baseline))} → ${e(jobStatus(item.summary.candidate))}</p></div><span class="row-arrow" aria-hidden="true">→</span></a>`).join('')}</div>` : '<p class="muted comparison-list-empty">No comparisons saved. Select two terminal runs to preserve their configuration and outcome evidence.</p>'}<p class="muted comparison-list-limit">${items.length} / ${e(view.limits.comparisons)} saved in this project. ${atLimit ? 'Delete an unused comparison before saving another.' : !enoughRuns ? 'At least two terminal runs are required.' : 'Repeated runs are selected individually; no automatic ranking or aggregation is applied.'}</p></section>`;
}

export function deleteComparisonDialog(comparison) {
  return `<dialog id="delete-comparison-dialog" aria-labelledby="delete-comparison-heading"><h2 id="delete-comparison-heading">Delete this comparison?</h2><p>Remove ${e(comparison.name)} and its saved analysis. The original runs, experiments and their retained artifacts are preserved.</p><form id="delete-comparison-form"><fieldset><p class="form-error" role="alert"></p><div class="form-actions"><button type="button" data-action="close-dialog" autofocus>Keep comparison</button><button type="submit" class="danger">Delete comparison</button></div></fieldset></form></dialog>`;
}

export function comparisonRunLinks(projectId, jobId, access) {
  return `<div class="comparison-run-links">${access.exists ? `<a href="${e(jobPath(projectId, jobId))}">Run overview</a>` : '<span class="muted">Original run removed</span>'}${access.exists && access.investigation_available ? `<a href="${e(jobPath(projectId, jobId))}/investigation">Investigate run</a>` : '<span class="muted">Detailed evidence unavailable</span>'}</div>`;
}

const finite = value => typeof value === 'number' && Number.isFinite(value);
const number = (value, digits = 3) => finite(value) ? value.toFixed(digits) : '—';
const percent = value => finite(value) ? `${(value * 100).toFixed(2)}%` : '—';
const delta = (value, digits = 3) => finite(value) ? `${value > 0 ? '+' : ''}${value.toFixed(digits)}` : '—';
const sourceScores = run => run.own_support || run.recorded_full_run;

function table(caption, rows, withDelta = false) {
  return `<div class="comparison-table-scroll" tabindex="0" aria-label="${e(caption)}"><table class="comparison-table run-comparison-table"><caption>${e(caption)}</caption><thead><tr><th scope="col">Measurement</th><th scope="col">Baseline</th><th scope="col">Candidate</th>${withDelta ? '<th scope="col">Candidate − baseline</th>' : ''}</tr></thead><tbody>${rows.map(([label, baseline, candidate, difference]) => `<tr><th scope="row">${e(label)}</th><td>${e(baseline)}</td><td>${e(candidate)}</td>${withDelta ? `<td>${e(difference ?? '—')}</td>` : ''}</tr>`).join('')}</tbody></table></div>`;
}

function runCard(comparison, side, label) {
  const run = comparison.analysis[side];
  const access = comparison[`${side}_access`];
  const scores = sourceScores(run);
  return `<section class="surface comparison-run-card" aria-labelledby="${side}-run-heading"><p class="eyebrow">${label} · version ${e(run.revision_version)}</p><h3 id="${side}-run-heading">${e(run.experiment_name)}</h3><div class="comparison-run-state"><span class="pill">${e(jobStatus(run))}</span><span class="mono">${e(parameterText(run.parameters))}</span></div><p class="mono job-id">${e(run.id)}</p>${comparisonRunLinks(comparison.project_id, comparison[`${side}_job_id`], access)}${run.failure ? `<p class="comparison-failure error"><strong>${e(run.failure.code)}</strong> · ${e(run.failure.message)}</p>` : ''}<p class="muted">Integration qualification: ${run.qualification ? run.qualification.passed ? 'passed' : 'did not pass' : 'unavailable'}. ${run.investigation?.run_state === 'partial' ? 'Partial evidence from an unsuccessful execution.' : ''}</p><div class="comparison-coverage"><div><span>Estimated</span><strong>${percent(scores?.coverage_fraction)}</strong></div>${coverageBar(scores?.coverage_fraction, `${label} estimate coverage`, side)}<div><span>Evaluated</span><strong>${percent(scores?.evaluated_fraction)}</strong></div>${coverageBar(scores?.evaluated_fraction, `${label} evaluated coverage`, side)}</div><p class="muted">${run.own_support ? 'Full-resolution evidence in the saved selection.' : run.recorded_full_run ? 'Recorded full-run summary; detailed evidence was unavailable when saved.' : 'No numerical result was available when saved.'}</p></section>`;
}

function coverageBar(fraction, label, side) {
  if (!finite(fraction)) return '<p class="muted">Coverage unavailable</p>';
  const width = Math.max(0, Math.min(1, fraction)) * 100;
  return `<svg viewBox="0 0 100 5" role="img" aria-label="${e(label)}: ${percent(fraction)}" class="comparison-coverage-bar"><rect class="coverage-track" width="100" height="5"/><rect class="coverage-${side}" width="${width}" height="5"/></svg>`;
}

function ownSupport(analysis) {
  const baseline = sourceScores(analysis.baseline);
  const candidate = sourceScores(analysis.candidate);
  const rows = [
    ['Metric source', analysis.baseline.own_support ? 'Saved interval · full resolution' : baseline ? 'Recorded full run' : 'Unavailable', analysis.candidate.own_support ? 'Saved interval · full resolution' : candidate ? 'Recorded full run' : 'Unavailable'],
    ['Expected camera epochs', baseline?.counts?.expected_camera_samples ?? '—', candidate?.counts?.expected_camera_samples ?? '—'],
    ['Estimated epochs', baseline?.counts?.estimate_samples ?? '—', candidate?.counts?.estimate_samples ?? '—'],
    ['Reference-supported epochs', baseline?.counts?.reference_supported_camera_samples ?? '—', candidate?.counts?.reference_supported_camera_samples ?? '—'],
    ['Evaluated epochs', baseline?.counts?.evaluated_camera_samples ?? '—', candidate?.counts?.evaluated_camera_samples ?? '—'],
    ['ATE RMSE · m', number(baseline?.metrics?.ate_rmse_m, 5), number(candidate?.metrics?.ate_rmse_m, 5)],
    ['Orientation RMSE · deg', number(baseline?.metrics?.orientation_rmse_deg), number(candidate?.metrics?.orientation_rmse_deg)],
    ['Reference coverage', percent(baseline?.reference_coverage_fraction), percent(candidate?.reference_coverage_fraction)],
  ];
  return `<section class="comparison-section" aria-labelledby="own-support-heading"><div class="section-heading"><div><p class="eyebrow">Each run’s own support</p><h3 id="own-support-heading">Outcome and coverage</h3></div></div><p class="muted">These errors can use different camera epochs. A lower error over fewer estimates is not evidence of a better configuration.</p>${table('Own-support metrics and their denominators', rows)}</section>`;
}

function commonSupport(analysis) {
  const support = analysis.common_support;
  const available = support.status === 'ok';
  const rows = [
    ['ATE RMSE · m', 'ate_rmse_m', 5],
    ['Position error mean · m', 'position_error_mean_m', 5],
    ['Position error max · m', 'position_error_max_m', 5],
    ['Orientation RMSE · deg', 'orientation_rmse_deg', 3],
    ['Orientation error mean · deg', 'orientation_error_mean_deg', 3],
    ['Orientation error max · deg', 'orientation_error_max_deg', 3],
  ].map(([label, key, digits]) => [label, number(support.baseline_metrics?.[key], digits), number(support.candidate_metrics?.[key], digits), delta(support.delta_candidate_minus_baseline?.[key], digits)]);
  return `<section class="comparison-section" aria-labelledby="common-support-heading"><div class="section-heading"><div><p class="eyebrow">Identical evaluated camera epochs</p><h3 id="common-support-heading">Common-support errors</h3></div><span class="pill">${available ? `${e(support.evaluated_camera_samples)} shared epochs` : 'Unavailable'}</span></div><p class="muted">${available ? `${e(support.evaluated_camera_samples)} / ${e(support.expected_camera_samples)} expected epochs (${percent(support.fraction_of_expected)}).` : e(support.reason || String(support.status).replaceAll('_', ' '))} Each run retains its original full-recording global SE(3) alignment. The common subset is not realigned.</p>${table('Full-resolution errors on the common evaluated support', rows, true)}<p class="section-note">Signed differences are candidate minus baseline. They describe these exact executions; coverage, failures and host conditions remain part of the assessment.</p></section>`;
}

function supportRegions(analysis) {
  const difference = analysis.support_difference;
  if (!difference) return '<section class="comparison-section"><h3>Support differences unavailable</h3><p class="muted">Both full-resolution investigation artifacts are required to identify common and exclusive camera epochs. Missing evidence does not imply missing estimator output.</p></section>';
  const labels = { both: 'Both runs', baseline_only: 'Baseline only', candidate_only: 'Candidate only', neither: 'Neither run' };
  const first = analysis.interval.first_camera_index;
  const last = analysis.interval.last_camera_index;
  const span = Number.isSafeInteger(first) && Number.isSafeInteger(last) ? Math.max(1, last - first + 1) : 1;
  return `<section class="comparison-section" aria-labelledby="support-difference-heading"><div class="section-heading"><div><p class="eyebrow">Common and exclusive support</p><h3 id="support-difference-heading">Where the runs differ</h3></div></div><p class="muted">Every expected epoch remains accounted for. Missing estimates and reference eligibility are separate from numerical error.</p>${Object.entries(difference).map(([type, groups]) => `<div class="comparison-support-row"><h4>${type === 'estimated' ? 'Estimated output' : 'Evaluated errors'}</h4><svg viewBox="0 0 1000 20" role="img" aria-label="${type === 'estimated' ? 'Estimated' : 'Evaluated'} support by camera epoch" class="comparison-support-track">${Object.entries(groups).flatMap(([key, group]) => group.intervals.map(region => `<rect class="support-${e(key)}" x="${((region.first_camera_index - first) / span) * 1000}" width="${((region.last_camera_index - region.first_camera_index + 1) / span) * 1000}" height="20"/>`)).join('')}</svg><div class="comparison-support-legend">${Object.entries(groups).map(([key, group]) => `<span class="support-${e(key)}">${labels[key]}: <strong>${e(group.sample_count)}</strong></span>`).join('')}</div><details><summary>Exact ${type} support regions</summary><div class="comparison-table-scroll" tabindex="0" aria-label="${type} support regions"><table class="comparison-table support-regions-table"><thead><tr><th scope="col">Support</th><th scope="col">Camera indices</th><th scope="col">Epochs</th><th scope="col">First → last timestamp · ns</th></tr></thead><tbody>${Object.entries(groups).flatMap(([key, group]) => group.intervals.map(region => `<tr><th scope="row">${labels[key]}</th><td>${e(region.first_camera_index)}–${e(region.last_camera_index)}</td><td>${e(region.sample_count)}</td><td class="mono">${e(region.start_timestamp_ns)} → ${e(region.end_timestamp_ns)}</td></tr>`)).join('')}</tbody></table></div></details></div>`).join('')}</section>`;
}

function hostCosts(analysis) {
  const baseline = analysis.baseline;
  const candidate = analysis.candidate;
  const changes = analysis.observed_deltas || {};
  const rows = [
    ['First estimate delay · s (full recording)', number(baseline.initialization_delay_s), number(candidate.initialization_delay_s), delta(changes.initialization_delay_s)],
    ['Native wall time · s', number(baseline.execution?.native_wall_seconds), number(candidate.execution?.native_wall_seconds), delta(changes.native_wall_seconds)],
    ['Native CPU time · s', number(baseline.execution?.native_cpu_seconds), number(candidate.execution?.native_cpu_seconds), delta(changes.native_cpu_seconds)],
    ['Native peak RSS · KiB', number(baseline.execution?.native_peak_rss_kib, 0), number(candidate.execution?.native_peak_rss_kib, 0), delta(changes.native_peak_rss_kib, 0)],
    ['Container wall time · s', number(baseline.execution?.container_wall_seconds), number(candidate.execution?.container_wall_seconds), '—'],
    ['Execution attempts', baseline.attempt_count ?? '—', candidate.attempt_count ?? '—', '—'],
  ];
  return `<section class="comparison-section" aria-labelledby="host-cost-heading"><div class="section-heading"><div><p class="eyebrow">Initialization and measured execution cost</p><h3 id="host-cost-heading">Host observations</h3></div></div>${table('Recorded initialization and host resource measurements', rows, true)}<p class="section-note">Native and container durations exclude other parts of the job lifecycle. Host contention, retries and scheduling can affect observations. These measurements do not establish embedded, flight or real-time performance.</p></section>`;
}

export function renderRunComparison(comparison) {
  const analysis = comparison.analysis;
  const repeated = analysis.kind === 'repeated_execution';
  const contextualWarnings = new Set(['global_alignment_retained', 'no_automatic_ranking', 'host_cost_not_controlled', 'two_run_repeat_only']);
  const prominentWarnings = analysis.warnings.filter(warning => !contextualWarnings.has(warning.code));
  return `<section class="run-comparison" aria-labelledby="run-comparison-heading"><a class="back-link" href="#project/${e(comparison.project_id)}/results">← Project results</a><div class="section-heading"><div><p class="eyebrow">Saved comparison · ${e(kindLabel(analysis.kind))}</p><h2 id="run-comparison-heading">${e(comparison.name)}</h2><p class="muted">Saved ${e(time(comparison.created_at))}</p></div><button type="button" class="danger" data-action="delete-comparison">Delete comparison</button></div>${comparison.description ? `<p class="comparison-description preserve-lines">${e(comparison.description)}</p>` : ''}<p class="section-note">${repeated ? 'Two distinct run requests share the same saved specification. Available observations describe these exact runs, without aggregation or confidence bounds.' : 'These runs satisfy the saved comparison contract. Configuration changes, support, failures and host observations are retained together.'} This snapshot remains after the original runs are removed or their detailed files expire.</p>${prominentWarnings.length ? `<div class="comparison-warnings" role="status"><h3>Evidence to consider</h3><ul>${prominentWarnings.map(warning => `<li>${e(warning.message)}</li>`).join('')}</ul></div>` : ''}<div class="comparison-run-grid">${runCard(comparison, 'baseline', 'Baseline')}${runCard(comparison, 'candidate', 'Candidate')}</div><section class="comparison-section" aria-labelledby="configuration-difference-heading"><div class="section-heading"><div><p class="eyebrow">Saved estimator configuration</p><h3 id="configuration-difference-heading">Configuration difference</h3></div></div>${analysis.configuration_changes.length ? table('Exact supported parameter changes', analysis.configuration_changes.map(change => [`${change.label} (${change.parameter})${change.unit ? ` · ${change.unit}` : ''}`, change.baseline, change.candidate])) : '<p class="muted">No supported parameter changes. The two execution identities remain distinct.</p>'}<p class="muted comparison-interval">Selected interval: ${number(analysis.interval.start_time_s)}–${number(analysis.interval.end_time_s)} s · camera epochs ${e(analysis.interval.first_camera_index ?? 'none')}–${e(analysis.interval.last_camera_index ?? 'none')} · inclusive bounds.</p></section>${ownSupport(analysis)}${commonSupport(analysis)}${supportRegions(analysis)}${hostCosts(analysis)}<details class="comparison-provenance"><summary>Exact identities, compatibility contract and provenance</summary><p class="muted">Numerical scores are computed by the service. Full-resolution scoring, compatibility checks and the saved analysis identity come from the service.</p><dl class="details-list"><div><dt>Analysis SHA-256</dt><dd class="mono">${e(comparison.analysis_sha256)}</dd></div>${['baseline', 'candidate'].map(side => `<div><dt>${side === 'baseline' ? 'Baseline' : 'Candidate'} specification SHA-256</dt><dd class="mono">${e(analysis[side].specification_sha256)}</dd></div>`).join('')}</dl><pre class="job-json" tabindex="0" aria-label="Comparison protocol and provenance">${e(JSON.stringify({ warnings: analysis.warnings, protocol: analysis.protocol, interval: analysis.interval, provenance: analysis.provenance, baseline: { started_at: analysis.baseline.started_at, finished_at: analysis.baseline.finished_at, published_attempt: analysis.baseline.published_attempt, investigation: analysis.baseline.investigation, specification: analysis.baseline.specification, evidence_provenance: analysis.baseline.evidence_provenance }, candidate: { started_at: analysis.candidate.started_at, finished_at: analysis.candidate.finished_at, published_attempt: analysis.candidate.published_attempt, investigation: analysis.candidate.investigation, specification: analysis.candidate.specification, evidence_provenance: analysis.candidate.evidence_provenance } }, null, 2))}</pre></details>${deleteComparisonDialog(comparison)}</section>`;
}
