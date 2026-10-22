import assert from 'node:assert/strict';
import test from 'node:test';
import { PRIVATE_PROFILE, executionSelection, executionDraft, privateExperimentPayload, renderExecutionInputs, renderExecutionAdmission, renderExecutionIdentity } from '../src/workspace-execution-inputs.js';
import { revisionDraft, draftChanged, createExperimentDialog, renderComparison } from '../src/workspace-experiments.js';
import { validateInvestigation, investigationPresentation, finitePlotDomain } from '../src/workspace-investigation-data.js';
import { investigationContent, renderIntervalMetrics } from '../src/workspace-investigation.js';
import { renderJobDetail } from '../src/workspace-jobs.js';
import { renderRunExport } from '../src/workspace-exports.js';

const id = '12345678-abcd-abcd-abcd-123456789abc';
const first = '1520530308199447626';
const last = '1520530308199447627';
const values = { name: 'Private run', description: '', num_pts: '200', profile_id: PRIVATE_PROFILE,
  input_revision_id: id, segment_id: 'segment0', first_ns: first, last_ns: last };
const profile = { id: PRIVATE_PROFILE, estimator: 'OpenVINS',
  parameters: { num_pts: { choices: [100, 200, 400], default: 200, label: 'Feature budget', unit: 'features', description: 'Fixed choices.' } },
  limitations: ['Supplied calibration remains fixed.'] };
const interval = { segment_id: 'segment0', first_ns: first, last_ns: last };

test('private selection retains one-nanosecond epoch differences and never derives absolute floats', () => {
  assert.deepEqual(executionSelection(values), { input_revision_id: id, interval });
  assert.deepEqual(executionSelection(values, { full: true }), { input_revision_id: id, interval: null });
  const payload = privateExperimentPayload({ ...values, dataset_id: 'ignored', command: 'ignored' }, profile);
  assert.deepEqual(payload, { schema_version: 1, profile_id: PRIVATE_PROFILE, input_revision_id: id,
    interval, name: 'Private run', description: '', parameters: { num_pts: 200 } });
  assert.equal(privateExperimentPayload(values, profile, { baseVersion: 4 }).base_version, 4);
});

test('private admission selectors reject noncanonical, negative, overflow and inverted epochs', () => {
  for (const timestamp of ['-1', '+1', '01', '1.0', '1e18', '9223372036854775808', Number(first), false]) {
    assert.throws(() => executionSelection({ ...values, first_ns: timestamp }), /integer nanosecond/);
  }
  assert.throws(() => executionSelection({ ...values, first_ns: last, last_ns: first }));
  assert.throws(() => executionSelection({ ...values, segment_id: '../segment' }));
  assert.throws(() => executionSelection({ ...values, input_revision_id: 'not-owned-id' }));
  assert.throws(() => privateExperimentPayload({ ...values, num_pts: '250' }, profile), /supported/);
});

test('private drafts and dirty checking retain every exact immutable input selector', () => {
  const selection = { input_revision_id: id, interval };
  assert.deepEqual(executionDraft(selection), { profile_id: PRIVATE_PROFILE, input_revision_id: id, ...interval });
  const draft = revisionDraft({ name: values.name, description: '', parameters: { num_pts: 200 }, input_selection: selection }, profile);
  assert.deepEqual(draft, values);
  for (const key of ['profile_id', 'input_revision_id', 'segment_id', 'first_ns', 'last_ns']) {
    assert.ok(draftChanged(values, { ...values, [key]: `${values[key]}changed` }), key);
  }
});

test('reference workflow remains a distinct default while private input files stay explicitly identified', () => {
  const html = createExperimentDialog({ dataset_id: 'legacy-room1' }, { ...profile, id: 'legacy' },
    { name: '', description: '', num_pts: '200', profile_id: 'legacy' }, { profile, inputs: [
      { id, name: 'Prepared <candidate>', acquisition_name: 'Owned recording', availability: 'deleted' },
    ] });
  assert.match(html, /Reference benchmark · legacy-room1/);
  assert.match(html, /Private prepared input · stereo \+ IMU/);
  assert.match(html, /Prepared &lt;candidate&gt; · deleted/);
  assert.doesNotMatch(html, /<candidate>/);
  assert.match(renderExecutionInputs([], values), /Saved revision · availability must be checked/);
});

test('ineligible inputs expose server reasons without suggesting numerical failure or launching jobs', () => {
  const html = renderExecutionAdmission({ eligible: false,
    reasons: [{ code: 'calibration_required', message: 'Select a compatible <calibration>.' }] });
  assert.match(html, /Input cannot run with this profile/);
  assert.match(html, /calibration_required/);
  assert.match(html, /&lt;calibration&gt;/);
  assert.doesNotMatch(html, /Run version|qualification passed|<calibration>/);
});

test('frozen private identity declares fixed calibration, actual preparation and unavailable reference', () => {
  const html = renderExecutionIdentity({ profile_id: PRIVATE_PROFILE, input: { input_revision_id: id,
    prepared_input_sha256: 'prepared-hash', calibration: { id: 'supplied', sha256: 'calibration-hash' }, clock_mappings: [] },
    clock_id: 'camera-clock', reference: { stream: null, unavailable_reason: 'not_selected' }, policy: { gravity_m_s2: 9.80766 } });
  for (const text of ['prepared-hash', 'calibration-hash', 'camera-clock', 'remain fixed', 'masks are disabled', '9.80766', 'Accuracy unavailable']) assert.ok(html.includes(text), text);
  assert.equal(renderExecutionIdentity({ profile_id: 'legacy' }), '');
});

test('version differences display structured interval identities rather than an object placeholder', () => {
  const html = renderComparison({ from_version: 1, to_version: 2,
    changes: [{ field: 'input_selection.interval', before: interval, after: { ...interval, last_ns: first } }] });
  assert.match(html, new RegExp(last));
  assert.doesNotMatch(html, /\[object Object\]/);
});

const pose = { position_m: [1, 2, 3], quaternion_xyzw: [0, 0, 0, 1], rpy_deg: [0, 0, 0] };
function investigation() {
  return { schema_version: 2, specification_sha256: 'spec', run_state: 'complete', alignment: null,
    input: { profile_id: PRIVATE_PROFILE, input_revision_id: id, prepared_input_sha256: 'prepared-hash',
      clock_id: 'camera-clock', imu_frame_id: 'corrected-imu', processing: [{ kind: 'imu_calibration' }] },
    cameras: [0, 1].map(index => ({ slot: `cam${index}`, stream_id: `camera${index}`,
      frame_id: `optical${index}`, resolution: [640, 480], encoding: 'mono8' })),
    time: { origin_timestamp_ns: first, duration_s: .05 }, summary: { metrics: null },
    evaluation: { status: 'unavailable', reason: 'reference_not_selected' },
    imu: { source: 'prepared_input', samples: [{ timestamp_ns: first, time_s: 0, gyro_rad_s: [0, 0, 0], accel_m_s2: [0, 0, 9.80766] }] },
    frames: [{ index: 0, timestamp_ns: first, time_s: 0, estimated: true, estimate_raw: structuredClone(pose),
      estimate: null, reference: null, reference_supported: false, associated: false, evaluated: false,
      position_error_m: null, orientation_error_deg: null }] };
}

test('no-reference private evidence retains raw poses without labelling them aligned or overlaying reference', () => {
  const data = investigation();
  assert.equal(validateInvestigation(data, 'spec'), data);
  const view = investigationPresentation(data);
  assert.equal(view.poseKey, 'estimate_raw');
  assert.equal(view.showReference, false);
  assert.equal(view.accuracyAvailable, false);
  assert.equal(view.worldLabel, 'Estimator world');
  const html = investigationContent(data);
  for (const text of ['Accuracy unavailable', 'estimator’s own world frame', '640 × 480', 'prepared mono8 PNG', 'Nearest prepared IMU sample', 'exact prepared measurements']) assert.ok(html.includes(text), text);
  assert.doesNotMatch(html, /original mono16|512 × 512|without resampling or bias correction/);
});

test('private investigation rejects invented scores and ambiguous pose frames when alignment is absent', () => {
  for (const mutate of [
    data => { data.frames[0].estimate = pose; },
    data => { data.frames[0].position_error_m = 0; },
    data => { data.cameras[0].resolution[0] = 0; },
    data => { data.cameras[1].slot = 'cam0'; },
    data => { data.evaluation.status = 'passed'; },
  ]) {
    const data = investigation(); mutate(data);
    assert.throws(() => validateInvestigation(data, 'spec'), /inconsistent/);
  }
});

test('partial reference preserves aligned evaluation and legacy unavailable-alignment behavior', () => {
  const data = investigation(); data.alignment = { rotation: 'retained' }; data.evaluation.status = 'partial';
  const view = investigationPresentation(data);
  assert.equal(view.poseKey, 'estimate'); assert.equal(view.showReference, true); assert.equal(view.accuracyAvailable, true);
  assert.equal(investigationPresentation({ schema_version: 1, alignment: null }).poseKey, 'estimate');
  const html = renderIntervalMetrics({ status: 'ok', evaluation: { status: 'partial', reason: 'reference_gaps' },
    counts: { expected_camera_samples: 100, estimate_samples: 90, reference_supported_camera_samples: 75, evaluated_camera_samples: 70 },
    metrics: { ate_rmse_m: .125 }, coverage_fraction: .9, evaluated_fraction: .7, reference_coverage_fraction: .75 });
  for (const text of ['partial', 'reference_gaps', '0.12500', '75 reference-supported', '70 evaluated']) assert.ok(html.includes(text), text);
});

test('unavailable interval accuracy has an explicit reason and no fabricated trajectory-error metric', () => {
  const html = renderIntervalMetrics({ status: 'insufficient_associations', evaluation: { status: 'unavailable', reason: 'not_selected' },
    metrics: null, counts: { expected_camera_samples: 100, estimate_samples: 90, reference_supported_camera_samples: 0, evaluated_camera_samples: 0 },
    coverage_fraction: .9, evaluated_fraction: 0, reference_coverage_fraction: 0 });
  assert.match(html, /Accuracy unavailable/); assert.match(html, /90 estimated/); assert.match(html, /not_selected/);
  assert.doesNotMatch(html, /ATE RMSE|Orientation RMSE|0\.00000/);
});

test('successful private jobs remain successful without a room1 numerical qualification or accuracy score', () => {
  const job = { id, project_id: id, experiment_id: id, experiment_name: 'Private', revision_version: 1,
    profile_id: PRIVATE_PROFILE, parameters: { num_pts: 200 }, state: 'succeeded', phase: 'complete', attempt_count: 1, max_attempts: 2,
    result: { qualification: { passed: null, status: 'not_assessed' },
      evaluation: { status: 'insufficient_associations', availability: { status: 'unavailable', reason: 'reference_not_selected' },
        metrics: null, counts: { expected_camera_samples: 100, estimate_samples: 90 }, coverage_fraction: .9, protocol: {} },
      execution: {}, artifacts: {} } };
  const html = renderJobDetail(job);
  assert.match(html, /Succeeded/); assert.match(html, /Accuracy unavailable/); assert.match(html, /not assessed/);
  assert.doesNotMatch(html, /qualification did not pass|qualification passed|ATE RMSE|NaN|undefined/);
});

test('private exports cannot promise the public room1 reproduction recipe', () => {
  const html = renderRunExport({ profile_id: PRIVATE_PROFILE, state: 'succeeded', export_available: true });
  assert.match(html, /private inputs are not available yet/);
  assert.doesNotMatch(html, /Download experiment|public TUM-VI recording/);
});

test('finite but unrepresentable display ranges are withheld rather than producing invalid SVG values', () => {
  assert.equal(finitePlotDomain([-1e308, 1e308]), null);
  const bounds = finitePlotDomain([1, 3]);
  assert.ok(bounds[0] < 1 && bounds[1] > 3 && bounds.every(Number.isFinite));
  assert.deepEqual(finitePlotDomain([]), [-1, 1]);
});
