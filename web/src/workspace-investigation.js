import { escapeHtml as e, errorMessage } from './workspace-api.js';
import { jobPath } from './workspace-jobs.js';
import { finite, validateInvestigation, nearestFrame, intervalIndices, frameStatus } from './workspace-investigation-data.js';
import { createInvestigationPlots } from './workspace-investigation-plots.js';

const number = (value, digits = 3) => finite(value) ? value.toFixed(digits) : '—';
const percent = value => finite(value) ? `${(value * 100).toFixed(1)}%` : '—';
const vector = (value, digits = 3) => value ? value.map(item => number(item, digits)).join(' / ') : 'Unavailable';

export function investigationShell(job) {
  return `<section class="investigation" aria-labelledby="investigation-heading"><a class="back-link" href="${e(jobPath(job.project_id, job.id))}">← Run overview</a><div class="section-heading"><div><p class="eyebrow">Synchronized investigation · version ${e(job.revision_version)}</p><h2 id="investigation-heading">${e(job.experiment_name)}</h2><p class="mono job-id">${e(job.id)}</p></div><span class="pill">${e(job.state)}</span></div><div id="investigation-content"><p class="loading" role="status">Loading full-resolution investigation…</p></div></section>`;
}

export function investigationError(error) {
  const detail = {
    investigation_unavailable: ['Investigation unavailable', 'This run has no retained investigation artifact. Failed, cancelled and older runs may have no inspectable trajectory. The run overview preserves its recorded outcome.'],
    artifacts_expired: ['Artifact retention has expired', 'Images and detailed signals are no longer retained for this run. Saved summary metrics remain in the run overview.'],
    artifact_unavailable: ['Artifact unavailable', 'The service could not validate the retained investigation data. No unverified data is displayed. Try again later.'],
  }[error.code] || ['Unable to load investigation', errorMessage(error)];
  return `<div class="empty" role="alert"><h3>${e(detail[0])}</h3><p>${e(detail[1])}</p><button type="button" data-action="retry">Retry investigation</button></div>`;
}

export function renderIntervalMetrics(summary) {
  const metrics = summary.metrics || {};
  const counts = summary.counts || {};
  return `<div class="interval-status">${summary.status === 'ok' ? 'Full-resolution interval evaluation' : e(String(summary.status).replaceAll('_', ' '))}</div><dl class="investigation-metrics"><div><dt>ATE RMSE</dt><dd>${number(metrics.ate_rmse_m, 5)} <span>m</span></dd></div><div><dt>Orientation RMSE</dt><dd>${number(metrics.orientation_rmse_deg)} <span>deg</span></dd></div><div><dt>Estimate coverage</dt><dd>${percent(summary.coverage_fraction)}</dd></div><div><dt>Evaluated coverage</dt><dd>${percent(summary.evaluated_fraction)}</dd></div></dl><p class="muted">${e(counts.expected_camera_samples ?? '—')} expected epochs · ${e(counts.estimate_samples ?? '—')} estimated · ${e(counts.reference_supported_camera_samples ?? '—')} reference-supported · ${e(counts.evaluated_camera_samples ?? '—')} evaluated. Reference coverage: ${percent(summary.reference_coverage_fraction)}.</p>`;
}

// Aborting network work alone is insufficient: a transport may resolve late.
export function createLatestRequest({ read, onValue, onError }) {
  let generation = 0;
  let controller;
  let destroyed = false;
  return {
    async run(value) {
      if (destroyed) return;
      controller?.abort();
      const current = ++generation;
      controller = new AbortController();
      try {
        const result = await read(value, controller.signal);
        if (!destroyed && current === generation) onValue(result, value);
      } catch (error) {
        if (!destroyed && current === generation && error.name !== 'AbortError') onError(error);
      }
    },
    invalidate() { generation += 1; controller?.abort(); },
    destroy() { destroyed = true; generation += 1; controller?.abort(); },
  };
}

function eventsMarkup(data) {
  const names = { first_estimated_pose: 'First estimated pose', native_initialization: 'Native initialization', missing_estimate: 'Missing estimator output', unsupported_reference: 'Unsupported reference' };
  return (data.events || []).map(event => {
    const time = event.time_s ?? event.first_time_s;
    return `<li><button type="button" class="text-button" data-event-time="${e(time)}">${number(time)} s${finite(event.last_time_s) ? ` – ${number(event.last_time_s)} s` : ''}</button><div><strong>${e(names[event.kind] || event.kind)}</strong>${event.sample_count ? `<span class="muted"> · ${e(event.sample_count)} camera epochs</span>` : ''}<p class="muted">${e(event.source)}</p></div></li>`;
  }).join('');
}

function content(data) {
  const last = data.frames.length - 1;
  const summary = data.summary;
  return `${data.run_state === 'partial' ? '<div class="job-failure" role="status"><h3>Partial evidence from a failed run</h3><p>This run did not complete successfully. The timeline and coverage retain every expected camera epoch, including missing output. Raw IMU samples are recording inputs; their presence does not mean the estimator processed them.</p></div>' : ''}<div class="investigation-overview"><p class="muted">Full recording · ${number(data.time.duration_s)} s · ${data.frames.length.toLocaleString('en')} stereo epochs · ${data.imu.samples.length.toLocaleString('en')} raw IMU samples</p><p class="muted">ATE ${number(summary.metrics?.ate_rmse_m, 5)} m · orientation ${number(summary.metrics?.orientation_rmse_deg)} deg · first estimate ${number(summary.initialization_delay_s)} s</p></div>
    ${data.alignment === null ? '<p class="error" role="status">Global alignment is unavailable. Aligned trajectory, attitude and scored errors cannot be displayed; raw and reference coordinate frames are not interchangeable.</p>' : ''}<section class="surface investigation-controls" aria-labelledby="timeline-heading"><div class="section-heading"><h3 id="timeline-heading">Shared timeline</h3><output id="cursor-time" class="mono" aria-live="off"></output></div><div class="timeline-cursor"><button type="button" id="frame-previous" aria-label="Previous camera frame">←</button><label for="frame-cursor" class="sr-only">Camera frame cursor</label><input id="frame-cursor" type="range" min="0" max="${last}" value="0" step="1" aria-describedby="cursor-help"><button type="button" id="frame-next" aria-label="Next camera frame">→</button></div><p class="muted" id="cursor-help">Arrow keys move one camera epoch; Home and End reach the selected bounds. Click a signal plot to move the same cursor.</p><div class="interval-controls"><label for="interval-start">Interval start · s<input id="interval-start" type="number" min="0" max="${data.time.duration_s}" step="any" value="0"></label><label for="interval-end">Interval end · s<input id="interval-end" type="number" min="0" max="${data.time.duration_s}" step="any" value="${data.time.duration_s}"></label><button type="button" id="apply-interval">Apply interval</button><button type="button" id="reset-interval">Full recording</button><button type="button" id="start-at-cursor">Start at cursor</button><button type="button" id="end-at-cursor">End at cursor</button></div><p id="interval-error" class="error" role="alert"></p></section>
    <section aria-labelledby="interval-heading" class="investigation-interval"><div class="section-heading"><h3 id="interval-heading">Selected interval</h3><span class="muted" id="interval-label"></span></div><div id="interval-metrics" aria-live="polite"><p class="muted">Loading interval metrics…</p></div><p class="section-note">Inclusive camera epochs. Metrics use every evaluated sample in the selection and the unchanged full-recording SE(3) alignment; no interval refit, fitted scale or fitted time offset.</p></section>
    <section aria-labelledby="stereo-heading"><div class="section-heading"><h3 id="stereo-heading">Stereo observation</h3><span id="frame-status" class="muted"></span></div><div class="investigation-observation"><div class="stereo-pair">${['cam0', 'cam1'].map(camera => `<figure><div class="stereo-image"><img id="image-${camera}" alt="" hidden><span class="muted" id="image-status-${camera}">Loading ${camera}…</span></div><figcaption>${camera} · 512 × 512 · original mono16 PNG</figcaption></figure>`).join('')}</div><div class="cursor-inspector surface"><p class="eyebrow">Selected camera epoch</p><dl class="details-list"><div><dt>Timestamp · ns</dt><dd class="mono" id="cursor-timestamp"></dd></div><div><dt>Aligned position · m</dt><dd class="mono" id="cursor-position"></dd></div><div><dt>Roll / pitch / yaw · deg</dt><dd class="mono" id="cursor-attitude"></dd></div><div><dt>Position / orientation error</dt><dd class="mono" id="cursor-error"></dd></div><div><dt>Nearest raw IMU sample</dt><dd class="mono" id="cursor-imu"></dd></div></dl><p class="muted">Images are fetched only for the selected epoch. Original intensity encoding; no feature overlays are available.</p></div></div></section>
    <section aria-labelledby="signals-heading"><div class="section-heading"><h3 id="signals-heading">Trajectory and synchronized signals</h3><span class="muted">Time from first camera epoch · s</span></div><div id="investigation-plots"></div><p class="section-note">Plots preserve missing-data breaks. Dense signals use a min/max display envelope; metrics always use full-resolution data. Euler angles show principal values with branch crossings broken, and are not orientation-error measurements. Raw IMU samples use the same timestamp origin, without resampling or bias correction; samples outside the camera interval remain in the artifact.</p></section>
    <div class="investigation-evidence"><section class="surface"><h3>Events and support gaps</h3><p class="muted">Native initialization and the first published pose can differ because of estimator buffering. Missing output alone does not establish tracking loss.</p><ol class="investigation-events">${eventsMarkup(data)}</ol></section><section class="surface"><h3>Diagnostic availability</h3><dl class="diagnostic-list">${Object.entries(data.diagnostics || {}).map(([key, diagnostic]) => `<div><dt>${e(key.replaceAll('_', ' '))}</dt><dd>${diagnostic.available ? 'Available in the artifact' : 'Unavailable'}<p class="muted">${e(diagnostic.reason)}</p></dd></div>`).join('')}</dl></section></div>
    <details class="investigation-provenance"><summary>Frames, units, protocol and provenance</summary><p class="muted">Reference is used only for independent evaluation and display. This handheld recording is not flight validation.</p><pre class="job-json" tabindex="0" aria-label="Investigation provenance">${e(JSON.stringify({ specification_sha256: data.specification_sha256, dataset_manifest_sha256: data.dataset_manifest_sha256, time: data.time, coordinate_frames: data.coordinate_frames, units: data.units, protocol: data.protocol, alignment: data.alignment, provenance: data.provenance }, null, 2))}</pre></details>`;
}

export function mountInvestigation(container, { api, job, onSessionExpired }) {
  const path = `/projects/${job.project_id}/jobs/${job.id}/investigation`;
  const controller = new AbortController();
  let destroyed = false;
  let data;
  let plots;
  let imageTimer;
  let metricTimer;
  let index = 0;
  let bounds;
  let images;
  let metrics;
  const urls = [];
  const listeners = [];
  const get = id => container.querySelector(`#${id}`);
  const listen = (node, event, handler) => { node.addEventListener(event, handler); listeners.push(() => node.removeEventListener(event, handler)); };
  const revokeImages = () => {
    for (const camera of ['cam0', 'cam1']) { const image = get(`image-${camera}`); if (image) { image.hidden = true; image.removeAttribute('src'); } }
    for (const url of urls.splice(0)) URL.revokeObjectURL(url);
  };
  const handleError = error => {
    if (destroyed || error.name === 'AbortError') return;
    if (error.status === 401) { destroy(); onSessionExpired(); }
    else { destroy(); container.innerHTML = investigationError(error); }
  };
  function requestImages() {
    clearTimeout(imageTimer);
    images.invalidate();
    revokeImages();
    for (const camera of ['cam0', 'cam1']) { get(`image-status-${camera}`).hidden = false; get(`image-status-${camera}`).textContent = `Loading ${camera}…`; }
    imageTimer = setTimeout(() => images.run(index), 120);
  }
  function moveCursor(next) {
    index = Math.max(bounds[0], Math.min(bounds[1], next));
    const frame = data.frames[index];
    get('frame-cursor').value = index;
    get('frame-cursor').setAttribute('aria-valuetext', `Frame ${index}, ${number(frame.time_s)} seconds, ${frameStatus(frame)}`);
    get('cursor-time').textContent = `${number(frame.time_s)} s · frame ${index} / ${data.frames.length - 1}`;
    get('cursor-timestamp').textContent = frame.timestamp_ns;
    get('cursor-position').textContent = vector(frame.estimate?.position_m);
    get('cursor-attitude').textContent = vector(frame.estimate?.rpy_deg, 2);
    get('cursor-error').textContent = `${number(frame.position_error_m, 5)} m / ${number(frame.orientation_error_deg)} deg`;
    get('frame-status').textContent = frameStatus(frame);
    const imu = data.imu.samples.length ? data.imu.samples[nearestFrame(data.imu.samples, frame.time_s)] : null;
    get('cursor-imu').textContent = imu ? `${imu.timestamp_ns} ns · Δt ${number((imu.time_s - frame.time_s) * 1000, 3)} ms · ω ${vector(imu.gyro_rad_s)} rad/s · a ${vector(imu.accel_m_s2)} m/s² (nearest, not interpolated)` : 'Unavailable';
    get('frame-previous').disabled = index === bounds[0];
    get('frame-next').disabled = index === bounds[1];
    plots.setCursor(index);
    requestImages();
  }
  function applyInterval(start, end) {
    try {
      if (!finite(start) || !finite(end) || start < data.frames[0].time_s || end > data.frames.at(-1).time_s) throw new RangeError('Choose bounds inside the camera recording.');
      bounds = intervalIndices(data.frames, start, end);
      get('interval-error').textContent = '';
    } catch (error) { get('interval-error').textContent = error.message; return; }
    const first = data.frames[bounds[0]];
    const last = data.frames[bounds[1]];
    get('interval-start').value = first.time_s;
    get('interval-end').value = last.time_s;
    get('interval-label').textContent = `${number(first.time_s)} – ${number(last.time_s)} s · frames ${bounds[0]}–${bounds[1]}`;
    get('frame-cursor').min = bounds[0];
    get('frame-cursor').max = bounds[1];
    plots.setInterval(bounds);
    moveCursor(index);
    clearTimeout(metricTimer);
    metrics.invalidate();
    get('interval-metrics').innerHTML = '<p class="muted" role="status">Loading full-resolution interval metrics…</p>';
    metricTimer = setTimeout(() => metrics.run({ start_ns: first.timestamp_ns, end_ns: last.timestamp_ns }), 180);
  }
  async function load() {
    try {
      const response = await api.request(path, { signal: controller.signal });
      if (destroyed) return;
      data = validateInvestigation(response.investigation, job.specification_sha256);
      container.innerHTML = content(data);
      bounds = [0, data.frames.length - 1];
      plots = createInvestigationPlots(get('investigation-plots'), data, time => moveCursor(nearestFrame(data.frames, time)));
      images = createLatestRequest({
        read: (frame, signal) => Promise.all(['cam0', 'cam1'].map(camera => api.image(`${path}/images/${camera}/${frame}`, { signal }))),
        onValue(blobs, frame) {
          revokeImages();
          for (const [i, camera] of ['cam0', 'cam1'].entries()) {
            const image = get(`image-${camera}`);
            const url = URL.createObjectURL(blobs[i]);
            urls.push(url);
            image.alt = `${camera}, camera frame ${frame}, ${number(data.frames[frame].time_s)} seconds`;
            image.src = url;
            image.hidden = false;
            get(`image-status-${camera}`).hidden = true;
          }
        },
        onError: handleError,
      });
      metrics = createLatestRequest({ read: (query, signal) => api.request(`${path}/metrics`, { query, signal }), onValue: response => { get('interval-metrics').innerHTML = renderIntervalMetrics(response.interval); }, onError: handleError });
      listen(get('frame-cursor'), 'input', event => moveCursor(Number(event.target.value)));
      listen(get('frame-previous'), 'click', () => moveCursor(index - 1));
      listen(get('frame-next'), 'click', () => moveCursor(index + 1));
      listen(get('apply-interval'), 'click', () => applyInterval(get('interval-start').valueAsNumber, get('interval-end').valueAsNumber));
      listen(get('reset-interval'), 'click', () => applyInterval(data.frames[0].time_s, data.frames.at(-1).time_s));
      listen(get('start-at-cursor'), 'click', () => applyInterval(data.frames[index].time_s, data.frames[bounds[1]].time_s));
      listen(get('end-at-cursor'), 'click', () => applyInterval(data.frames[bounds[0]].time_s, data.frames[index].time_s));
      for (const button of container.querySelectorAll('[data-event-time]')) listen(button, 'click', () => {
        const target = nearestFrame(data.frames, Number(button.dataset.eventTime));
        if (target < bounds[0] || target > bounds[1]) applyInterval(data.frames[0].time_s, data.frames.at(-1).time_s);
        moveCursor(target);
        get('frame-cursor').focus();
      });
      for (const camera of ['cam0', 'cam1']) listen(get(`image-${camera}`), 'error', () => {
        get(`image-${camera}`).hidden = true;
        get(`image-status-${camera}`).hidden = false;
        get(`image-status-${camera}`).textContent = 'The browser could not decode this original image.';
      });
      applyInterval(data.frames[0].time_s, data.frames.at(-1).time_s);
    } catch (error) { handleError(error); }
  }
  function destroy() {
    if (destroyed) return;
    destroyed = true;
    controller.abort();
    clearTimeout(imageTimer);
    clearTimeout(metricTimer);
    images?.destroy();
    metrics?.destroy();
    revokeImages();
    plots?.destroy();
    for (const unlisten of listeners.splice(0)) unlisten();
    data = null;
  }
  load();
  return { destroy };
}
