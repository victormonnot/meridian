// Display helpers preserve integer timestamps; analysis remains authoritative
// in the immutable inspection artifacts and bounded server window responses.
const MIN_NS = -(1n << 63n);
const MAX_NS = (1n << 63n) - 1n;
const BILLION = 1_000_000_000n;

export const finite = value => typeof value === 'number' && Number.isFinite(value);
export const identifier = value => typeof value === 'string' && /^[A-Za-z][A-Za-z0-9._-]{0,79}$/.test(value);
export const sampleIdentity = value => typeof value === 'string' && /^(0|[1-9]\d{0,18})$/.test(value);

export function timestamp(value) {
  return typeof value === 'string' && /^(0|-?[1-9]\d{0,18})$/.test(value)
    && BigInt(value) >= MIN_NS && BigInt(value) <= MAX_NS;
}

function exact(value) {
  if (!timestamp(value)) throw new TypeError('Expected a canonical signed integer timestamp.');
  return BigInt(value);
}

export function relativeSeconds(value, origin) {
  return Number(exact(value) - exact(origin)) / 1e9;
}

export function relativeSecondsText(value, origin) {
  const delta = exact(value) - exact(origin);
  const magnitude = delta < 0n ? -delta : delta;
  const fraction = String(magnitude % BILLION).padStart(9, '0').replace(/0+$/, '');
  return `${delta < 0n ? '-' : ''}${magnitude / BILLION}${fraction ? '.' + fraction : ''}`;
}

export function timestampFromSeconds(text, origin) {
  if (typeof text !== 'string' || !/^-?(0|[1-9]\d{0,10})(?:\.\d{1,9})?$/.test(text)) {
    throw new RangeError('Enter seconds as a decimal with at most nine fractional digits.');
  }
  const negative = text.startsWith('-');
  const [whole, fraction = ''] = (negative ? text.slice(1) : text).split('.');
  const magnitude = BigInt(whole) * BILLION + BigInt(fraction.padEnd(9, '0'));
  const result = exact(origin) + (negative ? -magnitude : magnitude);
  if (result < MIN_NS || result > MAX_NS) throw new RangeError('The interval exceeds the supported timestamp range.');
  return String(result);
}

export function nearestSample(samples, selectedNs) {
  if (!samples.length) return -1;
  const selected = exact(selectedNs);
  let low = 0;
  let high = samples.length - 1;
  while (low < high) {
    const mid = Math.floor((low + high) / 2);
    if (exact(samples[mid].timestamp_ns) < selected) low = mid + 1;
    else high = mid;
  }
  if (low === 0) return 0;
  const before = selected - exact(samples[low - 1].timestamp_ns);
  const after = exact(samples[low].timestamp_ns) - selected;
  return before <= after ? low - 1 : low;
}

const count = value => Number.isSafeInteger(value) && value >= 0;
const duration = (value, maximum = (1n << 64n) - 1n) => typeof value === 'string' && /^(0|[1-9]\d{0,19})$/.test(value)
  && BigInt(value) <= maximum;
const vector = value => Array.isArray(value) && value.length === 3 && value.every(finite);
const invalid = () => { throw new Error('The sensor inspection has an unsupported or inconsistent structure.'); };

function timing(value) {
  if (!value || !count(value.interval_count) || !count(value.gap_count)
    || !count(value.duplicate_count) || !count(value.backward_count)
    || !(value.rate_hz === null || finite(value.rate_hz) && value.rate_hz >= 0)
    || !duration(value.elapsed_ns) || !value.interval_ns
    || !Object.values(value.interval_ns).every(item => item === null || duration(item))
    || !(value.gap_threshold_ns === null || duration(value.gap_threshold_ns, 3n * ((1n << 64n) - 1n)))
    || !Array.isArray(value.histogram) || value.histogram.length > 128) invalid();
  for (const bin of value.histogram) {
    if (!duration(bin.lower_ns) || !duration(bin.upper_ns)
      || BigInt(bin.lower_ns) > BigInt(bin.upper_ns) || !count(bin.count)) invalid();
  }
}

export function validateInspection(value, acquisitionDescriptor) {
  if (value?.schema_version !== 1 || value.kind !== 'sensor_inspection'
    || value.method_version !== 'sensor-inspection-v1' || value.status !== 'available'
    || !/^[a-f\d]{64}$/.test(value.acquisition?.sha256 || '')
    || acquisitionDescriptor && value.acquisition?.id !== acquisitionDescriptor.id
    || !Array.isArray(value.streams) || !value.streams.length || value.streams.length > 128
    || !Array.isArray(value.frames) || !Array.isArray(value.clocks)
    || !value.evidence || value.policy?.display_point_limit !== 1200
    || !Array.isArray(value.comparisons) || !Array.isArray(value.limitations)) invalid();
  const names = new Set();
  for (const stream of value.streams) {
    if (!identifier(stream.id) || names.has(stream.id) || !identifier(stream.frame_id)
      || !identifier(stream.clock_id) || !['camera', 'imu', 'reference_pose'].includes(stream.kind)
      || !count(stream.sample_count) || !Array.isArray(stream.segments) || !stream.segments.length) invalid();
    names.add(stream.id);
    const segments = new Set();
    for (const segment of stream.segments) {
      if (!identifier(segment.id) || segments.has(segment.id) || !timestamp(segment.first_ns)
        || !timestamp(segment.last_ns) || BigInt(segment.first_ns) > BigInt(segment.last_ns)
        || !count(segment.sample_count) || !segment.sample_count) invalid();
      segments.add(segment.id);
      timing(segment.timing);
    }
  }
  return value;
}

function gap(value) {
  if (!value || !sampleIdentity(value.previous_sample_id)
    || !timestamp(value.previous_timestamp_ns) || !duration(value.interval_ns)) invalid();
}

export function validateInspectionWindow(value, stream, segment, query) {
  if (value?.schema_version !== 1 || value.method_version !== 'sensor-inspection-v1'
    || value.stream_id !== stream.id || value.segment_id !== segment.id
    || value.clock_id !== stream.clock_id || value.interval?.first_ns !== query.first_ns
    || value.interval?.last_ns !== query.last_ns || value.interval?.boundary !== 'closed'
    || !count(value.sample_count) || !Array.isArray(value.gaps) || value.gaps.length > 1200
    || !Array.isArray(value.support) || value.support.length > 128
    || !['available', 'narrower_interval_required'].includes(value.display?.status)
    || !Array.isArray(value.display.points) || value.display.points.length > 1200
    || value.display.point_count !== value.display.points.length
    || value.display.point_count > value.sample_count
    || value.display.source_samples !== value.sample_count
    || typeof value.display.reduced !== 'boolean') invalid();
  timing(value.timing);
  if (value.display.status === 'narrower_interval_required' && value.display.points.length) invalid();
  let previousNs = null;
  let previousId = -1n;
  for (const point of value.display.points) {
    if (!timestamp(point.timestamp_ns) || !sampleIdentity(point.sample_id)
      || BigInt(point.sample_id) <= previousId
      || BigInt(point.timestamp_ns) < BigInt(query.first_ns)
      || BigInt(point.timestamp_ns) > BigInt(query.last_ns)
      || previousNs !== null && BigInt(point.timestamp_ns) <= previousNs) invalid();
    if (point.gap_before !== null) gap(point.gap_before);
    if (stream.kind === 'imu' && (!vector(point.gyro_rad_s) || !vector(point.specific_force_m_s2))) invalid();
    if (stream.kind === 'camera' && (!point.image
      || !/^[a-f\d]{64}$/.test(point.image.source_sha256 || '')
      || !count(point.image.width) || !count(point.image.height)
      || !Array.isArray(point.support))) invalid();
    if (point.stereo_match && (!identifier(point.stereo_match.stream_id)
      || !identifier(point.stereo_match.segment_id) || !sampleIdentity(point.stereo_match.sample_id)
      || !timestamp(point.stereo_match.timestamp_ns) || !timestamp(point.stereo_match.delta_ns))) invalid();
    previousNs = BigInt(point.timestamp_ns);
    previousId = BigInt(point.sample_id);
  }
  for (const item of value.gaps) {
    gap(item);
    if (!timestamp(item.timestamp_ns) || !sampleIdentity(item.sample_id)) invalid();
  }
  for (const item of value.support) {
    if (!identifier(item.stream_id) || !count(item.supported_samples)
      || !count(item.total_samples) || !count(item.not_comparable_samples)
      || item.supported_samples > item.total_samples || item.not_comparable_samples > item.total_samples
      || !(item.fraction === null || finite(item.fraction) && item.fraction >= 0 && item.fraction <= 1)) invalid();
  }
  return value;
}

export function plotRuns(points) {
  const result = [];
  let current = [];
  for (const point of points) {
    if (point.gap_before && current.length) {
      result.push(current);
      current = [];
    }
    current.push(point);
  }
  if (current.length) result.push(current);
  return result;
}

export function comparableImuSegments(summary, window) {
  if (!window.sample_count) return [];
  const permitted = new Set(window.support.filter(item => item.kind === 'imu'
    && item.not_comparable_samples === 0).map(item => item.stream_id));
  return summary.streams.filter(stream => stream.kind === 'imu' && permitted.has(stream.id)
    && stream.clock_id === window.clock_id).flatMap(stream => stream.segments.filter(segment =>
    BigInt(segment.first_ns) <= BigInt(window.interval.last_ns)
    && BigInt(segment.last_ns) >= BigInt(window.interval.first_ns)).map(segment => ({ stream, segment })));
}

export function plotDomain(values) {
  const available = values.filter(finite);
  if (!available.length) return [-1, 1];
  const low = Math.min(...available), high = Math.max(...available);
  const span = high - low;
  if (!finite(span)) return null;
  const pad = Math.max(span * .08, Math.abs(high) * .01, .001);
  const domain = [low - pad, high + pad];
  // D3 interpolation needs both endpoints and their difference to be finite.
  // Withhold the trace when a finite input range cannot satisfy that contract.
  return domain.every(finite) && finite(domain[1] - domain[0]) ? domain : null;
}
