// Display operations only. Evaluation remains authoritative on the server.
export const finite = value => typeof value === 'number' && Number.isFinite(value);
const timestamp = value => typeof value === 'string' && /^(0|[1-9]\d{0,19})$/.test(value);
const vector = (value, length) => Array.isArray(value) && value.length === length && value.every(finite);
const pose = value => value === null || value && vector(value.position_m, 3) && vector(value.quaternion_xyzw, 4) && vector(value.rpy_deg, 3);

export function validateInvestigation(value, specificationSha256) {
  const invalid = () => { throw new Error('The investigation artifact has an unsupported or inconsistent structure.'); };
  if (![1, 2].includes(value?.schema_version) || value.specification_sha256 !== specificationSha256 || !Array.isArray(value.frames) || !value.frames.length || !Array.isArray(value.imu?.samples) || !value.summary || !timestamp(value.time?.origin_timestamp_ns)) invalid();
  if (value.schema_version === 2) {
    if (value.input?.profile_id !== 'openvins-private-stereo-v1'
      || typeof value.input.input_revision_id !== 'string' || typeof value.input.prepared_input_sha256 !== 'string'
      || typeof value.input.imu_frame_id !== 'string' || typeof value.input.clock_id !== 'string'
      || !Array.isArray(value.input.processing) || !['available', 'partial', 'unavailable'].includes(value.evaluation?.status)
      || !Array.isArray(value.cameras) || value.cameras.length !== 2) invalid();
    for (const [index, camera] of value.cameras.entries()) {
      if (camera.slot !== `cam${index}` || typeof camera.stream_id !== 'string' || typeof camera.frame_id !== 'string'
        || !['mono8', 'mono16'].includes(camera.encoding) || !Array.isArray(camera.resolution)
        || camera.resolution.length !== 2 || camera.resolution.some(size => !Number.isSafeInteger(size) || size < 1)) invalid();
    }
  }
  let previousTime = -Infinity;
  let previousNs = -1n;
  for (const [index, frame] of value.frames.entries()) {
    if (frame.index !== index || !timestamp(frame.timestamp_ns) || BigInt(frame.timestamp_ns) <= previousNs || !finite(frame.time_s) || frame.time_s <= previousTime || !pose(frame.estimate) || !pose(frame.reference) || ['estimated', 'reference_supported', 'associated', 'evaluated'].some(key => typeof frame[key] !== 'boolean') || [frame.position_error_m, frame.orientation_error_deg].some(error => error !== null && (!finite(error) || error < 0))) invalid();
    if (!pose(frame.estimate_raw) || frame.estimated !== Boolean(frame.estimate_raw) || frame.evaluated && (!frame.estimated || !frame.reference_supported || !frame.reference || !frame.estimate)) invalid();
    if (value.schema_version === 2 && (value.alignment === null && frame.estimate !== null
      || value.evaluation.status === 'unavailable' && (frame.evaluated || frame.position_error_m !== null || frame.orientation_error_deg !== null))) invalid();
    previousNs = BigInt(frame.timestamp_ns);
    previousTime = frame.time_s;
  }
  previousTime = -Infinity;
  for (const sample of value.imu.samples) {
    if (!timestamp(sample.timestamp_ns) || !finite(sample.time_s) || sample.time_s <= previousTime || !vector(sample.gyro_rad_s, 3) || !vector(sample.accel_m_s2, 3)) invalid();
    previousTime = sample.time_s;
  }
  return value;
}

export function investigationPresentation(data) {
  const privateInput = data.schema_version === 2;
  const raw = privateInput && data.alignment === null;
  return { privateInput, raw, poseKey: raw ? 'estimate_raw' : 'estimate',
    estimateLabel: raw ? 'Estimate · estimator world' : 'Aligned estimate',
    worldLabel: raw ? 'Estimator world' : 'Reference world',
    showReference: !raw,
    accuracyAvailable: !privateInput || data.evaluation.status !== 'unavailable',
    imuLabel: privateInput ? 'Prepared' : 'Raw',
    imuFrame: privateInput ? data.input.imu_frame_id : 'IMU body' };
}

export function finitePlotDomain(values) {
  const numbers = values.filter(finite);
  if (!numbers.length) return [-1, 1];
  let low = Infinity, high = -Infinity;
  for (const value of numbers) { low = Math.min(low, value); high = Math.max(high, value); }
  const pad = Math.max((high - low) * .08, Math.abs(high) * .01, .01);
  const bounds = [low - pad, high + pad];
  return bounds.every(finite) && finite(bounds[1] - bounds[0]) ? bounds : null;
}

export function nearestFrame(frames, time) {
  let low = 0;
  let high = frames.length - 1;
  while (low < high) {
    const mid = Math.floor((low + high) / 2);
    if (frames[mid].time_s < time) low = mid + 1;
    else high = mid;
  }
  return low > 0 && Math.abs(frames[low - 1].time_s - time) <= Math.abs(frames[low].time_s - time) ? low - 1 : low;
}

export function intervalIndices(frames, start, end) {
  if (!finite(start) || !finite(end) || start > end) throw new RangeError('Choose a finite interval with start at or before end.');
  return [nearestFrame(frames, start), nearestFrame(frames, end)];
}

export function frameStatus(frame) {
  if (!frame.estimated) return ({ initialization: 'No estimate · initialization', interior: 'Missing estimator output', trailing: 'No estimate · trailing gap', no_estimates: 'No estimates in this run' })[frame.missing_estimate_reason] || 'No estimate';
  if (!frame.reference_supported) return 'Estimate available · reference unsupported';
  if (!frame.evaluated) return 'Estimate available · not evaluated';
  return 'Estimate and supported reference · evaluated';
}

// Preserve first/last and extrema in each time-ordered bucket. Missing values
// break paths; callers split gaps before reducing. Never use this for scoring.
export function displayEnvelope(points, maxPoints = 1000) {
  if (points.length <= maxPoints) return points;
  const buckets = Math.max(1, Math.floor((maxPoints - 2) / 2));
  const result = [points[0]];
  for (let bucket = 0; bucket < buckets; bucket += 1) {
    const start = 1 + Math.floor(bucket * (points.length - 2) / buckets);
    const end = 1 + Math.floor((bucket + 1) * (points.length - 2) / buckets);
    let min = start;
    let max = start;
    for (let index = start + 1; index < end; index += 1) {
      if (points[index][1] < points[min][1]) min = index;
      if (points[index][1] > points[max][1]) max = index;
    }
    for (const index of [...new Set([min, max])].sort((a, b) => a - b)) result.push(points[index]);
  }
  result.push(points.at(-1));
  return result;
}

export function plotSegments(rows, getValue, { angular = false, maxPoints = 1000 } = {}) {
  const segments = [];
  let segment = [];
  const flush = () => { if (segment.length) segments.push(displayEnvelope(segment, maxPoints)); segment = []; };
  for (const row of rows) {
    const value = getValue(row);
    if (!finite(value)) { flush(); continue; }
    if (angular && segment.length && Math.abs(value - segment.at(-1)[1]) > 180) flush();
    segment.push([row.time_s, value]);
  }
  flush();
  return segments;
}

export function supportIntervals(frames, key) {
  const result = [];
  let start = 0;
  for (let index = 1; index <= frames.length; index += 1) {
    if (index < frames.length && frames[index][key] === frames[start][key]) continue;
    result.push({ start: frames[start].time_s, end: frames[Math.min(index, frames.length - 1)].time_s, supported: frames[start][key] });
    start = index;
  }
  return result;
}
