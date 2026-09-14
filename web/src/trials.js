const METHODS = ['gyro', 'complementary', 'kalman', 'ekf'];

function check(condition, message) {
  if (!condition) throw new Error(`Invalid comparison data: repeated trials ${message}`);
}

function exactKeys(value, keys) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    && JSON.stringify(Object.keys(value).sort()) === JSON.stringify([...keys].sort());
}

function validateValues(scenario) {
  const trials = scenario?.repeated_trials;
  check(exactKeys(trials, ['seeds', 'rmse_deg']), 'fields');
  check(Array.isArray(trials.seeds) && trials.seeds.length > 0
    && [...trials.seeds].every(seed => Number.isSafeInteger(seed) && seed >= 0)
    && new Set(trials.seeds).size === trials.seeds.length, 'seeds');
  check(exactKeys(trials.rmse_deg, METHODS), 'methods');
  for (const method of METHODS) {
    const values = trials.rmse_deg[method];
    check(Array.isArray(values) && values.length === trials.seeds.length
      && [...values].every(value => Number.isFinite(value) && value >= 0), `${method} values`);
    const selected = scenario.metrics?.rmse_deg?.[method];
    check(Number.isFinite(selected) && selected >= 0, `${method} selected metric`);
  }
  return trials;
}

export function validateRepeatedTrials(scenario, selectedSeed) {
  const trials = validateValues(scenario);
  check(Number.isSafeInteger(selectedSeed) && selectedSeed >= 0, 'selected seed');
  const index = trials.seeds.indexOf(selectedSeed);
  if (index >= 0) {
    for (const method of METHODS) {
      const selected = scenario.metrics.rmse_deg[method];
      check(Math.abs(trials.rmse_deg[method][index] - selected) <= 1e-10 + 1e-10 * Math.abs(selected),
        `${method} selected seed does not match its full-run metric`);
    }
  }
  return trials;
}

// These are full-run endpoint RMSE values, independent of playback or a window.
export function trialStatistics(scenario, method) {
  check(METHODS.includes(method), 'unknown method');
  const trials = validateValues(scenario);
  const seeds = trials.seeds.slice();
  const values = trials.rmse_deg[method].slice();
  let min = Infinity;
  let max = 0;
  let mean = 0;
  values.forEach((value, index) => {
    min = Math.min(min, value);
    max = Math.max(max, value);
    // Avoid overflowing a sum even if all finite inputs approach MAX_VALUE.
    mean += (value - mean) / (index + 1);
  });
  const selected = scenario.metrics.rmse_deg[method];
  const low = Math.min(min, selected);
  const high = Math.max(max, selected);
  const padding = Math.max(Number.MIN_VALUE, high > low ? .1 * (high - low) : .1 * (high || 1));
  const domain = [Math.max(0, low - padding), Math.min(Number.MAX_VALUE, high + padding)];
  return { seeds, values, min, mean, max, selected, domain };
}
