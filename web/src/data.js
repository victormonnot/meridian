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
  require(data?.schema_version === 1 && data.experiment === 'vector_ekf_comparison'
    && data.data_source === 'simulation', 'unsupported format');
  require(JSON.stringify(data.columns) === JSON.stringify(COLUMNS), 'columns or units');
  const config = data.config;
  const configFields = ['duration_s', 'sample_rate_hz', 'amplitude_deg', 'frequency_hz',
    'bias_deg_s', 'gyro_noise_std_deg_s', 'accel_noise_std_m_s2', 'observation_every',
    'complementary_tau_s', 'pulse_start_s', 'pulse_end_s', 'pulse_y_m_s2'];
  require(config && configFields.every(field => Number.isFinite(config[field])), 'configuration');
  require(config.duration_s > 0 && config.sample_rate_hz > 0
    && config.observation_every > 0, 'measurement schedule');
  require(Number.isSafeInteger(data.seed) && data.seed >= 0, 'seed');
  require(Number.isInteger(data.display_stride) && data.display_stride > 0, 'display stride');
  require(Number.isFinite(data.provenance?.initialization?.angle_rad)
    && Number.isFinite(data.provenance?.initialization?.bias_rad_s), 'initialization');
  for (const kind of ['roll_deg', 'bias_deg_s']) {
    const domain = data.domains?.[kind];
    require(domain?.length === 2 && domain.every(Number.isFinite) && domain[1] >= domain[0], 'domains');
  }
  for (const name of ['nominal', 'translation_pulse']) {
    const scenario = data.scenarios?.[name];
    require(scenario && Array.isArray(scenario.rows) && scenario.rows.length >= 2, `${name} is empty`);
    require(Number.isInteger(scenario.source_sample_count)
      && scenario.source_sample_count >= scenario.rows.length, 'sample count');
    let previous = -Infinity;
    for (const row of scenario.rows) {
      require(row.length === COLUMNS.length && row.every(Number.isFinite), 'non-finite or incomplete row');
      require(row[0] > previous, 'timestamps must increase');
      previous = row[0];
    }
    require(scenario.rows[0][0] === 0
      && Math.abs(previous - config.duration_s) < 1e-9, 'time extent');
    for (const { id } of SERIES.slice(1)) {
      const rmse = scenario.metrics?.rmse_deg?.[id];
      require(Number.isFinite(rmse) && rmse >= 0, 'full-run metrics');
    }
    if (name === 'nominal') require(scenario.disturbance === null, 'nominal disturbance');
    else {
      const pulse = scenario.disturbance;
      require(pulse?.axis === 'body_y' && Number.isFinite(pulse.value_m_s2)
        && Number.isFinite(pulse.start_s) && Number.isFinite(pulse.end_s)
        && pulse.start_s >= 0 && pulse.end_s > pulse.start_s
        && pulse.end_s <= config.duration_s, 'disturbance interval');
    }
  }
  return data;
}

export function clampTime(time, duration) {
  return Math.max(0, Math.min(duration, time));
}

export function sampleAt(rows, time) {
  // Binary search works for retained pulse boundaries and irregular final spacing.
  if (time <= rows[0][0]) return rows[0].slice();
  if (time >= rows.at(-1)[0]) return rows.at(-1).slice();
  let lo = 0;
  let hi = rows.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >>> 1;
    if (rows[mid][0] <= time) lo = mid;
    else hi = mid;
  }
  const weight = (time - rows[lo][0]) / (rows[hi][0] - rows[lo][0]);
  return rows[lo].map((value, column) => column === 0 ? time
    : value + weight * (rows[hi][column] - value));
}

export function advanceTime(time, elapsedSeconds, speed, duration) {
  const next = clampTime(time + Math.max(0, elapsedSeconds) * speed, duration);
  return { time: next, ended: next >= duration };
}

export function formatValue(value, digits = 2) {
  const rounded = Math.abs(value) < 0.5 * 10 ** -digits ? 0 : value;
  return rounded.toFixed(digits);
}
