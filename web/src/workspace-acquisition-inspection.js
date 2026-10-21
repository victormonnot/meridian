import { escapeHtml as e, errorMessage } from './workspace-api.js';
import { acquisitionPath, acquisitionRoute } from './workspace-acquisitions.js';
import { createLatestRequest } from './workspace-investigation.js';
import { comparableImuSegments, finite, identifier, nearestSample, relativeSecondsText,
  sampleIdentity, timestampFromSeconds, validateInspection, validateInspectionWindow } from './workspace-acquisition-inspection-data.js';
import { createInspectionPlots } from './workspace-acquisition-inspection-plots.js';

const number = (value, digits = 3) => finite(value) ? value.toLocaleString('en', { maximumFractionDigits: digits }) : 'Unavailable';
const integer = value => Number.isSafeInteger(value) ? value.toLocaleString('en') : 'Unavailable';
const words = value => String(value ?? 'unknown').replaceAll('_', ' ');
const percentage = value => finite(value) ? `${number(value * 100, 1)}%` : 'Unavailable';
const nsMs = value => typeof value === 'string' ? `${number(Number(BigInt(value)) / 1e6, 6)} ms` : 'Unavailable';
const vector = (value, unit) => Array.isArray(value) ? `${value.map(item => number(item, 6)).join(' / ')} ${unit}` : 'Unavailable';
const details = (label, value) => `<div><dt>${e(label)}</dt><dd>${value}</dd></div>`;
const lower = (a, b) => BigInt(a) < BigInt(b) ? a : b;
const upper = (a, b) => BigInt(a) > BigInt(b) ? a : b;

export function inspectionRoute(projectId, acquisitionId) {
  return `${acquisitionRoute(projectId, acquisitionId)}/inspection`;
}

export function inspectionImagePath(projectId, acquisitionId, streamId, sampleId) {
  if (!identifier(streamId) || !sampleIdentity(sampleId)) throw new TypeError('Invalid camera sample identity.');
  return `${acquisitionPath(projectId, acquisitionId)}/inspection/images/${streamId}/${sampleId}`;
}

export function inspectionShell(acquisition) {
  return `<section class="acquisition-inspection investigation" aria-labelledby="inspection-heading">
    <a class="back-link" href="${acquisitionRoute(acquisition.project_id, acquisition.id)}">← Acquisition overview</a>
    <div class="section-heading"><div><p class="eyebrow">Sensor inspection · no estimator required</p><h2 id="inspection-heading">${e(acquisition.name)}</h2></div><span class="pill">Private to your account</span></div>
    <div id="acquisition-inspection-content"><p class="loading" role="status">Loading sensor inspection…</p></div></section>`;
}

export function unavailableInspection(response) {
  const old = response.availability === 'not_generated';
  return `<div class="empty inspection-unavailable" role="status"><h3>${old ? 'Inspection was not generated' : 'Inspection unavailable'}</h3>
    <p>${e(response.reason || (old ? 'This acquisition predates sensor inspection. Upload its original archive as a new acquisition to generate an inspection.' : 'The retained inspection is unavailable. The acquisition overview preserves its import outcome.'))}</p>
    <p>Import validity, available inspection and suitability for calibration or estimation are separate evidence.</p></div>`;
}

export function renderEvidence(summary) {
  const evidence = summary.evidence;
  const labels = [
    ['Import validity', evidence.import_validity],
    ['Inspection', evidence.inspection_availability],
    ['Calibration suitability', evidence.calibration_suitability],
    ['Estimator compatibility', evidence.estimator_compatibility],
    ['Estimator output', evidence.estimate_support],
  ];
  return `<dl class="inspection-evidence">${labels.map(([label, value]) => details(label, e(words(value)))).join('')}
    ${details('Independent evaluation', summary.reference_availability === 'absent'
      ? 'No reference included · accuracy evaluation unavailable'
      : 'Reference included · input coverage shown below; accuracy is not evaluated')}</dl>
    <p class="section-note">These observations do not establish calibration accuracy, synchronization accuracy or VIO compatibility. No estimate has been generated. An absent reference does not itself make sensor inputs unusable for estimation.</p>`;
}

function comparisons(summary) {
  return `<section class="inspection-comparisons"><h3>Full-recording sensor and reference support</h3>
    <p class="muted">Support is checked at camera observations, under the declared timing policies. Reference support and sensor support remain separate from estimator output.</p>
    ${summary.comparisons.length ? summary.comparisons.map(item => {
      const scalars = Object.entries(item).filter(([key, value]) =>
        !['kind', 'stream_ids', 'status', 'reason'].includes(key)
        && (typeof value === 'string' || typeof value === 'number' || value === null));
      return `<div class="inspection-comparison surface"><h4>${e(words(item.kind))} · ${e((item.stream_ids || []).join(' / '))}</h4>
        <p>${e(words(item.status))}${item.reason ? ` · ${e(words(item.reason))}` : ''}</p>
        <dl class="details-list">${scalars.map(([key, value]) => details(words(key), e(value === null ? 'Unavailable' : key === 'fraction' ? percentage(value) : typeof value === 'number' ? number(value, 6) : value))).join('')}
          ${item.unmatched_samples ? Object.entries(item.unmatched_samples).map(([name, value]) => details(`${name} unmatched observations`, integer(value))).join('') : ''}</dl></div>`;
    }).join('') : '<p class="muted">No cross-stream comparison is available for this acquisition.</p>'}
    ${summary.reference_availability === 'absent' ? '<p class="inspection-reference-absent">No independent reference is included. Reference coverage and accuracy evaluation are unavailable.</p>' : ''}
    </section>`;
}

function content(summary) {
  return `${renderEvidence(summary)}
    <section class="surface inspection-controls" aria-labelledby="inspection-selection-heading">
      <h3 id="inspection-selection-heading">Choose observations</h3>
      <div class="inspection-selectors"><label>Sensor stream<select data-inspection-stream>${summary.streams.map(stream =>
        `<option value="${e(stream.id)}">${e(stream.id)} · ${e(words(stream.kind))}</option>`).join('')}</select></label>
        <label>Clock segment<select data-inspection-segment></select></label></div>
      <div data-inspection-conventions></div>
      <form data-inspection-range><div class="interval-controls">
        <label>Interval start · s<input name="first" inputmode="decimal" type="text" required></label>
        <label>Interval end · s<input name="last" inputmode="decimal" type="text" required></label>
        <button type="submit">Apply interval</button><button type="button" data-inspection-action="reset">Full segment</button>
      </div></form><p class="muted">Time is relative to the selected segment's first observation. Decimal inputs retain nanoseconds; segments are never stitched across resets.</p>
      <p class="inspection-error" role="alert" data-inspection-error></p>
    </section>
    <div data-inspection-window aria-live="off"></div>
    ${comparisons(summary)}
    <details class="inspection-provenance"><summary>Frames, units, processing and inspection policies</summary>
      <p class="muted">SI conversion does not remove an existing calibration, filtering or message timestamp delay. A shared clock declaration enables only the comparisons explicitly reported here.</p>
      <pre class="job-json" tabindex="0">${e(JSON.stringify({ acquisition: summary.acquisition, method_version: summary.method_version,
        policy: summary.policy, clocks: summary.clocks, frames: summary.frames, streams: summary.streams.map(stream => ({
          id: stream.id, frame_id: stream.frame_id, clock_id: stream.clock_id, timestamp_semantics: stream.timestamp_semantics,
          measurement_semantics: stream.measurement_semantics, source_units: stream.source_units,
          units: stream.units, processing: stream.processing, saturation: stream.saturation,
        })), limitations: summary.limitations }, null, 2))}</pre>
    </details>`;
}

function conventions(summary, stream, segment) {
  const frame = summary.frames.find(item => item.id === stream.frame_id);
  const clock = summary.clocks.find(item => item.id === stream.clock_id);
  return `<dl class="details-list inspection-conventions">
    ${details('Frame', `${e(stream.frame_id)} · ${frame?.axes ? e(frame.axes.join(' / ')) : 'axis directions not declared'}`)}
    ${details('Clock', `${e(stream.clock_id)} · ${e(clock?.epoch || 'unknown')} epoch · source ticks ${e(clock?.unit || 'unknown')}`)}
    ${details('Timestamp meaning', e(words(stream.timestamp_semantics)))}
    ${details('Measurements', e(words(stream.measurement_semantics)))}
    ${details('Segment bounds · ns', `<span class="mono">${e(segment.first_ns)} → ${e(segment.last_ns)}</span>`)}
    </dl>${segment.boundary_from_previous ? `<p class="inspection-boundary">Declared boundary before this segment: ${e(words(segment.boundary_from_previous.kind))}. Previous final timestamp: <span class="mono">${e(segment.boundary_from_previous.previous_last_ns)}</span> ns. No cross-boundary interval or clock correction is inferred.</p>` : ''}
    ${stream.timestamp_semantics === 'message_write' ? '<p class="section-note">Message-write timestamps describe logging time, not guaranteed simultaneous physical sampling.</p>' : ''}`;
}

export function renderWindowStatistics(window, stream) {
  const timing = window.timing;
  const referenceGaps = timing.gap_policy === 'reference_bracket_limit';
  const quantiles = timing.interval_ns || {};
  const gaps = window.gap_count ?? timing.gap_count;
  return `<section class="inspection-statistics"><div class="section-heading"><h3>Full-resolution interval checks</h3>
    <span class="muted">${integer(window.sample_count)} source observations · ${e(stream.id)} / ${e(window.segment_id)}</span></div>
    <dl class="investigation-metrics"><div><dt>Observed rate</dt><dd>${number(timing.rate_hz)} <span>Hz</span></dd></div>
      <div><dt>${referenceGaps ? 'Reference support gaps' : 'Cadence gaps'}</dt><dd>${integer(gaps)}</dd></div><div><dt>Duplicate timestamps</dt><dd>${integer(timing.duplicate_count)}</dd></div>
      <div><dt>Backward timestamps</dt><dd>${integer(timing.backward_count)}</dd></div></dl>
    <p class="muted">${integer(timing.interval_count)} adjacent intervals; sample ordering is enforced within each segment by import. Resets and overlaps remain separate segment boundaries.</p>
    <dl class="inspection-quantiles">${[['minimum', 'Minimum'], ['p05', '5th percentile'], ['p50', 'Median'], ['p95', '95th percentile'], ['maximum', 'Maximum']].map(([key, label]) => details(label, e(nsMs(quantiles[key])))).join('')}</dl>
    <p class="muted">Quantiles: nearest rank. ${referenceGaps
      ? `A reference support gap exceeds the fixed ${e(nsMs(timing.gap_threshold_ns))} interpolation bracket limit.`
      : `A cadence gap is an interval strictly above ${e(nsMs(timing.gap_threshold_ns))}, using the full segment's fixed threshold.`}</p></section>`;
}

function support(window) {
  return `<section class="inspection-window-support"><h3>Support in this interval</h3>
    ${window.support.length ? `<dl class="details-list">${window.support.map(item => details(
      `${item.stream_id} · ${words(item.kind)}`,
      `${integer(item.supported_samples)} / ${integer(item.total_samples)} supported camera observations · ${percentage(item.fraction)}${item.not_comparable_samples ? ` · ${integer(item.not_comparable_samples)} not comparable` : ''}`)).join('')}</dl>`
    : '<p class="muted">Select a camera stream to inspect IMU and reference support at its observations.</p>'}</section>`;
}

function events(window) {
  const total = window.gap_count ?? window.timing.gap_count;
  const label = window.timing.gap_policy === 'reference_bracket_limit' ? 'Reference support gaps' : 'Cadence gaps';
  return `<section class="inspection-events"><h3>${label} and exact source samples</h3>
    ${window.gaps.length ? `<ol>${window.gaps.map(gap => `<li><button class="text-button" type="button" data-inspection-gap="${e(gap.timestamp_ns)}">${e(nsMs(gap.interval_ns))}</button>
      <span>Samples <span class="mono">${e(gap.previous_sample_id)} → ${e(gap.sample_id)}</span> · <span class="mono">${e(gap.previous_timestamp_ns)} → ${e(gap.timestamp_ns)}</span> ns</span></li>`).join('')}</ol>`
    : `<p class="muted">${total ? 'Gap details require a narrower interval.' : 'No gap intersects this interval under the recorded policy.'}</p>`}
    ${window.gaps_truncated ? `<p class="inspection-boundary">Showing ${integer(window.gaps.length)} of ${integer(total)} gaps. Narrow the interval to inspect the omitted gap details.</p>` : ''}</section>`;
}

function imuEvidence(window) {
  if (!window.imu) return '';
  const axes = window.imu.axes || {};
  const activity = window.imu.activity_evidence || {};
  return `<details class="inspection-imu-evidence"><summary>All six IMU axes · full-resolution component statistics</summary>
    <div class="inspection-table-scroll"><table><thead><tr><th>Axis</th><th>Minimum</th><th>Maximum</th><th>Mean</th><th>Std. deviation</th></tr></thead><tbody>
      ${['wx', 'wy', 'wz', 'fx', 'fy', 'fz'].map(key => `<tr><th>${key} · ${key.startsWith('w') ? 'rad/s' : 'm/s²'}</th>${['minimum', 'maximum', 'mean', 'stddev'].map(field => `<td>${number(axes[key]?.[field], 6)}</td>`).join('')}</tr>`).join('')}
    </tbody></table></div><dl class="details-list">
      ${details('Gyro norm RMS', `${number(activity.gyro_norm_rms_rad_s, 6)} rad/s`)}
      ${details('Specific-force norm mean', `${number(activity.specific_force_norm_mean_m_s2, 6)} m/s²`)}
      ${details('Specific-force norm std. deviation', `${number(activity.specific_force_norm_stddev_m_s2, 6)} m/s²`)}
      ${details('Indicator status', e(words(activity.status)))}
      ${details('Saturation', `${e(words(window.imu.saturation?.status))} · sensor limits are not declared`)}
    </dl><p class="muted">Component statistics and norm variation are apparent-motion evidence. They do not prove stationarity, calibrated bias, physical noise parameters or sensor saturation.</p></details>`;
}

function windowContent(window, stream, candidates, selectedLinked) {
  const points = window.display.points;
  return `${renderWindowStatistics(window, stream)}${support(window)}
    ${window.display.status === 'narrower_interval_required' ? '<p class="inspection-boundary" role="status">Narrower interval required. The display limit cannot preserve every mandatory gap boundary or support transition. Full-resolution checks remain available; no reduced trace is drawn.</p>' : ''}
    ${points.length ? `<section class="surface inspection-cursor"><h3>Linked observation cursor</h3>
      <div class="timeline-cursor"><button type="button" data-inspection-action="previous" aria-label="Previous displayed observation">←</button>
        <label class="sr-only" for="inspection-cursor">Displayed observation cursor</label><input id="inspection-cursor" type="range" min="0" max="${points.length - 1}" value="0" step="1">
        <button type="button" data-inspection-action="next" aria-label="Next displayed observation">→</button></div>
      <p class="muted">${integer(window.display.point_count)} displayed / ${integer(window.display.source_samples)} source observations. ${window.display.reduced ? 'Display reduction preserves extrema, gap boundaries and support transitions; cursor steps follow displayed observations.' : 'Every observation in this interval is displayed.'}</p>
      <div data-inspection-cursor-detail></div><div class="inspection-images" data-inspection-images></div></section>`
    : '<p class="muted">No displayable observation is selected. Choose another interval.</p>'}
    ${stream.kind === 'camera' ? `<div class="inspection-linked-control"><label>Linked IMU segment<select data-inspection-linked>
      <option value="">No linked IMU</option>${candidates.map(({ stream: imu, segment }) => `<option value="${e(imu.id + '/' + segment.id)}" ${selectedLinked === imu.id + '/' + segment.id ? 'selected' : ''}>${e(imu.id)} / ${e(segment.id)}</option>`).join('')}</select></label>
      <p class="muted">Only explicitly comparable clock segments with overlapping intervals are listed. Other IMUs remain available in the sensor selector.</p></div>` : ''}
    <p class="inspection-linked-status" role="status" data-inspection-linked-status></p>
    <div class="inspection-plots" data-inspection-plots></div>
    <p class="section-note">Plot time starts at the selected interval boundary. Shading and line breaks follow each stream's recorded gap policy. Plots use reduced observations; counts, distributions and component statistics use every source observation in the interval.</p>
    ${imuEvidence(window)}<div data-inspection-linked-evidence></div>${events(window)}`;
}

export function mountAcquisitionInspection(container, { api, acquisition, descriptor, onSessionExpired }) {
  const base = `${acquisitionPath(acquisition.project_id, acquisition.id)}/inspection`;
  const controller = new AbortController();
  let live = true;
  let summary, stream, segment, window, linked, candidates = [];
  let selectedLinked = '';
  let cursorIndex = 0;
  let plots;
  let imageTimer;
  const imageUrls = [];
  const node = selector => container.querySelector(selector);
  const revokeImages = () => {
    clearTimeout(imageTimer);
    container.querySelectorAll('[data-inspection-images] img').forEach(image => image.removeAttribute('src'));
    for (const url of imageUrls.splice(0)) URL.revokeObjectURL(url);
  };
  const feedback = message => {
    const target = node('[data-inspection-error]');
    if (target) target.textContent = message;
  };
  const fail = error => {
    if (!live || error.name === 'AbortError') return;
    if (error.status === 401) { destroy(); onSessionExpired?.(); return; }
    const message = error.code ? errorMessage(error) : error.message || 'Sensor inspection is unavailable.';
    if (node('[data-inspection-error]')) feedback(message);
    else container.innerHTML = `<div class="empty" role="alert"><h3>Unable to load sensor inspection</h3><p>${e(message)}</p><button type="button" data-inspection-action="retry">Retry inspection</button></div>`;
  };
  const images = createLatestRequest({
    read: async (rows, signal) => Promise.all(rows.map(async row => {
      const blob = await api.image(inspectionImagePath(acquisition.project_id, acquisition.id, row.stream_id, row.sample_id), { signal });
      if (blob.size > 128 * 1024) throw new Error('Camera preview exceeds its recorded display limit.');
      return { row, blob };
    })),
    onValue(results) {
      if (!live) return;
      for (const { row, blob } of results) {
        const target = container.querySelector(`img[data-preview-stream="${row.stream_id}"]`);
        if (!target) continue;
        const url = URL.createObjectURL(blob);
        imageUrls.push(url);
        target.src = url;
        target.hidden = false;
        target.parentNode.querySelector('span').hidden = true;
      }
    },
    onError(error) {
      if (!live) return;
      if (error.status === 401) { fail(error); return; }
      for (const label of container.querySelectorAll('[data-inspection-images] .inspection-image-status')) {
        label.textContent = 'Preview unavailable for this exact observation.';
      }
    },
  });
  const windows = createLatestRequest({
    read: async (selection, signal) => {
      const response = await api.request(`${base}/window`, { signal, query: selection.query });
      return { selection, window: validateInspectionWindow(response.window, selection.stream, selection.segment, selection.query) };
    },
    onValue(result) {
      if (!live) return;
      window = result.window;
      candidates = comparableImuSegments(summary, window);
      if (!candidates.some(item => item.stream.id + '/' + item.segment.id === selectedLinked)) {
        selectedLinked = candidates.length ? candidates[0].stream.id + '/' + candidates[0].segment.id : '';
      }
      node('[data-inspection-window]').innerHTML = windowContent(window, stream, candidates, selectedLinked);
      cursorIndex = 0;
      updatePlots();
      moveCursor(0);
      requestLinked();
    },
    onError(error) {
      if (!live) return;
      const target = node('[data-inspection-window]');
      if (target) target.innerHTML = '<p class="muted">No unverified observations are displayed. Adjust the interval or retry the selection.</p>';
      fail(error);
    },
  });
  const linkedWindows = createLatestRequest({
    read: async (selection, signal) => {
      const response = await api.request(`${base}/window`, { signal, query: selection.query });
      return { stream: selection.stream,
        window: validateInspectionWindow(response.window, selection.stream, selection.segment, selection.query) };
    },
    onValue(result) {
      linked = result;
      node('[data-inspection-linked-status]').textContent = result.window.display.status === 'narrower_interval_required'
        ? 'The linked IMU needs a narrower interval to preserve its gap boundaries. Its full-resolution statistics are retained below.'
        : `Linked IMU: ${result.stream.id} / ${result.window.segment_id}. No interpolation or clock correction.`;
      node('[data-inspection-linked-evidence]').innerHTML = imuEvidence(result.window);
      updatePlots();
      moveCursor(cursorIndex);
    },
    onError(error) {
      if (!live) return;
      if (error.status === 401) { fail(error); return; }
      node('[data-inspection-linked-status]').textContent = 'Linked IMU inspection is unavailable. Select that stream directly for details.';
    },
  });

  function updatePlots() {
    plots?.destroy();
    plots = createInspectionPlots(node('[data-inspection-plots]'), { primary: window, stream, linked }, ns => {
      moveCursor(nearestSample(window.display.points, ns));
    });
  }
  function requestLinked() {
    linkedWindows.invalidate();
    linked = null;
    const selected = candidates.find(item => item.stream.id + '/' + item.segment.id === selectedLinked);
    if (!selected) return;
    node('[data-inspection-linked-status]').textContent = 'Loading full-resolution linked IMU checks…';
    linkedWindows.run({ ...selected, query: { stream_id: selected.stream.id, segment_id: selected.segment.id,
      first_ns: upper(window.interval.first_ns, selected.segment.first_ns),
      last_ns: lower(window.interval.last_ns, selected.segment.last_ns) } });
  }
  function moveCursor(index) {
    if (!window?.display.points.length) return;
    cursorIndex = Math.max(0, Math.min(window.display.points.length - 1, index));
    const row = window.display.points[cursorIndex];
    const input = node('#inspection-cursor');
    input.value = cursorIndex;
    input.setAttribute('aria-valuetext', `Sample ${row.sample_id}, ${relativeSecondsText(row.timestamp_ns, segment.first_ns)} seconds`);
    node('[data-inspection-action="previous"]').disabled = cursorIndex === 0;
    node('[data-inspection-action="next"]').disabled = cursorIndex === window.display.points.length - 1;
    const closest = linked?.window.display.points.length
      ? linked.window.display.points[nearestSample(linked.window.display.points, row.timestamp_ns)] : null;
    node('[data-inspection-cursor-detail]').innerHTML = `<dl class="details-list inspection-cursor-details">
      ${details('Exact source observation', `<span class="mono">${e(stream.id)} / ${e(segment.id)} / sample ${e(row.sample_id)}</span>`)}
      ${details('Timestamp · ns', `<span class="mono">${e(row.timestamp_ns)}</span> · ${relativeSecondsText(row.timestamp_ns, segment.first_ns)} s from segment origin`)}
      ${stream.kind === 'imu' ? details('Angular velocity · X / Y / Z', e(vector(row.gyro_rad_s, 'rad/s'))) + details('Specific force · X / Y / Z', e(vector(row.specific_force_m_s2, 'm/s²'))) : ''}
      ${row.support ? row.support.map(item => details(`${item.stream_id} support`, e(words(item.status)))).join('') : ''}
      ${closest ? details('Closest displayed IMU observation',
        `<span class="mono">${e(linked.stream.id)} / ${e(linked.window.segment_id)} / sample ${e(closest.sample_id)}</span> · ${e(closest.timestamp_ns)} ns · Δt ${relativeSecondsText(closest.timestamp_ns, row.timestamp_ns)} s. Display selection, no interpolation.`)
        + details('Displayed angular velocity · X / Y / Z', e(vector(closest.gyro_rad_s, 'rad/s')))
        + details('Displayed specific force · X / Y / Z', e(vector(closest.specific_force_m_s2, 'm/s²'))) : ''}
      </dl>`;
    plots?.setCursor(row.timestamp_ns);
    images.invalidate();
    revokeImages();
    const imageContainer = node('[data-inspection-images]');
    if (stream.kind !== 'camera') { imageContainer.innerHTML = ''; return; }
    const selectedRows = [{ ...row, stream_id: stream.id, segment_id: segment.id }];
    if (row.stereo_match && summary.streams.some(item => item.id === row.stereo_match.stream_id && item.kind === 'camera')) selectedRows.push(row.stereo_match);
    imageContainer.innerHTML = selectedRows.map(item => `<figure>
      <div class="inspection-image"><img data-preview-stream="${e(item.stream_id)}" alt="${e(item.stream_id)} sample ${e(item.sample_id)} at ${e(item.timestamp_ns)} nanoseconds" hidden><span class="inspection-image-status muted">Loading preview…</span></div>
      <figcaption><strong>${e(item.stream_id)}</strong> · ${e(item.segment_id)} · sample ${e(item.sample_id)}<br><span class="mono">${e(item.timestamp_ns)} ns</span>${item.delta_ns !== undefined ? ` · pair Δt ${e(item.delta_ns)} ns` : ''}</figcaption></figure>`).join('');
    const indicators = row.image.indicators || {};
    imageContainer.insertAdjacentHTML('beforeend', `<div class="inspection-image-evidence"><p class="muted">Preview: longest edge at most 320 px, mono8. Source ${integer(row.image.width)} × ${integer(row.image.height)}; original bytes are retained. Display contrast is not a calibration or blur assessment.</p>
      <p class="muted">Selected image evidence · mean brightness ${number(indicators.brightness_mean_0_1, 4)} · dark endpoint ${percentage(indicators.dark_endpoint_fraction)} · bright endpoint ${percentage(indicators.bright_endpoint_fraction)} · edge contrast ${number(indicators.edge_contrast_mean_0_1, 4)}.</p>
      <details><summary>Exact image source identity</summary><p class="mono">${e(row.image.source_path)}<br>SHA-256 ${e(row.image.source_sha256)}</p></details></div>`);
    imageTimer = setTimeout(() => images.run(selectedRows), 100);
  }
  function requestWindow(first, last) {
    if (BigInt(first) < BigInt(segment.first_ns) || BigInt(last) > BigInt(segment.last_ns) || BigInt(first) > BigInt(last)) {
      feedback('Choose an ordered interval inside the selected clock segment.');
      return;
    }
    feedback('');
    linkedWindows.invalidate();
    images.invalidate();
    revokeImages();
    plots?.destroy();
    plots = null;
    linked = null;
    window = null;
    node('[data-inspection-window]').innerHTML = '<p class="loading" role="status">Checking every observation in the selected interval…</p>';
    windows.run({ stream, segment, query: { stream_id: stream.id, segment_id: segment.id, first_ns: first, last_ns: last } });
  }
  function selectSegment(id) {
    segment = stream.segments.find(item => item.id === id) || stream.segments[0];
    node('[data-inspection-segment]').value = segment.id;
    node('[data-inspection-conventions]').innerHTML = conventions(summary, stream, segment);
    node('[name="first"]').value = '0';
    node('[name="last"]').value = relativeSecondsText(segment.last_ns, segment.first_ns);
    requestWindow(segment.first_ns, segment.last_ns);
  }
  function selectStream(id) {
    stream = summary.streams.find(item => item.id === id) || summary.streams[0];
    node('[data-inspection-stream]').value = stream.id;
    node('[data-inspection-segment]').innerHTML = stream.segments.map(item => `<option value="${e(item.id)}">${e(item.id)} · ${integer(item.sample_count)} observations${item.boundary_from_previous ? ` · ${e(words(item.boundary_from_previous.kind))}` : ''}</option>`).join('');
    selectSegment(stream.segments[0].id);
  }
  async function load() {
    try {
      const response = await api.request(base, { signal: controller.signal });
      if (!live) return;
      if (!response.inspection) { container.innerHTML = unavailableInspection(response); return; }
      summary = validateInspection(response.inspection, descriptor);
      container.innerHTML = content(summary);
      selectStream((summary.streams.find(item => item.kind === 'camera') || summary.streams.find(item => item.kind === 'imu') || summary.streams[0]).id);
    } catch (error) { fail(error); }
  }
  const change = event => {
    if (event.target.matches('[data-inspection-stream]')) selectStream(event.target.value);
    if (event.target.matches('[data-inspection-segment]')) selectSegment(event.target.value);
    if (event.target.matches('[data-inspection-linked]')) {
      selectedLinked = event.target.value;
      linked = null;
      updatePlots();
      moveCursor(cursorIndex);
      node('[data-inspection-linked-status]').textContent = '';
      node('[data-inspection-linked-evidence]').innerHTML = '';
      requestLinked();
    }
  };
  const input = event => { if (event.target.id === 'inspection-cursor') moveCursor(Number(event.target.value)); };
  const imageError = event => {
    if (!event.target.matches('img[data-preview-stream]')) return;
    event.target.hidden = true;
    const label = event.target.parentNode.querySelector('.inspection-image-status');
    label.hidden = false;
    label.textContent = 'This preview could not be decoded.';
  };
  const submit = event => {
    if (!event.target.matches('[data-inspection-range]')) return;
    event.preventDefault();
    event.stopPropagation();
    try { requestWindow(timestampFromSeconds(node('[name="first"]').value, segment.first_ns), timestampFromSeconds(node('[name="last"]').value, segment.first_ns)); }
    catch (error) { feedback(error.message); }
  };
  const click = event => {
    const button = event.target.closest('[data-inspection-action], [data-inspection-gap]');
    if (!button) return;
    const action = button.dataset.inspectionAction;
    if (action === 'retry') load();
    if (action === 'reset') selectSegment(segment.id);
    if (action === 'previous') moveCursor(cursorIndex - 1);
    if (action === 'next') moveCursor(cursorIndex + 1);
    if (button.dataset.inspectionGap && window?.display.points.length) {
      moveCursor(nearestSample(window.display.points, button.dataset.inspectionGap));
      node('#inspection-cursor').focus();
    }
  };
  function destroy() {
    if (!live) return;
    live = false;
    controller.abort();
    windows.destroy();
    linkedWindows.destroy();
    images.destroy();
    plots?.destroy();
    revokeImages();
    for (const [name, handler] of [['change', change], ['input', input], ['submit', submit], ['click', click]]) container.removeEventListener(name, handler);
    container.removeEventListener('error', imageError, true);
    summary = window = linked = null;
  }
  for (const [name, handler] of [['change', change], ['input', input], ['submit', submit], ['click', click]]) container.addEventListener(name, handler);
  container.addEventListener('error', imageError, true);
  load();
  return { destroy };
}
