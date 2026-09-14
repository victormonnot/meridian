import { adjacentCorrection } from './data.js';
import { diagnosticStateAt, stateDiagnostic } from './diagnostics.js';

const TIME_TOLERANCE = 1e-10;

export function validateWindow(start, end, duration) {
  if (![start, end, duration].every(Number.isFinite) || start < 0 || end > duration
    || end - start < .001 - TIME_TOLERANCE) {
    throw new RangeError(`Use 0 ≤ start < end ≤ ${duration} s, with at least 0.001 s between bounds.`);
  }
  return [start, end];
}

export function windowPreset(name, time, duration) {
  const width = Math.min(5, duration);
  const start = name === 'last' ? duration - width
    : name === 'around' ? Math.max(0, Math.min(duration - width, time - width / 2)) : 0;
  if (!['full', 'first', 'last', 'around'].includes(name)) throw new RangeError('Unknown time window preset');
  return name === 'full' ? [0, duration] : [start, start + width];
}

export function inWindow(time, [start, end]) {
  return time >= start - TIME_TOLERANCE && time <= end + TIME_TOLERANCE;
}

export function clampToWindow(time, [start, end]) {
  return Math.max(start, Math.min(end, time));
}

export function correctionInWindow(scenario, time, direction, bounds) {
  const target = adjacentCorrection(scenario, time, direction);
  return target !== null && inWindow(target, bounds) ? target : null;
}

// Boundary vertices extend existing holds; they are not new estimator outputs.
export function statesInWindow(scenario, method, component, [start, end]) {
  const first = stateDiagnostic(diagnosticStateAt(scenario, start), method, component);
  if (first === null) return [];
  const points = [{ ...first, sourceTime: first.time, time: start }];
  for (const row of scenario.diagnostics.states) {
    if (row[0] > first.time && inWindow(row[0], [start, end])) {
      const sample = stateDiagnostic(row, method, component);
      points.push({ ...sample, sourceTime: sample.time, time: Math.min(sample.time, end) });
    }
  }
  if (points.at(-1).time < end) points.push({ ...points.at(-1), time: end });
  return points;
}

export function innovationsInWindow(scenario, method, bounds) {
  return (scenario.diagnostics[method]?.rows ?? []).filter(row => inWindow(row[0], bounds));
}
