export const STD_COLUMNS = ['kalman_roll_std_deg', 'ekf_roll_std_deg', 'kalman_bias_std_deg_s', 'ekf_bias_std_deg_s'];
export const INNOVATION_COLUMNS = {
  kalman: ['arrival_time_s', 'innovation_deg', 's_deg2', 'nis'],
  ekf: ['arrival_time_s', 'innovation_y_m_s2', 'innovation_z_m_s2', 's_yy_m2_s4', 's_yz_m2_s4', 's_zz_m2_s4', 'nis'],
};

function check(condition, message) {
  if (!condition) throw new Error(`Invalid comparison data: diagnostics ${message}`);
}

function endpointIndex(rows, time) {
  let lo = 0;
  let hi = rows.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (rows[mid][0] <= time + 1e-10) lo = mid + 1;
    else hi = mid;
  }
  return lo - 1;
}

export function validateDiagnostics(scenario, baseColumns) {
  const data = scenario.diagnostics;
  check(data?.state_phase === 'endpoint_after_available_correction'
    && data.innovation_phase === 'prior_before_correction', 'phases');
  check(JSON.stringify(data.state_columns) === JSON.stringify([...baseColumns, ...STD_COLUMNS]), 'state columns/units');
  check(Array.isArray(data.states) && data.states.length === scenario.source_sample_count, 'endpoint count');
  let dtMin = Infinity;
  let dtMax = 0;
  data.states.forEach((row, i) => {
    check(Array.isArray(row) && row.length === 13 && row.every(Number.isFinite)
      && row.slice(9).every(value => value >= 0), 'finite state and nonnegative uncertainty');
    if (i > 0) {
      const dt = row[0] - data.states[i - 1][0];
      check(dt > 0, 'endpoint order');
      dtMin = Math.min(dtMin, dt);
      dtMax = Math.max(dtMax, dt);
    }
  });
  check(data.states[0][0] === 0 && data.states.at(-1)[0] === 30, 'time extent');
  check(Math.abs(dtMin - scenario.timing.gyro_interval_min_s) < 1e-10
    && Math.abs(dtMax - scenario.timing.gyro_interval_max_s) < 1e-10, 'endpoint intervals');
  check(data.states[0].slice(9).every((value, i) => value === (i < 2
    ? scenario.initialization.angle_std_deg : scenario.initialization.bias_std_deg_s)), 'initial uncertainty');
  for (const row of scenario.rows) {
    const original = data.states[endpointIndex(data.states, row[0])];
    check(original && row.every((value, i) => Math.abs(value - original[i]) < 1e-10), 'trajectory alignment');
  }
  for (const [method, dimension] of [['kalman', 1], ['ekf', 2]]) {
    const record = data[method];
    const columns = INNOVATION_COLUMNS[method];
    check(record?.dimension === dimension && JSON.stringify(record.columns) === JSON.stringify(columns), 'innovation dimension/units');
    check(Array.isArray(record.rows) && record.rows.length === scenario.correction_times_s.length, 'innovation count');
    let sum = 0;
    record.rows.forEach((row, i) => {
      check(Array.isArray(row) && row.length === columns.length && row.every(Number.isFinite), 'finite innovation');
      check(Math.abs(row[0] - scenario.correction_times_s[i]) < 1e-10, 'innovation arrival');
      const index = endpointIndex(data.states, row[0]);
      check(index > 0 && Math.abs(data.states[index][0] - row[0]) < 1e-10
        && Math.abs(data.states[index - 1][0] - scenario.timing.correction_predecessor_times_s[i]) < 1e-10, 'correction predecessor');
      let expected;
      if (dimension === 1) {
        check(row[2] > 0, 'positive scalar S');
        expected = row[1] ** 2 / row[2];
      } else {
        const [, vy, vz, syy, syz, szz] = row;
        const det = syy * szz - syz ** 2;
        check(syy > 0 && szz > 0 && det > 0, 'positive definite vector S');
        expected = (szz * vy ** 2 - 2 * syz * vy * vz + syy * vz ** 2) / det;
      }
      check(Number.isFinite(expected) && row.at(-1) >= 0
        && Math.abs(row.at(-1) - expected) <= 1e-8 * Math.max(1, expected), 'NIS identity');
      sum += row.at(-1);
    });
    check(Number.isFinite(record.mean_nis) && Math.abs(sum / record.rows.length - record.mean_nis)
      <= 1e-8 * Math.max(1, record.mean_nis), 'mean NIS');
  }
}

export function diagnosticStateAt(scenario, time) {
  const rows = scenario.diagnostics.states;
  return rows[Math.max(0, endpointIndex(rows, time))].slice();
}

export function lastInnovation(scenario, method, time) {
  const rows = scenario.diagnostics[method]?.rows;
  if (!rows) return null;
  const index = endpointIndex(rows, time);
  return index < 0 ? null : rows[index].slice();
}

export function stateDiagnostic(row, method, component) {
  const roll = { gyro: 2, complementary: 3, kalman: 4, ekf: 5 };
  const bias = { kalman: 7, ekf: 8 };
  const columns = component === 'roll' ? roll : bias;
  if (!(method in columns)) return null;
  const sigmaColumn = component === 'roll' ? { kalman: 9, ekf: 10 } : { kalman: 11, ekf: 12 };
  return { time: row[0], error: row[columns[method]] - row[component === 'roll' ? 1 : 6],
    sigma: method in sigmaColumn ? row[sigmaColumn[method]] : null };
}
