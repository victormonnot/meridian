import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { SERIES, advanceTime, clampTime, formatValue, sampleAt, validateComparison } from '../src/data.js';

const artifact = JSON.parse(readFileSync(new URL('../public/data/roll-comparison.json', import.meta.url)));
const summary = JSON.parse(readFileSync(new URL('../../results/ekf-comparison/summary.json', import.meta.url)));

test('shipped display artifact matches the public experiment summary', () => {
  validateComparison(artifact);
  assert.equal(artifact.seed, summary.seed);
  assert.deepEqual(artifact.config, summary.config);
  for (const [name, scenario] of Object.entries(artifact.scenarios)) {
    assert.equal(scenario.source_sample_count, 3001);
    assert.equal(scenario.rows.length, 601);
    for (const boundary of [0, 12, 17, 30]) assert.ok(scenario.rows.some(row => row[0] === boundary));
    for (const method of SERIES.slice(1)) {
      const expected = method.id === 'ekf' ? summary.scenarios[name].vector_ekf
        : summary.scenarios[name].baselines[method.id];
      assert.equal(scenario.metrics.rmse_deg[method.id], expected.rmse_deg);
    }
  }
  assert.deepEqual(artifact.provenance.stream_seeds, {
    gyro: '16138347438539916964', accelerometer: '134183728835869882',
  });
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
  'unknown schema': data => { data.schema_version = 2; },
  'incorrect units': data => { data.columns[1] = 'truth_roll_rad'; },
  'empty scenario': data => { data.scenarios.nominal.rows = []; },
  'non-finite sample': data => { data.scenarios.nominal.rows[10][3] = NaN; },
  'duplicate timestamp': data => { data.scenarios.nominal.rows[10][0] = data.scenarios.nominal.rows[9][0]; },
  'missing endpoint': data => { data.scenarios.nominal.rows.pop(); },
  'missing metric': data => { delete data.scenarios.nominal.metrics.rmse_deg.ekf; },
  'invalid disturbance': data => { data.scenarios.translation_pulse.disturbance.end_s = 31; },
};
for (const [name, mutate] of Object.entries(mutations)) {
  test(`reject ${name} instead of displaying misleading records`, () => {
    const data = structuredClone(artifact);
    mutate(data);
    assert.throws(() => validateComparison(data), /Invalid comparison data/);
  });
}
