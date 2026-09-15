const BASELINE_METRICS = [
  'angle_rmse_deg', 'angle_time_weighted_rmse_deg', 'late_angle_rmse_deg',
  'max_abs_angle_error_deg', 'final_angle_error_deg',
];
const COMMON_METRICS = [...BASELINE_METRICS, 'bias_rmse_deg_s', 'late_bias_rmse_deg_s',
  'max_abs_bias_error_deg_s', 'final_bias_error_deg_s', 'mean_nis'];

export const STUDY_SPECS = {
  r: {
    directory: 'r-sensitivity', experiment: 'accelerometer_covariance_sensitivity',
    reference: '0.2', settings: ['0.1', '0.2', '0.6'], cases: ['nominal', 'accel_noise_mismatch'],
    evaluationStart: 1000, metrics: COMMON_METRICS,
    protocolHash: 'cb1f9ad916798455b815e19a03c78194360217cf73caef5189588994896e86ac',
  },
  bias: {
    directory: 'bias-random-walk', experiment: 'gyro_bias_random_walk',
    reference: '0', settings: ['0', '0.03', '0.1'], cases: ['nominal', 'bias_ramp'],
    evaluationStart: 2000,
    metrics: [...COMMON_METRICS, 'ramp_angle_rmse_deg', 'ramp_bias_rmse_deg_s',
      'late_bias_mean_error_deg_s', 'late_bias_temporal_std_deg_s'],
    protocolHash: '36b06126c5b4bff69d9e600f1752140e5b19e48bffcc5edaa0a6abf39641cf7c',
  },
};

export function studyCheck(condition, message) {
  if (!condition) throw new Error(`Invalid parameter study data: ${message}`);
}

export function exactStudyKeys(value, expected) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    && JSON.stringify(Object.keys(value).sort()) === JSON.stringify([...expected].sort());
}

function equal(value, expected) {
  return JSON.stringify(value) === JSON.stringify(expected);
}

function safeText(value) {
  return typeof value === 'string' && value.length > 0
    && !/(?:\.personal|AGENTS\.md|\/home\/|\/Users\/|outputs\/)/.test(value);
}

function hash(value) { return typeof value === 'string' && /^[a-f0-9]{64}$/.test(value); }

export function expectedStudySeeds(id, phase) {
  const start = phase === 'exploration' ? 0 : STUDY_SPECS[id].evaluationStart;
  return Array.from({ length: 20 }, (_, i) => start + i);
}

export function validateStudyMetrics(metrics, id, baseline = false) {
  const names = baseline ? BASELINE_METRICS : STUDY_SPECS[id].metrics;
  studyCheck(exactStudyKeys(metrics, names), 'metric fields');
  for (const [name, value] of Object.entries(metrics)) {
    studyCheck(Number.isFinite(value), `${name} must be finite`);
    if (!['final_angle_error_deg', 'final_bias_error_deg_s', 'late_bias_mean_error_deg_s'].includes(name)) {
      studyCheck(value >= 0, `${name} must be nonnegative`);
    }
  }
}

// Schema 1 presents two fixed, published protocols. Changing their assumptions
// requires revisiting the labels and comparisons, not silently accepting a new run.
export function validateStudyProtocol(protocol, id) {
  const spec = STUDY_SPECS[id];
  studyCheck(protocol && protocol.version === 1 && protocol.declared_date === '2026-09-15', 'protocol version');
  const shared = {
    duration_s: 30, roll_amplitude_deg: 20, roll_frequency_hz: 0.1,
    gravity_m_s2: 9.80665, initial_state_rad_rad_s: [0, 0],
    nis_dimension: 2, accuracy_thresholds: null,
  };
  for (const [key, value] of Object.entries(shared)) {
    studyCheck(equal(protocol[key], value), `protocol ${key}`);
  }
  studyCheck(exactStudyKeys(protocol.phase_seeds, ['exploration', 'evaluation']), 'protocol phases');
  for (const phase of ['exploration', 'evaluation']) {
    studyCheck(equal(protocol.phase_seeds[phase], expectedStudySeeds(id, phase)), `protocol ${phase} seeds`);
  }
  studyCheck(Array.isArray(protocol.scenarios) && protocol.scenarios.length === 2, 'protocol cases');
  for (const [i, scenario] of protocol.scenarios.entries()) {
    const name = spec.cases[i];
    studyCheck(exactStudyKeys(scenario, ['name', 'description', 'initial_angle_deg', 'initial_angle_std_deg',
      'bias_ramp', 'accel_dropout', 'timing_jitter', 'accel_noise_std_m_s2', 'accel_delay_s']), 'protocol case fields');
    studyCheck(scenario.name === name && safeText(scenario.description)
      && scenario.initial_angle_deg === 0 && scenario.initial_angle_std_deg === 0
      && scenario.bias_ramp === (name === 'bias_ramp') && scenario.accel_dropout === false
      && scenario.timing_jitter === false && scenario.accel_delay_s === 0
      && scenario.accel_noise_std_m_s2 === (name === 'accel_noise_mismatch' ? 0.6 : 0.2), 'protocol case assumptions');
  }
  const expected = id === 'r' ? {
    assumed_accel_std_m_s2: [0.1, 0.2, 0.6], default_setting: '0.2',
    r_diagonal_m2_s4: { '0.1': 0.1 ** 2, '0.2': 0.2 ** 2, '0.6': 0.6 ** 2 },
    gyro_intervals: 3000, accel_observations: 300, true_bias_deg_s: 0.5,
    initial_covariance_si: [[0, 0], [0, (Math.PI / 180) ** 2]],
    assumed_gyro_std_rad_s: Math.PI / 180, complementary_tau_s: 1,
    late_window_s_closed: [25, 30],
    q_model: 'diag(sigma_gyro^2 * dt^2, 0); independent interval-mean noise',
    rmse_weighting: 'all endpoints including initialization; no window restart',
    nis_epochs: 'all corrections, using the prior innovation and full S',
    pairing: 'same arrays within case; same gyro and standardized force draws across cases',
    delta: 'per-seed setting metric minus default-setting metric',
    selection_rule: 'none; report every setting',
    frame: 'forward-right-down; positive right-hand roll about x',
    observation: 'unnormalized [f_y, f_z] = [-g*sin(roll), -g*cos(roll)] + noise',
    timing: 'predict using interval mean, then correct at endpoint; no t=0 observation',
    aggregation: 'mean and observed min/max across seeds, separately by phase/case',
    numerical_covariance_tolerance_si: 1e-12,
  } : {
    bias_std_deg_s_per_sqrt_s: [0, 0.03, 0.1], reference_setting: '0',
    assumed_gyro_std_deg_s: 1, assumed_accel_std_m_s2: 0.2,
    initial_std_deg_deg_s: [0, 1], initial_cross_covariance: 0,
    interval_count: 3000, observation_count: 300,
    closed_windows_s: { ramp: [10, 20], late: [25, 30] },
    bias_model: 'db = sigma_b dW; predicted mean bias unchanged',
    q_bias: 'sigma_b^2 * [[dt^3/3, -dt^2/2], [-dt^2/2, dt]]',
    q_bias_density_units: 'rad^2/s^3',
    q_gyro: 'diag(sigma_g^2 * dt^2, 0); interval-mean noise',
    rmse: 'unwrapped endpoint error including initialization',
    late_bias_fluctuation: 'temporal error std around its own mean, ddof=0; includes settling',
    nis: 'joint prior innovation, all corrections', selection: 'none; retain all settings',
    frame: 'forward-right-down; positive right-hand roll',
    timing: 'interval-mean gyro; predict then correct at endpoint',
    aggregation: 'per-seed metrics and paired setting-minus-zero differences; mean/min/max',
    covariance_tolerance_si: 1e-12,
    parity_tolerances: Object.fromEntries(
      ['angle_rad', 'bias_rad_s', 'p00', 'p01', 'p10', 'p11', 'v0', 'v1', 's00', 's01', 's10', 's11']
        .map(name => [name, { atol: name.startsWith('p') ? 1e-12 : 1e-10, rtol: 1e-10 }]),
    ),
  };
  studyCheck(exactStudyKeys(protocol, ['version', 'declared_date', 'scenarios', 'phase_seeds',
    ...Object.keys(shared), ...Object.keys(expected)]), 'protocol fields');
  for (const [key, value] of Object.entries(expected)) {
    studyCheck(equal(protocol[key], value), `protocol ${key}`);
  }
  // These metadata strings must remain public text; they are displayed as provenance.
  for (const value of Object.values(protocol)) {
    if (typeof value === 'string') studyCheck(safeText(value), 'protocol text');
  }
}

function validateProvenance(provenance, id, phase) {
  const spec = STUDY_SPECS[id];
  studyCheck(exactStudyKeys(provenance, ['summary_file', 'summary_sha256', 'protocol_sha256',
    'source_sha256', 'environment', ...(id === 'bias' ? ['cpp_binary'] : [])]), 'provenance fields');
  studyCheck(provenance.summary_file === `results/${spec.directory}/${phase}-summary.json`, 'summary path');
  studyCheck(hash(provenance.summary_sha256) && provenance.protocol_sha256 === spec.protocolHash, 'provenance hash');
  const sources = id === 'r'
    ? ['r_sensitivity.py', 'r_sensitivity_plot.py', 'stress_scenarios.py', 'stress_evaluation.py',
      'simulation.py', 'ekf.py', 'integration.py', 'tilt.py']
    : ['bias_experiment.py', 'ekf.py', 'cpp_parity.py', 'stress_scenarios.py', 'simulation.py', 'stress_evaluation.py'];
  studyCheck(exactStudyKeys(provenance.source_sha256, sources)
    && Object.values(provenance.source_sha256).every(hash), 'source fingerprints');
  studyCheck(exactStudyKeys(provenance.environment, ['python', 'numpy', 'matplotlib'])
    && Object.values(provenance.environment).every(safeText), 'environment');
  if (id === 'bias') {
    const cpp = provenance.cpp_binary;
    studyCheck(exactStudyKeys(cpp, ['sha256', 'protocol_version', 'compiler_id', 'compiler_version',
      'build_type', 'eigen_version', 'cxx_standard']), 'C++ provenance fields');
    studyCheck(hash(cpp.sha256) && cpp.protocol_version === 1 && cpp.cxx_standard === 17
      && ['compiler_id', 'compiler_version', 'build_type', 'eigen_version'].every(key => safeText(cpp[key])), 'C++ provenance');
  }
}

export function validateStudies(data) {
  studyCheck(exactStudyKeys(data, ['schema_version', 'data_source', 'studies'])
    && data.schema_version === 1 && data.data_source === 'simulation', 'schema');
  studyCheck(exactStudyKeys(data.studies, ['r', 'bias']), 'studies');
  for (const [id, spec] of Object.entries(STUDY_SPECS)) {
    const study = data.studies[id];
    studyCheck(exactStudyKeys(study, ['reference_setting', 'settings', 'phases'])
      && study.reference_setting === spec.reference && equal(study.settings, spec.settings), 'setting grid');
    studyCheck(exactStudyKeys(study.phases, ['exploration', 'evaluation']), 'phases');
    for (const phaseName of ['exploration', 'evaluation']) {
      const phase = study.phases[phaseName];
      studyCheck(exactStudyKeys(phase, ['seeds', 'protocol', 'limitations', 'provenance', 'cases']), 'phase fields');
      studyCheck(equal(phase.seeds, expectedStudySeeds(id, phaseName)), 'phase seeds');
      validateStudyProtocol(phase.protocol, id);
      validateProvenance(phase.provenance, id, phaseName);
      studyCheck(Array.isArray(phase.limitations) && phase.limitations.length > 0
        && phase.limitations.every(safeText), 'limitations');
      studyCheck(exactStudyKeys(phase.cases, spec.cases), 'cases');
      for (const scenario of Object.values(phase.cases)) {
        studyCheck(exactStudyKeys(scenario, ['trials']) && Array.isArray(scenario.trials)
          && scenario.trials.length === phase.seeds.length, 'trials');
        scenario.trials.forEach((trial, i) => {
          studyCheck(exactStudyKeys(trial, ['seed', 'measurement_sha256', 'metrics', ...(id === 'r' ? ['baselines'] : [])]), 'trial fields');
          studyCheck(trial.seed === phase.seeds[i] && hash(trial.measurement_sha256), 'trial seed or fingerprint');
          studyCheck(exactStudyKeys(trial.metrics, spec.settings), 'trial settings');
          Object.values(trial.metrics).forEach(metrics => validateStudyMetrics(metrics, id));
          if (id === 'r') {
            studyCheck(exactStudyKeys(trial.baselines, ['gyro', 'complementary']), 'baseline methods');
            Object.values(trial.baselines).forEach(metrics => validateStudyMetrics(metrics, id, true));
          }
        });
      }
    }
    studyCheck(equal(study.phases.exploration.protocol, study.phases.evaluation.protocol), 'protocol changed between phases');
    for (const key of ['source_sha256', 'environment', ...(id === 'bias' ? ['cpp_binary'] : [])]) {
      studyCheck(equal(study.phases.exploration.provenance[key], study.phases.evaluation.provenance[key]),
        `${key} changed between phases`);
    }
  }
  return data;
}

export function summarizeStudyValues(values) {
  studyCheck(Array.isArray(values) && values.length > 0 && values.every(Number.isFinite), 'statistics values');
  const mean = values.reduce((sum, value) => sum + value / values.length, 0);
  studyCheck(Number.isFinite(mean), 'statistics overflow');
  return { mean, min: Math.min(...values), max: Math.max(...values) };
}

// Differences are paired by seed before aggregation. Ranges are observed ranges,
// not confidence intervals; the sign of NIS or signed-error deltas is not a ranking.
export function studyStatistics(phase, caseId, setting, metric, reference) {
  const trials = phase?.cases?.[caseId]?.trials;
  studyCheck(Array.isArray(trials) && trials.length > 0, 'statistics case');
  const values = trials.map(trial => {
    const value = trial.metrics?.[setting]?.[metric];
    const referenceValue = trial.metrics?.[reference]?.[metric];
    studyCheck(Number.isFinite(value) && Number.isFinite(referenceValue), 'statistics metric or setting');
    return { seed: trial.seed, value, reference: referenceValue, delta: value - referenceValue };
  });
  const deltas = values.map(value => value.delta);
  return {
    values, ...summarizeStudyValues(values.map(value => value.value)),
    reference_mean: summarizeStudyValues(values.map(value => value.reference)).mean,
    delta: {
      ...summarizeStudyValues(deltas),
      negative: deltas.filter(value => value < 0).length,
      zero: deltas.filter(value => value === 0).length,
      positive: deltas.filter(value => value > 0).length,
    },
  };
}
