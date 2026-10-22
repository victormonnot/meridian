import { ApiError, escapeHtml as e, errorMessage } from './workspace-api.js';

export const PRIVATE_PROFILE = 'openvins-private-stereo-v1';
const uuid = /^[a-f\d]{8}(?:-[a-f\d]{4}){3}-[a-f\d]{12}$/i;
const identifier = /^[A-Za-z][A-Za-z0-9._-]{0,79}$/;
const timestamp = value => typeof value === 'string' && /^(0|[1-9]\d{0,18})$/.test(value)
  && BigInt(value) <= (1n << 63n) - 1n;

export function executionSelection(values, { full = false } = {}) {
  if (!uuid.test(values.input_revision_id || '')) throw new Error('Choose a saved prepared input revision.');
  if (full) return { input_revision_id: values.input_revision_id, interval: null };
  const { segment_id, first_ns, last_ns } = values;
  if (!identifier.test(segment_id || '') || !timestamp(first_ns) || !timestamp(last_ns)
    || BigInt(first_ns) > BigInt(last_ns)) {
    throw new Error('Use a declared segment and ordered, nonnegative integer nanosecond bounds.');
  }
  return { input_revision_id: values.input_revision_id, interval: { segment_id, first_ns, last_ns } };
}

export function privateExperimentPayload(values, profile, { baseVersion } = {}) {
  const num_pts = Number(values.num_pts);
  if (!Number.isInteger(num_pts) || !profile.parameters.num_pts.choices.includes(num_pts)) {
    throw new Error('Choose a supported feature count.');
  }
  return { schema_version: 1, profile_id: PRIVATE_PROFILE, ...executionSelection(values),
    ...(baseVersion === undefined ? {} : { base_version: baseVersion }),
    name: values.name, description: values.description, parameters: { num_pts } };
}

export function executionDraft(selection) {
  return selection ? { profile_id: PRIVATE_PROFILE, input_revision_id: selection.input_revision_id,
    segment_id: selection.interval?.segment_id || '', first_ns: selection.interval?.first_ns || '',
    last_ns: selection.interval?.last_ns || '' } : {};
}

export function renderExecutionInputs(inputs, values = {}) {
  const chosen = inputs.find(item => item.id === values.input_revision_id);
  const options = inputs.map(item => `<option value="${e(item.id)}" ${item.id === values.input_revision_id ? 'selected' : ''}>${e(item.acquisition_name)} · ${e(item.name)}${item.availability !== 'available' ? ` · ${e(item.availability)}` : ''}</option>`).join('');
  return `<section class="execution-input-controls" data-private-input-controls>
    <p class="muted">Choose an immutable prepared revision. Its selected calibration, clock mappings and reference policy are preserved in the experiment.</p>
    <label for="execution-input-revision">Prepared input revision<select id="execution-input-revision" name="input_revision_id"><option value="">Choose an owned input revision</option>${options}${values.input_revision_id && !chosen ? `<option value="${e(values.input_revision_id)}" selected>Saved revision · availability must be checked</option>` : ''}</select></label>
    ${!inputs.length ? '<p class="section-note">No prepared input revisions are available. Import an acquisition and create a revision in Calibration &amp; timing.</p>' : ''}
    <div class="execution-interval"><label for="execution-segment">Clock segment<input id="execution-segment" name="segment_id" value="${e(values.segment_id || '')}" autocomplete="off"></label><label for="execution-first">First camera timestamp · ns<input id="execution-first" name="first_ns" value="${e(values.first_ns || '')}" inputmode="numeric" autocomplete="off"></label><label for="execution-last">Last camera timestamp · ns<input id="execution-last" name="last_ns" value="${e(values.last_ns || '')}" inputmode="numeric" autocomplete="off"></label></div>
    <p class="muted">Inclusive exact camera bounds in the prepared clock. Selecting a revision checks its full interval; editing these values requires another check. Reset segments are never joined.</p>
    <div class="form-actions"><button type="button" data-check-execution-input>Check input</button><button type="button" data-full-execution-input>Full prepared interval</button></div>
    <div class="execution-admission" data-execution-admission role="status"><p class="muted">Choose a revision to check execution eligibility.</p></div></section>`;
}

export function renderExecutionAdmission(value) {
  const reasons = Array.isArray(value.reasons) ? value.reasons : [];
  return `<h3>${value.eligible ? 'Input admitted for this execution profile' : 'Input cannot run with this profile'}</h3>
    ${reasons.length ? `<ul>${reasons.map(reason => `<li><strong>${e(reason.code)}</strong> · ${e(reason.message)}</li>`).join('')}</ul>` : ''}
    <p class="muted">Admission checks declared compatibility and retained inputs. It does not establish physical calibration accuracy or successful initialization.</p>
    ${value.specification ? renderExecutionIdentity(value.specification) : ''}
    ${value.specification ? `<details><summary>Frozen input, calibration and execution policy</summary><pre class="job-json" tabindex="0">${e(JSON.stringify({ profile_id: value.profile_id, selection: value.selection, specification: value.specification }, null, 2))}</pre></details>` : ''}`;
}

export function renderExecutionIdentity(specification) {
  if (specification?.profile_id !== PRIVATE_PROFILE) return '';
  const input = specification.input || {};
  const policy = specification.policy || {};
  return `<section class="execution-identity"><h3>Frozen private input</h3><dl class="details-list">
    <div><dt>Prepared revision</dt><dd class="mono">${e(input.input_revision_id)}</dd></div>
    <div><dt>Prepared descriptor SHA-256</dt><dd class="mono">${e(input.prepared_input_sha256)}</dd></div>
    <div><dt>Calibration identity</dt><dd class="mono">${e(input.calibration?.id)}<br>${e(input.calibration?.sha256)}</dd></div>
    <div><dt>Prepared clock</dt><dd class="mono">${e(specification.clock_id)}</dd></div>
    <div><dt>Clock mappings</dt><dd>${input.clock_mappings?.length ? input.clock_mappings.map(item => `<p class="mono">${e(item.id)} · ${e(item.sha256)}</p>`).join('') : 'No additional clock mapping selected'}</dd></div>
    <div><dt>Reference</dt><dd>${specification.reference?.stream ? 'Selected for independent evaluation only; actual coverage is evaluated after execution.' : `Accuracy unavailable · ${e(specification.reference?.unavailable_reason || 'No usable reference selected')}`}</dd></div></dl>
    <p class="section-note">Supplied camera, geometry and timing parameters remain fixed during this run. Residual IMU bias states are estimated; they are not exported as a calibrated sensor artifact. Image masks are disabled. Gravity magnitude: ${e(policy.gravity_m_s2)} m/s².</p></section>`;
}

// Assessment never creates a job. Each response is tied to its exact draft;
// navigation, selection changes and logout invalidate late private responses.
export function mountExecutionInputs(form, { api, projectId, profile, onChange, onSessionExpired }) {
  if (!form?.querySelector('[data-private-input-controls]')) return null;
  const controls = form.querySelector('[data-private-input-controls]');
  const feedback = form.querySelector('[data-execution-admission]');
  const submit = form.querySelector('button[type="submit"]');
  const values = () => Object.fromEntries(new FormData(form));
  let live = true, generation = 0, checked = null, controller;
  const privateMode = () => values().profile_id === PRIVATE_PROFILE;
  const identity = value => JSON.stringify({ ...executionSelection(value), num_pts: value.num_pts });
  function sync() {
    controls.hidden = !privateMode();
    if (!privateMode()) submit.disabled = false;
    onChange?.(values());
  }
  function invalidate() {
    generation += 1;
    controller?.abort();
    checked = null;
    if (privateMode()) {
      submit.disabled = true;
      feedback.innerHTML = '<p class="muted">Check this exact input selection before saving.</p>';
    }
    sync();
  }
  async function assess(full) {
    invalidate();
    if (!privateMode()) return;
    const current = generation;
    controller = new AbortController();
    let selection;
    try {
      selection = executionSelection(values(), { full });
      const num_pts = Number(values().num_pts);
      if (!profile.parameters.num_pts.choices.includes(num_pts)) throw new Error('Choose a supported feature count.');
      feedback.innerHTML = '<p class="muted">Checking retained inputs and execution compatibility…</p>';
      const response = await api.request(`/projects/${projectId}/execution-inputs/assess`, {
        method: 'POST', signal: controller.signal,
        body: { schema_version: 1, ...selection, parameters: { num_pts } },
      });
      if (!live || current !== generation) return;
      if (response.selection?.interval) {
        for (const key of ['segment_id', 'first_ns', 'last_ns']) form.elements[key].value = response.selection.interval[key];
      }
      feedback.innerHTML = renderExecutionAdmission(response);
      if (response.eligible) {
        checked = identity(values());
        submit.disabled = false;
      }
      sync();
    } catch (error) {
      if (!live || current !== generation || error.name === 'AbortError') return;
      if (error.status === 401) { destroy(); void onSessionExpired?.(); return; }
      feedback.textContent = error instanceof ApiError ? errorMessage(error) : error.message;
    }
  }
  const change = event => {
    if (event.target.name === 'input_revision_id') void assess(true);
    else if (event.target.name === 'profile_id') { invalidate(); if (privateMode() && values().input_revision_id) void assess(false); }
    else if (['segment_id', 'first_ns', 'last_ns', 'num_pts'].includes(event.target.name)) invalidate();
  };
  const input = event => { if (['segment_id', 'first_ns', 'last_ns', 'num_pts'].includes(event.target.name)) invalidate(); };
  const click = event => {
    if (event.target.closest('[data-check-execution-input]')) void assess(false);
    if (event.target.closest('[data-full-execution-input]')) void assess(true);
  };
  function destroy() {
    live = false; generation += 1; controller?.abort(); checked = null;
    form.removeEventListener('change', change); form.removeEventListener('input', input); form.removeEventListener('click', click);
  }
  form.addEventListener('change', change); form.addEventListener('input', input); form.addEventListener('click', click);
  sync();
  if (privateMode()) { submit.disabled = true; if (values().input_revision_id) void assess(false); }
  return { destroy, isChecked(value) { return live && checked !== null && checked === identity(value); } };
}
