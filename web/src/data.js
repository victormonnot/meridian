import { validateDiagnostics } from './diagnostics.js';

// Display units are explicit. These columns never enter an estimator.
export const COLUMNS = [
  'time_s', 'truth_roll_deg', 'gyro_roll_deg', 'complementary_roll_deg',
  'kalman_roll_deg', 'ekf_roll_deg', 'truth_bias_deg_s',
  'kalman_bias_deg_s', 'ekf_bias_deg_s',
];

export const SERIES = [
  { id: 'truth', label: 'Simulation truth', roll: 1, bias: 6, dash: '5 4' },
  { id: 'gyro', label: 'Gyro integration', roll: 2, bias: null, dash: '2 4' },
  { id: 'complementary', label: 'Complementary', roll: 3, bias: null, dash: '' },
  { id: 'kalman', label: 'Angle KF', roll: 4, bias: 7, dash: '7 3' },
  { id: 'ekf', label: 'Vector EKF', roll: 5, bias: 8, dash: '' },
].map(series => ({ ...series, color: `var(--${series.id})` }));

function require(condition, message) {
  if (!condition) throw new Error(`Invalid comparison data: ${message}`);
}

export function validateComparison(data) {
  require(data?.schema_version === 4 && data.experiment === 'roll_scenario_explorer'
    && data.data_source === 'simulation', 'unsupported format');
  require(JSON.stringify(data.columns) === JSON.stringify(COLUMNS), 'columns or units');
  const config = data.config;
  const fields = ['duration_s', 'sample_rate_hz', 'amplitude_deg', 'frequency_hz',
    'bias_deg_s', 'gyro_noise_std_deg_s', 'accel_noise_std_m_s2', 'observation_every', 'complementary_tau_s'];
  require(config && fields.every(field => Number.isFinite(config[field])), 'configuration');
  require(config.duration_s === 30 && config.sample_rate_hz === 100
    && config.observation_every === 10, 'measurement schedule');
  require(Number.isSafeInteger(data.seed) && data.seed >= 0, 'seed');
  require(Number.isInteger(data.display_stride) && data.display_stride > 0, 'display stride');
  const names = ['nominal', 'translation_pulse', 'initial_offset', 'accel_dropout', 'bias_ramp',
    'initial_overconfident', 'accel_noise_mismatch', 'timing_jitter', 'accel_delay'];
  require(JSON.stringify(Object.keys(data.scenarios ?? {}).sort()) === JSON.stringify([...names].sort()), 'scenario selection');
  for (const [source, experiment] of [['paired', 'vector_ekf_comparison'], ['controlled', 'controlled_scenarios']]) {
    const provenance = data.sources?.[source];
    require(provenance?.experiment === experiment && provenance.source_sha256
      && Object.keys(provenance.source_sha256).length > 0, 'source provenance');
    for (const [path, hash] of Object.entries(provenance.source_sha256)) {
      require(!path.startsWith('/') && !path.includes('..') && !path.includes('\\')
        && /^[a-f0-9]{64}$/.test(hash), 'source fingerprint');
    }
  }
  for (const name of names) {
    const scenario = data.scenarios[name];
    require(scenario && Array.isArray(scenario.rows) && scenario.rows.length >= 2, `${name} is empty`);
    require(scenario.source === (names.indexOf(name) < 2 ? 'paired' : 'controlled'), 'scenario source');
    require(scenario.source_sample_count === 3001 && scenario.rows.length <= 3001, 'sample count');
    const initial = scenario.initialization;
    require(initial && ['roll_deg', 'angle_std_deg', 'bias_deg_s', 'bias_std_deg_s'].every(field => Number.isFinite(initial[field])), 'initialization');
    require(initial.roll_deg === (['initial_offset', 'initial_overconfident'].includes(name) ? 60 : 0)
      && initial.angle_std_deg === (name === 'initial_offset' ? 30 : 0)
      && initial.bias_deg_s === 0 && initial.bias_std_deg_s === 1, 'initialization contract');
    const noise = scenario.accelerometer_noise;
    require(noise?.actual_std_m_s2 === (name === 'accel_noise_mismatch' ? .6 : .2)
      && noise.assumed_std_m_s2 === .2 && config.accel_noise_std_m_s2 === .2, 'accelerometer noise contract');
    const irregular = name === 'timing_jitter';
    const timing = scenario.timing;
    require(timing?.kind === (irregular ? 'irregular' : 'uniform')
      && timing.accel_delay_s === (name === 'accel_delay' ? .1 : 0), 'timing contract');
    const dtMin = timing.gyro_interval_min_s;
    const dtMax = timing.gyro_interval_max_s;
    require(Number.isFinite(dtMin) && Number.isFinite(dtMax) && dtMin > 0 && dtMax >= dtMin, 'interval range');
    require(irregular ? dtMin >= 30 / 9000 && dtMax <= .03 && dtMax / dtMin <= 3 && dtMax - dtMin > 1e-6
      : Math.abs(dtMin - .01) < 1e-10 && Math.abs(dtMax - .01) < 1e-10, 'interval model');
    const rowTimes = [];
    let previous = -Infinity;
    for (const row of scenario.rows) {
      require(Array.isArray(row) && row.length === COLUMNS.length && row.every(Number.isFinite), 'non-finite or incomplete row');
      require(row[0] > previous, 'timestamps must increase');
      if (!irregular) require(Math.abs(row[0] * 100 - Math.round(row[0] * 100)) < 1e-7, 'endpoint grid');
      previous = row[0];
      rowTimes.push(row[0]);
    }
    require(scenario.rows[0][0] === 0 && Math.abs(previous - config.duration_s) < 1e-9, 'time extent');
    require(scenario.rows[0].slice(2, 6).every(value => value === initial.roll_deg)
      && scenario.rows[0].slice(7).every(value => value === initial.bias_deg_s), 'initial row');
    for (const [kind, columns] of [['roll_deg', [1, 2, 3, 4, 5]], ['bias_deg_s', [6, 7, 8]]]) {
      const domain = scenario.domains?.[kind];
      require(Array.isArray(domain) && domain.length === 2 && domain.every(Number.isFinite) && domain[1] >= domain[0], 'domains');
      require(scenario.rows.every(row => columns.every(column => row[column] >= domain[0] - 1e-6
        && row[column] <= domain[1] + 1e-6)), 'domain excludes data');
    }
    for (const { id } of SERIES.slice(1)) {
      for (const basis of ['rmse_deg', 'time_weighted_rmse_deg']) {
        const rmse = scenario.metrics?.[basis]?.[id];
        require(Number.isFinite(rmse) && rmse >= 0, 'full-run metrics');
      }
    }
    for (const id of ['kalman', 'ekf']) require(Number.isFinite(scenario.metrics?.final_bias_error_deg_s?.[id]), 'bias metrics');
    const expectedTimes = Array.from({ length: 300 }, (_, i) => (i + 1) / 10)
      .filter(time => name !== 'accel_dropout' || time < 12 || time >= 17);
    require(scenario.scheduled_accel_count === 300 && Array.isArray(scenario.correction_times_s)
      && scenario.correction_times_s.length === expectedTimes.length, 'correction schedule');
    const arrivals = scenario.correction_times_s;
    for (const field of ['correction_predecessor_times_s', 'correction_sample_times_s']) {
      require(Array.isArray(timing[field]) && timing[field].length === arrivals.length
        && timing[field].every(Number.isFinite), 'correction timing metadata');
    }
    const correctionRows = new Set();
    arrivals.forEach((time, i) => {
      const previous = i === 0 ? 0 : arrivals[i - 1];
      require(Number.isFinite(time) && time > previous && time <= config.duration_s, 'correction order');
      if (!irregular) require(Math.abs(time - expectedTimes[i]) < 1e-10, 'uniform correction schedule');
      else require(time - previous >= 10 * dtMin - 1e-10 && time - previous <= 10 * dtMax + 1e-10, 'irregular correction gap');
      const index = firstAfter(rowTimes, time) - 1;
      const predecessor = timing.correction_predecessor_times_s[i];
      const sample = timing.correction_sample_times_s[i];
      require(index > 0 && Math.abs(rowTimes[index] - time) < 1e-10
        && Math.abs(rowTimes[index - 1] - predecessor) < 1e-10
        && time - predecessor >= dtMin - 1e-10 && time - predecessor <= dtMax + 1e-10,
      'corrections must retain exact endpoints and predecessors');
      require(sample >= 0 && Math.abs(time - sample - timing.accel_delay_s) < 1e-10, 'acquisition time');
      correctionRows.add(index);
    });
    require(Math.abs(arrivals.at(-1) - config.duration_s) < 1e-10, 'final correction');
    if (irregular) require(arrivals.some(time => Math.abs(time * 100 - Math.round(time * 100)) > 1e-7), 'irregular times resampled');
    require(scenario.rows.every((row, i, rows) => i === 0 || correctionRows.has(i)
      || (row[7] === rows[i - 1][7] && row[8] === rows[i - 1][8])), 'bias changes without correction');
    const event = scenario.event;
    if (name === 'translation_pulse') {
      require(event?.kind === 'translation' && event.axis === 'body_y' && Number.isFinite(event.value_m_s2)
        && Number.isFinite(event.start_s) && Number.isFinite(event.end_s)
        && event.start_s >= 0 && event.end_s > event.start_s && event.end_s <= config.duration_s, 'disturbance interval');
    } else if (name === 'accel_dropout') {
      require(event?.kind === 'accel_dropout' && event.start_s === 12 && event.end_s === 17
        && event.omitted_count === 50 && Math.abs(event.last_correction_before_s - 11.9) < 1e-10
        && event.first_correction_after_s === 17, 'dropout interval');
    } else if (name === 'bias_ramp') {
      require(event?.kind === 'bias_ramp' && event.start_s === 10 && event.end_s === 20
        && event.initial_bias_deg_s === .5 && event.final_bias_deg_s === 1.5
        && event.slope_deg_s2 === .1, 'bias ramp');
    } else require(event === null, 'unexpected interval');
    require(scenario.rows.every(row => Math.abs(row[6] - (name === 'bias_ramp'
      ? .5 + .1 * Math.max(0, Math.min(10, row[0] - 10)) : config.bias_deg_s)) <= 1e-6), 'bias truth');
    if (event) for (const time of [event.start_s, event.end_s]) {
      require(rowTimes.some(value => Math.abs(value - time) < 1e-10), 'event boundary missing');
    }
    validateDiagnostics(scenario, COLUMNS);
  }
  return data;
}

function firstAfter(times, time) {
  let lo = 0;
  let hi = times.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (times[mid] <= time + 1e-10) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

export function latestCorrection(scenario, time) {
  const times = scenario.correction_times_s;
  const index = firstAfter(times, time) - 1;
  return index < 0 ? null : times[index];
}

export function correctionDetails(scenario, time) {
  const times = scenario.correction_times_s;
  const index = firstAfter(times, time) - 1;
  return index < 0 ? null : {
    arrival: times[index], sample: scenario.timing.correction_sample_times_s[index],
    previousArrival: index > 0 ? times[index - 1] : null,
  };
}

export function adjacentCorrection(scenario, time, direction) {
  const times = scenario.correction_times_s;
  let index = firstAfter(times, time);
  if (direction > 0) return times[index] ?? null;
  if (index > 0 && Math.abs(times[index - 1] - time) < 1e-10) index -= 1;
  return times[index - 1] ?? null;
}

export function clampTime(time, duration) {
  return Math.max(0, Math.min(duration, time));
}

export function sampleAt(rows, time) {
  // Truth evolves continuously; only estimated biases wait for a correction.
  if (time <= rows[0][0]) return rows[0].slice();
  if (time >= rows.at(-1)[0]) return rows.at(-1).slice();
  let lo = 0;
  let hi = rows.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >>> 1;
    if (rows[mid][0] <= time) lo = mid;
    else hi = mid;
  }
  // Decimal slider times and binary source times can differ by roundoff.
  if (Math.abs(rows[hi][0] - time) < 1e-10) return [time, ...rows[hi].slice(1)];
  const weight = (time - rows[lo][0]) / (rows[hi][0] - rows[lo][0]);
  return rows[lo].map((value, column) => column === 0 ? time
    : column >= 7 ? value : value + weight * (rows[hi][column] - value));
}

export function advanceTime(time, elapsedSeconds, speed, duration) {
  const next = clampTime(time + Math.max(0, elapsedSeconds) * speed, duration);
  return { time: next, ended: next >= duration };
}

export function formatValue(value, digits = 2) {
  const rounded = Math.abs(value) < 0.5 * 10 ** -digits ? 0 : value;
  return rounded.toFixed(digits);
}
