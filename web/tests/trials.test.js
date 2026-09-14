import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { validateComparison } from '../src/data.js';
import { trialStatistics, validateRepeatedTrials } from '../src/trials.js';

const METHODS = ['gyro', 'complementary', 'kalman', 'ekf'];

function scenario(values = [1, 3, 2], selected = 4, seeds = [7, 3, 9]) {
  return {
    metrics: { rmse_deg: Object.fromEntries(METHODS.map(method => [method, selected])) },
    repeated_trials: { seeds: seeds.slice(), rmse_deg: Object.fromEntries(METHODS.map(method => [method, values.slice()])) },
  };
}

test('trial statistics preserve seed pairing and compute cohort-only minimum, mean and maximum', () => {
  const input = scenario();
  const before = structuredClone(input);
  validateRepeatedTrials(input, 42);
  const result = trialStatistics(input, 'ekf');
  assert.deepEqual(result.seeds, [7, 3, 9]);
  assert.deepEqual(result.values, [1, 3, 2]);
  assert.deepEqual([result.min, result.mean, result.max, result.selected], [1, 2, 3, 4]);
  assert.deepEqual(result.domain, [.7, 4.3]);
  result.seeds[0] = 100;
  result.values[0] = 100;
  result.domain[0] = 100;
  assert.deepEqual(input, before);
});

test('selected metrics outside the cohort stay visible without changing the cohort statistics', () => {
  for (const selected of [0, 8]) {
    const result = trialStatistics(scenario([1, 3, 2], selected), 'kalman');
    assert.deepEqual([result.min, result.mean, result.max], [1, 2, 3]);
    assert.ok(result.domain[0] <= Math.min(1, selected));
    assert.ok(result.domain[1] >= Math.max(3, selected));
    assert.ok(result.domain[0] >= 0);
  }
});

test('single, constant, zero, subnormal and very large finite trials retain useful finite domains', () => {
  for (const values of [[2], [2, 2, 2], [0], [0, 0], [Number.MIN_VALUE],
    [Number.MAX_VALUE, Number.MAX_VALUE], [Number.MAX_VALUE, 0, Number.MAX_VALUE]]) {
    const input = scenario(values, values[0], values.map((_, i) => i));
    const result = trialStatistics(input, 'gyro');
    assert.ok(Number.isFinite(result.mean) && result.mean >= result.min && result.mean <= result.max);
    assert.ok(result.domain.every(Number.isFinite));
    assert.ok(result.domain[0] >= 0 && result.domain[1] > result.domain[0]);
    assert.ok(values.every(value => value >= result.domain[0] && value <= result.domain[1]));
    if (values.every(value => value === values[0])) assert.equal(result.mean, values[0]);
  }
});

test('statistics do not use diagnostic-window fields or time-weighted metrics', () => {
  const input = scenario();
  const expected = trialStatistics(input, 'complementary');
  input.metrics.time_weighted_rmse_deg = { complementary: 1000 };
  input.window = [5, 10];
  input.rows = [[0, 0], [30, 1000]];
  assert.deepEqual(trialStatistics(input, 'complementary'), expected);
});

test('selected seed consistency uses its position and checks all methods', () => {
  const input = scenario([1, 3, 2], 3);
  validateRepeatedTrials(input, 3);
  for (const method of METHODS) {
    const changed = structuredClone(input);
    changed.metrics.rmse_deg[method] += 1e-8;
    assert.throws(() => validateRepeatedTrials(changed, 3), /selected seed/);
  }
  input.metrics.rmse_deg.ekf += 1e-11;
  validateRepeatedTrials(input, 3);
  validateRepeatedTrials(scenario(), 42);
});

for (const [label, seeds] of [
  ['missing', undefined], ['empty', []], ['scalar', 1], ['duplicate', [1, 1, 2]],
  ['negative', [-1, 2, 3]], ['boolean', [true, 2, 3]], ['fraction', [.5, 2, 3]],
  ['string', ['1', 2, 3]], ['unsafe', [Number.MAX_SAFE_INTEGER + 1, 2, 3]],
  ['nonfinite', [Infinity, 2, 3]], ['sparse', new Array(3)],
]) test(`trial validation rejects ${label} seeds`, () => {
  const input = scenario();
  input.repeated_trials.seeds = seeds;
  assert.throws(() => validateRepeatedTrials(input, 42), /seeds/);
});

for (const [label, values] of [
  ['missing', undefined], ['short', [1, 2]], ['long', [1, 2, 3, 4]], ['scalar', 2],
  ['negative', [-1, 2, 3]], ['boolean', [true, 2, 3]], ['string', ['1', 2, 3]],
  ['nonfinite', [NaN, 2, 3]], ['infinite', [Infinity, 2, 3]], ['sparse', new Array(3)],
]) test(`trial validation rejects ${label} RMSE values`, () => {
  const input = scenario();
  input.repeated_trials.rmse_deg.ekf = values;
  assert.throws(() => validateRepeatedTrials(input, 42), /values/);
});

test('trial validation rejects missing or extra fields and unknown methods', () => {
  for (const mutate of [
    input => { delete input.repeated_trials; },
    input => { input.repeated_trials = null; },
    input => { input.repeated_trials.extra = 1; },
    input => { input.repeated_trials.rmse_deg = []; },
    input => { delete input.repeated_trials.rmse_deg.kalman; },
    input => { input.repeated_trials.rmse_deg.extra = [1, 2, 3]; },
  ]) {
    const input = scenario();
    mutate(input);
    assert.throws(() => validateRepeatedTrials(input, 42), /repeated trials/);
  }
  for (const method of ['truth', 'toString', '', null]) {
    assert.throws(() => trialStatistics(scenario(), method), /unknown method/);
  }
});

test('invalid selected metrics and selected seed are rejected', () => {
  for (const value of [undefined, NaN, Infinity, -1, true, '1']) {
    const input = scenario();
    input.metrics.rmse_deg.kalman = value;
    assert.throws(() => validateRepeatedTrials(input, 42), /selected metric/);
    assert.throws(() => trialStatistics(input, 'kalman'), /selected metric/);
  }
  for (const seed of [undefined, NaN, -1, true, '42', 1.5, Number.MAX_SAFE_INTEGER + 1]) {
    assert.throws(() => validateRepeatedTrials(scenario(), seed), /selected seed/);
  }
});

function readJson(relative) {
  return JSON.parse(readFileSync(new URL(relative, import.meta.url)));
}

test('comparison validation requires the repeated-trial schema and seed order shared within a source', () => {
  const input = readJson('../public/data/roll-comparison.json');
  input.schema_version = 5;
  for (const record of Object.values(input.scenarios)) {
    record.repeated_trials = scenario().repeated_trials;
  }
  validateComparison(input);
  const reordered = structuredClone(input);
  reordered.scenarios.translation_pulse.repeated_trials.seeds.reverse();
  assert.throws(() => validateComparison(reordered), /seeds differ within one source/);
  const missing = structuredClone(input);
  delete missing.scenarios.nominal.repeated_trials;
  assert.throws(() => validateComparison(missing), /repeated trials/);
  input.schema_version = 4;
  assert.throws(() => validateComparison(input), /unsupported format/);
});

test('shipped repeated trials retain every source seed and all four endpoint RMSE values', () => {
  const data = readJson('../public/data/roll-comparison.json');
  const sources = {
    paired: readJson('../../results/ekf-comparison/summary.json').validation,
    controlled: readJson('../../results/controlled-scenarios/summary.json').validation,
  };
  validateComparison(data);
  for (const [name, record] of Object.entries(data.scenarios)) {
    const source = sources[record.source];
    assert.deepEqual(record.repeated_trials.seeds, source.seeds);
    for (const method of METHODS) {
      const field = `${method === 'kalman' ? 'angle_kalman' : method}_angle_rmse_deg`;
      const expected = source.trials.map(trial => record.source === 'paired'
        ? trial[name][field] : trial.scenarios[name].metrics[method].angle_rmse_deg);
      assert.deepEqual(record.repeated_trials.rmse_deg[method], expected);
      const stats = trialStatistics(record, method);
      assert.equal(stats.min, Math.min(...expected));
      assert.equal(stats.max, Math.max(...expected));
      const expectedMean = expected.reduce((sum, value) => sum + value, 0) / expected.length;
      assert.ok(Math.abs(stats.mean - expectedMean) <= 1e-12 * Math.max(1, expectedMean));
      assert.ok(stats.selected >= stats.domain[0] && stats.selected <= stats.domain[1]);
    }
  }
});
