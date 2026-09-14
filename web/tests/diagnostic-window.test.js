import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import {
  clampToWindow, correctionInWindow, innovationsInWindow, inWindow,
  statesInWindow, validateWindow, windowPreset,
} from '../src/diagnostic-window.js';

const artifact = JSON.parse(readFileSync(new URL('../public/data/roll-comparison.json', import.meta.url)));

test('window bounds reject nonfinite, reversed, out-of-run and sub-millisecond ranges', () => {
  for (const bounds of [
    [-1, 5, 30], [0, 31, 30], [5, 4, 30], [5, 5, 30], [5, 5.0005, 30],
    [NaN, 5, 30], [0, Infinity, 30], [0, 5, NaN], [0, 5, Infinity], ['0', 5, 30],
  ]) assert.throws(() => validateWindow(...bounds), RangeError);
  assert.deepEqual(validateWindow(0, 30, 30), [0, 30]);
  assert.deepEqual(validateWindow(29.999, 30, 30), [29.999, 30]);
});

test('five-second presets retain their width when clipped to either run boundary', () => {
  assert.deepEqual(windowPreset('full', 8, 30), [0, 30]);
  assert.deepEqual(windowPreset('first', 8, 30), [0, 5]);
  assert.deepEqual(windowPreset('last', 8, 30), [25, 30]);
  assert.deepEqual(windowPreset('around', 8, 30), [5.5, 10.5]);
  assert.deepEqual(windowPreset('around', 1, 30), [0, 5]);
  assert.deepEqual(windowPreset('around', 29, 30), [25, 30]);
  for (const name of ['full', 'first', 'last', 'around']) {
    assert.deepEqual(windowPreset(name, 1.5, 3), [0, 3]);
  }
  assert.throws(() => windowPreset('unknown', 8, 30), RangeError);
});

test('window membership includes its boundaries and clipping preserves in-range times', () => {
  const bounds = [5, 10];
  assert.equal(inWindow(5, bounds), true);
  assert.equal(inWindow(10, bounds), true);
  assert.equal(inWindow(4.999, bounds), false);
  assert.equal(inWindow(10.001, bounds), false);
  assert.equal(clampToWindow(2, bounds), 5);
  assert.equal(clampToWindow(12, bounds), 10);
  assert.equal(clampToWindow(7.123456789, bounds), 7.123456789);
});

test('fractional boundary vertices retain the preceding state and its source timestamp', () => {
  const scenario = artifact.scenarios.nominal;
  const bounds = [5.003, 5.037];
  const points = statesInWindow(scenario, 'ekf', 'roll', bounds);
  for (const [point, boundary] of [[points[0], bounds[0]], [points.at(-1), bounds[1]]]) {
    const source = scenario.diagnostics.states.findLast(row => row[0] <= boundary);
    assert.equal(point.time, boundary);
    assert.equal(point.sourceTime, source[0]);
    assert.equal(point.error, source[5] - source[1]);
    assert.equal(point.sigma, source[10]);
    assert.ok(point.sourceTime < point.time);
  }
  const interior = scenario.diagnostics.states.filter(row => row[0] > bounds[0] && row[0] < bounds[1]);
  assert.deepEqual(points.slice(1, -1).map(point => point.time), interior.map(row => row[0]));
});

test('a short window with no source endpoint extends a single causal hold', () => {
  const scenario = artifact.scenarios.nominal;
  const bounds = [5.003, 5.007];
  assert.ok(!scenario.diagnostics.states.some(row => inWindow(row[0], bounds)));
  const points = statesInWindow(scenario, 'kalman', 'bias', bounds);
  const source = scenario.diagnostics.states.findLast(row => row[0] < bounds[0]);
  assert.equal(points.length, 2);
  assert.deepEqual(points.map(point => point.time), bounds);
  for (const point of points) {
    assert.equal(point.sourceTime, source[0]);
    assert.equal(point.error, source[7] - source[6]);
    assert.equal(point.sigma, source[11]);
  }
});

test('full windows preserve every original endpoint without duplicate boundary vertices', () => {
  for (const name of ['nominal', 'timing_jitter', 'accel_dropout']) {
    const scenario = artifact.scenarios[name];
    const points = statesInWindow(scenario, 'ekf', 'roll', [0, 30]);
    assert.equal(points.length, scenario.source_sample_count);
    points.forEach((point, i) => {
      const row = scenario.diagnostics.states[i];
      assert.deepEqual(point, { time: row[0], error: row[5] - row[1], sigma: row[10], sourceTime: row[0] });
    });
  }
});

test('final-window extents exclude the initial angle error and uncertainty transient', () => {
  const scenario = artifact.scenarios.initial_offset;
  const full = statesInWindow(scenario, 'ekf', 'roll', [0, 30]);
  const late = statesInWindow(scenario, 'ekf', 'roll', windowPreset('last', 0, 30));
  const extent = points => Math.max(...points.flatMap(point => [Math.abs(point.error), 2 * point.sigma]));
  assert.equal(full[0].error, 60);
  assert.equal(full[0].sigma, 30);
  assert.ok(extent(full) >= 60);
  assert.ok(extent(late) < 1);
  assert.ok(late.every(point => point.sourceTime >= 25));
  assert.equal(late[0].time, 25);
  assert.equal(late.at(-1).time, 30);
});

test('a window inside accelerometer loss has no innovations while uncertainty continues growing', () => {
  const scenario = artifact.scenarios.accel_dropout;
  const bounds = [12.05, 16.95];
  for (const method of ['kalman', 'ekf']) {
    assert.deepEqual(innovationsInWindow(scenario, method, bounds), []);
    const points = statesInWindow(scenario, method, 'roll', bounds);
    assert.ok(points.length > 400);
    assert.ok(points.at(-1).sigma > points[0].sigma);
    assert.equal(correctionInWindow(scenario, 14, -1, bounds), null);
    assert.equal(correctionInWindow(scenario, 14, 1, bounds), null);
  }
});

for (const name of ['nominal', 'timing_jitter', 'accel_delay']) {
  test(`${name} keeps actual corrections at both window boundaries and stops navigation outside`, () => {
    const scenario = artifact.scenarios[name];
    const arrivals = scenario.correction_times_s;
    const bounds = [arrivals[50], arrivals[52]];
    for (const method of ['kalman', 'ekf']) {
      const rows = innovationsInWindow(scenario, method, bounds);
      assert.deepEqual(rows.map(row => row[0]), arrivals.slice(50, 53));
    }
    assert.equal(correctionInWindow(scenario, bounds[0], -1, bounds), null);
    assert.equal(correctionInWindow(scenario, bounds[1], 1, bounds), null);
    assert.equal(correctionInWindow(scenario, bounds[0], 1, bounds), arrivals[51]);
    assert.equal(correctionInWindow(scenario, bounds[1], -1, bounds), arrivals[51]);
    assert.equal(correctionInWindow(scenario, arrivals[51], -1, bounds), bounds[0]);
    assert.equal(correctionInWindow(scenario, arrivals[51], 1, bounds), bounds[1]);
    if (name === 'timing_jitter') assert.notEqual(bounds[0], Math.round(bounds[0] * 1000) / 1000);
    if (name === 'accel_delay') {
      assert.ok(Math.abs(bounds[0] - scenario.timing.correction_sample_times_s[50] - .1) < 1e-12);
      assert.notEqual(innovationsInWindow(scenario, 'ekf', bounds)[0][0], scenario.timing.correction_sample_times_s[50]);
    }
  });
}

test('windowing does not invent covariance, bias estimates or innovations for baselines', () => {
  const scenario = artifact.scenarios.nominal;
  for (const method of ['gyro', 'complementary']) {
    const angle = statesInWindow(scenario, method, 'roll', [25, 30]);
    assert.ok(angle.length > 0);
    assert.ok(angle.every(point => point.sigma === null));
    assert.deepEqual(statesInWindow(scenario, method, 'bias', [25, 30]), []);
    assert.deepEqual(innovationsInWindow(scenario, method, [25, 30]), []);
  }
});

test('window selection leaves source records unchanged', () => {
  const scenario = structuredClone(artifact.scenarios.timing_jitter);
  const before = JSON.stringify(scenario);
  for (const bounds of [[0, 30], [5.003, 5.007], [12.05, 16.95], [25, 30]]) {
    for (const method of ['gyro', 'complementary', 'kalman', 'ekf']) {
      for (const component of ['roll', 'bias']) {
        const points = statesInWindow(scenario, method, component, bounds);
        if (points.length) points[0].error = 12345;
      }
      innovationsInWindow(scenario, method, bounds);
    }
  }
  assert.equal(JSON.stringify(scenario), before);
});
