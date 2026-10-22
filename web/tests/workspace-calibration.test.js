import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { parseRoute } from '../src/workspace-api.js';
import { artifactPath, calibrationRoute, calibrationShell, correctionChoices, inputRevisionPath,
  inputRevisionStatus, mappedTimestamp, mountCalibration, readArtifactText, renderCalibrationArtifact,
  renderInputRevision, renderRevisionFiles, renderRevisionPlan, revisionFilePath, revisionSelection } from '../src/workspace-calibration.js';

const project = '11111111-1111-4111-8111-111111111111';
const acquisition = '22222222-2222-4222-8222-222222222222';
const artifactId = '33333333-3333-4333-8333-333333333333';
const revisionId = '44444444-4444-4444-8444-444444444444';
const example = name => JSON.parse(readFileSync(new URL(`../../examples/sensor-contracts/${name}.json`, import.meta.url), 'utf8'));
const calibration = example('calibration'), mapping = example('clock-mapping');
const descriptor = example('stereo-no-reference');
const artifact = { id: artifactId, name: 'Supplied geometry', contract: calibration, contract_sha256: 'a'.repeat(64), source_bindings: [] };
const selection = { calibration_id: artifactId, clock_mapping_ids: [], apply_imu_intrinsics: false,
  imu_target_frame: null, imu_stream_id: 'imu0', include_reference: false };
const revision = { id: revisionId, name: 'Prepared version', state: 'ready', files_available: true,
  request_sha256: 'b'.repeat(64), selection, plan: {}, result: {}, prepared_input: { outputs: [{
    stream_id: 'imu0', index_path: 'sensors/imu0/data.csv', frame_id: 'imu', clock_id: 'camera-clock',
  }] }, manifest: { files: {
    'prepared-input.json': { sha256: 'c'.repeat(64), size_bytes: 123 },
    'preparation-report.json': { sha256: 'd'.repeat(64), size_bytes: 456 },
    'sensors/imu0/data.csv': { sha256: 'e'.repeat(64), size_bytes: 321 },
  } } };

test('calibration and immutable input revision deep links preserve acquisition scope', () => {
  assert.deepEqual(parseRoute(calibrationRoute(project, acquisition)), {
    page: 'project', id: project, section: 'data', acquisitionId: acquisition, calibration: true,
  });
  assert.deepEqual(parseRoute(calibrationRoute(project, acquisition, revisionId)), {
    page: 'project', id: project, section: 'data', acquisitionId: acquisition, calibration: true, inputRevisionId: revisionId,
  });
  for (const suffix of ['/unknown', '?source=foreign', '/revisions/1', '/revisions/../secret']) {
    assert.deepEqual(parseRoute(calibrationRoute(project, acquisition) + suffix), { page: 'not-found' });
  }
  assert.throws(() => calibrationRoute(project, acquisition, '../../other'));
  assert.equal(artifactPath(project, artifactId), `/projects/${project}/sensor-artifacts/${artifactId}`);
  assert.equal(inputRevisionPath(project, acquisition, revisionId), `/projects/${project}/acquisitions/${acquisition}/input-revisions/${revisionId}`);
});

test('descriptor file ingestion preserves duplicate keys and numeric spelling for strict server parsing', async () => {
  const original = '{"kind":"calibration","number":4.0,"kind":"clock_mapping"}\n';
  const bytes = new TextEncoder().encode(original);
  const result = await readArtifactText({ size: bytes.length, arrayBuffer: async () => bytes.buffer });
  assert.equal(result, original);
  assert.notEqual(result, JSON.stringify(JSON.parse(result)));
});

test('descriptor bounds and invalid UTF-8 are rejected without lossy replacement', async () => {
  let reads = 0;
  const oversized = { size: 65537, arrayBuffer: async () => { reads += 1; return new ArrayBuffer(65537); } };
  await assert.rejects(readArtifactText(oversized), /no larger/);
  assert.equal(reads, 0);
  await assert.rejects(readArtifactText({ size: 1, arrayBuffer: async () => Uint8Array.from([0xff]).buffer }), /valid UTF-8/);
  await assert.rejects(readArtifactText({ size: 2, arrayBuffer: async () => new ArrayBuffer(1) }), /size changed/);
  await assert.rejects(readArtifactText({ size: 0 }), /nonempty/);
});

test('constant clock examples retain signed epoch nanoseconds and offset direction exactly', () => {
  assert.equal(mappedTimestamp(mapping, '1520530308199447626'), '1520530308199447628');
  assert.equal(mappedTimestamp({ ...mapping, offset_ns: '-2' }, '1520530308199447626'), '1520530308199447624');
  assert.equal(mappedTimestamp({ ...mapping, source_origin_ns: '9223372036854775807',
    target_origin_ns: '-9223372036854775808', offset_ns: '1' }, '9223372036854775807'), '-9223372036854775807');
  assert.throws(() => mappedTimestamp({ ...mapping, offset_ns: '3' }, '9223372036854775807'), RangeError);
  for (const input of [1520530308199447626, '+1', '-0', '01', '1.0']) assert.throws(() => mappedTimestamp(mapping, input));
});

test('artifact selection alone never applies intrinsic or frame correction', () => {
  assert.deepEqual(revisionSelection(selection), { schema_version: 1, ...selection });
  assert.deepEqual(revisionSelection({ ...selection, calibration_id: '', imu_stream_id: '' }), {
    schema_version: 1, ...selection, calibration_id: null, imu_stream_id: null,
  });
  assert.throws(() => revisionSelection({ ...selection, calibration_id: null, apply_imu_intrinsics: true }), /Select a calibration/);
  assert.throws(() => revisionSelection({ ...selection, calibration_id: null, imu_target_frame: 'body' }), /Select a calibration/);
  assert.throws(() => revisionSelection({ ...selection, apply_imu_intrinsics: 'false' }), /explicit/);
  assert.throws(() => revisionSelection({ ...selection, clock_mapping_ids: [artifactId, artifactId] }), /once/);
  assert.throws(() => revisionSelection({ ...selection, clock_mapping_ids: ['../secret'] }), /once/);
});

test('offered corrections require explicit source processing and direct zero-translation IMU geometry', () => {
  const supplied = structuredClone(calibration);
  supplied.imu = { sensor_id: 'imu0', frame_id: 'imu' };
  supplied.applicability.imu_intrinsics = 'candidate_not_applied';
  const source = structuredClone(descriptor);
  source.streams.find(item => item.id === 'imu0').processing.calibration = 'not_applied';
  assert.deepEqual(correctionChoices(supplied, source, 'imu0'), { intrinsic: true, frames: ['cam0'] });
  for (const state of ['applied', 'unknown']) {
    source.streams.find(item => item.id === 'imu0').processing.calibration = state;
    assert.equal(correctionChoices(supplied, source, 'imu0').intrinsic, false);
  }
  assert.deepEqual(correctionChoices(supplied, source, 'cam0'), { intrinsic: false, frames: [] });
  supplied.transforms = supplied.transforms.map(item => ({ ...item, source_frame: 'unrelated' }));
  assert.deepEqual(correctionChoices(supplied, source, 'imu0').frames, []);
});

test('camera geometry is labeled with direction, parameter order and physical validity limits', () => {
  const html = renderCalibrationArtifact(artifact);
  for (const text of ['p_target = R_target_source', 'imu → cam0', '8 × 6 pixels',
    'fx', 'fy', 'cx', 'cy', 'k1', 'k2', 'p1', 'p2', '4 px', 'No independent validation',
    'does not warp', 'not added to angular velocity', 'original numeric spelling']) assert.ok(html.includes(text), text);
  assert.ok(html.includes('a'.repeat(64)));
  assert.doesNotMatch(html, /physically validated|data-action="launch-job"/);
});

test('reported external validation remains an unverified claim and private text is escaped', () => {
  const supplied = structuredClone(calibration);
  supplied.validation.status = 'reported';
  supplied.applicability.description = '<img src=x onerror=1>';
  supplied.method.name = '<script>';
  const html = renderCalibrationArtifact({ ...artifact, name: '<script>candidate', contract: supplied });
  assert.match(html, /External validation reported/);
  assert.match(html, /physical conclusions are not certified/);
  assert.match(html, /&lt;img/);
  assert.doesNotMatch(html, /<img|<script>/);
});

test('noise density and bias diffusion retain different explicit units', () => {
  const supplied = structuredClone(calibration);
  supplied.imu = { sensor_id: 'imu0', frame_id: 'imu', gyro_bias_rad_s: [1, 2, 3], accel_bias_m_s2: [4, 5, 6],
    gyro_scale: [1, 2, 3], accel_scale: [2, 3, 4], noise: { model: 'continuous_white_noise_and_bias_diffusion',
      gyro_density_rad_s_per_sqrt_hz: 0.01, accel_density_m_s2_per_sqrt_hz: 0.02,
      gyro_bias_diffusion_rad_s_per_sqrt_s: 0.03, accel_bias_diffusion_m_s2_per_sqrt_s: 0.04,
      assumptions: 'Externally supplied assumptions.' } };
  const html = renderCalibrationArtifact({ ...artifact, contract: supplied });
  for (const text of ['0.01 rad/s/√Hz', '0.02 m/s²/√Hz', '0.03 (rad/s)/√s', '0.04 (m/s²)/√s',
    'not sample variances', 'scale × (measurement − bias)', '1 / 2 / 3 rad/s']) assert.ok(html.includes(text), text);
});

test('clock-domain and sensor-offset purposes remain separate with exact origins and closed bounds', () => {
  const html = renderCalibrationArtifact({ ...artifact, contract: mapping });
  for (const text of ['Clock-domain mapping', '1520530308199447626 ns', '2 ns', 'inclusive',
    '1520530308194447628', 'positive offset increases', 'no rounding or drift fit',
    'not implicitly composed']) assert.ok(html.includes(text), text);
  assert.match(renderCalibrationArtifact({ ...artifact, contract: { ...mapping, purpose: 'sensor_time_offset' } }), /<h4>Sensor time offset/);
});

test('revision lifecycle distinguishes immutable metadata from retained file availability', () => {
  assert.equal(inputRevisionStatus(revision), 'Prepared input available');
  assert.equal(inputRevisionStatus({ ...revision, files_available: false }), 'Files unavailable');
  assert.equal(inputRevisionStatus({ ...revision, availability: 'deleted' }), 'Files deleted');
  assert.equal(inputRevisionStatus({ ...revision, state: 'preparing', cancel_requested: true }), 'Cancellation requested');
  assert.equal(inputRevisionStatus({ ...revision, state: 'queued' }), 'Queued');
  assert.equal(inputRevisionStatus({ ...revision, state: 'cancelled' }), 'Cancelled');
});

test('revision downloads point at retained bytes and only inventoried output files', () => {
  const html = renderRevisionFiles(project, acquisition, revision);
  assert.match(html, /files\?path=prepared-input.json/);
  assert.match(html, /files\?path=preparation-report.json/);
  assert.match(html, /files\?path=sensors%2Fimu0%2Fdata.csv/);
  assert.match(html, /e{64}/);
  assert.doesNotMatch(html, /blob:|source-acquisition.json/);
  for (const path of ['/secret', '../secret', 'sensors/../secret', 'sensors\\secret', 'sensors//data']) {
    assert.throws(() => revisionFilePath(project, acquisition, revisionId, path));
  }
});

test('unavailable revision files do not expose downloads or imply corrected source inspection', () => {
  const html = renderInputRevision(project, acquisition, { ...revision, files_available: false, availability: 'deleted' });
  assert.match(html, /Files deleted/);
  assert.match(html, /Source inspection remains an inspection of original observations/);
  assert.match(html, /earlier revisions are unchanged/);
  assert.doesNotMatch(html, /files\?path|Download|delete-revision-files/);
  const shell = calibrationShell({ project_id: project, id: acquisition, name: 'Acquisition' });
  assert.match(shell, /VIO execution is not available yet/);
});

test('checked plan preserves operation order and does not claim physical accuracy', () => {
  const html = renderRevisionPlan({ operations: [
    { kind: 'unit_conversion', stream_id: 'imu0', parameters: { quantity: 'angular_velocity' } },
    { kind: 'imu_calibration', stream_id: 'imu0', parameters: { model: 'diagonal_scale_after_bias_subtraction' } },
    { kind: 'frame_transform', stream_id: 'imu0', parameters: { source_frame: 'imu', target_frame: 'body' } },
  ] }, { physical_validity: 'not_assessed' });
  assert.ok(html.indexOf('unit conversion') < html.indexOf('imu calibration'));
  assert.ok(html.indexOf('imu calibration') < html.indexOf('frame transform'));
  assert.match(html, /do not establish physical calibration accuracy/);
});

test('late private responses after unmount cannot repopulate calibration content', async () => {
  const pending = [];
  const events = new Map();
  const container = { innerHTML: 'private content removed',
    addEventListener: (name, handler) => events.set(name, handler),
    removeEventListener: name => events.delete(name), querySelector: () => null };
  const api = { request: (path, options) => new Promise(resolve => pending.push({ path, options, resolve })) };
  const mounted = mountCalibration(container, { api, project: { id: project },
    acquisition: { id: acquisition, project_id: project }, descriptor });
  assert.equal(pending.length, 3);
  mounted.destroy();
  assert.ok(pending.every(item => item.options.signal.aborted));
  pending[0].resolve({ artifacts: [], limits: {} });
  pending[1].resolve({ revisions: [], limits: {}, service: {} });
  pending[2].resolve({ acquisitions: [] });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(container.innerHTML, 'private content removed');
  assert.equal(events.size, 0);
});
