import './style.css';
import { createChart } from './chart.js';
import { SERIES, adjacentCorrection, advanceTime, clampTime, correctionDetails, formatValue, sampleAt, validateComparison } from './data.js';

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
    $('#previous-correction').disabled = adjacentCorrection(scenario(), time, -1) === null;
    $('#next-correction').disabled = adjacentCorrection(scenario(), time, 1) === null;
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
    $('#display-note').textContent = `${scenario().rows.length.toLocaleString('en-US')} display samples: every ${data.display_stride}th endpoint plus all correction endpoints, their actual predecessors and event boundaries. Source times are retained; labels round to milliseconds. Previous/next correction uses exact arrival times. Roll and true bias are linearly interpolated; estimated biases are held until the next correction. Both RMSE metrics use all ${scenario().source_sample_count.toLocaleString('en-US')} original endpoints; display reduction can still hide short transients.`;
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
    return event ? event.start_s + (event.kind === 'bias_ramp' ? .5 : .4) * (event.end_s - event.start_s)
      : scenario().initialization.roll_deg !== 0 ? 0 : Math.min(8, duration);
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
  $('#rmse-weight').addEventListener('change', renderMetrics);
  for (const [id, direction] of [['previous-correction', -1], ['next-correction', 1]]) {
    $(`#${id}`).addEventListener('click', () => {
      stop();
      const target = adjacentCorrection(scenario(), time, direction);
      if (target !== null) setTime(target);
    });
  }
  $('#jump').addEventListener('click', () => {
    stop();
    setTime(eventInspectionTime());
  });
  $('#recovery').addEventListener('click', () => {
    stop();
    const event = scenario().event;
    setTime(event ? (event.kind === 'bias_ramp' ? event.end_s : event.first_correction_after_s) : duration);
  });
  document.querySelectorAll('[data-scenario]').forEach(button => button.addEventListener('click', () => {
    stop();
    scenarioId = button.dataset.scenario;
    time = eventInspectionTime();
    renderScenario();
    $('#announcement').textContent = `${button.textContent} selected.`;
  }));
  document.addEventListener('visibilitychange', () => { if (document.hidden) stop(); });
  window.addEventListener('pagehide', stop);
  renderScenario();
}

load();
