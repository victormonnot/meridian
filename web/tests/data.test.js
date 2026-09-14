import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import test from 'node:test';
import { SERIES, advanceTime, clampTime, formatValue, latestCorrection, sampleAt, validateComparison } from '../src/data.js';

const artifact = JSON.parse(readFileSync(new URL('../public/data/roll-comparison.json', import.meta.url)));
const summary = JSON.parse(readFileSync(new URL('../../results/ekf-comparison/summary.json', import.meta.url)));
const controlled = JSON.parse(readFileSync(new URL('../../results/controlled-scenarios/summary.json', import.meta.url)));

test('shipped display artifact matches the public experiment summary', () => {
  validateComparison(artifact);
  assert.equal(artifact.seed, summary.seed);
  for (const [key, value] of Object.entries(artifact.config)) assert.equal(value, summary.config[key]);
  assert.equal(artifact.seed, controlled.seed);
  for (const [name, scenario] of Object.entries(artifact.scenarios)) {
    assert.equal(scenario.source_sample_count, 3001);
    assert.equal(scenario.rows.length, name === 'accel_dropout' ? 852 : 901);
    for (const boundary of [0, 12, 17, 30]) assert.ok(scenario.rows.some(row => row[0] === boundary));
    for (const method of SERIES.slice(1)) {
      const expected = scenario.source === 'controlled' ? controlled.scenarios[name].metrics[method.id].angle_rmse_deg
        : method.id === 'ekf' ? summary.scenarios[name].vector_ekf.rmse_deg
          : summary.scenarios[name].baselines[method.id].rmse_deg;
      assert.equal(scenario.metrics.rmse_deg[method.id], expected);
    }
  }
  assert.deepEqual(artifact.sources.paired.stream_seeds, {
    gyro: '16138347438539916964', accelerometer: '134183728835869882',
  });
  for (const [source, folder] of [['paired', 'ekf-comparison'], ['controlled', 'controlled-scenarios']]) {
    const bytes = readFileSync(new URL(`../../results/${folder}/summary.json`, import.meta.url));
    assert.equal(artifact.sources[source].source_sha256['summary.json'], createHash('sha256').update(bytes).digest('hex'));
  }
});

test('wrong initialization remains distinct from simulation truth', () => {
  const scenario = artifact.scenarios.initial_offset;
  assert.equal(scenario.initialization.angle_std_deg, 30);
  assert.equal(sampleAt(scenario.rows, 0)[1], 0);
  assert.deepEqual(sampleAt(scenario.rows, 0).slice(2, 6), [60, 60, 60, 60]);
  assert.equal(latestCorrection(scenario, .099), null);
  assert.equal(latestCorrection(scenario, .1), .1);
});

test('dropout retains predictions and applies bias recovery only at 17 seconds', () => {
  const scenario = artifact.scenarios.accel_dropout;
  assert.equal(scenario.correction_times_s.length, 250);
  assert.ok(!scenario.correction_times_s.some(time => time >= 12 && time < 17));
  assert.ok(Math.abs(latestCorrection(scenario, 16.999) - 11.9) < 1e-10);
  assert.equal(latestCorrection(scenario, 17), 17);
  const before = sampleAt(scenario.rows, 11.9);
  assert.deepEqual(sampleAt(scenario.rows, 16.999).slice(7), before.slice(7));
  assert.notEqual(sampleAt(scenario.rows, 16).at(5), sampleAt(scenario.rows, 14).at(5));
  const recovery = scenario.rows.find(row => row[0] === 17);
  assert.deepEqual(sampleAt(scenario.rows, 17), recovery);
  assert.notDeepEqual(recovery.slice(7), before.slice(7));
});

test('bias ramp interpolates truth continuously while holding estimated biases', () => {
  const scenario = artifact.scenarios.bias_ramp;
  assert.equal(scenario.correction_times_s.length, 300);
  for (const time of [9.995, 10, 10.025, 15, 15.025, 19.995, 20, 20.025, 30]) {
    const row = sampleAt(scenario.rows, time);
    const expected = .5 + .1 * Math.max(0, Math.min(10, time - 10));
    assert.ok(Math.abs(row[6] - expected) < 1e-9, `truth at ${time}`);
    if (time === 15.025) assert.deepEqual(row.slice(7), sampleAt(scenario.rows, 15).slice(7));
  }
  assert.equal(latestCorrection(scenario, 15), 15);
  assert.ok(Math.abs(sampleAt(scenario.rows, 30)[8] - .934711) < 1e-6);
  // The four previous runs retain their constant true bias.
  for (const [name, other] of Object.entries(artifact.scenarios)) {
    if (name !== 'bias_ramp') assert.equal(sampleAt(other.rows, 15.025)[6], .5);
  }
});

test('decimal slider time resolves a correction at the same physical instant', () => {
  const rows = [[.29, 1, 1, 1, 1, 1, .5, 0, 0], [.30000000000000004, 2, 2, 2, 2, 2, .5, 1, 1], [.4, 3, 3, 3, 3, 3, .5, 2, 2]];
  assert.deepEqual(sampleAt(rows, .3).slice(7), [1, 1]);
  assert.equal(latestCorrection({ correction_times_s: [.1, .2, .30000000000000004] }, .3), .30000000000000004);
  assert.deepEqual(sampleAt(rows, .29999).slice(7), [0, 0]);
});

test('cursor interpolation uses actual time intervals and clamps endpoints', () => {
  const rows = [[0, 2, -5], [.1, 4, -1], [.7, 10, 11]];
  assert.deepEqual(sampleAt(rows, -.1), rows[0]);
  assert.deepEqual(sampleAt(rows, 1), rows[2]);
  assert.deepEqual(sampleAt(rows, .1), rows[1]);
  assert.deepEqual(sampleAt(rows, .05), [.05, 3, -3]);
  const sample = sampleAt(rows, .4);
  assert.ok(Math.abs(sample[1] - 7) < 1e-12);
  assert.ok(Math.abs(sample[2] - 5) < 1e-12);
  sampleAt(rows, 0)[1] = 100;
  assert.equal(rows[0][1], 2);
});

test('playback applies speed and stops exactly at the run end', () => {
  assert.deepEqual(advanceTime(8, .2, .5, 30), { time: 8.1, ended: false });
  assert.deepEqual(advanceTime(8, .2, 2, 30), { time: 8.4, ended: false });
  assert.deepEqual(advanceTime(29.9, 10, 2, 30), { time: 30, ended: true });
  assert.deepEqual(advanceTime(8, -1, 1, 30), { time: 8, ended: false });
  assert.equal(clampTime(-2, 30), 0);
  assert.equal(formatValue(-.00001, 3), '0.000');
});

const mutations = {
  'missing configuration': data => { delete data.config.amplitude_deg; },
  'unknown schema': data => { data.schema_version = 99; },
  'incorrect units': data => { data.columns[1] = 'truth_roll_rad'; },
  'empty scenario': data => { data.scenarios.nominal.rows = []; },
  'non-finite sample': data => { data.scenarios.nominal.rows[10][3] = NaN; },
  'duplicate timestamp': data => { data.scenarios.nominal.rows[10][0] = data.scenarios.nominal.rows[9][0]; },
  'missing endpoint': data => { data.scenarios.nominal.rows.pop(); },
  'missing metric': data => { delete data.scenarios.nominal.metrics.rmse_deg.ekf; },
  'invalid disturbance': data => { data.scenarios.translation_pulse.event.end_s = 31; },
  'incorrect initial covariance': data => { data.scenarios.initial_offset.initialization.angle_std_deg = 0; },
  'invented missing correction': data => { data.scenarios.accel_dropout.correction_times_s.push(16); },
  'wrong recovery': data => { data.scenarios.accel_dropout.event.first_correction_after_s = 16.9; },
  'domain hiding initial error': data => { data.scenarios.initial_offset.domains.roll_deg = [-20, 20]; },
  'bias moving before correction': data => { data.scenarios.accel_dropout.rows[1][8] += 1; },
  'incorrect source': data => { data.scenarios.initial_offset.source = 'paired'; },
  'missing correction predecessor': data => { data.scenarios.accel_dropout.rows = data.scenarios.accel_dropout.rows.filter(row => Math.abs(row[0] - 16.99) > 1e-10); },
  'wrong ramp endpoint': data => { data.scenarios.bias_ramp.event.final_bias_deg_s = .5; },
  'wrong ramp slope': data => { data.scenarios.bias_ramp.event.slope_deg_s2 = .2; },
  'truth plateau returning to baseline': data => { data.scenarios.bias_ramp.rows.at(-1)[6] = .5; },
  'ramp mislabeled as dropout': data => { data.scenarios.bias_ramp.event.kind = 'accel_dropout'; },
  'missing ramp boundary': data => { data.scenarios.bias_ramp.rows = data.scenarios.bias_ramp.rows.filter(row => row[0] !== 20); },
};
for (const [name, mutate] of Object.entries(mutations)) {
  test(`reject ${name} instead of displaying misleading records`, () => {
    const data = structuredClone(artifact);
    mutate(data);
    assert.throws(() => validateComparison(data), /Invalid comparison data/);
  });
}
