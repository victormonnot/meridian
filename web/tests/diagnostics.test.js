import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { COLUMNS } from '../src/data.js';
import { diagnosticStateAt, lastInnovation, stateDiagnostic, validateDiagnostics } from '../src/diagnostics.js';

const artifact = JSON.parse(readFileSync(new URL('../public/data/roll-comparison.json', import.meta.url)));

test('full diagnostic state is causal at original uniform and irregular endpoints', () => {
  for (const name of ['nominal', 'timing_jitter', 'accel_delay']) {
    const scenario = artifact.scenarios[name];
    validateDiagnostics(scenario, COLUMNS);
    assert.equal(scenario.diagnostics.states.length, 3001);
    for (const time of scenario.correction_times_s) {
      const i = scenario.diagnostics.states.findIndex(row => Math.abs(row[0] - time) < 1e-12);
      const before = scenario.diagnostics.states[i - 1];
      const after = scenario.diagnostics.states[i];
      assert.deepEqual(diagnosticStateAt(scenario, (before[0] + time) / 2), before);
      assert.deepEqual(diagnosticStateAt(scenario, time), after);
    }
  }
});

test('model uncertainty remains distinct from actual angle error', () => {
  const nominal = diagnosticStateAt(artifact.scenarios.nominal, 14);
  const pulse = diagnosticStateAt(artifact.scenarios.translation_pulse, 14);
  assert.deepEqual(nominal.slice(9), pulse.slice(9));
  const value = stateDiagnostic(pulse, 'ekf', 'roll');
  assert.ok(value.error < -6 && value.sigma < .23);
  const initial = stateDiagnostic(diagnosticStateAt(artifact.scenarios.initial_overconfident, 0), 'ekf', 'roll');
  assert.deepEqual(initial, { time: 0, error: 60, sigma: 0 });
});

test('baselines have no invented covariance, bias estimate or NIS', () => {
  const scenario = artifact.scenarios.nominal;
  for (const method of ['gyro', 'complementary']) {
    assert.equal(stateDiagnostic(diagnosticStateAt(scenario, 8), method, 'roll').sigma, null);
    assert.equal(stateDiagnostic(diagnosticStateAt(scenario, 8), method, 'bias'), null);
    assert.equal(lastInnovation(scenario, method, 8), null);
  }
});

test('dropout contains no innovation dots and reports only the timestamped last correction', () => {
  const scenario = artifact.scenarios.accel_dropout;
  for (const method of ['kalman', 'ekf']) {
    assert.equal(lastInnovation(scenario, method, 0), null);
    assert.equal(scenario.diagnostics[method].rows.length, 250);
    assert.ok(!scenario.diagnostics[method].rows.some(row => row[0] >= 12 && row[0] < 17));
    const before = lastInnovation(scenario, method, 11.9);
    assert.deepEqual(lastInnovation(scenario, method, 16.999), before);
    assert.equal(lastInnovation(scenario, method, 17)[0], 17);
  }
  const before = stateDiagnostic(diagnosticStateAt(scenario, 16.99), 'ekf', 'roll');
  const after = stateDiagnostic(diagnosticStateAt(scenario, 17), 'ekf', 'roll');
  assert.ok(before.sigma > after.sigma);
});

const mutations = {
  'wrong state phase': scenario => { scenario.diagnostics.state_phase = 'prior'; },
  'wrong innovation phase': scenario => { scenario.diagnostics.innovation_phase = 'posterior'; },
  'missing endpoint': scenario => { scenario.diagnostics.states.splice(22, 1); },
  'negative model standard deviation': scenario => { scenario.diagnostics.states[10][10] = -.1; },
  'incorrect state units': scenario => { scenario.diagnostics.state_columns[10] = 'ekf_roll_std_rad'; },
  'misaligned trajectory': scenario => { scenario.diagnostics.states[10][5] += 1; },
  'wrong NIS dimension': scenario => { scenario.diagnostics.ekf.dimension = 1; },
  'zero scalar S': scenario => { scenario.diagnostics.kalman.rows[0][2] = 0; },
  'nonpositive vector S': scenario => { scenario.diagnostics.ekf.rows[0][4] = 1; },
  'wrong NIS': scenario => { scenario.diagnostics.ekf.rows[0][6] += 1; },
  'wrong aggregate': scenario => { scenario.diagnostics.ekf.mean_nis += 1; },
  'premature innovation': scenario => { scenario.diagnostics.ekf.rows[0][0] -= .01; },
};
for (const [name, mutate] of Object.entries(mutations)) test(`reject diagnostic ${name}`, () => {
  const scenario = structuredClone(artifact.scenarios.nominal);
  mutate(scenario);
  assert.throws(() => validateDiagnostics(scenario, COLUMNS), /Invalid comparison data: diagnostics/);
});
