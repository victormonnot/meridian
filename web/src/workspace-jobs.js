import { escapeHtml as e } from './workspace-api.js';
import { experimentPath } from './workspace-experiments.js';
import { renderRunExport } from './workspace-exports.js';

export const activeJob = job => ['queued', 'running', 'evaluating'].includes(job.state);
export const jobPath = (projectId, jobId) => `#project/${projectId}/results/${jobId}`;
const number = (value, digits = 2) => typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '—';
const percent = value => typeof value === 'number' && Number.isFinite(value) ? `${(value * 100).toFixed(2)}%` : '—';
const time = value => {
  if (!value) return '—';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? '—' : new Intl.DateTimeFormat('en', { dateStyle: 'medium', timeStyle: 'medium' }).format(parsed);
};

export function jobStatus(job) {
  if (activeJob(job) && job.cancel_requested) return 'Cancellation requested';
  return ({ queued: 'Queued', running: 'Running', evaluating: 'Evaluating', succeeded: 'Succeeded', failed: 'Failed', cancelled: 'Cancelled' })[job.state] || 'Unknown state';
}

function badge(job) {
  const kind = activeJob(job) ? 'active' : job.state === 'succeeded' ? 'complete' : job.state === 'failed' ? 'failed' : 'stopped';
  return `<span class="job-status job-status-${kind}">${e(jobStatus(job))}</span>`;
}

export function renderJobRows(jobs) {
  return `<div class="job-list">${jobs.map(job => `<a class="job-row" href="${e(jobPath(job.project_id, job.id))}"><div><h3>${e(job.experiment_name)}</h3><p class="muted">Version ${e(job.revision_version)} · ${e(time(job.created_at))}</p><span class="mono job-id">${e(job.id)}</span></div><div>${badge(job)}<p class="muted">${e(job.phase)} · attempt ${e(job.attempt_count)} / ${e(job.max_attempts)}</p></div><span class="row-arrow" aria-hidden="true">→</span></a>`).join('')}</div>`;
}

export function renderRevisionJobs(view, execution, editing = false) {
  const jobs = view.jobs.filter(job => job.experiment_id === view.experiment.id && job.revision_version === view.revision.version);
  return `<section class="revision-jobs" aria-labelledby="revision-jobs-heading"><div class="section-heading"><div><p class="eyebrow">Execution history</p><h3 id="revision-jobs-heading">Runs for version ${view.revision.version}</h3></div>${execution && !editing ? `<button type="button" class="primary" data-action="launch-job">Run version ${view.revision.version}</button>` : ''}</div>
    <p class="muted">${editing ? 'Save or cancel your draft before launching a saved version.' : execution ? 'Each run uses this exact saved specification. You can leave the page while computation continues.' : 'New computation is disabled on this deployment. Existing runs remain available.'}</p>
    <p id="job-submission-status" class="job-feedback" role="status"></p><div id="job-service">${renderService(view.jobService)}</div><div id="revision-job-list">${jobs.length ? renderJobRows(jobs) : '<p class="muted job-empty">No runs for this saved version.</p>'}</div>${renderLimits(view.jobLimits)}</section>`;
}

export function renderService(service) {
  if (!service) return '';
  const description = !service.enabled ? 'New computation is disabled on this deployment.'
    : !service.worker_available ? 'Waiting for the execution service. Queued runs remain saved.'
      : !service.capacity_available ? 'Waiting for available host resources. Queued runs remain saved.'
        : 'The execution service is available. Runs share the deployment’s execution capacity.';
  return `<p class="muted job-service">${description}</p>`;
}

export function renderLimits(limits) {
  if (!limits) return '';
  const budget = Number.isSafeInteger(limits.submissions_per_account_24h) ? ` Submission budget: ${e(limits.submissions_per_account_24h)} new runs per account${Number.isSafeInteger(limits.submissions_global_24h) ? ` and ${e(limits.submissions_global_24h)} across the deployment` : ''} in each fixed 24-hour window; deleting runs does not reset it.` : '';
  const retention = Number.isSafeInteger(limits.artifact_retention_days) ? ` Private artifact files are retained for ${e(limits.artifact_retention_days)} days after publication. Run metadata and saved metrics remain until the run, experiment or project is deleted.` : '';
  return `<p class="muted job-limits">Limits: ${e(limits.active_per_account)} active and ${e(limits.retained_per_account)} retained runs per account · ${e(limits.concurrency_global)} running at a time across this deployment.${budget}${retention}</p>`;
}

export function renderJobList(view) {
  return `<section aria-labelledby="jobs-heading"><div class="section-heading"><div><p class="eyebrow">Persisted execution history</p><h2 id="jobs-heading">Project runs</h2><p class="muted">Every run belongs to an immutable experiment version.</p></div></div><div id="job-service">${renderService(view.service)}</div>${view.jobs.length ? renderJobRows(view.jobs) : '<div class="empty"><h2>No results in this project</h2><p>No runs have been submitted in this project. Choose a saved experiment version to start a run when computation is enabled. The local reference qualification is separate from project results.</p></div>'}${renderLimits(view.limits)}</section>`;
}

function resultSummary(result, job) {
  const evaluation = result.evaluation;
  const metrics = evaluation.metrics || {};
  const counts = evaluation.counts || {};
  const passed = result.qualification.passed;
  const retention = job.artifacts_available === false ? 'Private artifact files are no longer retained. Saved metrics and provenance remain available.' : job.artifacts_available === true ? `Private artifact files are retained until ${time(job.artifacts_expire_at)}. Saved metrics remain after file expiry.` : '';
  return `<section class="job-result" aria-labelledby="job-result-heading"><div class="section-heading"><div><p class="eyebrow">Published result</p><h3 id="job-result-heading">Validated output and evaluation</h3></div></div>
    ${retention ? `<p class="muted job-retention">${e(retention)}</p>` : ''}
    <p class="qualification-note ${passed ? '' : 'error'}">${passed ? 'Integration qualification passed.' : 'Integration qualification did not pass.'} Job success confirms validated output structure and provenance; numerical qualification is a separate check.</p>
    <dl class="job-metrics"><div><dt>ATE RMSE</dt><dd>${number(metrics.ate_rmse_m, 5)} <span>m</span></dd></div><div><dt>Orientation RMSE</dt><dd>${number(metrics.orientation_rmse_deg, 3)} <span>deg</span></dd></div><div><dt>First estimate delay</dt><dd>${number(evaluation.initialization_delay_s, 3)} <span>s</span></dd></div><div><dt>Estimate coverage</dt><dd>${percent(evaluation.coverage_fraction)}</dd></div><div><dt>Evaluated coverage</dt><dd>${percent(evaluation.evaluated_fraction)}</dd></div><div><dt>Reference coverage</dt><dd>${percent(evaluation.reference_coverage_fraction)}</dd></div></dl>
    <p class="muted">Coverage uses all ${e(counts.expected_camera_samples ?? '—')} expected camera samples: ${e(counts.estimate_samples ?? '—')} estimated, ${e(counts.evaluated_camera_samples ?? '—')} evaluated, ${e(counts.reference_supported_camera_samples ?? '—')} supported by reference. Missing estimates and missing reference support remain distinct.</p>
    <p class="section-note">Evaluation: ${e(evaluation.protocol.name)} v${e(evaluation.protocol.version)} · global SE(3) alignment, without fitted scale or time offset. Errors describe the evaluated samples of this handheld recording, not flight performance.</p>
    <details><summary>Qualification, protocol and resource evidence</summary><dl class="details-list"><div><dt>Evaluation status</dt><dd>${e(evaluation.status)}</dd></div><div><dt>Container wall time</dt><dd>${number(result.execution.container_wall_seconds, 3)} s</dd></div><div><dt>Native wall / CPU time</dt><dd>${number(result.execution.native_wall_seconds, 3)} / ${number(result.execution.native_cpu_seconds, 3)} s</dd></div><div><dt>Native peak RSS</dt><dd>${number(result.execution.native_peak_rss_kib, 0)} KiB</dd></div><div><dt>Published artifacts</dt><dd>${e(result.artifacts.file_count)} files · ${e(result.artifacts.total_bytes)} bytes</dd></div><div><dt>Manifest SHA-256</dt><dd class="mono">${e(result.artifacts.manifest_sha256)}</dd></div></dl><pre class="job-json" tabindex="0" aria-label="Published evaluation evidence">${e(JSON.stringify({ qualification: result.qualification, protocol: evaluation.protocol }, null, 2))}</pre></details></section>`;
}

export function renderJobDetail(job) {
  const active = activeJob(job);
  return `<section class="job-detail" aria-labelledby="job-heading"><a class="back-link" href="#project/${e(job.project_id)}/results">← All project runs</a><div class="section-heading"><div><p class="eyebrow">VIO execution · version ${e(job.revision_version)}</p><h2 id="job-heading">${e(job.experiment_name)}</h2><p class="mono job-id">${e(job.id)}</p></div><div class="job-actions">${active && !job.cancel_requested ? '<button type="button" class="danger" data-action="cancel-job">Cancel run</button>' : ''}${!active ? `<a class="button primary" href="${e(jobPath(job.project_id, job.id))}/investigation">Investigate run</a><button type="button" class="danger" data-action="delete-job">Delete run</button>` : ''}</div></div>
    <div class="surface job-progress"><div class="job-state-line">${badge(job)}<span class="muted">Phase: ${e(job.phase)}</span><span class="muted">Attempt ${e(job.attempt_count)} / ${e(job.max_attempts)}</span></div><p class="muted">${active ? job.cancel_requested ? 'Cancellation is awaiting worker acknowledgement. Outputs will not be published after cancellation is accepted.' : job.state === 'queued' ? 'Waiting for a worker. This run remains queued when you leave the page.' : job.state === 'evaluating' ? 'The estimator has finished. Output validation and independent reference evaluation are in progress.' : 'The worker is preparing or executing the saved specification.' : job.state === 'cancelled' ? 'This run was cancelled. No successful result was published.' : job.state === 'failed' ? 'This run ended without a published successful result.' : 'Computation and output validation are complete.'}</p></div>
    ${job.failure ? `<div class="job-failure error" role="alert"><h3>${e(job.failure.code)}</h3><p>${e(job.failure.message)}</p></div>` : ''}
    <dl class="details-list job-identity"><div><dt>Saved configuration</dt><dd><a href="${e(experimentPath(job.project_id, job.experiment_id, job.revision_version))}">Version ${e(job.revision_version)} · ${e(job.experiment_name)}</a><p class="muted">Feature budget: ${e(job.parameters.num_pts)}</p></dd></div><div><dt>Specification SHA-256</dt><dd class="mono">${e(job.specification_sha256)}</dd></div><div><dt>Submitted</dt><dd>${e(time(job.created_at))}</dd></div><div><dt>Started</dt><dd>${e(time(job.started_at))}</dd></div><div><dt>Finished</dt><dd>${e(time(job.finished_at))}</dd></div></dl>
    ${job.state === 'succeeded' && job.result ? resultSummary(job.result, job) : ''}
    ${renderRunExport(job)}
    ${job.attempts?.length ? `<details class="job-attempts"><summary>Execution attempts (${job.attempts.length})</summary><ol>${job.attempts.map(attempt => `<li><div><strong>Attempt ${e(attempt.number)}</strong> · ${e(attempt.state)}</div><p class="muted">${e(time(attempt.started_at))} → ${e(time(attempt.finished_at))}</p>${attempt.failure ? `<p class="muted">${e(attempt.failure.code)}: ${e(attempt.failure.message)}</p>` : ''}</li>`).join('')}</ol></details>` : ''}</section>`;
}

export function jobActionDialog(job, action) {
  const cancel = action === 'cancel';
  return `<dialog id="job-action-dialog" aria-labelledby="job-action-heading"><h2 id="job-action-heading">${cancel ? 'Cancel this run?' : 'Delete this run?'}</h2><p>${cancel ? 'Queued work is cancelled immediately. Active work stops after the worker acknowledges the request. The saved experiment version is preserved.' : 'Remove this run and its recorded outcome. Artifact files are cleaned up asynchronously. The saved experiment version is preserved. Saved comparisons keep their snapshots until deleted separately.'}</p><form id="${action}-job-form" data-job-id="${e(job.id)}"><fieldset><p class="form-error" role="alert"></p><div class="form-actions"><button type="button" data-action="close-dialog" autofocus>Keep run</button><button type="submit" class="danger">${cancel ? 'Confirm cancellation' : 'Delete run'}</button></div></fieldset></form></dialog>`;
}

// One request at a time. Stopping invalidates late responses as well as timers.
export function createJobPoller({ read, onValue, onError, shouldContinue, isVisible = () => true, delay = 2500, setTimer = setTimeout, clearTimer = clearTimeout }) {
  let timer;
  let stopped = false;
  const controller = new AbortController();
  const schedule = () => { if (!stopped) timer = setTimer(tick, delay); };
  async function tick() {
    if (stopped) return;
    if (!isVisible()) { schedule(); return; }
    try {
      const value = await read(controller.signal);
      if (stopped) return;
      onValue(value);
      if (!shouldContinue(value)) { stop(); return; }
    } catch (error) {
      if (stopped || error.name === 'AbortError') return;
      if (onError(error) === false) { stop(); return; }
    }
    schedule();
  }
  function stop() { stopped = true; clearTimer(timer); controller.abort(); }
  schedule();
  return { stop };
}
