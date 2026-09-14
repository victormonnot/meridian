import './style.css';
import { createChart } from './chart.js';
import { SERIES, advanceTime, clampTime, formatValue, latestCorrection, sampleAt, validateComparison } from './data.js';

const $ = selector => document.querySelector(selector);
const dataUrl = `${import.meta.env.BASE_URL}data/roll-comparison.json`;

async function load() {
  try {
    const response = await fetch(dataUrl);
    if (!response.ok) throw new Error(`Data request returned HTTP ${response.status}`);
    const data = validateComparison(await response.json());
    $('#load-status').hidden = true;
    $('#explorer').hidden = false;
    startExplorer(data);
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
    time = clampTime(next, duration);
    const row = sampleAt(scenario().rows, time);
    $('#time').value = time;
    $('#time').setAttribute('aria-valuetext', `${formatValue(time)} seconds`);
    $('#time-value').textContent = `${formatValue(time)} s`;
    $('#chart-time').textContent = `${formatValue(time)} s`;
    $('#truth-angle').textContent = `${formatValue(row[1])}°`;
    $('#estimate-angle').textContent = `${formatValue(row[method().roll])}°`;
    $('#angle-error').textContent = `${formatValue(row[method().roll] - row[1])}°`;
    for (const item of SERIES.filter(item => item.bias !== null)) {
      $(`#bias-${item.id}`).textContent = `${formatValue(row[item.bias], 3)}°/s`;
    }
    $('#method-name').textContent = method().label;
    const last = latestCorrection(scenario(), time);
    const event = scenario().event;
    const unavailable = event?.kind === 'accel_dropout' && time >= event.start_s && time < event.end_s;
    $('#correction-status').textContent = last === null
      ? `No accelerometer correction yet. First correction at ${formatValue(scenario().correction_times_s[0])} s.`
      : `${unavailable ? 'Accelerometer unavailable · gyro predictions continue. ' : ''}Latest accelerometer correction: ${formatValue(last)} s.`;
    // Forward-right-down, looking forward from behind: positive roll lowers right.
    $('#truth-pose').setAttribute('transform', `rotate(${row[1]})`);
    $('#estimate-pose').setAttribute('transform', `rotate(${row[method().roll]})`);
    charts.forEach(chart => chart.setCursor(row));
    $('#play').textContent = playing ? 'Pause' : time >= duration ? 'Replay' : 'Play';
    return row;
  }

  function redraw() {
    tooltip.hidden = true;
    for (const button of legendButtons) button.setAttribute('aria-pressed', String(visible.has(button.dataset.series)));
    charts.forEach(chart => chart.setData(scenario(), visible));
    setTime(time);
  }

  function renderMetrics() {
    $('#metrics').replaceChildren();
    for (const item of SERIES.slice(1)) {
      const row = document.createElement('tr');
      row.dataset.focused = String(item.id === methodId);
      const name = document.createElement('td');
      name.textContent = item.label;
      const value = document.createElement('td');
      value.textContent = formatValue(scenario().metrics.rmse_deg[item.id], 3);
      row.append(name, value);
      $('#metrics').append(row);
    }
  }

  function renderScenario() {
    const event = scenario().event;
    const pulse = event?.kind === 'translation' ? event : null;
    const dropout = event?.kind === 'accel_dropout' ? event : null;
    const ramp = event?.kind === 'bias_ramp' ? event : null;
    const initial = scenario().initialization;
    $('#scenario-description').textContent = scenarioId === 'initial_offset'
      ? `All estimates start at ${initial.roll_deg}°, while true roll is 0°. The Kalman filters declare an initial angle standard deviation of ${initial.angle_std_deg}°.`
      : ramp ? `True gyro bias rises from ${ramp.initial_bias_deg_s} to ${ramp.final_bias_deg_s}°/s between ${ramp.start_s} and ${ramp.end_s} s, then stays at ${ramp.final_bias_deg_s}°/s. All accelerometer corrections remain available.`
        : dropout ? `${dropout.omitted_count} accelerometer observations are omitted from 12 to 17 s. Gyro predictions continue; corrections resume at 17 s.`
        : pulse ? `Added body-y acceleration: +${pulse.value_m_s2} m/s² from ${pulse.start_s} to ${pulse.end_s} s. The gravity model is disturbed.`
          : 'Smooth roll with constant gyro bias. Accelerometer measurements follow the gravity model.';
    $('#jump').hidden = scenarioId === 'nominal';
    $('#jump').textContent = scenarioId === 'initial_offset' ? 'Inspect start'
      : ramp ? 'Inspect ramp' : dropout ? 'Inspect loss' : 'Inspect disturbance';
    $('#recovery').hidden = !dropout && !ramp;
    $('#recovery').textContent = ramp ? 'Inspect plateau' : 'Inspect recovery';
    $('#metric-note').textContent = scenarioId === 'initial_offset'
      ? 'Includes the initial 60° error and the recovery transient. This is one noise realization.'
      : ramp ? 'The filters keep their constant-bias model. This run evaluates a model mismatch, not retuned filters.'
        : dropout ? 'A lower error on this seed does not mean losing observations improves estimation.'
        : 'Computed from every original endpoint, including initialization.';
    document.querySelectorAll('[data-scenario]').forEach(button => {
      button.setAttribute('aria-pressed', String(button.dataset.scenario === scenarioId));
    });
    const settings = [
      ['Roll motion', `${config.amplitude_deg}° amplitude · ${config.frequency_hz} Hz · ${duration} s`],
      ['Measurement schedule', `Gyro ${config.sample_rate_hz} Hz · accelerometer ${config.sample_rate_hz / config.observation_every} Hz scheduled; ${scenario().correction_times_s.length}/${scenario().scheduled_accel_count} corrections received`],
      ['True gyro bias', ramp
        ? `${ramp.initial_bias_deg_s} → ${ramp.final_bias_deg_s}°/s over ${ramp.start_s}–${ramp.end_s} s (${ramp.slope_deg_s2}°/s²), then constant`
        : `${config.bias_deg_s}°/s, constant`],
      ['Kalman bias model', 'Constant in prediction; updated at corrections. No bias random walk.'],
      ['Gyro noise', `${config.gyro_noise_std_deg_s}°/s per interval-mean sample`],
      ['Accelerometer noise', `${config.accel_noise_std_m_s2} m/s² per component`],
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
    $('#display-note').textContent = `${scenario().rows.length.toLocaleString('en-US')} display samples: every ${data.display_stride}th endpoint plus all correction endpoints, their predecessors and event boundaries. Roll and true bias are linearly interpolated; estimated biases are held until the next correction. RMSE uses all ${scenario().source_sample_count.toLocaleString('en-US')} original endpoints; display reduction can still hide short transients.`;
    $('#source-note').textContent = `Source for this scenario: meridian.${scenario().source === 'paired' ? 'ekf_experiment' : 'stress_experiment'}. The download contains all ${Object.keys(data.scenarios).length} selected runs, full-run metrics and source-file SHA-256 fingerprints grouped by experiment.`;
    renderMetrics();
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
    return event ? event.start_s + (event.kind === 'bias_ramp' ? .5 : .4) * (event.end_s - event.start_s) : 0;
  }

  function tick(timestamp) {
    if (!playing) return;
    const elapsed = lastFrame === null ? 0 : (timestamp - lastFrame) / 1000;
    lastFrame = timestamp;
    const next = advanceTime(time, elapsed, speed, duration);
    setTime(next.time);
    if (next.ended) stop();
    else frame = requestAnimationFrame(tick);
  }

  $('#play').addEventListener('click', () => {
    if (playing) { stop(); return; }
    if (time >= duration) setTime(0);
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
  $('#method').addEventListener('change', event => {
    methodId = event.target.value;
    visible.add(methodId);
    renderMetrics();
    redraw();
  });
  $('#jump').addEventListener('click', () => {
    stop();
    setTime(eventInspectionTime());
  });
  $('#recovery').addEventListener('click', () => {
    stop();
    const event = scenario().event;
    setTime(event.kind === 'bias_ramp' ? event.end_s : event.first_correction_after_s);
  });
  document.querySelectorAll('[data-scenario]').forEach(button => button.addEventListener('click', () => {
    stop();
    scenarioId = button.dataset.scenario;
    time = scenarioId === 'nominal' ? Math.min(8, duration) : eventInspectionTime();
    renderScenario();
    $('#announcement').textContent = `${button.textContent} selected.`;
  }));
  document.addEventListener('visibilitychange', () => { if (document.hidden) stop(); });
  window.addEventListener('pagehide', stop);
  renderScenario();
}

load();
