import { ApiError, escapeHtml as e } from './workspace-api.js';

export function revisionDraft(revision, profile) {
  return {
    name: revision?.name ?? '',
    description: revision?.description ?? '',
    num_pts: String(revision?.specification?.parameters.num_pts ?? revision?.parameters?.num_pts ?? profile.parameters.num_pts.default),
  };
}

export function draftChanged(baseline, draft) {
  return ['name', 'description', 'num_pts'].some(key => String(baseline[key]) !== String(draft[key]));
}

export function experimentPayload(draft, profile, { datasetId, baseVersion } = {}) {
  const parameter = Number(draft.num_pts);
  if (!Number.isInteger(parameter) || !profile.parameters.num_pts.choices.includes(parameter)) {
    throw new ApiError(400, { error: { code: 'invalid_parameter', message: 'Choose a supported feature count.', fields: { 'parameters.num_pts': ['Choose a value from the supported list.'] } } });
  }
  return {
    schema_version: 1,
    ...(baseVersion === undefined ? { dataset_id: datasetId } : { base_version: baseVersion }),
    name: draft.name,
    description: draft.description,
    parameters: { num_pts: parameter },
  };
}

export const experimentPath = (projectId, experimentId, version) =>
  `#project/${projectId}/experiments/${experimentId}${version === undefined ? '' : `/revisions/${version}`}`;

function formatTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : new Intl.DateTimeFormat('en', { dateStyle: 'medium', timeStyle: 'short' }).format(date);
}

function fields(draft, profile, prefix) {
  const parameter = profile.parameters.num_pts;
  return `<div class="form-field"><label for="${prefix}-name">Experiment name</label><input id="${prefix}-name" name="name" value="${e(draft.name)}" maxlength="100" required autocomplete="off"><small class="field-error" data-field-error="name"></small></div>
    <div class="form-field"><label for="${prefix}-description">Description <span class="muted">Optional</span></label><textarea id="${prefix}-description" name="description" maxlength="2000" rows="3">${e(draft.description)}</textarea><small class="field-error" data-field-error="description"></small></div>
    <div class="parameter-field"><div class="form-field"><label for="${prefix}-num-pts">${e(parameter.label)}</label><select id="${prefix}-num-pts" name="num_pts" aria-describedby="${prefix}-parameter-help">${parameter.choices.map(value => `<option value="${e(value)}" ${String(value) === draft.num_pts ? 'selected' : ''}>${e(value)}${value === parameter.default ? ' · default' : ''}</option>`).join('')}</select><small class="field-error" data-field-error="parameters.num_pts"></small></div>
    <p id="${prefix}-parameter-help" class="muted">${e(parameter.description)}<span class="parameter-constraints">Unit: ${e(parameter.unit)} · Default: ${e(parameter.default)} · Supported values: ${parameter.choices.map(e).join(', ')}</span></p></div>`;
}

function limitations(profile) {
  return `<details><summary>Configuration scope and limits</summary><ul class="limitations">${profile.limitations.map(item => `<li>${e(item)}</li>`).join('')}</ul></details>`;
}

export function createExperimentDialog(project, profile, draft) {
  return `<dialog id="experiment-dialog" class="experiment-dialog" aria-labelledby="create-experiment-heading"><h2 id="create-experiment-heading">New experiment</h2><p>Create the first immutable configuration version for this project.</p>
    <p class="experiment-identity">${e(profile.estimator)} · <span class="mono">${e(project.dataset_id)}</span></p>
    <form id="create-experiment-form" data-experiment-draft><fieldset>${fields(draft, profile, 'create-experiment')}
    <p class="muted">Uses this project’s reference dataset. Saving a configuration does not execute the estimator.</p><p class="form-error" role="alert"></p>
    <div class="form-actions"><button type="button" data-action="close-dialog">Cancel</button><button class="primary" type="submit">Create experiment</button></div></fieldset></form></dialog>`;
}

export function renderExperimentList(project, view) {
  const { experiments, limits, profile } = view;
  const available = experiments.length < limits.experiments;
  return `<section aria-labelledby="experiments-heading"><div class="section-heading"><div><p class="eyebrow">Versioned configurations · ${e(profile.estimator)}</p><h2 id="experiments-heading">Experiments</h2><p class="muted">Define and compare configurations before execution.</p></div>${available ? '<button type="button" class="primary" data-action="new-experiment">New experiment</button>' : ''}</div>
    <div class="list-heading"><span class="eyebrow">${experiments.length} ${experiments.length === 1 ? 'experiment' : 'experiments'}</span><span class="muted">${limits.experiments} per project · ${limits.revisions} versions per experiment</span></div>
    ${experiments.length ? `<div class="experiment-list">${experiments.map(item => `<a class="experiment-row" href="${e(experimentPath(project.id, item.id))}"><div><h3>${e(item.name)}</h3><p class="muted">${e(item.description || 'No description')}</p></div><div><span class="eyebrow">Latest version</span><p class="mono">v${item.version}</p></div><div><span class="eyebrow">${e(profile.parameters.num_pts.label)}</span><p class="mono">${e(item.parameters.num_pts)}</p></div><span class="row-arrow" aria-hidden="true">→</span></a>`).join('')}</div>` : '<div class="empty"><h2>No experiments in this project</h2><p>Create an experiment to preserve a configuration, record revisions and compare changes.</p></div>'}
    ${!available ? '<p class="section-note">Experiment limit reached. Delete an unused experiment before creating another.</p>' : ''}
    <p class="section-note">Saving a configuration does not start computation. Open a saved version to inspect its runs${view.execution ? ' or launch a new one' : '; new computation is disabled on this deployment'}.</p>${limitations(profile)}
  </section>`;
}

export function renderComparison(comparison) {
  if (!comparison.changes.length) return '<p class="muted comparison-empty">No changes to the name, description or supported parameters between these versions.</p>';
  return `<div class="comparison-table-scroll" tabindex="0" aria-label="Version differences"><table class="comparison-table"><caption>Exact changes · v${comparison.from_version} → v${comparison.to_version}</caption><thead><tr><th scope="col">Field</th><th scope="col">v${comparison.from_version}</th><th scope="col">v${comparison.to_version}</th></tr></thead><tbody>${comparison.changes.map(change => `<tr><th scope="row" class="mono">${e(change.field)}</th><td>${e(String(change.before) || '(empty)')}</td><td>${e(String(change.after) || '(empty)')}</td></tr>`).join('')}</tbody></table></div>`;
}

export function renderConflict(latest, draft, profile) {
  const saved = revisionDraft(latest, profile);
  const changed = ['name', 'description', 'num_pts'].filter(key => saved[key] !== draft[key]);
  return `<h3>Version ${latest.version} is now the latest</h3><p>Your draft is preserved. Review the saved values before choosing a new base for your draft.</p>
    <dl class="conflict-values">${changed.map(key => `<div><dt>${e(key === 'num_pts' ? profile.parameters.num_pts.label : key)}</dt><dd><span class="muted">Saved v${latest.version}</span><span>${e(saved[key] || '(empty)')}</span><span class="muted">Your draft</span><span>${e(draft[key] || '(empty)')}</span></dd></div>`).join('') || '<div><dt>Editable values</dt><dd>Your draft already matches the latest version.</dd></div>'}</dl>
    <button type="button" data-action="rebase-experiment">Use version ${latest.version} as base</button>`;
}

export function renderExperimentDetail(project, view, draftState, runs = '') {
  const { experiment, revision, revisions, profile, limits, comparison } = view;
  const latest = revision.version === experiment.version;
  const editable = experiment.version < limits.revisions;
  const editing = Boolean(draftState?.kind === 'edit');
  const values = revisionDraft(revision, profile);
  const baseRoute = `#project/${project.id}/experiments`;
  const options = selected => revisions.map(item => `<option value="${item.version}" ${item.version === selected ? 'selected' : ''}>Version ${item.version}${item.version === experiment.version ? ' · latest' : ''}</option>`).join('');
  return `<section class="experiment-detail" aria-labelledby="experiment-heading"><a class="back-link" href="${e(baseRoute)}">← All experiments</a>
    <div class="section-heading"><div><p class="eyebrow">Experiment · ${latest ? 'Latest saved version' : 'Historical version · read only'}</p><h2 id="experiment-heading">${e(revision.name)}</h2><p class="muted">Version ${revision.version} · <time datetime="${e(revision.created_at)}">${e(formatTime(revision.created_at))}</time></p></div><div class="experiment-actions">${latest && editable && !editing ? '<button type="button" class="primary" data-action="edit-experiment">Edit configuration</button>' : ''}<button type="button" data-action="duplicate-experiment">Duplicate version ${revision.version}</button></div></div>
    <div class="experiment-identity"><span>${e(revision.specification.estimator.name)}</span><span>Dataset <span class="mono">${e(revision.specification.dataset.id)}</span></span><span>Profile <span class="mono">${e(revision.specification.profile_id)}</span></span></div>
    <div class="experiment-layout"><div class="experiment-main">
      ${editing ? `<form id="edit-experiment-form" class="surface experiment-editor" data-experiment-draft><h3>New version from v${draftState.baseVersion}</h3><p class="muted">Previous versions remain unchanged.</p><fieldset>${fields(draftState.values, profile, 'edit-experiment')}<div id="experiment-conflict" class="conflict-panel" role="alert" hidden></div><p class="form-error" role="alert"></p><div class="form-actions"><button type="button" data-action="cancel-experiment-edit">Cancel</button><button type="submit" class="primary">Save new version</button></div></fieldset></form>` : `<div class="surface experiment-configuration"><h3>Saved configuration</h3><dl class="details-list"><div><dt>Name</dt><dd>${e(revision.name)}</dd></div><div><dt>Description</dt><dd class="preserve-lines">${e(revision.description || 'No description')}</dd></div><div><dt>${e(profile.parameters.num_pts.label)}</dt><dd><span class="parameter-value mono">${e(values.num_pts)}</span> <span class="muted">${e(profile.parameters.num_pts.unit)}</span></dd></div></dl><p class="muted parameter-description">${e(profile.parameters.num_pts.description)}</p></div>`}
      ${!latest ? '<p class="section-note">This saved version is immutable. Select the latest version to edit, or duplicate this version into a separate experiment.</p>' : ''}
      ${latest && !editable ? '<p class="section-note">Version limit reached. Duplicate a saved version to continue in a separate experiment.</p>' : ''}
      ${runs}
      <section class="comparison-section" aria-labelledby="comparison-heading"><div class="section-heading"><div><p class="eyebrow">Revision history</p><h3 id="comparison-heading">Compare versions</h3></div></div><div class="comparison-controls"><div class="form-field"><label for="compare-from">From version</label><select id="compare-from">${options(comparison.from_version)}</select></div><div class="form-field"><label for="compare-to">To version</label><select id="compare-to">${options(comparison.to_version)}</select></div></div><div id="comparison-result" aria-live="polite">${renderComparison(comparison)}</div></section>
      <details class="specification-details"><summary>Immutable specification and provenance</summary><p class="muted">This descriptor records the dataset, estimator, runtime configuration, evaluation protocol and resource bounds. Run history identifies executions of this exact version.</p><dl class="details-list"><div><dt>Specification SHA-256</dt><dd class="mono">${e(revision.specification_sha256)}</dd></div>${revision.copied_from ? `<div><dt>Copied from</dt><dd>Experiment <span class="mono">${e(revision.copied_from.experiment_id)}</span> · version ${revision.copied_from.version}<p class="mono">${e(revision.copied_from.specification_sha256)}</p></dd></div>` : ''}</dl><pre tabindex="0" aria-label="Saved specification JSON">${e(JSON.stringify(revision.specification, null, 2))}</pre></details>
      ${limitations(profile)}<p class="section-note">A saved configuration alone does not imply estimator performance. Each run records its own outcome.</p>
    </div><aside class="surface revision-panel" aria-labelledby="versions-heading"><h3 id="versions-heading">Versions</h3><p class="muted">${revisions.length} / ${limits.revisions} saved</p><nav aria-label="Saved versions">${revisions.map(item => `<a href="${e(experimentPath(project.id, experiment.id, item.version))}" ${item.version === revision.version ? 'aria-current="page"' : ''}><span>Version ${item.version}${item.version === experiment.version ? ' <span class="pill">Latest</span>' : ''}</span><time datetime="${e(item.created_at)}">${e(formatTime(item.created_at))}</time><span class="mono">${e(profile.parameters.num_pts.label)}: ${e(item.parameters.num_pts)}</span></a>`).join('')}</nav><button type="button" class="danger" data-action="delete-experiment">Delete experiment</button></aside></div>
    <dialog id="duplicate-experiment-dialog" aria-labelledby="duplicate-experiment-heading"><h2 id="duplicate-experiment-heading">Duplicate version ${revision.version}</h2><p>The selected saved configuration becomes version 1 of a new experiment in this project.</p><form id="duplicate-experiment-form"><fieldset><div class="form-field"><label for="duplicate-name">Experiment name</label><input id="duplicate-name" name="name" value="${e(`${revision.name} (copy)`.slice(0, 100))}" maxlength="100" required></div><p class="form-error" role="alert"></p><div class="form-actions"><button type="button" data-action="close-dialog">Cancel</button><button type="submit" class="primary">Duplicate experiment</button></div></fieldset></form></dialog>
    <dialog id="delete-experiment-dialog" aria-labelledby="delete-experiment-heading"><h2 id="delete-experiment-heading">Delete this experiment?</h2><p>Permanently delete ${e(experiment.name)}, all ${revisions.length} saved versions and its run history? Active runs must finish or be cancelled first. Artifact cleanup is asynchronous. Saved comparisons keep their snapshots until deleted separately.</p><form id="delete-experiment-form"><fieldset><p class="form-error" role="alert"></p><div class="form-actions"><button type="button" data-action="close-dialog" autofocus>Keep experiment</button><button type="submit" class="danger">Delete experiment</button></div></fieldset></form></dialog>
  </section>`;
}
