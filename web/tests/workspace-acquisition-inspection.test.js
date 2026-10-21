import assert from 'node:assert/strict';
import test from 'node:test';
import { createApiClient, parseRoute } from '../src/workspace-api.js';
import { acquisitionRoute, renderAcquisitionDetail } from '../src/workspace-acquisitions.js';
import { comparableImuSegments, nearestSample, plotDomain, plotRuns, relativeSeconds, relativeSecondsText, timestamp,
  timestampFromSeconds, validateInspection, validateInspectionWindow } from '../src/workspace-acquisition-inspection-data.js';
import { inspectionImagePath, inspectionShell, renderEvidence, renderWindowStatistics,
  unavailableInspection } from '../src/workspace-acquisition-inspection.js';

const projectId = '12345678-abcd-abcd-abcd-123456789abc';
const acquisitionId = '87654321-abcd-abcd-abcd-987654321abc';
const route = acquisitionRoute(projectId, acquisitionId);

test('inspection deep links preserve acquisition ownership scope', () => {
  assert.deepEqual(parseRoute(route + '/inspection'), { page: 'project', id: projectId, section: 'data',
    acquisitionId, inspection: true });
  for (const suffix of ['/inspection/extra', '/inspection?stream=imu0', '/inspection/..']) {
    assert.deepEqual(parseRoute(route + suffix), { page: 'not-found' });
  }
});

test('inspection query selectors preserve signed epoch nanoseconds exactly', async () => {
  const calls = [];
  const api = createApiClient(async (...args) => {
    calls.push(args);
    return new Response(JSON.stringify({ schema_version: 1 }), { headers: { 'Content-Type': 'application/json' } });
  });
  await api.request('/inspection/window', { query: { stream_id: 'imu.0', segment_id: 'reset-1',
    first_ns: '-9223372036854775808', last_ns: '9223372036854775807' } });
  assert.match(calls[0][0], /stream_id=imu\.0&segment_id=reset-1&first_ns=-9223372036854775808&last_ns=9223372036854775807$/);
  assert.equal(calls[0][1].cache, 'no-store');
  assert.equal(calls[0][1].credentials, 'same-origin');
  for (const value of [0, true, '+1', '-0', '01', '1.0', '1e9', '9223372036854775808', '-9223372036854775809']) {
    await assert.rejects(api.request('/inspection/window', { query: { first_ns: value } }), TypeError);
  }
  for (const value of ['', '../imu', 'imu/0', '<script>', 'https://other.invalid', 'x'.repeat(81)]) {
    await assert.rejects(api.request('/inspection/window', { query: { stream_id: value } }), TypeError);
  }
  assert.equal(calls.length, 1);
});

test('time labels and interval inputs retain one nanosecond at large epochs', () => {
  const origin = '1500000000000000000';
  assert.equal(relativeSeconds('1500000000000000001', origin), 1e-9);
  assert.equal(relativeSecondsText('1500000000000000001', origin), '0.000000001');
  assert.equal(relativeSecondsText('1499999999999999999', origin), '-0.000000001');
  assert.equal(timestampFromSeconds('0.000000001', origin), '1500000000000000001');
  assert.equal(timestampFromSeconds('-0.000000001', origin), '1499999999999999999');
  assert.equal(timestampFromSeconds('2.5', '-3000000000'), '-500000000');
  assert.equal(relativeSecondsText('-500000000', '-3000000000'), '2.5');
});

test('time input rejects lossy forms and signed overflow before a request', () => {
  for (const value of [true, 1, '01', '-0', '+1', '1e9', '-9223372036854775809']) assert.equal(timestamp(value), false);
  for (const value of ['', ' 1 ', '1e-9', '0.0000000001', '+1', '01.0', Infinity]) {
    assert.throws(() => timestampFromSeconds(value, '0'), RangeError);
  }
  assert.throws(() => timestampFromSeconds('0.000000001', '9223372036854775807'), RangeError);
  assert.throws(() => timestampFromSeconds('-0.000000001', '-9223372036854775808'), RangeError);
});

test('cursor selection finds an exact sample without losing epoch differences', () => {
  const rows = ['1500000000000000000', '1500000000000000001', '1500000000000000003']
    .map((timestamp_ns, index) => ({ timestamp_ns, sample_id: String(index + 20) }));
  assert.equal(nearestSample(rows, '1500000000000000001'), 1);
  assert.equal(nearestSample(rows, '1500000000000000002'), 1);
  assert.equal(nearestSample(rows, '1500000000000001000'), 2);
  assert.equal(nearestSample(rows, '1499999999999999999'), 0);
  assert.equal(nearestSample([], '0'), -1);
});

test('acquisition metadata uses singular sample and segment labels', () => {
  const html = renderAcquisitionDetail({ id: projectId }, {
    acquisition: { id: acquisitionId, name: 'One observation', state: 'ready', files_available: true },
    descriptor: { streams: [{ id: 'imu0', kind: 'imu', segments: [{ sample_count: 1 }] }] },
  });
  assert.match(html, /1 sample<\/span>/);
  assert.match(html, /1 segment<\/span>/);
  assert.doesNotMatch(html, /1 samples|1 segments/);
});

const timing = () => ({ interval_count: 99, elapsed_ns: '1000000000', rate_hz: 99,
  interval_ns: { minimum: '1', p05: '1', p50: '10', p95: '20', maximum: '1000' },
  quantile_method: 'nearest_rank', histogram: [{ lower_ns: '1', upper_ns: '1000', count: 99 }],
  gap_threshold_ns: '30', gap_count: 1, duplicate_count: 0, backward_count: 0,
  ordering_evidence: 'enforced_by_import' });
const stream = () => ({ id: 'imu0', kind: 'imu', clock_id: 'clock0', frame_id: 'body', sample_count: 100,
  segments: [{ id: 's0', first_ns: '0', last_ns: '1000000000', sample_count: 100, gap_count: 1,
    timing: timing(), boundary_from_previous: null }] });
const summary = () => ({ schema_version: 1, kind: 'sensor_inspection', method_version: 'sensor-inspection-v1',
  status: 'available', acquisition: { id: 'source0', sha256: 'a'.repeat(64) },
  policy: { display_point_limit: 1200 }, frames: [{ id: 'body', axes: null }], clocks: [{ id: 'clock0', unit: 'ns', epoch: 'unknown' }],
  streams: [stream()], comparisons: [], reference_availability: 'absent', limitations: [],
  evidence: { import_validity: 'validated', inspection_availability: 'available', calibration_suitability: 'not_assessed',
    estimator_compatibility: 'not_assessed', estimate_support: 'not_generated', evaluation_availability: 'not_assessed' } });
const window = () => ({ schema_version: 1, method_version: 'sensor-inspection-v1', stream_id: 'imu0', segment_id: 's0',
  clock_id: 'clock0', interval: { first_ns: '0', last_ns: '1000000000', boundary: 'closed' },
  sample_count: 100, timing: timing(), gap_count: 1, gaps_truncated: false,
  gaps: [{ previous_sample_id: '0', previous_timestamp_ns: '0', sample_id: '99', timestamp_ns: '1000000000', interval_ns: '1000000000' }],
  support: [], imu: null, display: { status: 'available', source_samples: 100, point_count: 2, point_limit: 1200, reduced: true,
    points: [{ sample_id: '0', timestamp_ns: '0', gap_before: null, gyro_rad_s: [1, 2, 3], specific_force_m_s2: [-1, -2, -3] },
      { sample_id: '99', timestamp_ns: '1000000000', gap_before: { previous_sample_id: '0', previous_timestamp_ns: '0', interval_ns: '1000000000' },
        gyro_rad_s: [4, 5, 6], specific_force_m_s2: [-4, -5, -6] }] } });
const validateWindow = value => validateInspectionWindow(value, stream(), stream().segments[0], value.interval);

test('summary and window validation retain independent source identities and all six axes', () => {
  assert.equal(validateInspection(summary(), { id: 'source0' }).streams[0].id, 'imu0');
  assert.throws(() => validateInspection(summary(), { id: 'other-source' }), /inconsistent/);
  const value = validateWindow(window());
  assert.deepEqual(value.display.points[1].gyro_rad_s, [4, 5, 6]);
  assert.deepEqual(value.display.points[1].specific_force_m_s2, [-4, -5, -6]);
  for (const mutate of [
    value => { value.stream_id = 'other'; }, value => { value.segment_id = 'reset1'; },
    value => { value.display.points[1].sample_id = '0'; },
    value => { value.display.points[1].timestamp_ns = '0'; },
    value => { value.display.points[1].specific_force_m_s2[2] = Infinity; },
  ]) {
    const bad = window(); mutate(bad);
    assert.throws(() => validateWindow(bad), /inconsistent/);
  }
});

test('display paths never bridge preserved gap boundaries', () => {
  const points = window().display.points;
  assert.deepEqual(plotRuns(points).map(run => run.map(row => row.sample_id)), [['0'], ['99']]);
  assert.deepEqual(plotRuns([{ ...points[0] }, { ...points[1], gap_before: null }]).map(run => run.length), [2]);
});

test('finite extreme observations cannot overflow display coordinates or create invalid SVG traces', () => {
  for (const values of [[-1e308, 1e308], [Number.MAX_VALUE], [-8e307, 8e307]]) {
    assert.equal(plotDomain(values), null);
  }
  for (const values of [[0], [-1, 1], [1e308, 1.01e308], [Number.MIN_VALUE]]) {
    const domain = plotDomain(values);
    assert.ok(domain.every(Number.isFinite));
    assert.ok(Number.isFinite(domain[1] - domain[0]));
    assert.ok(domain[0] < Math.min(...values) && domain[1] > Math.max(...values));
  }
});

test('full-resolution statistics do not use the reduced display point count', () => {
  const html = renderWindowStatistics(window(), stream());
  assert.match(html, /100 source observations/);
  assert.match(html, /99 adjacent intervals/);
  assert.match(html, /Cadence gaps/);
  assert.doesNotMatch(html, /2 source observations/);
});

test('reference support gaps use their fixed bracket policy rather than a cadence claim', () => {
  const value = window();
  value.timing.gap_policy = 'reference_bracket_limit';
  value.timing.gap_threshold_ns = '20000000';
  const html = renderWindowStatistics(value, { ...stream(), kind: 'reference_pose' });
  assert.match(html, /Reference support gaps/);
  assert.match(html, /fixed 20 ms interpolation bracket limit/);
  assert.doesNotMatch(html, /A cadence gap/);
});

test('an overly fragmented interval retains nonzero gap evidence without invented display traces', () => {
  const value = window();
  value.gap_count = 1400; value.gaps_truncated = true;
  value.display = { ...value.display, status: 'narrower_interval_required', point_count: 0, points: [] };
  assert.equal(validateWindow(value).display.status, 'narrower_interval_required');
  assert.match(renderWindowStatistics(value, stream()), /1,400/);
  value.display.point_count = 1;
  value.display.points = [window().display.points[0]];
  assert.throws(() => validateWindow(value), /inconsistent/);
});

test('linked IMU segments require explicit comparability and overlap, not equal epoch text', () => {
  const value = window();
  value.support = [{ stream_id: 'imu0', kind: 'imu', supported_samples: 15, total_samples: 17, fraction: 15 / 17, not_comparable_samples: 0 }];
  assert.equal(comparableImuSegments(summary(), value).length, 1);
  value.support[0].not_comparable_samples = 17;
  assert.deepEqual(comparableImuSegments(summary(), value), []);
  value.support[0].not_comparable_samples = 0;
  value.clock_id = 'unrelated-clock';
  assert.deepEqual(comparableImuSegments(summary(), value), []);
});

test('int64 endpoint spans retain unsigned 20-digit intervals and threefold gap thresholds', () => {
  const value = window();
  const first = '-9223372036854775808', last = '9223372036854775807', span = '18446744073709551615';
  value.interval.first_ns = first; value.interval.last_ns = last;
  value.display.points[0].timestamp_ns = first; value.display.points[1].timestamp_ns = last;
  value.display.points[1].gap_before.previous_timestamp_ns = first;
  value.display.points[1].gap_before.interval_ns = span;
  value.gaps[0] = { ...value.gaps[0], previous_timestamp_ns: first, timestamp_ns: last, interval_ns: span };
  value.timing.elapsed_ns = span;
  value.timing.interval_ns = Object.fromEntries(['minimum', 'p05', 'p50', 'p95', 'maximum'].map(key => [key, span]));
  value.timing.histogram = [{ lower_ns: span, upper_ns: span, count: 99 }];
  value.timing.gap_threshold_ns = '55340232221128654845';
  assert.equal(validateWindow(value), value);
  assert.equal(relativeSecondsText(last, first), '18446744073.709551615');
  assert.ok(Number.isFinite(relativeSeconds(last, first)));
});

test('absence of a reference never becomes a zero score or an estimator rejection', () => {
  const html = renderEvidence(summary());
  assert.match(html, /accuracy evaluation unavailable/);
  assert.match(html, /does not itself make sensor inputs unusable/);
  assert.match(html, /not assessed/);
  assert.doesNotMatch(html, /RMSE|0%|ready for VIO|compatible with OpenVINS/);
});

test('legacy and resource-limited inspection states remain distinct and escaped', () => {
  const old = unavailableInspection({ availability: 'not_generated' });
  assert.match(old, /was not generated/);
  assert.match(old, /new acquisition/);
  const limited = unavailableInspection({ availability: 'unavailable', reason: '<img src=x>Resource limit' });
  assert.match(limited, /Inspection unavailable/);
  assert.match(limited, /&lt;img/);
  assert.doesNotMatch(limited, /<img/);
});

test('exact preview routes preserve large source sample IDs and forbid unowned URLs', () => {
  assert.equal(inspectionImagePath(projectId, acquisitionId, 'cam0', '9007199254740993'),
    `/projects/${projectId}/acquisitions/${acquisitionId}/inspection/images/cam0/9007199254740993`);
  for (const value of ['../cam0', '//other.invalid', '<script>', 'cam0?owner=other']) {
    assert.throws(() => inspectionImagePath(projectId, acquisitionId, value, '1'), TypeError);
  }
  assert.throws(() => inspectionImagePath(projectId, acquisitionId, 'cam0', 9007199254740993), TypeError);
  assert.doesNotMatch(inspectionShell({ id: acquisitionId, project_id: projectId, name: '<img src=x>' }), /<img/);
});
