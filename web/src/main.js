import './style.css';
import { loadStudies } from './study-view.js';
import { createChart } from './chart.js';
import { createDiagnosticChart } from './diagnostic-chart.js';
import { createTrialChart } from './trial-chart.js';
import { trialStatistics } from './trials.js';
import { diagnosticStateAt, lastInnovation, stateDiagnostic } from './diagnostics.js';
import { clampToWindow, correctionInWindow, innovationsInWindow, inWindow, validateWindow, windowPreset } from './diagnostic-window.js';
import { SERIES, advanceTime, correctionDetails, formatValue, sampleAt, validateComparison } from './data.js';

const $ = selector => document.querySelector(selector);
const dataUrl = `${import.meta.env.BASE_URL}data/roll-comparison.json`;
let replayController;

function selectPage() {
  const studies = window.location.hash === '#studies';
  replayController?.pause();
  $('#replay-page').hidden = studies;
  $('#studies-page').hidden = !studies;
  document.querySelectorAll('.page-nav a').forEach(link => {
    if (link.hash === (studies ? '#studies' : '#replay')) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
  if (studies) loadStudies();
}

async function load() {
  try {
    const response = await fetch(dataUrl);
    if (!response.ok) throw new Error(`Data request returned HTTP ${response.status}`);
    const data = validateComparison(await response.json());
    $('#load-status').hidden = true;
    $('#explorer').hidden = false;
    replayController = startExplorer(data);
  } catch (error) {
    $('#explorer').hidden = true;
    const status = $('#load-status');
    status.hidden = false;
    status.setAttribute('role', 'alert');
    status.textContent = 'The recorded simulations could not be loaded. Reload the page to try again.';
    console.error('Meridian data loading failed:', error);
  }
}

function startExplorer(data) {
  const config = data.config;
  const duration = config.duration_s;
  const visible = new Set(SERIES.map(item => item.id));
  let scenarioId = 'nominal';
  let methodId = 'ekf';
  let time = Math.min(8, duration);
  let playing = false;
  let lastFrame = null;
  let frame = null;
  let speed = 1;
  let view = 'trajectories';
  let diagnosticWindow = [0, duration];
  const activeBounds = () => view === 'diagnostics' ? diagnosticWindow : [0, duration];
  const scenario = () => data.scenarios[scenarioId];
  const method = () => SERIES.find(item => item.id === methodId);
  const tooltip = $('#tooltip');
  const legendButtons = [];
  const charts = ['roll', 'bias'].map(kind => createChart(
    $(`#${kind}-chart`), kind, duration, (next, pause) => {
      if (pause) stop();
      if (playing) return null;
      return setTime(next);
    }, tooltip,
  ));
  const diagnosticCharts = ['state', 'innovation'].map((kind, index) => createDiagnosticChart(
    $(index === 0 ? '#error-chart' : '#innovation-chart'), kind, duration, (next, pause) => {
      if (pause) stop();
      if (!playing) setTime(next);
    },
  ));
  const trialChart = createTrialChart($('#trial-chart'), $('#trial-readout'));

  $('#download').href = dataUrl;
  $('#record-info').textContent = `Simulation · ${formatValue(duration, 0)} s · seed ${data.seed}`;
  $('#duration').textContent = `${formatValue(duration, 0)} s`;
  $('#time').max = duration;

  function swatch(item) {
    const element = document.createElement('i');
    element.className = 'swatch';
    element.style.borderColor = item.color;
    element.style.borderTopStyle = item.dash ? (item.id === 'gyro' ? 'dotted' : 'dashed') : 'solid';
    element.setAttribute('aria-hidden', 'true');
    return element;
  }

  for (const kind of ['roll', 'bias']) {
    for (const item of SERIES.filter(item => item[kind] !== null)) {
      const element = document.createElement(item.id === 'truth' ? 'span' : 'button');
      element.append(swatch(item), document.createTextNode(item.label));
      if (item.id !== 'truth') {
        element.type = 'button';
        element.dataset.series = item.id;
        element.setAttribute('aria-pressed', 'true');
        element.title = `Show or hide ${item.label}`;
        element.addEventListener('click', () => {
          if (visible.has(item.id)) visible.delete(item.id);
          else visible.add(item.id);
          redraw();
        });
        legendButtons.push(element);
      }
      $(`#${kind}-legend`).append(element);
    }
  }

  function setTime(next) {
    time = clampToWindow(next, activeBounds());
    const snapshot = diagnosticStateAt(scenario(), time);
    const row = view === 'diagnostics' ? snapshot : sampleAt(scenario().rows, time);
    $('#state-timestamp').textContent = `Recorded state at ${formatValue(snapshot[0], 3)} s. Held until the next endpoint.`;
    $('#time').value = time;
    $('#time').setAttribute('aria-valuetext', `${formatValue(time, 3)} seconds`);
    $('#time-value').textContent = `${formatValue(time, 3)} s`;
    $('#chart-time').textContent = `${formatValue(time, 3)} s`;
    $('#truth-angle').textContent = `${formatValue(row[1])}°`;
    $('#estimate-angle').textContent = `${formatValue(row[method().roll])}°`;
    $('#angle-error').textContent = `${formatValue(row[method().roll] - row[1])}°`;
    for (const item of SERIES.filter(item => item.bias !== null)) {
      $(`#bias-${item.id}`).textContent = `${formatValue(row[item.bias], 3)}°/s`;
    }
    $('#method-name').textContent = method().label;
    const correction = correctionDetails(scenario(), time);
    const event = scenario().event;
    const unavailable = event?.kind === 'accel_dropout' && time >= event.start_s && time < event.end_s;
    $('#correction-status').textContent = correction === null
      ? `No accelerometer correction yet. First correction at ${formatValue(scenario().correction_times_s[0], 3)} s.`
      : `${unavailable ? 'Accelerometer unavailable · gyro predictions continue. ' : ''}Latest accelerometer correction: ${formatValue(correction.arrival, 3)} s.`;
    $('#timing-readout').textContent = correction === null
      ? 'Advance to a correction to inspect acquisition and arrival times.'
      : `Acquired at ${formatValue(correction.sample, 3)} s → applied at ${formatValue(correction.arrival, 3)} s · age at arrival ${formatValue((correction.arrival - correction.sample) * 1000, 1)} ms.`
        + (correction.previousArrival === null ? ' First correction.'
          : ` Previous correction ${formatValue((correction.arrival - correction.previousArrival) * 1000, 3)} ms earlier.`);
    $('#previous-correction').disabled = correctionInWindow(scenario(), time, -1, activeBounds()) === null;
    $('#next-correction').disabled = correctionInWindow(scenario(), time, 1, activeBounds()) === null;
    // Forward-right-down, looking forward from behind: positive roll lowers right.
    $('#truth-pose').setAttribute('transform', `rotate(${row[1]})`);
    $('#estimate-pose').setAttribute('transform', `rotate(${row[method().roll]})`);
    charts.forEach(chart => chart.setCursor(row));
    diagnosticCharts.forEach(chart => chart.setCursor(time));
    renderDiagnosticReadings(snapshot);
    $('#play').textContent = playing ? 'Pause' : time >= activeBounds()[1] ? 'Replay' : 'Play';
    return row;
  }

  function redraw() {
    tooltip.hidden = true;
    for (const button of legendButtons) button.setAttribute('aria-pressed', String(visible.has(button.dataset.series)));
    charts.forEach(chart => chart.setData(scenario(), visible));
    renderDiagnostics();
    setTime(time);
  }

  function renderDiagnostics() {
    const active = view === 'diagnostics';
    $('#trajectory-panels').hidden = active;
    $('#diagnostic-panels').hidden = !active;
    $('#state-timestamp').hidden = !active;
    $('#time').min = activeBounds()[0];
    $('#time').max = activeBounds()[1];
    $('#duration').textContent = `${formatValue(activeBounds()[1], active ? 3 : 0)} s`;
    $('#window-start').max = duration;
    $('#window-end').max = duration;
    $('#window-start').value = diagnosticWindow[0];
    $('#window-end').value = diagnosticWindow[1];
    const correctionCount = innovationsInWindow(scenario(), 'kalman', diagnosticWindow).length;
    $('#window-summary').textContent = `Viewing ${formatValue(diagnosticWindow[0], 3)}–${formatValue(diagnosticWindow[1], 3)} s · ${correctionCount} corrections. Playback and axes follow this window; RMSE and mean NIS remain full-run values.`;
    $('#correction-navigation').hidden = !active && scenarioId !== 'timing_jitter' && scenarioId !== 'accel_delay';
    document.querySelectorAll('[data-view]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.view === view)));
    const component = $('#diagnostic-component').value;
    const mode = $('#innovation-mode').value;
    const current = stateDiagnostic(scenario().diagnostics.states[0], methodId, component);
    const record = scenario().diagnostics[methodId];
    $('#diagnostic-method').textContent = `${method().label} · follows the inspected estimate`;
    $('#error-chart').hidden = current === null;
    $('#uncertainty-note').textContent = current === null ? 'This method does not estimate gyro bias.'
      : current.sigma === null ? 'Solid line: estimate − truth. This baseline does not report model covariance.'
        : 'Solid line: estimate − truth. Shaded band and dotted bounds: ±2 model standard deviations about zero.';
    $('#innovation-chart').hidden = !record;
    $('#innovation-mode').disabled = !record;
    $('#innovation-note').textContent = !record ? 'Innovation covariance and NIS are available for the angle KF and vector EKF.'
      : (mode === 'nis' ? `Dots: joint NIS. Dashed line: ideal-model mean ${record.dimension}, not a test threshold. `
        : methodId === 'kalman' ? 'Dots: measured tilt − predicted roll, before correction (°). '
          : 'Dots: body-y innovation; crosses: body-z innovation, before correction (m/s²). ')
        + `Full-run mean NIS ${formatValue(record.mean_nis, 3)} · ${record.dimension} measurement ${record.dimension === 1 ? 'dimension' : 'dimensions'}. Includes startup; KF and EKF NIS have different dimensions. No statistical consistency is established by this view.`
        + (correctionCount === 0 ? ' No corrections in this window.' : '');
    diagnosticCharts.forEach(chart => chart.setData(scenario(), method(), component, mode, diagnosticWindow));
  }

  function renderDiagnosticReadings(snapshot) {
    const component = $('#diagnostic-component').value;
    const value = stateDiagnostic(snapshot, methodId, component);
    const unit = component === 'roll' ? '°' : '°/s';
    $('#error-readout').textContent = value === null ? ''
      : `State at ${formatValue(value.time, 3)} s · error ${formatValue(value.error, 3)}${unit}`
        + (value.sigma === null ? '' : ` · model σ ${formatValue(value.sigma, 6)}${unit}`);
    const innovation = lastInnovation(scenario(), methodId, time);
    const correction = correctionDetails(scenario(), time);
    $('#innovation-readout').textContent = !scenario().diagnostics[methodId] ? ''
      : innovation === null ? 'No innovation yet: the first measurement has not arrived.'
        : `Latest innovation${inWindow(innovation[0], diagnosticWindow) ? '' : ' (before this window; not plotted)'} at arrival ${formatValue(innovation[0], 3)} s, acquired at ${formatValue(correction.sample, 3)} s (${formatValue((time - innovation[0]) * 1000, 1)} ms since arrival): `
          + (methodId === 'kalman' ? `${formatValue(innovation[1], 3)}°`
            : `y ${formatValue(innovation[1], 3)}, z ${formatValue(innovation[2], 3)} m/s²`)
          + ` · NIS ${formatValue(innovation.at(-1), 3)}. No new value between corrections.`;
    $('#innovation-scale').textContent = !innovation ? '' : methodId === 'kalman'
      ? `Prior innovation standard deviation √S: ${formatValue(Math.sqrt(innovation[2]), 3)}°.`
      : `Prior innovation standard deviations: y ${formatValue(Math.sqrt(innovation[3]), 3)}, z ${formatValue(Math.sqrt(innovation[5]), 3)} m/s². Joint NIS uses the full S, including its cross term.`;
  }

  function renderMetrics() {
    const basis = $('#rmse-weight').value;
    const weighted = basis === 'time_weighted_rmse_deg';
    $('#metric-caption').textContent = `Full-run angle RMSE in degrees, weighted by ${weighted ? 'elapsed time' : 'sample count'}`;
    $('#weighting-note').textContent = weighted
      ? 'Trapezoidal integration of squared endpoint errors over 30 s; an approximation across correction jumps.'
      : 'Each original endpoint has equal weight, even when intervals vary.';
    $('#metrics').replaceChildren();
    for (const item of SERIES.slice(1)) {
      const row = document.createElement('tr');
      row.dataset.focused = String(item.id === methodId);
      const name = document.createElement('td');
      name.textContent = item.label;
      const value = document.createElement('td');
      value.textContent = formatValue(scenario().metrics[basis][item.id], 3);
      row.append(name, value);
      $('#metrics').append(row);
    }
  }

  function renderTrials() {
    const focusedMethod = document.activeElement?.dataset.trialMethod;
    const statistics = trialStatistics(scenario(), methodId);
    const seeds = statistics.seeds;
    const consecutive = seeds.every((seed, index) => seed === seeds[0] + index);
    const seedLabel = seeds.length === 1 ? `seed ${seeds[0]}`
      : consecutive ? `seeds ${seeds[0]}–${seeds.at(-1)}` : 'seeds listed on the plot';
    const scenarioLabel = $(`[data-scenario="${scenarioId}"]`).textContent;
    $('#trial-context').textContent = `${scenarioLabel} · ${seeds.length} recorded ${seeds.length === 1 ? 'trial' : 'trials'} · ${seedLabel}. `
      + (seeds.includes(data.seed) ? `Displayed seed ${data.seed} is also included in these trials.`
        : `Displayed seed ${data.seed} is a separate reference, excluded from their mean and range.`);
    $('#trial-reference-label').textContent = `Displayed seed ${data.seed}`;
    $('#trial-reference-key').style.borderColor = method().color;
    $('#trial-dot-key').style.backgroundColor = method().color;
    $('#trial-method').value = methodId;
    $('#trial-displayed-heading').textContent = `Displayed ${data.seed}`;
    $('#trial-values').replaceChildren();
    for (const item of SERIES.slice(1)) {
      const result = trialStatistics(scenario(), item.id);
      const row = document.createElement('tr');
      row.dataset.focused = String(item.id === methodId);
      const name = document.createElement('td');
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = item.label;
      button.dataset.trialMethod = item.id;
      button.setAttribute('aria-pressed', String(item.id === methodId));
      button.addEventListener('click', () => selectMethod(item.id));
      name.append(button);
      const mean = document.createElement('td');
      mean.textContent = formatValue(result.mean, 3);
      const range = document.createElement('td');
      range.className = 'trial-range';
      const lo = document.createElement('span'), hi = document.createElement('span');
      lo.textContent = formatValue(result.min, 3);
      hi.textContent = formatValue(result.max, 3);
      range.append(lo, document.createTextNode(' – '), hi);
      const selected = document.createElement('td');
      selected.textContent = formatValue(result.selected, 3);
      row.append(name, mean, range, selected);
      $('#trial-values').append(row);
    }
    const position = statistics.selected < statistics.min ? 'below'
      : statistics.selected > statistics.max ? 'above' : 'within';
    $('#trial-position').textContent = `${method().label}: the displayed run is ${position} the observed range of these ${seeds.length} trials. The mean and range use full-precision scores; labels are rounded.`;
    trialChart.setData(scenario(), method(), data.seed);
    if (focusedMethod) $(`[data-trial-method="${focusedMethod}"]`).focus({ preventScroll: true });
  }

  function selectMethod(id) {
    methodId = id;
    $('#method').value = id;
    visible.add(id);
    renderMetrics();
    renderTrials();
    redraw();
  }

  function renderScenario() {
    const event = scenario().event;
    const pulse = event?.kind === 'translation' ? event : null;
    const dropout = event?.kind === 'accel_dropout' ? event : null;
    const ramp = event?.kind === 'bias_ramp' ? event : null;
    const initial = scenario().initialization;
    const wrongStart = initial.roll_deg !== 0;
    const overconfident = scenarioId === 'initial_overconfident';
    const mismatch = scenarioId === 'accel_noise_mismatch';
    const noise = scenario().accelerometer_noise;
    const timing = scenario().timing;
    const irregular = timing.kind === 'irregular';
    const delayed = timing.accel_delay_s > 0;
    $('#timing-inspector').hidden = !irregular && !delayed;
    $('#scenario-description').textContent = wrongStart
      ? `All estimates start at ${initial.roll_deg}°, while true roll is 0°. The Kalman filters declare an initial angle standard deviation of ${initial.angle_std_deg}°.`
      : irregular ? 'Gyro intervals vary over the same 30 s motion. Accelerometer observations arrive at every tenth endpoint. Predictions and corrections use the recorded times.'
        : delayed ? 'Each accelerometer observation describes the roll 100 ms before arrival. The filters apply it at arrival without compensating for the delay.'
        : mismatch ? `Accelerometer noise standard deviation is ${noise.actual_std_m_s2} m/s² per component; the Kalman filters assume ${noise.assumed_std_m_s2} m/s². The simulated noise has three times the standard deviation and nine times the assumed variance.`
        : ramp ? `True gyro bias rises from ${ramp.initial_bias_deg_s} to ${ramp.final_bias_deg_s}°/s between ${ramp.start_s} and ${ramp.end_s} s, then stays at ${ramp.final_bias_deg_s}°/s. All accelerometer corrections remain available.`
        : dropout ? `${dropout.omitted_count} accelerometer observations are omitted from 12 to 17 s. Gyro predictions continue; corrections resume at 17 s.`
        : pulse ? `Added body-y acceleration: +${pulse.value_m_s2} m/s² from ${pulse.start_s} to ${pulse.end_s} s. The gravity model is disturbed.`
          : 'Smooth roll with constant gyro bias. Accelerometer measurements follow the gravity model.';
    $('#jump').hidden = !wrongStart && !event;
    $('#jump').textContent = wrongStart ? 'Inspect start'
      : ramp ? 'Inspect ramp' : dropout ? 'Inspect loss' : 'Inspect disturbance';
    $('#recovery').hidden = !dropout && !ramp && !wrongStart;
    $('#recovery').textContent = wrongStart ? 'Inspect end' : ramp ? 'Inspect plateau' : 'Inspect recovery';
    $('#metric-note').textContent = overconfident
      ? 'Zero initial angle variance does not freeze the angle: prediction adds uncertainty. Corrections can attribute the initial error to gyro bias. Inspect the end to see the remaining error.'
      : wrongStart ? 'Includes the initial 60° error and the recovery transient. Compare with Overconfident start: only the declared initial angle uncertainty changes.'
        : irregular ? 'Sample weighting and elapsed-time weighting differ on irregular intervals. Both metrics use the original timestamps; no resampling is applied.'
        : delayed ? 'The acquisition times explain the mismatch; they were never provided to the filters. This run does not implement delay compensation.'
        : mismatch ? 'The noise increase applies throughout the run. Filter tuning stays fixed; this is one paired noise realization, not a robustness ranking.'
        : ramp ? 'The filters keep their constant-bias model. This run evaluates a model mismatch, not retuned filters.'
        : dropout ? 'A lower error on this seed does not mean losing observations improves estimation.'
        : 'Computed from every original endpoint, including initialization.';
    document.querySelectorAll('[data-scenario]').forEach(button => {
      button.setAttribute('aria-pressed', String(button.dataset.scenario === scenarioId));
    });
    const settings = [
      ['Roll motion', `${config.amplitude_deg}° amplitude · ${config.frequency_hz} Hz · ${duration} s`],
      ['Measurement schedule', irregular
        ? '3,000 gyro intervals over 30 s; 300 accelerometer arrivals at every tenth endpoint (100 / 10 Hz on average)'
        : `Gyro ${config.sample_rate_hz} Hz · accelerometer ${config.sample_rate_hz / config.observation_every} Hz scheduled; ${scenario().correction_times_s.length}/${scenario().scheduled_accel_count} corrections received`],
      ['Gyro interval duration', `${formatValue(timing.gyro_interval_min_s * 1000, 3)}–${formatValue(timing.gyro_interval_max_s * 1000, 3)} ms; actual durations used by all methods`],
      ['Acquisition to arrival', `${formatValue(timing.accel_delay_s * 1000, 0)} ms. ${delayed ? 'Ignored by the filters; sample times are simulation provenance only.' : 'Acquisition and arrival coincide in this simulation.'}`],
      ['True gyro bias', ramp
        ? `${ramp.initial_bias_deg_s} → ${ramp.final_bias_deg_s}°/s over ${ramp.start_s}–${ramp.end_s} s (${ramp.slope_deg_s2}°/s²), then constant`
        : `${config.bias_deg_s}°/s, constant`],
      ['Kalman bias model', 'Constant in prediction; updated at corrections. No bias random walk.'],
      ['Gyro noise', `${config.gyro_noise_std_deg_s}°/s per interval-mean sample`],
      ['Simulated accelerometer noise', `Standard deviation ${noise.actual_std_m_s2} m/s² per component`],
      ['Assumed accelerometer noise', `Standard deviation ${noise.assumed_std_m_s2} m/s² per component; vector EKF uses R = σ² I, angle KF uses the tilt approximation`],
      ['Initialization', `Estimated roll ${initial.roll_deg}° (truth 0°) · angle std ${initial.angle_std_deg}° · estimated bias ${initial.bias_deg_s}°/s, std ${initial.bias_std_deg_s}°/s`],
      ['Seed / reference', `${data.seed} · known simulation truth`],
      ['Injected acceleration', pulse ? `+${pulse.value_m_s2} m/s² in body y; ${pulse.start_s} ≤ t < ${pulse.end_s} s` : 'None'],
      ['Complementary time constant', `${config.complementary_tau_s} s`],
    ];
    if (dropout) settings.push(['Correction gap', `${formatValue(dropout.last_correction_before_s)} → ${formatValue(dropout.first_correction_after_s)} s. No omitted observation is filled or replayed later.`]);
    $('#settings').replaceChildren();
    for (const [label, value] of settings) {
      const pair = document.createElement('div');
      const term = document.createElement('dt');
      const definition = document.createElement('dd');
      term.textContent = label;
      definition.textContent = value;
      pair.append(term, definition);
      $('#settings').append(pair);
    }
    $('#display-note').textContent = `Trajectories: ${scenario().rows.length.toLocaleString('en-US')} display samples, every ${data.display_stride}th endpoint plus corrections, their predecessors and event boundaries. Roll and true bias are interpolated; estimated biases are held until correction. Diagnostics: all ${scenario().source_sample_count.toLocaleString('en-US')} original endpoints, held until the next endpoint; innovations are discrete correction samples. Source times are retained; labels round to milliseconds. Both RMSE metrics use every original endpoint. Trajectory reduction can hide short transients; neither view reconstructs the continuous path between recorded operations.`;
    $('#source-note').textContent = `Source for this scenario: meridian.${scenario().source === 'paired' ? 'ekf_experiment' : 'stress_experiment'}. The download contains all ${Object.keys(data.scenarios).length} selected trajectories, their diagnostics, repeated-trial RMSE scores and source-file SHA-256 fingerprints grouped by experiment.`;
    renderMetrics();
    renderTrials();
    redraw();
  }

  function stop() {
    playing = false;
    cancelAnimationFrame(frame);
    frame = null;
    lastFrame = null;
    tooltip.hidden = true;
    setTime(time);
  }

  function eventInspectionTime() {
    const event = scenario().event;
    return event ? event.start_s + (event.kind === 'bias_ramp' ? .5 : .4) * (event.end_s - event.start_s)
      : scenario().initialization.roll_deg !== 0 ? 0 : Math.min(8, duration);
  }

  function applyWindow(bounds) {
    stop();
    diagnosticWindow = bounds;
    $('#window-error').hidden = true;
    redraw();
    $('#announcement').textContent = `Diagnostic window ${formatValue(bounds[0], 3)} to ${formatValue(bounds[1], 3)} seconds.`;
  }

  function inspectTime(target) {
    // Named inspection actions must reach their actual event, even after zooming.
    if (view === 'diagnostics' && !inWindow(target, diagnosticWindow)) applyWindow([0, duration]);
    setTime(target);
  }

  function tick(timestamp) {
    if (!playing) return;
    const elapsed = lastFrame === null ? 0 : (timestamp - lastFrame) / 1000;
    lastFrame = timestamp;
    const [start, end] = activeBounds();
    const next = advanceTime(time - start, elapsed, speed, end - start);
    setTime(next.ended ? end : start + next.time);
    if (next.ended) stop();
    else frame = requestAnimationFrame(tick);
  }

  $('#play').addEventListener('click', () => {
    if (playing) { stop(); return; }
    if (time >= activeBounds()[1]) setTime(activeBounds()[0]);
    playing = true;
    lastFrame = null;
    tooltip.hidden = true;
    $('#play').textContent = 'Pause';
    frame = requestAnimationFrame(tick);
  });
  $('#time').addEventListener('input', event => {
    const next = Number(event.target.value);
    stop();
    setTime(next);
  });
  $('#speed').addEventListener('change', event => { speed = Number(event.target.value); lastFrame = null; });
  $('#method').addEventListener('change', event => selectMethod(event.target.value));
  $('#trial-method').addEventListener('change', event => selectMethod(event.target.value));
  $('#rmse-weight').addEventListener('change', renderMetrics);
  $('#diagnostic-window').addEventListener('submit', event => {
    event.preventDefault();
    try {
      applyWindow(validateWindow($('#window-start').valueAsNumber, $('#window-end').valueAsNumber, duration));
    } catch (error) {
      $('#window-error').textContent = error.message;
      $('#window-error').hidden = false;
    }
  });
  document.querySelectorAll('[data-window]').forEach(button => button.addEventListener('click', () => {
    applyWindow(windowPreset(button.dataset.window, time, duration));
  }));
  document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => {
    stop();
    view = button.dataset.view;
    redraw();
    $('#announcement').textContent = `${button.textContent} view selected.`;
  }));
  for (const id of ['diagnostic-component', 'innovation-mode']) $(`#${id}`).addEventListener('change', () => {
    renderDiagnostics();
    setTime(time);
  });
  for (const [id, direction] of [['previous-correction', -1], ['next-correction', 1]]) {
    $(`#${id}`).addEventListener('click', () => {
      stop();
      const target = correctionInWindow(scenario(), time, direction, activeBounds());
      if (target !== null) setTime(target);
    });
  }
  $('#jump').addEventListener('click', () => {
    stop();
    inspectTime(eventInspectionTime());
  });
  $('#recovery').addEventListener('click', () => {
    stop();
    const event = scenario().event;
    inspectTime(event ? (event.kind === 'bias_ramp' ? event.end_s : event.first_correction_after_s) : duration);
  });
  document.querySelectorAll('[data-scenario]').forEach(button => button.addEventListener('click', () => {
    stop();
    scenarioId = button.dataset.scenario;
    diagnosticWindow = [0, duration];
    $('#window-error').hidden = true;
    time = eventInspectionTime();
    renderScenario();
    $('#announcement').textContent = `${button.textContent} selected.`;
  }));
  document.addEventListener('visibilitychange', () => { if (document.hidden) stop(); });
  window.addEventListener('pagehide', stop);
  renderScenario();
  return { pause: stop };
}

window.addEventListener('hashchange', selectPage);
selectPage();
load();
