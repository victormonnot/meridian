import './style.css';
import { createChart } from './chart.js';
import { SERIES, advanceTime, clampTime, formatValue, sampleAt, validateComparison } from './data.js';

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
    $(`#${kind}-chart`), kind, data.domains[kind === 'roll' ? 'roll_deg' : 'bias_deg_s'],
    duration, (next, pause) => {
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
    const pulse = scenario().disturbance;
    $('#scenario-description').textContent = pulse
      ? `Added body-y acceleration: +${pulse.value_m_s2} m/s² from ${pulse.start_s} to ${pulse.end_s} s. The gravity model is disturbed.`
      : 'Smooth roll with constant gyro bias. Accelerometer measurements follow the gravity model.';
    $('#jump').hidden = !pulse;
    document.querySelectorAll('[data-scenario]').forEach(button => {
      button.setAttribute('aria-pressed', String(button.dataset.scenario === scenarioId));
    });
    const settings = [
      ['Roll motion', `${config.amplitude_deg}° amplitude · ${config.frequency_hz} Hz · ${duration} s`],
      ['Measurement schedule', `Gyro ${config.sample_rate_hz} Hz · accelerometer ${config.sample_rate_hz / config.observation_every} Hz`],
      ['True gyro bias', `${config.bias_deg_s}°/s, constant`],
      ['Gyro noise', `${config.gyro_noise_std_deg_s}°/s per interval-mean sample`],
      ['Accelerometer noise', `${config.accel_noise_std_m_s2} m/s² per component`],
      ['Initialization', `Known roll · estimated bias ${formatValue(data.provenance.initialization.bias_rad_s * 180 / Math.PI, 2)}°/s`],
      ['Seed / reference', `${data.seed} · known simulation truth`],
      ['Injected acceleration', pulse ? `+${pulse.value_m_s2} m/s² in body y; ${pulse.start_s} ≤ t < ${pulse.end_s} s` : 'None'],
      ['Complementary time constant', `${config.complementary_tau_s} s`],
    ];
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
    $('#display-note').textContent = `${scenario().rows.length.toLocaleString('en-US')} display samples, normally ${formatValue(1000 * data.display_stride / config.sample_rate_hz, 0)} ms apart, with pulse boundaries retained. Cursor values are linearly interpolated. RMSE uses all ${scenario().source_sample_count.toLocaleString('en-US')} original endpoints; decimation can hide short transients.`;
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
    const pulse = scenario().disturbance;
    setTime(pulse.start_s + .4 * (pulse.end_s - pulse.start_s));
  });
  document.querySelectorAll('[data-scenario]').forEach(button => button.addEventListener('click', () => {
    stop();
    scenarioId = button.dataset.scenario;
    renderScenario();
    $('#announcement').textContent = `${button.textContent} selected.`;
  }));
  document.addEventListener('visibilitychange', () => { if (document.hidden) stop(); });
  window.addEventListener('pagehide', stop);
  renderScenario();
}

load();
