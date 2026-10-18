import { initializeTheme } from './theme.js';
import { ApiError, createApiClient, escapeHtml as e, errorMessage, parseRoute, safeExternalLink } from './workspace-api.js';
import { revisionDraft, draftChanged, experimentPayload, experimentPath, createExperimentDialog, renderExperimentList, renderExperimentDetail, renderComparison, renderConflict } from './workspace-experiments.js';
import { activeJob, jobPath, renderJobRows, renderRevisionJobs, renderJobList, renderJobDetail, renderService, jobActionDialog, createJobPoller } from './workspace-jobs.js';
import { investigationShell, mountInvestigation } from './workspace-investigation.js';

initializeTheme();
const api = createApiClient();
const app = document.querySelector('#app');
const notice = document.querySelector('#notice');
const account = document.querySelector('#account');
const logout = document.querySelector('#logout');
let session = null;
let datasets = [];
let currentProject = null;
let authMode = 'login';
let navigation = 0;
let sessionEpoch = 0;
let controller;
let checkingSession = false;
let experimentView = null;
let draftState = null;
let renderedHash = window.location.hash;
let comparisonRequest = 0;
let jobView = null;
let jobPoller;
let investigationView;
const submissions = new Map();

const integer = value => new Intl.NumberFormat('en').format(value);
const decimal = (value, digits = 2) => Number(value).toFixed(digits);
const date = value => {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? '—' : new Intl.DateTimeFormat('en', { month: 'short', day: 'numeric', year: 'numeric' }).format(parsed);
};

function announce(message = '', isError = false) {
  notice.textContent = message;
  notice.hidden = !message;
  notice.className = isError ? 'error' : '';
  notice.setAttribute('role', isError ? 'alert' : 'status');
}

function setSession(value) {
  jobPoller?.stop();
  investigationView?.destroy();
  submissions.clear();
  sessionEpoch += 1;
  session = value;
  authMode = 'login';
  api.setCsrfToken(value.csrf_token);
  account.textContent = value.user?.username || '';
  account.hidden = !value.user;
  logout.hidden = !value.user;
  datasets = [];
  currentProject = null;
  experimentView = null;
  draftState = null;
  jobView = null;
}

function removePrivateState() {
  navigation += 1;
  sessionEpoch += 1;
  controller?.abort();
  jobPoller?.stop();
  investigationView?.destroy();
  submissions.clear();
  datasets = [];
  currentProject = null;
  experimentView = null;
  draftState = null;
  jobView = null;
  app.replaceChildren();
  account.textContent = '';
  account.hidden = true;
  logout.hidden = true;
}

function mount(html, title, focus = false) {
  app.innerHTML = html;
  document.title = `${title} — Meridian`;
  if (focus) {
    const heading = app.querySelector('h1');
    if (heading) { heading.tabIndex = -1; heading.focus({ preventScroll: true }); }
  }
}

function renderAuth() {
  const register = authMode === 'register' && session?.registration_open !== false;
  mount(`<div class="auth-layout">
    <section class="auth-intro" aria-labelledby="workspace-heading">
      <p class="eyebrow">Visual-inertial estimation</p>
      <h1 id="workspace-heading">Your engineering workspace.</h1>
      <p>Organize private projects and versioned estimator configurations around calibrated stereo and IMU recordings.</p>
      <ul class="scope-list"><li><span>Private projects</span><span>Available</span></li><li><span>Reference catalog</span><span>TUM-VI</span></li><li><span>Versioned configurations</span><span>Available</span></li><li><span>VIO computation</span><span>${session?.capabilities.execution ? 'Enabled' : 'Disabled on this deployment'}</span></li></ul>
    </section>
    <section class="surface auth-panel" aria-labelledby="auth-heading">
      <h2 id="auth-heading">${register ? 'Create an account' : 'Sign in'}</h2>
      <form id="auth-form"><fieldset>
        <label for="username">Username<input id="username" name="username" autocomplete="username" autocapitalize="none" spellcheck="false" maxlength="150" required ${register ? 'pattern="[a-zA-Z0-9.@+_\\-]+" aria-describedby="username-help"' : ''}></label>
        ${register ? '<small id="username-help">Letters, numbers and . @ + - _ · Case-sensitive.</small>' : ''}
        <label for="password">Password<input id="password" type="password" name="password" autocomplete="${register ? 'new-password' : 'current-password'}" ${register ? 'minlength="12" aria-describedby="password-help"' : ''} maxlength="1024" required></label>
        ${register ? '<small id="password-help">At least 12 characters. Password recovery is not available in this preview. Save your password securely.</small>' : ''}
        <p class="form-error" role="alert"></p>
        <button class="primary" type="submit">${register ? 'Create account' : 'Sign in'}</button>
      </fieldset></form>
      ${session?.registration_open !== false ? `<p class="auth-switch">${register ? 'Already have an account?' : 'New to this workspace?'} <button class="text-button" type="button" data-action="auth-switch">${register ? 'Sign in' : 'Create account'}</button></p>` : ''}
    </section>
  </div>`, register ? 'Create account' : 'Sign in');
}

function projectDialog() {
  return `<dialog id="project-dialog" aria-labelledby="create-heading">
    <h2 id="create-heading">New private project</h2><p>Choose a name and a reference recording for your project.</p>
    <form id="create-project-form"><fieldset>
      <label for="project-name">Project name<input id="project-name" name="name" maxlength="100" required autocomplete="off"></label>
      <label for="project-description">Description <span class="muted">Optional</span><textarea id="project-description" name="description" maxlength="2000" rows="3"></textarea></label>
      <div class="form-field"><label for="project-dataset">Reference dataset</label><select id="project-dataset" name="dataset_id" required>${datasets.map(dataset => `<option value="${e(dataset.id)}">${e(dataset.name)} · ${e(dataset.version)}</option>`).join('')}</select></div>
      <p class="muted">This links the prepared reference recording. Personal recording uploads are not available.</p>
      <p class="form-error" role="alert"></p>
      <div class="form-actions"><button type="button" data-action="close-dialog">Cancel</button><button type="submit" class="primary">Create project</button></div>
    </fieldset></form>
  </dialog>`;
}

function renderProjects(projects) {
  const limit = session.limits.projects;
  const canCreate = projects.length < limit && datasets.length > 0;
  mount(`<section aria-labelledby="projects-heading">
    <div class="page-heading"><div><p class="eyebrow">Private workspace</p><h1 id="projects-heading">Projects</h1><p class="muted">Your reference data and experiment workspace, organized by project.</p></div>
      ${canCreate ? '<button type="button" class="primary" data-action="new-project">New project <span aria-hidden="true">＋</span></button>' : ''}
    </div>
    <div class="list-heading"><span class="eyebrow">${integer(projects.length)} ${projects.length === 1 ? 'project' : 'projects'}</span><span class="muted">Private to your account · ${integer(limit)} project limit</span></div>
    ${projects.length ? `<div class="project-list">${projects.map(project => {
      const dataset = datasets.find(item => item.id === project.dataset_id);
      return `<a class="project-row" href="#project/${e(project.id)}/data"><div><h2>${e(project.name)}</h2><p class="muted">${e(project.description || 'No description')}</p></div><div class="dataset-cell"><p class="eyebrow">Reference data</p><span class="muted">${e(dataset?.name || project.dataset_id)}</span></div><div class="updated-cell"><p class="eyebrow">Updated</p><time class="muted" datetime="${e(project.updated_at)}">${e(date(project.updated_at))}</time></div><span class="row-arrow" aria-hidden="true">→</span></a>`;
    }).join('')}</div>` : '<div class="empty"><h2>No projects yet</h2><p>Create a private project to inspect calibrated reference data and its qualification evidence.</p></div>'}
    ${projects.length >= limit ? '<p class="section-note">Project limit reached. Delete an unused project before creating another.</p>' : ''}
    ${!datasets.length ? '<p class="section-note">No reference datasets are currently available. Try again later.</p>' : ''}
  </section>${projectDialog()}`, 'Projects', true);
}

function sourceLink(dataset, path, label) {
  // Source evidence is public and pinned to its qualification commit, never a project artifact route.
  const base = safeExternalLink(dataset.repository_link_base);
  if (!base || typeof path !== 'string' || path.startsWith('/') || path.includes('..') || path.includes('://')) return `<span>${e(label)}<span class="source-path mono">${e(path)}</span></span>`;
  const href = safeExternalLink(new URL(path, base.endsWith('/') ? base : `${base}/`).href);
  return href ? `<a href="${e(href)}" target="_blank" rel="noopener noreferrer">${e(label)} <span aria-hidden="true">↗</span></a>` : e(label);
}

function externalLink(url, label) {
  const href = safeExternalLink(url);
  return href ? `<a href="${e(href)}" target="_blank" rel="noopener noreferrer">${e(label)} <span aria-hidden="true">↗</span></a>` : e(label);
}

function detail(label, content) { return `<div><dt>${e(label)}</dt><dd>${content}</dd></div>`; }

function renderDataset(dataset) {
  if (!dataset) return '<div class="empty"><h2>Dataset unavailable</h2><p>The reference metadata could not be found. Return to Projects and try again.</p></div>';
  const { calibration, reference, provenance } = dataset;
  return `<div class="dataset-layout"><section aria-labelledby="data-heading">
    <div class="section-heading"><div><p class="eyebrow">Reference dataset · Handheld recording</p><h2 id="data-heading">${e(dataset.name)}</h2></div><span class="pill">Stereo + IMU</span></div>
    <p class="mono">${e(dataset.version)}</p><p class="dataset-description">${e(dataset.description)}</p>
    <dl class="facts"><div><dt>Recording duration</dt><dd>${decimal(dataset.duration_s)} s</dd></div><div><dt>Stereo pairs</dt><dd>${integer(dataset.camera_pairs)}</dd></div><div><dt>IMU samples</dt><dd>${integer(dataset.imu_samples)}</dd></div></dl>
    <dl class="details-list">
      ${detail('Images', `${integer(dataset.camera_count)} cameras · ${dataset.camera_resolution.map(e).join(' × ')} px`)}
      ${detail('Camera model', `${e(calibration.camera_model)} · ${e(calibration.distortion_model)}`)}
      ${detail('Body frame', e(calibration.body_frame))}
      ${detail('Timing', `${e(dataset.timestamps.stereo_pairing)} Initial camera–IMU offset: ${e(calibration.initial_camera_imu_offset_s)} s.`)}
      ${detail('Preprocessing', e(calibration.dataset_preprocessing))}
      ${detail('Image conversion', e(calibration.image_conversion))}
    </dl>
    <details><summary>Calibration, coordinates and timestamps</summary><dl class="details-list">
      ${detail('Camera extrinsics', e(calibration.camera_extrinsics_convention))}
      ${detail('Reference pose', e(calibration.pose_convention))}
      ${detail('First camera sample', `<span class="mono">${e(dataset.timestamps.first_camera_timestamp_ns)} ns</span>`)}
      ${detail('Last camera sample', `<span class="mono">${e(dataset.timestamps.last_camera_timestamp_ns)} ns</span>`)}
      ${detail('Duration basis', e(dataset.duration_basis))}
      ${calibration.files.map(file => detail(file.role.replaceAll('_', ' '), `${sourceLink(dataset, file.url, 'Repository source')}<p class="mono">SHA-256 ${e(file.sha256)}</p>`)).join('')}
    </dl><p class="evidence-links">${sourceLink(dataset, calibration.source_url, 'Calibration and noise assumptions')}</p></details>
    <details><summary>Source, license and reproducibility</summary><dl class="details-list">
      ${detail('Recording source', externalLink(provenance.dataset_url, 'TUM VI Benchmark'))}
      ${detail('License', externalLink(provenance.license_url, provenance.license))}
      ${detail('Attribution', e(provenance.attribution))}
      ${detail('Dataset identity', `<span class="mono">${e(provenance.qualified_dataset_id)}</span>`)}
      ${detail('Archive SHA-256', `<span class="mono">${e(provenance.archive_sha256)}</span>`)}
      ${detail('Prepared manifest', `<span class="mono">${e(provenance.prepared_manifest_sha256)}</span>`)}
      ${detail('Qualification commit', `<span class="mono">${e(provenance.qualification_source_commit)}</span>`)}
    </dl></details>
    <details><summary>Qualification limits</summary><ul class="limitations">${dataset.limitations.map(item => `<li>${e(item)}</li>`).join('')}</ul></details>
  </section><aside class="surface side-panel" aria-labelledby="reference-heading">
    <p class="eyebrow">Independent evaluation reference</p><h2 id="reference-heading">Motion capture</h2><p>${e(reference.purpose)}</p>
    <dl><div><dt>Camera timestamps with reference support</dt><dd>${decimal(reference.coverage_fraction * 100)}%</dd></div><div><dt>Supported / total camera timestamps</dt><dd>${integer(reference.supported_camera_samples)} / ${integer(dataset.camera_pairs)}</dd></div><div><dt>Maximum reference gap</dt><dd>${decimal(reference.maximum_gap_s, 3)} s</dd></div></dl>
    <p>${integer(reference.unsupported_camera_samples)} camera timestamps have no reference support under the ${decimal(reference.bracket_rule.maximum_bracket_width_s * 1000, 0)} ms interpolation bracket rule. No extrapolation.</p>
    <p>Reference support describes the recording, not estimator accuracy or output coverage.</p>
    <details open><summary>Local reference qualification</summary><p class="muted">OpenVINS baseline · ${e(provenance.qualification_date)}. Recorded local execution; this is not a run in your project. Source paths below identify files in the Meridian repository.</p>
      <div class="evidence-links">${sourceLink(dataset, dataset.source_qualification_url, 'Qualification report')}${sourceLink(dataset, dataset.source_qualification_summary_url, 'Machine-readable evidence')}</div>
    </details>
  </aside></div>`;
}

function renderSettings(project) {
  return `<section class="settings" aria-labelledby="settings-heading"><form id="edit-project-form" class="surface"><h2 id="settings-heading">Project details</h2><fieldset>
    <label for="edit-name">Project name<input id="edit-name" name="name" maxlength="100" value="${e(project.name)}" required></label>
    <div class="form-field"><label for="edit-description">Description</label><textarea id="edit-description" name="description" rows="5" maxlength="2000">${e(project.description)}</textarea></div>
    <p class="muted">Projects are retained until you delete them.</p><p class="form-error" role="alert"></p>
    <div class="form-actions"><button type="submit" class="primary">Save changes</button></div>
  </fieldset></form>
  <div class="delete-panel"><h2>Delete project</h2><p>Remove this project, its experiments, saved versions and run history. Active runs must finish or be cancelled first.</p><button class="danger" data-action="delete-project" type="button">Delete project</button></div>
  <dialog id="delete-dialog" aria-labelledby="delete-heading"><h2 id="delete-heading">Delete ${e(project.name)}?</h2><p>Permanently delete this project, its experiments, saved versions and run history? Artifact cleanup is asynchronous.</p><form id="delete-project-form"><fieldset><p class="form-error" role="alert"></p><div class="form-actions"><button type="button" data-action="close-dialog" autofocus>Keep project</button><button type="submit" class="danger">Delete project</button></div></fieldset></form></dialog>
  </section>`;
}

function renderProject(project, section) {
  const tabs = [['data', 'Data'], ['experiments', 'Experiments'], ['results', 'Results'], ['settings', 'Settings']];
  const dataset = datasets.find(item => item.id === project.dataset_id);
  let content;
  if (section === 'data') content = renderDataset(dataset);
  else if (section === 'settings') content = renderSettings(project);
  else if (section === 'experiments') content = experimentView.experiment ? renderExperimentDetail(project, experimentView, draftState, renderRevisionJobs(experimentView, session.capabilities.execution, draftState?.kind === 'edit')) : renderExperimentList(project, experimentView);
  else content = `<div id="jobs-content">${jobView.investigation ? investigationShell(jobView.job) : jobView.job ? renderJobDetail(jobView.job) : renderJobList(jobView)}</div>`;
  mount(`<nav class="breadcrumb" aria-label="Breadcrumb"><a href="#projects">Projects</a><span aria-hidden="true">/</span><span>${e(project.name)}</span></nav>
    <div class="page-heading project-heading"><div><p class="eyebrow">Private project</p><h1>${e(project.name)}</h1>${project.description ? `<p class="muted project-description">${e(project.description)}</p>` : ''}</div><span class="pill">Private to your account</span></div>
    <nav class="project-nav" aria-label="Project">${tabs.map(([key, label]) => `<a href="#project/${e(project.id)}/${key}" ${key === section ? 'aria-current="page"' : ''}>${label}</a>`).join('')}</nav><p id="job-poll-status" class="job-feedback" role="status" hidden></p>${content}`, `${project.name} · ${tabs.find(([key]) => key === section)[1]}`, true);
  const pending = experimentView?.experiment && submissions.get(submissionIdentity());
  if (pending?.inFlight) app.querySelector('[data-action="launch-job"]')?.setAttribute('disabled', '');
  if (jobView?.investigation) investigationView = mountInvestigation(app.querySelector('#investigation-content'), { api, job: jobView.job, onSessionExpired: expireSession });
}

function routeError(error) {
  const unavailable = error instanceof ApiError && error.status === 404;
  const csrf = error instanceof ApiError && error.status === 403;
  const route = parseRoute(window.location.hash);
  const resource = route.jobId ? 'Run' : route.experimentId ? 'Experiment' : 'Project';
  mount(`<div class="empty error" role="alert"><h1>${unavailable ? `${resource} unavailable` : 'Unable to load the workspace'}</h1><p>${unavailable ? `This ${resource.toLowerCase()} does not exist or is not available to your account.` : e(errorMessage(error))}</p>${csrf ? '<button type="button" data-action="reload">Reload session</button>' : '<button type="button" data-action="retry">Try again</button>'} <a class="button" href="#projects">Return to Projects</a></div>`, 'Workspace unavailable', true);
}

async function expireSession() {
  removePrivateState();
  session = null;
  api.setCsrfToken('');
  announce('Your session has expired. Sign in again to continue.', true);
  await initialize(false);
}

async function renderRoute() {
  if (!session?.user) { renderAuth(); return; }
  const id = ++navigation;
  controller?.abort();
  jobPoller?.stop();
  investigationView?.destroy();
  investigationView = null;
  controller = new AbortController();
  currentProject = null;
  experimentView = null;
  jobView = null;
  mount('<p class="loading" role="status">Loading workspace…</p>', 'Workspace');
  try {
    const route = parseRoute(window.location.hash);
    if (route.page === 'not-found') {
      mount('<div class="empty"><h1>Page unavailable</h1><p>The workspace address is not recognized.</p><a class="button" href="#projects">Return to Projects</a></div>', 'Page unavailable', true);
      return;
    }
    const catalogPromise = datasets.length ? Promise.resolve({ datasets }) : api.request('/datasets', { signal: controller.signal });
    const resourcePromise = api.request(route.page === 'projects' ? '/projects' : `/projects/${route.id}`, { signal: controller.signal });
    const [catalog, resource] = await Promise.all([catalogPromise, resourcePromise]);
    if (id !== navigation) return;
    datasets = catalog.datasets;
    if (route.page === 'projects') renderProjects(resource.projects);
    else {
      if (route.section === 'experiments') {
        const path = `/projects/${route.id}/experiments`;
        const tasks = [api.request('/configuration-profile', { signal: controller.signal }), api.request(path, { signal: controller.signal })];
        if (route.experimentId) {
          tasks.push(api.request(`${path}/${route.experimentId}`, { signal: controller.signal }));
          tasks.push(api.request(`${path}/${route.experimentId}/revisions`, { signal: controller.signal }));
          if (route.version !== undefined) tasks.push(api.request(`${path}/${route.experimentId}/revisions/${route.version}`, { signal: controller.signal }));
        }
        const [profile, list, detail, history, historical] = await Promise.all(tasks);
        if (id !== navigation) return;
        const view = { profile: profile.profile, experiments: list.experiments, limits: list.limits, execution: session.capabilities.execution };
        if (detail) {
          view.experiment = detail.experiment;
          view.revision = historical?.revision || detail.revision;
          view.revisions = history.revisions;
          const [comparison, runs] = await Promise.all([
            api.request(`${path}/${route.experimentId}/compare`, { query: { from: Math.max(1, view.revision.version - 1), to: view.revision.version }, signal: controller.signal }),
            api.request(`/projects/${route.id}/jobs`, { signal: controller.signal }),
          ]);
          view.comparison = comparison;
          view.jobs = runs.jobs;
          view.jobLimits = runs.limits;
          view.jobService = runs.service;
        }
        if (id !== navigation) return;
        experimentView = view;
      } else if (route.section === 'results') {
        const value = await api.request(`/projects/${route.id}/jobs${route.jobId ? `/${route.jobId}` : ''}`, { signal: controller.signal });
        if (id !== navigation) return;
        jobView = { ...value, investigation: Boolean(route.investigation) };
      }
      currentProject = resource.project;
      renderProject(currentProject, route.section);
      startJobPolling(route, id);
    }
    renderedHash = window.location.hash;
  } catch (error) {
    if (id !== navigation || error.name === 'AbortError') return;
    if (error.status === 401) await expireSession();
    else routeError(error);
  }
}

async function initialize(clearNotice = true) {
  const epoch = sessionEpoch;
  try {
    const value = await api.request('/session');
    if (epoch !== sessionEpoch) return;
    setSession(value);
    if (clearNotice) announce();
    await renderRoute();
  } catch (error) {
    if (epoch !== sessionEpoch) return;
    routeError(error);
  }
}

function showFormError(form, error) {
  const field = form.querySelector('.form-error');
  field.textContent = errorMessage(error);
  for (const target of form.querySelectorAll('[data-field-error]')) {
    const messages = error.fields?.[target.dataset.fieldError];
    target.textContent = messages ? (Array.isArray(messages) ? messages.join(' ') : String(messages)) : '';
    const input = target.parentElement.querySelector('input, textarea, select');
    if (input) input.setAttribute('aria-invalid', String(Boolean(messages)));
  }
  if (error.status === 403) {
    const button = document.createElement('button');
    button.type = 'button';
    button.dataset.action = 'reload';
    button.textContent = 'Reload session';
    field.append(document.createElement('br'), button);
  }
}

function hasUnsavedDraft() {
  return draftState && draftChanged(draftState.baseline, draftState.values);
}

function discardDraft() {
  if (hasUnsavedDraft() && !window.confirm('Discard unsaved experiment changes?')) return false;
  draftState = null;
  return true;
}

async function navigateTo(hash) {
  if (window.location.hash === hash) await renderRoute();
  else window.location.hash = hash;
}

function openNewExperiment() {
  const values = revisionDraft(null, experimentView.profile);
  draftState = { kind: 'create', baseline: { ...values }, values };
  app.insertAdjacentHTML('beforeend', createExperimentDialog(currentProject, experimentView.profile, values));
  const dialog = app.querySelector('#experiment-dialog');
  dialog.addEventListener('close', () => { if (draftState?.kind === 'create') draftState = null; dialog.remove(); }, { once: true });
  dialog.showModal();
}

async function submitExperiment(form, data, epoch) {
  const submittedNavigation = navigation;
  const projectId = currentProject.id;
  const path = `/projects/${projectId}/experiments`;
  const experimentId = experimentView.experiment?.id;
  const selectedVersion = experimentView.revision?.version;
  let response;
  if (form.id === 'create-experiment-form') {
    response = await api.request(path, { method: 'POST', body: experimentPayload(data, experimentView.profile, { datasetId: currentProject.dataset_id }) });
  } else if (form.id === 'edit-experiment-form') {
    response = await api.request(`${path}/${experimentId}`, { method: 'PATCH', body: experimentPayload(data, experimentView.profile, { baseVersion: draftState.baseVersion }) });
  } else if (form.id === 'duplicate-experiment-form') {
    response = await api.request(`${path}/${experimentId}/duplicate`, { method: 'POST', body: { schema_version: 1, source_version: selectedVersion, name: data.name } });
  } else if (form.id === 'delete-experiment-form') {
    await api.request(`${path}/${experimentId}`, { method: 'DELETE' });
    if (epoch !== sessionEpoch || submittedNavigation !== navigation) return;
    draftState = null;
    await navigateTo(`#project/${projectId}/experiments`);
    announce('Experiment and its saved versions deleted.');
    return;
  }
  if (epoch !== sessionEpoch || submittedNavigation !== navigation) return;
  draftState = null;
  await navigateTo(experimentPath(response.experiment.project_id, response.experiment.id));
  announce(response.unchanged ? `No changes to save. Version ${response.experiment.version} is unchanged.` : `Version ${response.experiment.version} saved. No computation was started.`);
}

async function reviewLatestExperiment() {
  const id = navigation;
  const form = app.querySelector('#edit-experiment-form');
  const panel = app.querySelector('#experiment-conflict');
  const path = `/projects/${currentProject.id}/experiments/${experimentView.experiment.id}`;
  try {
    const [response, history] = await Promise.all([
      api.request(path, { signal: controller.signal }),
      api.request(`${path}/revisions`, { signal: controller.signal }),
    ]);
    if (id !== navigation || !draftState || !form.isConnected) return;
    draftState.latest = response.revision;
    draftState.latestExperiment = response.experiment;
    draftState.latestHistory = history.revisions;
    panel.innerHTML = renderConflict(response.revision, draftState.values, experimentView.profile);
    panel.hidden = false;
  } catch (error) {
    if (id !== navigation || error.name === 'AbortError') return;
    if (error.status === 401) await expireSession();
    else showFormError(form, error);
  }
}

async function compareSelectedVersions() {
  const id = ++comparisonRequest;
  const currentNavigation = navigation;
  const target = app.querySelector('#comparison-result');
  const path = `/projects/${currentProject.id}/experiments/${experimentView.experiment.id}/compare`;
  const query = { from: Number(app.querySelector('#compare-from').value), to: Number(app.querySelector('#compare-to').value) };
  target.textContent = 'Loading version differences…';
  try {
    const comparison = await api.request(path, { query, signal: controller.signal });
    if (id !== comparisonRequest || currentNavigation !== navigation || !target.isConnected) return;
    experimentView.comparison = comparison;
    target.innerHTML = renderComparison(comparison);
  } catch (error) {
    if (id !== comparisonRequest || currentNavigation !== navigation || error.name === 'AbortError') return;
    if (error.status === 401) await expireSession();
    else target.textContent = errorMessage(error);
  }
}

function selectedJobs(view = experimentView) {
  return view.jobs.filter(job => job.experiment_id === view.experiment.id && job.revision_version === view.revision.version);
}

function startJobPolling(route, routeId) {
  if (route.investigation) return;
  const detail = route.section === 'results' && route.jobId;
  const revision = route.section === 'experiments' && route.experimentId;
  if (!detail && !revision && route.section !== 'results') return;
  const remainsActive = value => detail ? activeJob(value.job) : revision ? value.jobs.some(job => job.experiment_id === experimentView.experiment.id && job.revision_version === experimentView.revision.version && activeJob(job)) : value.jobs.some(activeJob);
  const initial = revision ? experimentView : jobView;
  if (!remainsActive(initial)) return;
  let previous = JSON.stringify(detail ? initial.job : initial.jobs);
  jobPoller = createJobPoller({
    read: signal => api.request(`/projects/${route.id}/jobs${detail ? `/${route.jobId}` : ''}`, { signal }),
    isVisible: () => document.visibilityState === 'visible',
    shouldContinue: remainsActive,
    onValue(value) {
      if (routeId !== navigation) return;
      const status = app.querySelector('#job-poll-status');
      status.hidden = true;
      const serialized = JSON.stringify(detail ? value.job : value.jobs);
      if (revision) { experimentView.jobs = value.jobs; experimentView.jobLimits = value.limits; experimentView.jobService = value.service; }
      else jobView = value;
      const service = app.querySelector('#job-service');
      if (service) service.innerHTML = renderService(value.service);
      if (serialized === previous) return;
      previous = serialized;
      if (revision) {
        const jobs = selectedJobs();
        app.querySelector('#revision-job-list').innerHTML = jobs.length ? renderJobRows(jobs) : '<p class="muted job-empty">No runs for this saved version.</p>';
      } else {
        const target = app.querySelector('#jobs-content');
        const focusAction = target.contains(document.activeElement) ? document.activeElement.dataset.action : null;
        const focusHref = target.contains(document.activeElement) ? document.activeElement.getAttribute('href') : null;
        const openDetails = [...target.querySelectorAll('details')].map(item => item.open);
        target.innerHTML = detail ? renderJobDetail(value.job) : renderJobList(value);
        target.querySelectorAll('details').forEach((item, index) => { item.open = openDetails[index] || false; });
        if (focusAction) target.querySelector(`[data-action="${focusAction}"]`)?.focus({ preventScroll: true });
        else if (focusHref) [...target.querySelectorAll('a[href]')].find(link => link.getAttribute('href') === focusHref)?.focus({ preventScroll: true });
      }
    },
    onError(error) {
      if (routeId !== navigation) return false;
      if (error.status === 401) { void expireSession(); return false; }
      if (error.status === 404) { routeError(error); return false; }
      const status = app.querySelector('#job-poll-status');
      status.hidden = false;
      status.textContent = `Status refresh interrupted. Showing the last received state. ${errorMessage(error)}`;
      if (error.status === 403) {
        const button = document.createElement('button');
        button.type = 'button'; button.dataset.action = 'reload'; button.textContent = 'Reload session';
        status.append(document.createElement('br'), button);
        return false;
      }
      return true;
    },
  });
}

function submissionIdentity() {
  return `${currentProject.id}/${experimentView.experiment.id}/${experimentView.revision.version}`;
}

async function launchJob(button) {
  if (!session.capabilities.execution || draftState?.kind === 'edit') return;
  const identity = submissionIdentity();
  const pending = submissions.get(identity) || { key: crypto.randomUUID(), inFlight: false };
  if (pending.inFlight) return;
  submissions.set(identity, pending);
  pending.inFlight = true;
  const epoch = sessionEpoch;
  const routeId = navigation;
  const projectId = currentProject.id;
  const body = { schema_version: 1, experiment_id: experimentView.experiment.id, revision_version: experimentView.revision.version, idempotency_key: pending.key };
  button.disabled = true;
  const feedback = app.querySelector('#job-submission-status');
  feedback.textContent = 'Submitting the saved version…';
  try {
    const response = await api.request(`/projects/${projectId}/jobs`, { method: 'POST', body });
    if (epoch !== sessionEpoch) return;
    submissions.delete(identity);
    if (routeId !== navigation) return;
    await navigateTo(jobPath(projectId, response.job.id));
    announce(response.reused ? 'Existing submission recovered. No duplicate run was created.' : 'Run submitted. It continues when you leave the page.');
  } catch (error) {
    if (epoch !== sessionEpoch || routeId !== navigation) return;
    if (error.status === 401) { await expireSession(); return; }
    if (feedback.isConnected) {
      const uncertain = !(error instanceof ApiError) || error.status >= 500;
      feedback.textContent = `${errorMessage(error)}${uncertain ? ' Submission may have reached the server. Retry uses the same request identity to avoid duplicate work.' : ''}`;
      if (error.status === 403) {
        const reload = document.createElement('button');
        reload.type = 'button'; reload.dataset.action = 'reload'; reload.textContent = 'Reload session';
        feedback.append(document.createElement('br'), reload);
      }
    }
  } finally {
    pending.inFlight = false;
    if (epoch === sessionEpoch && button.isConnected) button.disabled = false;
    else if (epoch === sessionEpoch && experimentView?.experiment && currentProject && submissionIdentity() === identity) {
      app.querySelector('[data-action="launch-job"]')?.removeAttribute('disabled');
    }
  }
}

function openJobAction(action) {
  const job = jobView.job;
  app.querySelector('#job-action-dialog')?.remove();
  app.insertAdjacentHTML('beforeend', jobActionDialog(job, action));
  const dialog = app.querySelector('#job-action-dialog');
  dialog.addEventListener('close', () => dialog.remove(), { once: true });
  dialog.showModal();
}

async function submitJobAction(form, epoch, projectId) {
  const routeId = navigation;
  const cancel = form.id === 'cancel-job-form';
  const path = `/projects/${projectId}/jobs/${form.dataset.jobId}`;
  await api.request(cancel ? `${path}/cancel` : path, { method: cancel ? 'POST' : 'DELETE', ...(cancel ? { body: { schema_version: 1 } } : {}) });
  if (epoch !== sessionEpoch || routeId !== navigation) return;
  form.closest('dialog').close();
  if (cancel) await renderRoute();
  else await navigateTo(`#project/${projectId}/results`);
  announce(cancel ? 'Cancellation request processed. The current run state is shown below.' : 'Run removed. Artifact cleanup follows asynchronously.');
}

app.addEventListener('submit', async event => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement)) return;
  event.preventDefault();
  const data = Object.fromEntries(new FormData(form));
  const fields = form.querySelector('fieldset');
  const epoch = sessionEpoch;
  const projectId = currentProject?.id;
  fields.disabled = true;
  form.querySelector('.form-error').textContent = '';
  announce();
  try {
    if (['cancel-job-form', 'delete-job-form'].includes(form.id)) {
      await submitJobAction(form, epoch, projectId);
    } else if (['create-experiment-form', 'edit-experiment-form', 'duplicate-experiment-form', 'delete-experiment-form'].includes(form.id)) {
      await submitExperiment(form, data, epoch);
    } else if (form.id === 'auth-form') {
      const value = await api.request(`/auth/${authMode === 'register' && session.registration_open !== false ? 'register' : 'login'}`, { method: 'POST', body: data });
      if (epoch !== sessionEpoch) return;
      setSession(value);
      await renderRoute();
    } else if (form.id === 'create-project-form') {
      const value = await api.request('/projects', { method: 'POST', body: data });
      if (epoch !== sessionEpoch) return;
      app.querySelector('dialog')?.close();
      window.location.hash = `#project/${value.project.id}/data`;
      announce('Project created.');
    } else if (form.id === 'edit-project-form') {
      await api.request(`/projects/${projectId}`, { method: 'PATCH', body: data });
      if (epoch !== sessionEpoch) return;
      await renderRoute();
      announce('Project details saved.');
    } else if (form.id === 'delete-project-form') {
      await api.request(`/projects/${projectId}`, { method: 'DELETE' });
      if (epoch !== sessionEpoch) return;
      app.querySelector('dialog')?.close();
      window.location.hash = '#projects';
      announce('Project deleted.');
    }
  } catch (error) {
    if (epoch !== sessionEpoch) return;
    if (error.status === 401 && form.id !== 'auth-form') await expireSession();
    else if (form.isConnected) {
      showFormError(form, error);
      if (form.id === 'edit-experiment-form' && error.code === 'version_conflict') {
        const panel = app.querySelector('#experiment-conflict');
        panel.hidden = false;
        panel.innerHTML = '<h3>A newer version was saved</h3><p>Your draft has been preserved. Review the latest saved version before continuing.</p><button type="button" data-action="review-latest-experiment">Review latest version</button>';
      }
    }
  } finally {
    fields.disabled = false;
  }
});

app.addEventListener('click', event => {
  const button = event.target.closest('button[data-action]');
  if (!button) return;
  switch (button.dataset.action) {
    case 'auth-switch':
      authMode = authMode === 'login' ? 'register' : 'login';
      renderAuth();
      app.querySelector('#username').focus();
      break;
    case 'new-project': app.querySelector('#project-dialog').showModal(); break;
    case 'delete-project': app.querySelector('#delete-dialog').showModal(); break;
    case 'close-dialog':
      if (button.closest('dialog').querySelector('[data-experiment-draft]') && !discardDraft()) break;
      button.closest('dialog').close();
      break;
    case 'new-experiment': openNewExperiment(); break;
    case 'launch-job': void launchJob(button); break;
    case 'cancel-job': openJobAction('cancel'); break;
    case 'delete-job': openJobAction('delete'); break;
    case 'edit-experiment': {
      const values = revisionDraft(experimentView.revision, experimentView.profile);
      draftState = { kind: 'edit', baseline: { ...values }, values, baseVersion: experimentView.experiment.version };
      renderProject(currentProject, 'experiments');
      app.querySelector('#edit-experiment-name').focus();
      break;
    }
    case 'cancel-experiment-edit': if (discardDraft()) renderProject(currentProject, 'experiments'); break;
    case 'duplicate-experiment':
      if (!discardDraft()) break;
      renderProject(currentProject, 'experiments');
      app.querySelector('#duplicate-experiment-dialog').showModal();
      break;
    case 'delete-experiment':
      if (!discardDraft()) break;
      renderProject(currentProject, 'experiments');
      app.querySelector('#delete-experiment-dialog').showModal();
      break;
    case 'review-latest-experiment': reviewLatestExperiment(); break;
    case 'rebase-experiment':
      draftState.baseVersion = draftState.latest.version;
      draftState.baseline = revisionDraft(draftState.latest, experimentView.profile);
      experimentView.experiment = draftState.latestExperiment;
      experimentView.revision = draftState.latest;
      experimentView.revisions = draftState.latestHistory;
      renderProject(currentProject, 'experiments');
      app.querySelector('#edit-experiment-name').focus();
      announce(`Draft preserved. Its next save will use version ${draftState.baseVersion} as the base.`);
      break;
    case 'reload': window.location.reload(); break;
    case 'retry': session?.user ? renderRoute() : initialize(); break;
  }
});

logout.addEventListener('click', async () => {
  if (!discardDraft()) return;
  logout.disabled = true;
  try {
    const value = await api.request('/auth/logout', { method: 'POST', body: {} });
    removePrivateState();
    setSession(value);
    authMode = 'login';
    window.history.replaceState(null, '', '#projects');
    renderAuth();
    announce('You have signed out.');
  } catch (error) {
    if (error.status === 401) await expireSession();
    else {
      announce(errorMessage(error), true);
      if (error.status === 403) {
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = 'Reload session';
        button.addEventListener('click', () => window.location.reload());
        notice.append(document.createElement('br'), button);
      }
    }
  } finally { logout.disabled = false; }
});

// Recheck the cookie session when returning to a tab, including browser back/forward cache.
async function recheckSession() {
  if (checkingSession || !session) return;
  checkingSession = true;
  const epoch = sessionEpoch;
  try {
    const value = await api.request('/session');
    if (epoch !== sessionEpoch) return;
    if (value.user?.id !== session.user?.id) {
      removePrivateState();
      setSession(value);
      announce(value.user ? 'The active account changed. Workspace refreshed.' : 'Your session has ended. Sign in again.', !value.user);
      await renderRoute();
    } else {
      session = value;
      api.setCsrfToken(value.csrf_token);
    }
  } catch {
    // A network interruption does not change the authenticated server identity.
    // Subsequent private API requests still enforce authorization server-side.
  } finally { checkingSession = false; }
}

app.addEventListener('input', event => {
  const form = event.target.closest('form[data-experiment-draft]');
  if (form && draftState) draftState.values = Object.fromEntries(new FormData(form));
});
app.addEventListener('change', event => {
  if (event.target.matches('#compare-from, #compare-to')) compareSelectedVersions();
});
app.addEventListener('cancel', event => {
  if (event.target.querySelector('fieldset:disabled')) { event.preventDefault(); return; }
  if (event.target.querySelector('[data-experiment-draft]') && !discardDraft()) event.preventDefault();
}, true);
document.addEventListener('click', event => {
  const link = event.target.closest('a[href]');
  if (!link || link.target === '_blank' || event.ctrlKey || event.metaKey || event.shiftKey || link.getAttribute('href') === window.location.hash) return;
  if (hasUnsavedDraft() && !discardDraft()) event.preventDefault();
}, true);
window.addEventListener('beforeunload', event => {
  if (hasUnsavedDraft()) { event.preventDefault(); event.returnValue = ''; }
});
window.addEventListener('hashchange', () => {
  if (!discardDraft()) { window.history.replaceState(null, '', renderedHash || '#projects'); return; }
  announce();
  renderRoute();
});
window.addEventListener('focus', recheckSession);
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') recheckSession(); });
window.addEventListener('pageshow', event => { if (event.persisted) { removePrivateState(); initialize(); } });
await initialize();
