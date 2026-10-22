import { ApiError, escapeHtml as e, errorMessage } from './workspace-api.js';
import { acquisitionPath, acquisitionRoute, formatBytes } from './workspace-acquisitions.js';
import { timestamp } from './workspace-acquisition-inspection-data.js';

const UUID = /^[a-f\d]{8}(?:-[a-f\d]{4}){3}-[a-f\d]{12}$/i;
const words = value => String(value ?? 'unknown').replaceAll('_', ' ');
const detail = (label, value) => `<div><dt>${e(label)}</dt><dd>${value}</dd></div>`;
const value = item => typeof item === 'number' && Number.isFinite(item) ? String(item) : 'Unavailable';
const vector = (items, unit = '') => Array.isArray(items) ? `<span class="mono">${items.map(value).map(e).join(' / ')}${unit ? ` ${e(unit)}` : ''}</span>` : 'Not supplied';
const identity = ref => ref ? `<span class="mono">${e(ref.id)}<br>${e(ref.sha256)}</span>` : 'Not selected';

export function calibrationRoute(projectId, acquisitionId, revisionId) {
  const base = `${acquisitionRoute(projectId, acquisitionId)}/calibration`;
  if (revisionId === undefined) return base;
  if (!UUID.test(revisionId)) throw new TypeError('Invalid input revision identity.');
  return `${base}/revisions/${revisionId}`;
}

export function calibrationShell(acquisition) {
  return `<section class="calibration-workspace" aria-labelledby="calibration-heading">
    <a class="back-link" href="${acquisitionRoute(acquisition.project_id, acquisition.id)}">← Acquisition overview</a>
    <div class="section-heading"><div><p class="eyebrow">Versioned sensor inputs</p><h2 id="calibration-heading">Calibration &amp; timing</h2><p class="muted">${e(acquisition.name)}</p></div><span class="pill">Private to your account</span></div>
    <p class="section-note">Supplied parameters and exact corrections create immutable input revisions. A valid artifact does not establish physical accuracy, synchronization quality or estimator compatibility. Select a prepared revision in Experiments to check eligibility for private-recording VIO.</p>
    <div id="calibration-content"><p class="loading" role="status">Loading calibration artifacts and input revisions…</p></div></section>`;
}

function matrix(rotation) {
  return `<table class="calibration-matrix"><caption>Rotation R · rows in target frame, columns in source frame</caption><tbody>${rotation.map(row => `<tr>${row.map(item => `<td class="mono">${e(value(item))}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
}

function cameraParameters(camera) {
  const coefficients = camera.distortion === 'radtan' ? ['k1', 'k2', 'p1', 'p2'] : ['k1', 'k2', 'k3', 'k4'];
  return `<section class="calibration-parameter surface"><h4>Camera · ${e(camera.sensor_id)}</h4><dl class="details-list">
    ${detail('Frame', e(camera.frame_id))}${detail('Resolution', `${e(camera.resolution.join(' × '))} pixels`)}
    ${detail('Model', `${e(camera.projection)} · ${e(camera.distortion)}`)}
    ${['fx', 'fy', 'cx', 'cy'].map((label, index) => detail(label, `${e(value(camera.intrinsics[index]))} px`)).join('')}
    ${coefficients.map((label, index) => detail(label, e(value(camera.coefficients[index])))).join('')}
    </dl><p class="muted">These parameters describe projection and distortion. Selecting them does not warp or rectify the source images.</p></section>`;
}

function imuParameters(imu) {
  if (!imu) return '<p class="muted">No IMU intrinsic parameters supplied.</p>';
  const noise = imu.noise;
  const densities = [
    ['Gyroscope white-noise density', 'gyro_density_rad_s_per_sqrt_hz', 'rad/s/√Hz'],
    ['Specific-force white-noise density', 'accel_density_m_s2_per_sqrt_hz', 'm/s²/√Hz'],
    ['Gyroscope bias diffusion', 'gyro_bias_diffusion_rad_s_per_sqrt_s', '(rad/s)/√s'],
    ['Specific-force bias diffusion', 'accel_bias_diffusion_m_s2_per_sqrt_s', '(m/s²)/√s'],
  ];
  return `<section class="calibration-parameter surface"><h4>IMU · ${e(imu.sensor_id)}</h4>
    <p class="calibration-equation">corrected = scale × (measurement − bias)</p>
    <p class="muted">After SI unit conversion, in the declared sensor frame. Components below are X / Y / Z.</p>
    <dl class="details-list">${detail('Frame', e(imu.frame_id))}
      ${detail('Gyroscope bias', vector(imu.gyro_bias_rad_s, 'rad/s'))}${detail('Specific-force bias', vector(imu.accel_bias_m_s2, 'm/s²'))}
      ${detail('Gyroscope scale', vector(imu.gyro_scale, 'dimensionless'))}${detail('Specific-force scale', vector(imu.accel_scale, 'dimensionless'))}</dl>
    ${noise ? `<h5>Noise model · ${e(words(noise.model))}</h5><dl class="details-list">${densities.map(([label, key, unit]) => detail(label, `${e(value(noise[key]))} ${e(unit)}`)).join('')}</dl><p class="muted">${e(noise.assumptions)}</p>
      <p class="muted">Continuous noise densities and bias diffusion are not sample variances. Importing them does not fit or discretize a noise model.</p>` : '<p class="muted">Noise model not supplied.</p>'}</section>`;
}

export function mappedTimestamp(mapping, sourceNs) {
  for (const text of [sourceNs, mapping.source_origin_ns, mapping.target_origin_ns, mapping.offset_ns]) {
    if (!timestamp(text)) throw new TypeError('Expected canonical signed integer nanoseconds.');
  }
  const result = BigInt(mapping.target_origin_ns) + BigInt(sourceNs) - BigInt(mapping.source_origin_ns) + BigInt(mapping.offset_ns);
  if (result < -(1n << 63n) || result > (1n << 63n) - 1n) throw new RangeError('Mapped timestamp exceeds signed int64.');
  return String(result);
}

function clockParameters(mapping) {
  let endpoints = 'Unavailable';
  try { endpoints = `<span class="mono">${mappedTimestamp(mapping, mapping.first_source_ns)} → ${mappedTimestamp(mapping, mapping.last_source_ns)} ns</span>`; } catch { /* Server validation remains authoritative. */ }
  return `<section class="calibration-parameter surface"><h4>${mapping.purpose === 'sensor_time_offset' ? 'Sensor time offset' : 'Clock-domain mapping'}</h4>
    <p class="calibration-equation">t_target = target_origin + (t_source − source_origin) + offset</p>
    <dl class="details-list">
      ${detail('Model', e(words(mapping.model)))}${detail('Source observations', `${e(mapping.source_stream_id)} / ${e(mapping.source_segment_id)}`)}
      ${detail('Target clock', e(mapping.target_clock_id))}
      ${detail('Source origin', `<span class="mono">${e(mapping.source_origin_ns)} ns</span>`)}
      ${detail('Target origin', `<span class="mono">${e(mapping.target_origin_ns)} ns</span>`)}
      ${detail('Offset', `<span class="mono">${e(mapping.offset_ns)} ns</span>`)}
      ${detail('Source validity · inclusive', `<span class="mono">${e(mapping.first_source_ns)} → ${e(mapping.last_source_ns)} ns</span>`)}
      ${detail('Mapped validity · inclusive', endpoints)}
      ${detail('Evidence provenance', e(words(mapping.evidence.status)))}${detail('Evidence description', e(mapping.evidence.description))}
    </dl><p class="muted">A positive offset increases the target timestamp. Integer nanoseconds are applied once, after source tick conversion; no rounding or drift fit is introduced. Each clock reset requires its own explicit mapping.</p>
    <p class="muted">A clock-domain mapping and a sensor time offset describe different physical effects. Overlapping mappings are not implicitly composed.</p></section>`;
}

export function renderCalibrationArtifact(artifact) {
  const descriptor = artifact.contract;
  if (!descriptor || !['calibration', 'clock_mapping'].includes(descriptor.kind)) return '<p class="error" role="alert">Artifact details are unavailable.</p>';
  const calibration = descriptor.kind === 'calibration';
  return `<article class="calibration-artifact" data-artifact-id="${e(artifact.id)}"><div class="section-heading"><div><h3>${e(artifact.name || descriptor.id)}</h3><p class="muted">${calibration ? 'Calibration' : 'Clock mapping'} · immutable artifact</p></div></div>
    <dl class="details-list calibration-identity">
      ${detail('Contract identity', identity({ id: descriptor.id, sha256: artifact.contract_sha256 }))}
      ${detail('Source acquisition', identity(descriptor.acquisition))}
      ${calibration ? detail('Origin', e(words(descriptor.state))) + detail('Method / version', `${e(descriptor.method.name)} / ${e(descriptor.method.version)}`) : ''}
    </dl>
    ${calibration ? `<p class="section-note">${e(descriptor.applicability.description)}</p><dl class="details-list">
      ${detail('IMU intrinsic corrections', e(words(descriptor.applicability.imu_intrinsics)))}
      ${detail('Independent evidence', descriptor.validation.status === 'reported' ? 'External validation reported · its identity is recorded; physical conclusions are not certified here' : 'No independent validation report supplied')}</dl>
      <div class="calibration-parameters">${descriptor.cameras.map(cameraParameters).join('')}${imuParameters(descriptor.imu)}</div>
      <section class="calibration-transforms"><h4>Direct frame transforms</h4>${descriptor.transforms.length ? descriptor.transforms.map(transform => `<div class="surface calibration-transform"><h5>${e(transform.source_frame)} → ${e(transform.target_frame)}</h5>
        <p class="calibration-equation">p_target = R_target_source × p_source + t_target_source</p>${matrix(transform.rotation)}
        <dl class="details-list">${detail('Translation', vector(transform.translation_m, 'm'))}</dl>
        <p class="muted">Translation is the source origin expressed in the target frame. It is not added to angular velocity or specific force.</p></div>`).join('') : '<p class="muted">No direct transforms supplied.</p>'}</section>` : clockParameters(descriptor)}
    <details class="calibration-provenance"><summary>Descriptor values and supporting identities</summary><p class="muted">Download the retained JSON for its original numeric spelling and exact bytes. Content identities above are computed by the server.</p><pre tabindex="0" aria-label="Artifact descriptor JSON">${e(JSON.stringify(descriptor, null, 2))}</pre><pre tabindex="0" aria-label="Source acquisition bindings">${e(JSON.stringify(artifact.source_bindings || [], null, 2))}</pre></details>
    <p class="muted">Import and compatibility checks verify declarations. They do not demonstrate that a physical sensor, rig or clock matches those declarations.</p></article>`;
}

export function artifactPath(projectId, artifactId) {
  if (!UUID.test(projectId) || artifactId !== undefined && !UUID.test(artifactId)) throw new TypeError('Invalid artifact identity.');
  return `/projects/${projectId}/sensor-artifacts${artifactId === undefined ? '' : `/${artifactId}`}`;
}

export async function readArtifactText(file, maximum = 65536) {
  if (!Number.isSafeInteger(maximum) || maximum < 1 || maximum > 65536
    || !file || !Number.isSafeInteger(file.size) || file.size < 1 || file.size > maximum) {
    throw new TypeError(`Choose a nonempty JSON descriptor no larger than ${formatBytes(maximum)}.`);
  }
  const bytes = await file.arrayBuffer();
  if (bytes.byteLength !== file.size) throw new TypeError('The descriptor size changed while reading it.');
  try { return new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(bytes); }
  catch { throw new TypeError('The descriptor must be valid UTF-8 JSON text.'); }
}

export function inputRevisionPath(projectId, acquisitionId, revisionId) {
  const base = `${acquisitionPath(projectId, acquisitionId)}/input-revisions`;
  if (revisionId === undefined) return base;
  if (!UUID.test(revisionId)) throw new TypeError('Invalid input revision identity.');
  return `${base}/${revisionId}`;
}

export function revisionFilePath(projectId, acquisitionId, revisionId, path) {
  if (typeof path !== 'string' || !path || path.length > 512 || path.startsWith('/')
    || /[\\\u0000-\u001f]/.test(path) || path.split('/').some(part => !part || part === '.' || part === '..')) {
    throw new TypeError('Invalid retained file path.');
  }
  return `/api/v1${inputRevisionPath(projectId, acquisitionId, revisionId)}/files?${new URLSearchParams({ path })}`;
}

export function revisionSelection(values) {
  const optionalUuid = value => {
    if (value === '' || value === null || value === undefined) return null;
    if (!UUID.test(value)) throw new TypeError('Select an available artifact.');
    return value;
  };
  const clocks = values.clock_mapping_ids || [];
  if (!Array.isArray(clocks) || new Set(clocks).size !== clocks.length || clocks.some(id => !UUID.test(id))) {
    throw new TypeError('Select each clock mapping once.');
  }
  const identifier = value => value === '' || value === null || value === undefined ? null
    : typeof value === 'string' && /^[A-Za-z][A-Za-z0-9._-]{0,79}$/.test(value) ? value
      : (() => { throw new TypeError('Choose a declared sensor or frame.'); })();
  if (typeof values.apply_imu_intrinsics !== 'boolean' || typeof values.include_reference !== 'boolean') {
    throw new TypeError('Correction and reference choices must be explicit.');
  }
  const calibrationId = optionalUuid(values.calibration_id);
  const target = identifier(values.imu_target_frame);
  if (!calibrationId && (values.apply_imu_intrinsics || target)) throw new TypeError('Select a calibration before applying IMU corrections.');
  return { schema_version: 1, calibration_id: calibrationId, clock_mapping_ids: [...clocks],
    apply_imu_intrinsics: values.apply_imu_intrinsics, imu_target_frame: target,
    imu_stream_id: identifier(values.imu_stream_id), include_reference: values.include_reference };
}

export function inputRevisionStatus(revision) {
  if (revision.availability === 'deleted' || revision.deleted_at) return 'Files deleted';
  if (revision.state === 'ready') return revision.files_available ? 'Prepared input available' : 'Files unavailable';
  if (revision.cancel_requested && ['queued', 'preparing'].includes(revision.state)) return 'Cancellation requested';
  return { queued: 'Queued', preparing: 'Preparing', rejected: 'Rejected', cancelled: 'Cancelled' }[revision.state] || 'Unavailable';
}

function artifactList(projectId, artifacts) {
  return `<section class="calibration-library" aria-labelledby="artifact-list-heading"><div class="section-heading"><div><h3 id="artifact-list-heading">Saved artifacts</h3><p class="muted">Importing a descriptor preserves an immutable candidate. Selection and application are separate steps.</p></div><button type="button" data-calibration-action="import">Import artifact</button></div>
    ${artifacts.length ? `<div class="calibration-artifact-list">${artifacts.map(artifact => `<div class="calibration-artifact-row"><div><strong>${e(artifact.name)}</strong><p class="muted">${e(words(artifact.kind))} · ${e(artifact.contract_id)}${artifact.archived_at ? ' · archived' : ''}</p><p class="mono calibration-hash">${e(artifact.contract_sha256)}</p></div><div class="calibration-row-actions"><button type="button" data-calibration-action="inspect-artifact" data-id="${e(artifact.id)}" aria-label="Inspect ${e(artifact.name)}">Inspect</button><a class="button" href="/api/v1${artifactPath(projectId, artifact.id)}/contract" download>Download JSON</a>${!artifact.archived_at ? `<button type="button" data-calibration-action="archive-artifact" data-id="${e(artifact.id)}" aria-label="Archive ${e(artifact.name)}">Archive</button>` : ''}</div></div>`).join('')}</div>` : '<p class="calibration-empty muted">No supplied calibration or clock artifacts have been imported into this project.</p>'}
    <div data-calibration-artifact-detail></div></section>`;
}

function revisionList(projectId, acquisitionId, revisions) {
  return `<section class="calibration-history" aria-labelledby="input-revisions-heading"><h3 id="input-revisions-heading">Input revisions</h3><p class="muted">Each revision binds exact original observations, selected artifacts and an ordered preparation. Previous revisions remain unchanged.</p>
    ${revisions.length ? `<div class="calibration-revision-list">${revisions.map(revision => `<a href="${calibrationRoute(projectId, acquisitionId, revision.id)}"><div><h4>${e(revision.name)}</h4><p class="mono calibration-hash">${e(revision.id)}</p></div><span class="acquisition-status">${e(inputRevisionStatus(revision))}</span><span aria-hidden="true">→</span></a>`).join('')}</div>` : '<p class="calibration-empty muted">No derived input revision has been created.</p>'}</section>`;
}

function revisionForm(acquisition, descriptor, artifacts) {
  if (acquisition.state !== 'ready' || !acquisition.files_available || !descriptor) {
    return '<p class="section-note">The original acquisition files are unavailable. New input revisions cannot be prepared.</p>';
  }
  const calibrations = artifacts.filter(item => item.kind === 'calibration' && !item.archived_at);
  const clocks = artifacts.filter(item => item.kind === 'clock_mapping' && !item.archived_at);
  const imus = descriptor.streams.filter(stream => stream.kind === 'imu');
  const hasReference = descriptor.streams.some(stream => stream.kind === 'reference_pose');
  const selectedImu = acquisition.options?.imu_stream_id || (imus.length === 1 ? imus[0].id : '');
  return `<section class="surface calibration-preparation" aria-labelledby="create-input-heading"><h3 id="create-input-heading">Create an input revision</h3>
    <p class="muted">Prepare from the original recording. The source inspection describes original observations; corrected CSVs and the preparation report describe this revision.</p>
    <form data-calibration-revision><fieldset>
      <label>Revision name<input name="name" maxlength="100" required autocomplete="off"></label>
      <label>Calibration artifact<select name="calibration_id"><option value="">No calibration selected</option>${calibrations.map(item => `<option value="${e(item.id)}">${e(item.name)} · ${e(item.contract_id)}</option>`).join('')}</select></label>
      <p class="muted">Selecting a calibration binds its geometry and noise declarations. It does not apply IMU intrinsic corrections automatically.</p>
      <label>IMU stream<select name="imu_stream_id">${imus.length !== 1 ? '<option value="">Select an IMU stream</option>' : ''}${imus.map(stream => `<option value="${e(stream.id)}" ${stream.id === selectedImu ? 'selected' : ''}>${e(stream.id)} · ${e(stream.frame_id)}</option>`).join('')}</select></label>
      <div data-calibration-corrections><label class="acquisition-checkbox"><input type="checkbox" name="apply_imu_intrinsics" disabled>Apply supplied IMU bias and scale</label><label>IMU output frame<select name="imu_target_frame" disabled><option value="">Keep original frame</option></select></label></div>
      <p class="muted" data-calibration-correction-help>Select a calibration to inspect supported corrections. Camera images and reference poses are not transformed.</p>
      <fieldset class="calibration-clock-choices"><legend>Clock mappings to apply</legend>${clocks.length ? clocks.map(item => `<label class="acquisition-checkbox"><input type="checkbox" name="clock_mapping_ids" value="${e(item.id)}">${e(item.name)} · ${e(item.contract_id)}</label>`).join('') : '<p class="muted">No constant clock mappings are available. Import a descriptor to declare its purpose, segment and validity interval.</p>'}</fieldset>
      <label class="acquisition-checkbox"><input type="checkbox" name="include_reference" ${hasReference && acquisition.options?.include_reference !== false ? 'checked' : ''} ${hasReference ? '' : 'disabled'}>Include independent reference observations</label>
      <p class="muted">All supported source segments are retained. Mappings must cover their selected segments. No resampling, gap filling, clock drift fit or estimator execution is performed.</p>
      <div class="form-actions"><button type="button" data-calibration-action="assess">Check revision</button><button type="submit" class="primary" disabled>Create input revision</button></div>
      <p class="form-error" role="alert"></p>
    </fieldset></form><div data-calibration-assessment aria-live="polite"></div></section>`;
}

function importDialog(acquisitions, currentId, byteLimit) {
  const ready = acquisitions.filter(item => item.state === 'ready' && item.files_available);
  return `<dialog class="calibration-dialog" aria-labelledby="artifact-import-heading"><h3 id="artifact-import-heading">Import a structured artifact</h3>
    <p>Choose a Meridian calibration or clock-mapping JSON descriptor, up to ${formatBytes(byteLimit)}. It is stored without replacing an earlier candidate.</p>
    <form data-calibration-import><fieldset><label>Artifact name<input name="name" maxlength="100" required autocomplete="off"></label>
      <label>Descriptor JSON<input type="file" name="descriptor" accept=".json,application/json" required></label>
      <fieldset class="calibration-source-choices"><legend>Source and validation acquisitions</legend><p class="muted">Select the project recordings identified by the descriptor. IDs and content hashes must agree exactly.</p>
        ${ready.map(item => `<label class="acquisition-checkbox"><input type="checkbox" name="acquisition_ids" value="${e(item.id)}" ${item.id === currentId ? 'checked' : ''}>${e(item.name)}</label>`).join('')}</fieldset>
      <p class="form-error" role="alert"></p><div class="form-actions"><button type="button" data-calibration-action="close-dialog">Close</button><button class="primary" type="submit">Import artifact</button></div>
    </fieldset></form><p class="muted">This accepts structured sensor contracts. Runtime YAML, scripts, remote URLs and arbitrary preparation recipes are not accepted.</p></dialog>`;
}

export function renderRevisionPlan(plan, assessment) {
  const operations = plan?.operations || [];
  return `<section class="calibration-plan"><h4>Checked preparation</h4><p>The compatibility checks concern declared identities, supported operations and bounds. They do not establish physical calibration accuracy.</p>
    ${operations.length ? `<ol class="calibration-operations">${operations.map(operation => `<li><strong>${e(words(operation.kind))} · ${e(operation.stream_id)}</strong><p>${e(operation.description || '')}</p><code>${e(JSON.stringify(operation.parameters || {}))}</code></li>`).join('')}</ol>` : '<p class="muted">No numerical calibration or clock operation is listed. Selected metadata is bound to the derived input.</p>'}
    <details><summary>Exact assessment and preparation plan</summary><pre tabindex="0">${e(JSON.stringify({ assessment, plan }, null, 2))}</pre></details></section>`;
}

export function renderInputRevision(projectId, acquisitionId, revision) {
  const ready = revision.state === 'ready' && revision.files_available;
  const active = ['queued', 'preparing'].includes(revision.state);
  return `<article class="calibration-revision-detail"><a class="back-link" href="${calibrationRoute(projectId, acquisitionId)}">← Calibration &amp; timing</a>
    <div class="section-heading"><div><h3>${e(revision.name)}</h3><p class="mono calibration-hash">${e(revision.id)}</p></div><span class="acquisition-status">${e(inputRevisionStatus(revision))}</span></div>
    <p class="section-note">Immutable input revision. The original acquisition and earlier revisions are unchanged. Source inspection remains an inspection of original observations.</p>
    ${active ? '<p role="status">Preparation continues when you leave this page. Saved progress survives refresh.</p>' : ''}
    ${revision.failure ? `<div class="acquisition-failure" role="alert"><p>${e(revision.failure.message)}</p><p class="mono">${e(revision.failure.code)}</p></div>` : ''}
    ${revision.state === 'ready' && !ready ? '<p class="muted">Retained output files are unavailable. Saved lineage remains visible.</p>' : ''}
    <dl class="details-list">${detail('Request SHA-256', `<span class="mono">${e(revision.request_sha256)}</span>`)}
      ${detail('Selected calibration', e(revision.selection?.calibration_id || 'None'))}
      ${detail('IMU intrinsic application', revision.selection?.apply_imu_intrinsics ? 'Explicitly requested' : 'Not requested')}
      ${detail('IMU output frame', e(revision.selection?.imu_target_frame || 'Original frame'))}</dl>
    ${ready ? renderRevisionFiles(projectId, acquisitionId, revision) : ''}
    <details class="calibration-provenance"><summary>Saved selection, plan and result</summary><pre tabindex="0">${e(JSON.stringify({ selection: revision.selection, plan: revision.plan, result: revision.result }, null, 2))}</pre></details>
    <div class="calibration-row-actions">${active && !revision.cancel_requested ? '<button type="button" class="danger" data-calibration-action="cancel-revision">Cancel preparation</button>' : ''}
      ${ready ? '<button type="button" class="danger" data-calibration-action="delete-revision-files">Delete revision files</button>' : ''}
      <button type="button" data-calibration-action="refresh">Refresh status</button></div></article>`;
}

export function renderRevisionFiles(projectId, acquisitionId, revision) {
  const files = revision.manifest?.files || {};
  const descriptors = [
    ['prepared-input.json', 'Prepared descriptor'], ['preparation-report.json', 'Preparation report'],
    ['source-acquisition.json', 'Original acquisition descriptor'],
  ];
  const outputs = (revision.prepared_input?.outputs || []).map(output => [
    output.index_path, `${output.stream_id} · ${output.frame_id} · ${output.clock_id} · ns CSV`,
  ]);
  const paths = new Set();
  return `<section class="calibration-files"><h4>Retained revision files</h4><p class="muted">Downloads are the published bytes identified below. Observation IDs remain linked to the originals.</p>
    ${[...descriptors, ...outputs].filter(([path]) => files[path] && !paths.has(path) && paths.add(path)).map(([path, label]) => `<div class="calibration-file"><a href="${e(revisionFilePath(projectId, acquisitionId, revision.id, path))}" download>Download ${e(label)}</a><p class="muted">${formatBytes(files[path].size_bytes)}</p><p class="mono calibration-hash">${e(files[path].sha256)}</p></div>`).join('')}</section>`;
}

export function correctionChoices(contract, descriptor, imuId) {
  const stream = descriptor?.streams?.find(item => item.id === imuId && item.kind === 'imu');
  if (!stream || !contract || contract.kind !== 'calibration') return { intrinsic: false, frames: [] };
  const intrinsic = contract.imu?.sensor_id === stream.id
    && contract.imu?.frame_id === stream.frame_id
    && contract.applicability?.imu_intrinsics === 'candidate_not_applied'
    && stream.processing?.calibration === 'not_applied';
  const frames = contract.transforms.filter(transform => transform.source_frame === stream.frame_id
    && transform.translation_m.every(component => component === 0)).map(transform => transform.target_frame);
  return { intrinsic, frames: [...new Set(frames)] };
}

export function mountCalibration(container, { api, project, acquisition, descriptor, revisionId, onSessionExpired }) {
  if (!container) return null;
  const base = inputRevisionPath(project.id, acquisition.id);
  const controller = new AbortController();
  const details = new Map();
  const attempts = new Map();
  let live = true, busy = false, timer, generation = 0, artifactRequest = 0, checkedSelection = null;
  let artifacts = [], acquisitions = [], revisions = [], limits = {}, service = {}, artifactLimits = {}, revision = null;
  const node = selector => container.querySelector(selector);
  const reportError = (error, target) => {
    if (!live || error.name === 'AbortError') return;
    if (error.status === 401) { destroy(); void onSessionExpired?.(); return; }
    const message = error instanceof ApiError ? errorMessage(error) : error.message || 'The operation could not be completed.';
    const feedback = target?.isConnected ? target : node('[data-calibration-feedback]');
    if (feedback) { feedback.textContent = message; feedback.classList.add('error'); }
  };
  const request = (path, options = {}) => api.request(path, { ...options, signal: controller.signal });
  function attempt(key, body) {
    const identity = JSON.stringify(body);
    if (attempts.get(key)?.identity !== identity) attempts.set(key, { identity, key: crypto.randomUUID() });
    return { ...body, idempotency_key: attempts.get(key).key };
  }
  function selected() {
    const form = node('[data-calibration-revision]');
    const data = new FormData(form);
    return revisionSelection({ calibration_id: data.get('calibration_id'), clock_mapping_ids: data.getAll('clock_mapping_ids'),
      apply_imu_intrinsics: data.get('apply_imu_intrinsics') === 'on', imu_target_frame: data.get('imu_target_frame'),
      imu_stream_id: data.get('imu_stream_id'), include_reference: data.get('include_reference') === 'on' });
  }
  function invalidate() {
    generation += 1;
    checkedSelection = null;
    const submit = node('[data-calibration-revision] button[type="submit"]');
    if (submit) submit.disabled = true;
    const assessment = node('[data-calibration-assessment]');
    if (assessment) assessment.innerHTML = '';
  }
  function updateCorrections() {
    const form = node('[data-calibration-revision]');
    if (!form) return;
    const calibration = details.get(form.elements.calibration_id.value)?.contract;
    const choices = correctionChoices(calibration, descriptor, form.elements.imu_stream_id.value);
    const checkbox = form.elements.apply_imu_intrinsics, frame = form.elements.imu_target_frame, previous = frame.value;
    checkbox.disabled = !choices.intrinsic;
    if (!choices.intrinsic) checkbox.checked = false;
    frame.innerHTML = `<option value="">Keep original frame</option>${choices.frames.map(name => `<option value="${e(name)}" ${name === previous ? 'selected' : ''}>${e(name)} · direct IMU vector rotation</option>`).join('')}`;
    frame.disabled = !choices.frames.length;
    node('[data-calibration-correction-help]').textContent = !calibration ? 'Select a calibration to inspect supported corrections. Camera images and reference poses are not transformed.'
      : choices.intrinsic ? 'Bias/scale correction is optional and applied after SI conversion, before the selected frame rotation. The server verifies applicability before preparation.'
        : 'IMU intrinsic application is unavailable for this selection: the artifact and stream must match, with corrections explicitly declared not applied. Compatible zero-translation rotations are listed separately.';
  }
  async function inspectArtifact(id) {
    const token = ++artifactRequest;
    const panel = node('[data-calibration-artifact-detail]');
    if (!panel) return;
    panel.innerHTML = '<p class="loading" role="status">Loading artifact…</p>';
    try {
      if (!details.get(id)?.contract) {
        const response = await request(artifactPath(project.id, id));
        if (!live) return;
        details.set(id, response.artifact);
      }
      if (!live || token !== artifactRequest) return;
      panel.innerHTML = renderCalibrationArtifact(details.get(id));
      updateCorrections();
    } catch (error) { if (live && token === artifactRequest) { panel.innerHTML = ''; reportError(error); } }
  }
  function paint() {
    container.innerHTML = `<p class="calibration-feedback" role="status" data-calibration-feedback></p>${revisionId
      ? renderInputRevision(project.id, acquisition.id, revision)
      : `${artifactList(project.id, artifacts)}
        ${service.enabled === false ? '<p class="section-note">New input preparation is disabled on this deployment. Saved artifacts and revisions remain available.</p>' : ''}
        <p class="muted">Preparation limit: ${formatBytes(limits.prepared_bytes)} per revision${limits.per_acquisition ? ` · ${e(limits.per_acquisition)} revisions per acquisition` : ''}.</p>
        ${limits.per_acquisition && revisions.length >= limits.per_acquisition ? '<p class="section-note">The saved input revision limit has been reached for this acquisition.</p>' : revisionForm(acquisition, descriptor, artifacts)}
        <div data-calibration-history>${revisionList(project.id, acquisition.id, revisions)}</div>`}`;
  }
  function schedulePoll() {
    clearTimeout(timer);
    if (live && (revision ? ['queued', 'preparing'].includes(revision.state) : revisions.some(item => ['queued', 'preparing'].includes(item.state)))) {
      timer = setTimeout(poll, 2500);
    }
  }
  async function load() {
    try {
      const results = await Promise.all([
        request(artifactPath(project.id)), request(base), request(acquisitionPath(project.id)),
        ...(revisionId ? [request(inputRevisionPath(project.id, acquisition.id, revisionId))] : []),
      ]);
      if (!live) return;
      artifacts = results[0].artifacts; artifactLimits = results[0].limits || {};
      revisions = results[1].revisions; limits = results[1].limits || {}; service = results[1].service || {};
      acquisitions = results[2].acquisitions; revision = results[3]?.revision || null;
      checkedSelection = null; generation += 1;
      paint(); schedulePoll();
    } catch (error) {
      if (!live) return;
      container.innerHTML = '<p class="calibration-feedback error" role="alert" data-calibration-feedback></p><button type="button" data-calibration-action="refresh">Retry</button>';
      reportError(error);
    }
  }
  async function poll() {
    if (!live) return;
    if (busy || document.visibilityState === 'hidden' || container.querySelector('dialog[open]')) { schedulePoll(); return; }
    try {
      const response = await request(revisionId ? inputRevisionPath(project.id, acquisition.id, revisionId) : base);
      if (!live) return;
      if (revisionId) {
        const changed = JSON.stringify(revision) !== JSON.stringify(response.revision);
        revision = response.revision;
        if (changed) paint();
      } else {
        revisions = response.revisions;
        const history = node('[data-calibration-history]');
        if (history) history.innerHTML = revisionList(project.id, acquisition.id, revisions);
      }
    } catch (error) { reportError(error); }
    schedulePoll();
  }
  function openDialog(html) {
    container.querySelector('dialog')?.remove();
    container.insertAdjacentHTML('beforeend', html);
    const dialog = container.querySelector('dialog');
    dialog.addEventListener('close', () => dialog.remove(), { once: true });
    dialog.showModal();
  }
  function confirmation(action, id) {
    const archive = action === 'archive-artifact', cancel = action === 'cancel-revision';
    const title = archive ? 'Archive this artifact?' : cancel ? 'Cancel this preparation?' : 'Delete revision files?';
    const description = archive ? 'Remove this candidate from new selections. Its immutable identity and existing revision references remain recorded.'
      : cancel ? 'Request cancellation of this preparation. Earlier revisions and original observations remain unchanged.'
        : 'Revoke hosted downloads of this revision. Its saved lineage and original acquisition remain recorded. Downloaded copies remain usable.';
    return `<dialog class="calibration-dialog" aria-labelledby="calibration-confirm-heading"><h3 id="calibration-confirm-heading">${title}</h3><p>${description}</p>
      <form data-calibration-confirm="${e(action)}" data-id="${e(id || '')}"><fieldset><p class="form-error" role="alert"></p><div class="form-actions"><button type="button" data-calibration-action="close-dialog" autofocus>Keep unchanged</button><button type="submit" class="danger">${archive ? 'Archive artifact' : cancel ? 'Cancel preparation' : 'Delete revision files'}</button></div></fieldset></form></dialog>`;
  }
  async function assess() {
    const form = node('[data-calibration-revision]');
    if (busy || !form.reportValidity()) return;
    invalidate();
    const token = generation, selection = selected(), button = form.querySelector('[data-calibration-action="assess"]');
    const feedback = form.querySelector('.form-error');
    feedback.textContent = ''; busy = true; button.disabled = true;
    try {
      const response = await request(`${base}/assess`, { method: 'POST', body: selection });
      if (!live || token !== generation) return;
      if (response.eligible !== true) throw new Error('The selected revision is not eligible for supported preparation.');
      checkedSelection = JSON.stringify(selection);
      node('[data-calibration-assessment]').innerHTML = renderRevisionPlan(response.plan, response.assessment);
      form.querySelector('button[type="submit"]').disabled = service.enabled === false;
    } catch (error) { if (token === generation) reportError(error, feedback); }
    finally { busy = false; if (live && button.isConnected) button.disabled = false; }
  }
  const submit = async event => {
    const form = event.target;
    if (!form.matches('[data-calibration-import], [data-calibration-revision], [data-calibration-confirm]')) return;
    event.preventDefault(); event.stopPropagation();
    if (busy) return;
    busy = true;
    const fields = form.querySelector('fieldset'), feedback = form.querySelector('.form-error');
    feedback.textContent = '';
    try {
      if (form.matches('[data-calibration-import]')) {
        const data = new FormData(form), file = form.elements.descriptor.files[0];
        const maximum = Math.min(65536, artifactLimits.descriptor_bytes || 65536);
        if (!file || !file.size || file.size > maximum) throw new Error(`Choose a nonempty JSON descriptor no larger than ${formatBytes(maximum)}.`);
        const bindings = data.getAll('acquisition_ids');
        if (!bindings.length || bindings.length > 16) throw new Error('Select between one and sixteen exact source and validation acquisitions.');
        fields.disabled = true;
        const text = await readArtifactText(file, maximum);
        if (!live) return;
        const body = { schema_version: 1, name: data.get('name').trim(), descriptor_text: text, acquisition_ids: bindings };
        const response = await request(artifactPath(project.id), { method: 'POST', body: attempt('import', body) });
        if (!live) return;
        attempts.delete('import'); details.set(response.artifact.id, response.artifact);
        form.closest('dialog').close(); await load();
        if (live) await inspectArtifact(response.artifact.id);
      } else if (form.matches('[data-calibration-revision]')) {
        const selection = selected();
        if (JSON.stringify(selection) !== checkedSelection) throw new Error('Check this exact selection before creating the revision.');
        const name = form.elements.name.value.trim();
        fields.disabled = true;
        const response = await request(base, { method: 'POST', body: attempt('revision', { ...selection, name }) });
        if (!live) return;
        attempts.delete('revision');
        window.location.hash = calibrationRoute(project.id, acquisition.id, response.revision.id);
      } else {
        fields.disabled = true;
        const action = form.dataset.calibrationConfirm;
        const path = action === 'archive-artifact' ? artifactPath(project.id, form.dataset.id)
          : `${inputRevisionPath(project.id, acquisition.id, revision.id)}/${action === 'cancel-revision' ? 'cancel' : 'files'}`;
        await request(path, { method: action === 'cancel-revision' ? 'POST' : 'DELETE',
          ...(action === 'cancel-revision' ? { body: { schema_version: 1 } } : {}) });
        if (!live) return;
        form.closest('dialog').close(); await load();
      }
    } catch (error) { reportError(error, feedback); }
    finally { busy = false; if (live && fields.isConnected) fields.disabled = false; }
  };
  const change = event => {
    if (!event.target.closest('[data-calibration-revision]')) return;
    invalidate();
    if (event.target.name === 'calibration_id') {
      updateCorrections();
      if (event.target.value) void inspectArtifact(event.target.value);
    } else if (event.target.name === 'imu_stream_id') updateCorrections();
  };
  const input = event => { if (event.target.closest('[data-calibration-revision]')) invalidate(); };
  const click = event => {
    const button = event.target.closest('[data-calibration-action]');
    if (!button) return;
    const action = button.dataset.calibrationAction;
    if (action === 'close-dialog') { container.querySelector('dialog')?.close(); return; }
    if (busy) return;
    if (action === 'import') openDialog(importDialog(acquisitions, acquisition.id, Math.min(65536, artifactLimits.descriptor_bytes || 65536)));
    else if (action === 'inspect-artifact') void inspectArtifact(button.dataset.id);
    else if (action === 'assess') void assess().catch(error => reportError(error));
    else if (['archive-artifact', 'cancel-revision', 'delete-revision-files'].includes(action)) openDialog(confirmation(action, button.dataset.id));
    else if (action === 'refresh') void load();
  };
  function destroy() {
    live = false; generation += 1; artifactRequest += 1;
    clearTimeout(timer); controller.abort(); details.clear(); attempts.clear();
    artifacts = []; acquisitions = []; revisions = []; revision = null;
    container.removeEventListener('submit', submit); container.removeEventListener('change', change);
    container.removeEventListener('input', input); container.removeEventListener('click', click);
    container.querySelector('dialog')?.close();
  }
  container.addEventListener('submit', submit); container.addEventListener('change', change);
  container.addEventListener('input', input); container.addEventListener('click', click);
  void load();
  return { destroy };
}
