import assert from 'node:assert/strict';
import test from 'node:test';
import { createApiClient, parseRoute, ApiError } from '../src/workspace-api.js';
import { validateInvestigation, nearestFrame, intervalIndices, displayEnvelope, plotSegments, frameStatus, supportIntervals } from '../src/workspace-investigation-data.js';
import { createLatestRequest, investigationError, renderIntervalMetrics } from '../src/workspace-investigation.js';

const origin = 1520530000000000000n;
const pose = { position_m: [1, 2, 3], quaternion_xyzw: [0, 0, 0, 1], rpy_deg: [0, 0, 0] };
function fixture() {
  return { schema_version: 1, specification_sha256: 'identity', time: { origin_timestamp_ns: String(origin) }, summary: {}, imu: { samples: [{ timestamp_ns: String(origin - 1n), time_s: -1e-9, gyro_rad_s: [0, 1, 2], accel_m_s2: [0, 0, 9.81] }] }, frames: [0, 1, 2, 3].map(index => ({ index, timestamp_ns: String(origin + BigInt(index) * 50000000n), time_s: index * .05, estimated: index > 0, reference_supported: index !== 2, associated: index > 0 && index !== 2, evaluated: index > 0 && index !== 2, estimate_raw: index > 0 ? structuredClone(pose) : null, estimate: index > 0 ? structuredClone(pose) : null, reference: index !== 2 ? structuredClone(pose) : null, position_error_m: index > 0 && index !== 2 ? .1 : null, orientation_error_deg: index > 0 && index !== 2 ? 1 : null, missing_estimate_reason: index === 0 ? 'initialization' : null })) };
}

test('full-resolution validation keeps exact nanoseconds, initial gaps and raw leading IMU samples', () => {
  const data = fixture();
  assert.equal(validateInvestigation(data, 'identity'), data);
  assert.equal(data.frames[1].timestamp_ns, '1520530000050000000');
  assert.equal(data.frames[0].estimate, null);
  assert.ok(data.imu.samples[0].time_s < 0);
  assert.throws(() => validateInvestigation(data, 'another-run'), /inconsistent/);
});

test('malformed support, numeric absolute timestamps and non-finite signals fail closed', () => {
  const mutations = [data => { data.frames[1].timestamp_ns = Number(origin); }, data => { data.frames[2].index = 8; }, data => { data.frames[1].time_s = 0; }, data => { data.frames[0].evaluated = true; }, data => { data.frames[1].position_error_m = Infinity; }, data => { data.imu.samples[0].gyro_rad_s[0] = NaN; }];
  for (const mutate of mutations) { const data = fixture(); mutate(data); assert.throws(() => validateInvestigation(data, 'identity')); }
});

test('unavailable alignment can retain raw poses without treating them as aligned poses', () => {
  const data = fixture();
  data.alignment = null;
  for (const frame of data.frames) { frame.estimate = null; frame.evaluated = false; frame.position_error_m = null; frame.orientation_error_deg = null; }
  assert.equal(validateInvestigation(data, 'identity'), data);
});

test('shared cursor chooses actual nearest camera epochs, clamps bounds and breaks ties toward the earlier epoch', () => {
  const frames = fixture().frames;
  assert.equal(nearestFrame(frames, -10), 0);
  assert.equal(nearestFrame(frames, 10), 3);
  assert.equal(nearestFrame(frames, .025), 0);
  assert.deepEqual(intervalIndices(frames, .049, .149), [1, 3]);
  assert.throws(() => intervalIndices(frames, .2, .1), RangeError);
  assert.throws(() => intervalIndices(frames, NaN, .1), RangeError);
});

test('display envelope preserves isolated extrema and chronological endpoints without altering source rows', () => {
  const points = Array.from({ length: 10000 }, (_, index) => [index / 100, index === 5001 ? 1000 : index === 6001 ? -900 : Math.sin(index)]);
  const copy = structuredClone(points);
  const display = displayEnvelope(points, 200);
  assert.ok(display.length <= 200);
  assert.deepEqual(display[0], points[0]);
  assert.deepEqual(display.at(-1), points.at(-1));
  assert.ok(display.some(point => point[1] === 1000));
  assert.ok(display.some(point => point[1] === -900));
  assert.ok(display.every((point, index) => !index || point[0] > display[index - 1][0]));
  assert.deepEqual(points, copy);
});

test('plot paths never bridge missing output or principal-angle wrap crossings', () => {
  const rows = [170, 179, -179, -170, null, 10].map((value, index) => ({ time_s: index, value }));
  assert.deepEqual(plotSegments(rows, row => row.value, { angular: true }), [[[0, 170], [1, 179]], [[2, -179], [3, -170]], [[5, 10]]]);
  assert.equal(plotSegments(rows, row => row.value).length, 2);
});

test('missing estimator output and unsupported reference have independent labels and support bands', () => {
  const data = fixture();
  assert.match(frameStatus(data.frames[0]), /initialization/);
  assert.match(frameStatus(data.frames[2]), /reference unsupported/);
  assert.doesNotMatch(frameStatus({ estimated: false, missing_estimate_reason: 'interior' }), /tracking loss|failed/i);
  assert.equal(supportIntervals(data.frames, 'estimated').length, 2);
  assert.equal(supportIntervals(data.frames, 'reference_supported').length, 3);
});

test('interval requests send canonical nanosecond strings unchanged without loosening URL boundaries', async () => {
  let call;
  const api = createApiClient(async (...args) => { call = args; return new Response(JSON.stringify({ schema_version: 1, interval: {} })); });
  await api.request('/projects/id/jobs/id/investigation/metrics', { query: { start_ns: String(origin), end_ns: String(origin + 1n) } });
  assert.match(call[0], /start_ns=1520530000000000000&end_ns=1520530000000000001$/);
  for (const value of [Number(origin), '1e18', '001', '1&x=y', '-1', null]) await assert.rejects(api.request('/metrics', { query: { start_ns: value } }), TypeError);
  await assert.rejects(api.request('/metrics?start_ns=1'), TypeError);
});

test('private images preserve authenticated no-store requests and reject unexpected content', async () => {
  let call;
  const signal = new AbortController().signal;
  const client = createApiClient(async (...args) => { call = args; return new Response('png', { headers: { 'Content-Type': 'image/png' } }); });
  assert.equal((await client.image('/projects/id/jobs/id/investigation/images/cam0/0', { signal })).type, 'image/png');
  assert.equal(call[1].cache, 'no-store');
  assert.equal(call[1].credentials, 'same-origin');
  assert.equal(call[1].signal, signal);
  await assert.rejects(client.image('//external/image'), TypeError);
  await assert.rejects(createApiClient(async () => new Response('<html>')).image('/image'), error => error.status === 502);
  await assert.rejects(createApiClient(async () => new Response(JSON.stringify({ error: { code: 'artifacts_expired' } }), { status: 410 })).image('/image'), error => error.status === 410 && error.code === 'artifacts_expired');
});

test('latest-only requests abort superseded work and discard late responses, errors and destroyed sessions', async () => {
  const pending = [];
  const values = [];
  const errors = [];
  const request = createLatestRequest({ read: (value, signal) => new Promise((resolve, reject) => pending.push({ value, signal, resolve, reject })), onValue: result => values.push(result), onError: error => errors.push(error) });
  const first = request.run('first');
  const second = request.run('second');
  assert.equal(pending[0].signal.aborted, true);
  pending[1].resolve('new data');
  pending[0].resolve('old data');
  await Promise.all([first, second]);
  assert.deepEqual(values, ['new data']);
  const third = request.run('third');
  request.invalidate();
  pending[2].reject(new Error('obsolete'));
  await third;
  const fourth = request.run('fourth');
  request.destroy();
  assert.equal(pending[3].signal.aborted, true);
  pending[3].resolve('private late data');
  await fourth;
  assert.deepEqual(values, ['new data']);
  assert.deepEqual(errors, []);
});

test('no-support intervals and retained-data failures stay explicit without invented zero metrics', () => {
  const html = renderIntervalMetrics({ status: 'no_evaluated_samples', metrics: null, counts: { expected_camera_samples: 3, estimate_samples: 0, reference_supported_camera_samples: 3, evaluated_camera_samples: 0 }, coverage_fraction: 0, evaluated_fraction: 0, reference_coverage_fraction: 1 });
  assert.match(html, /no evaluated samples/);
  assert.match(html, /— <span>m/);
  assert.doesNotMatch(html, /0\.00000/);
  for (const code of ['investigation_unavailable', 'artifacts_expired', 'artifact_unavailable']) assert.match(investigationError(new ApiError(410, { error: { code } })), /unavailable|expired/i);
});

test('investigation deep links preserve owned run identities and reject extra selectors', () => {
  const id = '12345678-abcd-abcd-abcd-123456789abc';
  const hash = `#project/${id}/results/${id}/investigation`;
  assert.deepEqual(parseRoute(hash), { page: 'project', id, section: 'results', jobId: id, investigation: true });
  for (const suffix of ['/images', '?start=1', '/..']) assert.deepEqual(parseRoute(hash + suffix), { page: 'not-found' });
});
