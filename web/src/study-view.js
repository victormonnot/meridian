import { validateStudies, studyStatistics } from './studies.js';
import { createStudyChart } from './study-chart.js';

const $ = selector => document.querySelector(selector);
const dataUrl = `${import.meta.env.BASE_URL}data/parameter-studies.json`;
const metrics = {
  angle_rmse_deg: { label: 'Roll RMSE · full run', unit: '°', note: '0–30 s · all 3,001 endpoints, including initialization. Each endpoint has equal weight.', error: true },
  late_angle_rmse_deg: { label: 'Roll RMSE · late', unit: '°', note: '25–30 s inclusive · 501 endpoints from the continuing run; no filter restart.', error: true },
  bias_rmse_deg_s: { label: 'Bias RMSE · full run', unit: '°/s', note: '0–30 s · all 3,001 endpoints, including initialization. Each endpoint has equal weight.', error: true },
  ramp_angle_rmse_deg: { label: 'Roll RMSE · 10–20 s', unit: '°', note: '10–20 s inclusive · 1,001 endpoints. This is the ramp interval in the changing-bias case and the same window in the constant-bias case.', error: true },
  ramp_bias_rmse_deg_s: { label: 'Bias RMSE · 10–20 s', unit: '°/s', note: '10–20 s inclusive · 1,001 endpoints. This is the ramp interval in the changing-bias case and the same window in the constant-bias case.', error: true },
  late_bias_rmse_deg_s: { label: 'Bias RMSE · late', unit: '°/s', note: '25–30 s inclusive · 501 endpoints from the continuing run; no filter restart.', error: true },
  late_bias_temporal_std_deg_s: { label: 'Bias fluctuation · late', unit: '°/s', note: '25–30 s inclusive · temporal standard deviation of bias error about its own mean (ddof = 0). It includes settling and correlated estimates; it is not sensor noise or an accuracy score.' },
  mean_nis: { label: 'Mean NIS · all corrections', unit: 'dimensionless', note: 'All 300 corrections · joint prior innovation with the full covariance S. The ideal-model mean is 2 for this two-component measurement. A smaller value is not automatically better; this descriptive mean is not a consistency test.' },
};
const commonMetrics = ['angle_rmse_deg', 'late_angle_rmse_deg', 'bias_rmse_deg_s', 'late_bias_rmse_deg_s'];
const definitions = {
  r: {
    question: 'How much should the filter trust the accelerometer?',
    description: 'Vary the assumed component noise while keeping the recorded measurements fixed. Compare matched noise with a case where the sensor noise is larger.',
    cases: { nominal: 'Nominal · noise 0.2 m/s²', accel_noise_mismatch: 'Higher noise · 0.6 m/s²' },
    metrics: [...commonMetrics, 'mean_nis'],
    parameter: 'Assumed σa (m/s²)',
    setting: value => `${value} m/s²`,
    defaults: { phase: 'evaluation', caseId: 'nominal', metric: 'angle_rmse_deg', setting: '0.6', seed: 1000 },
  },
  bias: {
    question: 'How quickly can the filter follow a changing bias?',
    description: 'Add uncertainty to the predicted bias so measurements can correct it more readily. Compare the tracking benefit with fluctuations when the true bias stays constant.',
    cases: { nominal: 'Constant bias · 0.5°/s', bias_ramp: 'Bias ramp · 0.5 → 1.5°/s' },
    metrics: [...commonMetrics, 'ramp_angle_rmse_deg', 'ramp_bias_rmse_deg_s', 'late_bias_temporal_std_deg_s', 'mean_nis'],
    parameter: 'Bias σb ((°/s)/√s)',
    setting: value => `${value} (°/s)/√s`,
    defaults: { phase: 'evaluation', caseId: 'bias_ramp', metric: 'ramp_bias_rmse_deg_s', setting: '0.1', seed: 2000 },
  },
};

function number(value, signed = false) {
  const absolute = Math.abs(value);
  const formatted = absolute > 0 && absolute < 0.00005 ? absolute.toExponential(2) : absolute.toFixed(4);
  return `${value < 0 ? '−' : signed && value > 0 ? '+' : ''}${formatted}`;
}

function options(element, entries, selected) {
  element.replaceChildren(...entries.map(([value, label]) => {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = label;
    return option;
  }));
  element.value = selected;
}

function readings(element, entries) {
  element.replaceChildren(...entries.map(([label, value]) => {
    const row = document.createElement('div');
    const dt = document.createElement('dt');
    const dd = document.createElement('dd');
    dt.textContent = label;
    dd.textContent = value;
    row.append(dt, dd);
    return row;
  }));
}

let loading;
// The study request fails independently of the original replay data request.
export function loadStudies() {
  loading ??= (async () => {
    try {
      const response = await fetch(dataUrl);
      if (!response.ok) throw new Error(`Study request returned HTTP ${response.status}`);
      const data = validateStudies(await response.json());
      $('#studies-status').hidden = true;
      $('#study-content').hidden = false;
      startStudies(data);
    } catch (error) {
      $('#study-content').hidden = true;
      const status = $('#studies-status');
      status.hidden = false;
      status.setAttribute('role', 'alert');
      status.textContent = 'The recorded studies could not be loaded. Reload the page to try again. Experiment replay is available separately.';
      console.error('Meridian study loading failed:', error);
    }
  })();
  return loading;
}

function startStudies(data) {
  let studyId = 'r';
  const states = Object.fromEntries(Object.entries(definitions).map(([key, definition]) => [key, { ...definition.defaults }]));
  let currentStatistics;
  let chartModel;
  const chart = createStudyChart($('#study-chart'), $('#study-chart-readout'));
  $('#study-download').href = dataUrl;

  function selectSeed(seed) {
    states[studyId].seed = seed;
    $('#study-seed').value = String(seed);
    const state = states[studyId];
    const definition = definitions[studyId];
    const study = data.studies[studyId];
    const trial = currentStatistics.values.find(value => value.seed === seed);
    const unit = metrics[state.metric].unit;
    readings($('#study-seed-values'), [
      [`Selected · ${definition.setting(state.setting)}`, `${number(trial.value)} ${unit}`],
      [`Reference · ${definition.setting(study.reference_setting)}`, `${number(trial.reference)} ${unit}`],
      ['Difference', `${number(trial.delta, true)} ${unit}`],
    ]);
    const inputHash = study.phases[state.phase].cases[state.caseId].trials.find(item => item.seed === seed).measurement_sha256;
    const hashLabel = studyId === 'r' ? 'Measurement SHA-256' : 'Serialized input SHA-256';
    $('#study-seed-source').textContent = `${hashLabel}: ${inputHash}`;
  }

  function render() {
    const definition = definitions[studyId];
    const state = states[studyId];
    const study = data.studies[studyId];
    const phase = study.phases[state.phase];
    const metric = metrics[state.metric];
    const reference = study.reference_setting;
    const isR = studyId === 'r';
    if (!phase.seeds.includes(state.seed)) state.seed = phase.seeds[0];
    document.querySelectorAll('[data-study]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.study === studyId)));
    options($('#study-case'), Object.entries(definition.cases), state.caseId);
    options($('#study-metric'), definition.metrics.map(key => [key, metrics[key].label]), state.metric);
    options($('#study-setting'), study.settings.map(key => [key, `${definition.setting(key)}${key === reference ? ' · reference' : ''}`]), state.setting);
    options($('#study-seed'), phase.seeds.map(seed => [String(seed), `Seed ${seed}`]), String(state.seed));
    $('#study-phase').value = state.phase;
    $('#study-context').textContent = `${state.phase === 'evaluation' ? 'Final evaluation' : 'Exploration'} · seeds ${phase.seeds[0]}–${phase.seeds.at(-1)} · ${phase.seeds.length} paired trials per setting · 30 s each. Trial sets are summarized separately.`;
    $('#study-question').textContent = definition.question;
    $('#study-description').textContent = definition.description;
    readings($('#study-assumptions'), isR ? [
      ['Changed', 'Assumed σa = 0.1 / 0.2 / 0.6 m/s²; R = σa² I₂.'],
      ['Reference', 'σa = 0.2 m/s² in both cases.'],
      ['Fixed', 'Constant bias model, initial uncertainty and gyro noise. True bias = 0.5°/s.'],
    ] : [
      ['Changed', 'Bias diffusion σb = 0 / 0.03 / 0.1 (°/s)/√s.'],
      ['Reference', 'σb = 0: no bias diffusion in prediction.'],
      ['Fixed', 'Initial uncertainty, gyro noise and R = 0.2² I₂ (m/s²)². In the ramp case, bias changes over 10–20 s.'],
    ]);
    $('#study-setting-column').textContent = definition.parameter;
    $('#study-table-caption').textContent = `${metric.label} (${metric.unit}) · differences from ${definition.setting(reference)}`;
    $('#study-metric-note').textContent = `${metric.note}${metric.error ? ' Lower RMSE means less error against simulation truth in this window.' : ''}`;
    const statistics = study.settings.map(setting => ({ setting, stats: studyStatistics(phase, state.caseId, setting, state.metric, reference) }));
    $('#study-settings').replaceChildren(...statistics.map(({ setting, stats }) => {
      const row = document.createElement('tr');
      row.dataset.focused = String(state.setting === setting);
      const label = document.createElement('td');
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = `${setting}${setting === reference ? ' · ref.' : ''}`;
      button.setAttribute('aria-label', `Inspect ${definition.setting(setting)}${setting === reference ? ', reference setting' : ''}`);
      button.setAttribute('aria-pressed', String(state.setting === setting));
      button.dataset.studySetting = setting;
      button.addEventListener('click', () => {
        state.setting = setting;
        render();
        // Replacing the summary rows must not discard keyboard focus.
        [...$('#study-settings').querySelectorAll('button')].find(item => item.dataset.studySetting === setting).focus({ preventScroll: true });
      });
      label.append(button);
      row.append(label);
      for (const [index, text] of [number(stats.mean), `${number(stats.min)}–${number(stats.max)}`, number(stats.delta.mean, true)].entries()) {
        const cell = document.createElement('td');
        if (index === 1) {
          for (const bound of [number(stats.min), `–${number(stats.max)}`]) {
            const span = document.createElement('span');
            span.className = 'study-range-bound';
            span.textContent = bound;
            cell.append(span);
          }
        } else cell.textContent = text;
        row.append(cell);
      }
      return row;
    }));
    const baselineMetrics = phase.cases[state.caseId].trials;
    const baselines = isR && Object.hasOwn(baselineMetrics[0].baselines.gyro, state.metric);
    $('#study-baselines').hidden = !baselines;
    $('#study-baselines').textContent = baselines ? `Baselines on the same inputs · mean ${metric.label.toLowerCase()}: ${[['gyro', 'Gyro integration'], ['complementary', 'Complementary']].map(([key, label]) => `${label} ${(baselineMetrics.reduce((sum, trial) => sum + trial.baselines[key][state.metric], 0) / phase.seeds.length).toFixed(4)} ${metric.unit}`).join('; ')}. These do not change with R.` : '';
    currentStatistics = statistics.find(item => item.setting === state.setting).stats;
    const delta = currentStatistics.delta;
    $('#study-delta-summary').textContent = `${definition.setting(state.setting)} minus reference ${definition.setting(reference)} · ${delta.negative} lower / ${delta.zero} equal / ${delta.positive} higher scores. Mean difference ${number(delta.mean, true)} ${metric.unit}; observed range ${number(delta.min, true)} to ${number(delta.max, true)}. Counts use unrounded differences.`;
    selectSeed(state.seed);
    chartModel = { label: metric.label, unit: metric.unit, settingLabel: definition.setting(state.setting), referenceLabel: definition.setting(reference), values: currentStatistics.values, selectedSeed: state.seed, onSelect: selectSeed };
    chart.update(chartModel);
    $('#study-phase-note').textContent = isR
      ? 'R study: exploration seeds 0–19; final evaluation seeds 1000–1019. Every case/seed supplies the same measurement arrays to all three settings. Across cases, standardized noise draws are paired, but the accelerometer noise amplitude changes.'
      : 'Bias study: exploration seeds 0–19; final evaluation seeds 2000–2019. Every case/seed supplies the same serialized events to all three settings and both languages. Across cases, accelerometer measurements are identical and gyro noise draws are paired, but the true bias and gyro measurements change.';
    $('#study-protocol-note').textContent = isR
      ? 'R is the covariance of the two unnormalized body-y/z force measurements. Its diagonal entries are 0.01, 0.04 or 0.36 (m/s²)². Raising R changes measurement trust; it does not remove the actual noise or correct a translation disturbance. The R report records the source revision used before the optional bias model was added.'
      : 'The predicted mean bias stays constant. The model adds Qb = σb² [[dt³/3, −dt²/2], [−dt²/2, dt]] after converting σb to (rad/s)/√s. This is uncertainty growth, not a fitted ramp slope. The experiment records Python/C++ agreement after every operation for all settings and trials; it does not measure real-time performance or calibrate a hardware noise density.';
    $('#study-provenance').textContent = `Recorded source: ${phase.provenance.summary_file}. Summary SHA-256: ${phase.provenance.summary_sha256}. Protocol SHA-256: ${phase.provenance.protocol_sha256}. The download retains the recorded protocol, source fingerprints and environment; fingerprints identify the artifacts used, not the current working tree.`;
  }

  document.querySelectorAll('[data-study]').forEach(button => button.addEventListener('click', () => {
    studyId = button.dataset.study;
    render();
  }));
  for (const [id, field] of [['case', 'caseId'], ['phase', 'phase'], ['metric', 'metric'], ['setting', 'setting']]) {
    $(`#study-${id}`).addEventListener('change', event => { states[studyId][field] = event.target.value; render(); });
  }
  $('#study-seed').addEventListener('change', event => {
    selectSeed(Number(event.target.value));
    chart.update({ ...chartModel, selectedSeed: states[studyId].seed });
  });
  render();
}
