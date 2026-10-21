import { ApiError, escapeHtml as e, errorMessage } from './workspace-api.js';

const UUID = /^[a-f\d]{8}(?:-[a-f\d]{4}){3}-[a-f\d]{12}$/i;
const SHA256 = /^[a-f\d]{64}$/;
const MAX_CHUNK_BYTES = 4 * 1024 * 1024;
const count = value => new Intl.NumberFormat('en').format(value);

export function acquisitionPath(projectId, acquisitionId) {
  if (!UUID.test(projectId) || (acquisitionId !== undefined && !UUID.test(acquisitionId))) throw new TypeError('Invalid acquisition identity');
  return `/projects/${projectId}/acquisitions${acquisitionId ? `/${acquisitionId}` : ''}`;
}

export function acquisitionRoute(projectId, acquisitionId) {
  acquisitionPath(projectId, acquisitionId);
  return `#project/${projectId}/data${acquisitionId ? `/acquisitions/${acquisitionId}` : ''}`;
}

export function formatBytes(value) {
  if (!Number.isSafeInteger(value) || value < 0) return '—';
  if (value < 1024) return `${count(value)} B`;
  const unit = value >= 1024 ** 3 ? 3 : value >= 1024 ** 2 ? 2 : 1;
  return `${(value / 1024 ** unit).toFixed(1)} ${['B', 'KiB', 'MiB', 'GiB'][unit]}`;
}

export function acquisitionStatus(value) {
  if (value.state === 'ready' && value.availability === 'deleted') return 'Deleted';
  if (value.state === 'ready' && value.files_available === false) return 'Files unavailable';
  if (value.cancel_requested && ['receiving', 'validating'].includes(value.state)) return 'Cancellation requested';
  return ({ receiving: 'Receiving', validating: 'Validating', ready: 'Ready to inspect',
    rejected: 'Rejected', cancelled: 'Cancelled' })[value.state] || 'Unavailable';
}

function aborted(signal) {
  if (signal?.aborted) throw new DOMException('Transfer paused', 'AbortError');
}

function checkedTransfer(value, file) {
  if (!Number.isSafeInteger(file?.size) || file.size < 1 || file.size !== value.size_bytes) {
    throw new Error('Select the same archive: its size must match the saved upload.');
  }
  if (!Number.isSafeInteger(value.bytes_received) || value.bytes_received < 0
    || value.bytes_received > value.size_bytes || !Number.isSafeInteger(value.chunk_bytes)
    || value.chunk_bytes < 1 || value.chunk_bytes > MAX_CHUNK_BYTES) {
    throw new Error('The saved upload progress could not be verified. Reload the acquisition.');
  }
}

export async function hashChunk(buffer, { signal } = {}) {
  aborted(signal);
  if (!(buffer instanceof ArrayBuffer) || buffer.byteLength > MAX_CHUNK_BYTES) throw new TypeError('Invalid chunk buffer');
  const digest = await globalThis.crypto.subtle.digest('SHA-256', buffer);
  aborted(signal);
  return Array.from(new Uint8Array(digest), value => value.toString(16).padStart(2, '0')).join('');
}

async function readChunk(file, offset, size, signal) {
  aborted(signal);
  const buffer = await file.slice(offset, offset + size).arrayBuffer();
  aborted(signal);
  if (buffer.byteLength !== size) throw new Error('The selected archive could not be read completely.');
  return buffer;
}

export async function verifyReceivedPrefix(file, acquisition, chunks, { signal, onProgress, digest = hashChunk } = {}) {
  checkedTransfer(acquisition, file);
  if (!Array.isArray(chunks) || chunks.length > 1024) throw new Error('The saved chunk manifest could not be verified.');
  let offset = 0;
  for (const chunk of chunks) {
    aborted(signal);
    const expected = Math.min(acquisition.chunk_bytes, file.size - offset);
    if (chunk.offset !== offset || chunk.size_bytes !== expected || !SHA256.test(chunk.sha256 || '')
      || offset + expected > acquisition.bytes_received) {
      throw new Error('The saved chunk manifest does not match the upload progress. Reload the acquisition.');
    }
    const buffer = await readChunk(file, offset, expected, signal);
    if (await digest(buffer, { signal }) !== chunk.sha256) {
      throw new Error('This archive differs from the received bytes. Select the original archive to resume.');
    }
    offset += expected;
    aborted(signal);
    onProgress?.({ phase: 'verifying', completed: offset, total: acquisition.bytes_received });
  }
  if (offset !== acquisition.bytes_received) throw new Error('The saved chunk manifest is incomplete. Reload the acquisition.');
  return offset;
}

// Server acknowledgements own progress. Aborting a request never assumes that
// the server discarded its chunk; the next attempt reads and verifies it again.
export function createAcquisitionTransfer({ api, projectId, acquisitionId, onProgress, onState, onDone, onError }) {
  const path = acquisitionPath(projectId, acquisitionId);
  let controller = null;
  let stopped = false;
  let pending = false;
  return {
    async start(file) {
      if (pending || stopped) return;
      pending = true;
      controller = new AbortController();
      const { signal } = controller;
      try {
        let acquisition = (await api.request(path, { signal })).acquisition;
        aborted(signal);
        if (acquisition.state !== 'receiving' || acquisition.cancel_requested) {
          onState?.(acquisition);
          throw new Error('This acquisition is no longer receiving bytes. Reload its saved status.');
        }
        checkedTransfer(acquisition, file);
        const manifest = await api.request(`${path}/chunks`, { signal });
        aborted(signal);
        let offset = await verifyReceivedPrefix(file, acquisition, manifest.chunks, { signal, onProgress });
        while (offset < file.size) {
          const size = Math.min(acquisition.chunk_bytes, file.size - offset);
          const body = await readChunk(file, offset, size, signal);
          const sha256 = await hashChunk(body, { signal });
          const next = (await api.upload(`${path}/content`, { offset, sha256, body, signal })).acquisition;
          aborted(signal);
          if (next.bytes_received !== offset + size || next.size_bytes !== file.size
            || next.state !== 'receiving' || next.cancel_requested) {
            throw new Error('The server did not acknowledge the expected chunk. Reload to resume safely.');
          }
          acquisition = next;
          offset = next.bytes_received;
          onState?.(next);
          onProgress?.({ phase: 'uploading', completed: offset, total: file.size });
        }
        const completed = (await api.request(`${path}/complete`, { method: 'POST', body: { schema_version: 1 }, signal })).acquisition;
        aborted(signal);
        onState?.(completed);
        onDone?.(completed);
      } catch (error) {
        if (!stopped && error.name !== 'AbortError') onError?.(error);
      } finally { pending = false; }
    },
    destroy() { stopped = true; controller?.abort(); },
  };
}

function statusMarkup(acquisition) {
  const style = acquisition.state === 'rejected' ? 'acquisition-status-error'
    : ['receiving', 'validating'].includes(acquisition.state) ? 'acquisition-status-active' : '';
  return `<span class="acquisition-status ${style}">${e(acquisitionStatus(acquisition))}</span>`;
}

function progressMarkup(acquisition) {
  const received = Number.isSafeInteger(acquisition.bytes_received) ? acquisition.bytes_received : 0;
  const size = Number.isSafeInteger(acquisition.size_bytes) && acquisition.size_bytes > 0 ? acquisition.size_bytes : 1;
  return `<div class="acquisition-progress"><progress value="${received}" max="${size}" aria-label="Archive upload progress"></progress><span>${formatBytes(received)} of ${formatBytes(acquisition.size_bytes)} received</span></div>`;
}

export function renderAcquisitionLimits(limits = {}, storage = {}) {
  const items = [];
  if (limits.upload_bytes) items.push(`Archives up to ${formatBytes(limits.upload_bytes)}`);
  if (limits.unpacked_bytes) items.push(`unpacked recordings up to ${formatBytes(limits.unpacked_bytes)}`);
  if (limits.prepared_bytes) items.push(`prepared data up to ${formatBytes(limits.prepared_bytes)}`);
  return `<p class="muted acquisition-limits">${e(items.join(' · '))}${items.length ? '.' : ''}</p>${Number.isSafeInteger(storage.reserved_bytes) && limits.account_bytes
    ? `<p class="muted">Account storage reserved: ${formatBytes(storage.reserved_bytes)} of ${formatBytes(limits.account_bytes)}. Space is reserved for the archive and preparation before transfer.</p>` : ''}
    ${limits.upload_inactivity_hours ? `<p class="muted">Interrupted uploads can resume for ${count(limits.upload_inactivity_hours)} hours after the last received chunk.</p>` : ''}`;
}

export function renderAcquisitionList(project, view = {}) {
  const acquisitions = view.acquisitions || [];
  const enabled = view.service?.enabled !== false;
  const atLimit = acquisitions.length >= (view.limits?.per_project ?? Infinity);
  return `<section class="acquisitions" aria-labelledby="acquisitions-heading">
    <div class="section-heading"><div><p class="eyebrow">Private to your account</p><h2 id="acquisitions-heading">Acquisitions</h2></div>${enabled && !atLimit ? '<button class="primary" type="button" data-acquisition-action="new">Upload recording</button>' : ''}</div>
    <p class="acquisition-intro">Keep your original recordings and prepared sensor data together in this project. Accepted recordings are ready for inspection; private-recording VIO execution is not available yet.</p>
    ${renderAcquisitionLimits(view.limits, view.storage)}
    ${!enabled ? '<p class="section-note">Recording uploads are disabled on this deployment.</p>' : ''}
    ${atLimit ? '<p class="section-note">This project has reached its saved acquisition limit.</p>' : ''}
    ${enabled && view.service?.worker_available === false ? '<p class="muted">Imports are waiting for the preparation service. Received bytes and status remain saved.</p>' : ''}
    ${acquisitions.length ? `<div class="acquisition-list">${acquisitions.map(item => `<a class="acquisition-row" href="${acquisitionRoute(project.id, item.id)}"><div><h3>${e(item.name)}</h3><p class="muted">${e(item.filename)} · ${formatBytes(item.size_bytes)}</p>${item.state === 'receiving' ? progressMarkup(item) : ''}</div>${statusMarkup(item)}<span aria-hidden="true">→</span></a>`).join('')}</div>`
      : '<div class="acquisition-empty"><h3>No private acquisitions yet</h3><p>Upload a Meridian recording package to retain its originals and inspect its metadata.</p></div>'}
    <p class="muted acquisition-guide">Have TUM-VI or DataFlash files? <a href="https://github.com/victormonnot/meridian/blob/main/docs/sensor-import.md" target="_blank" rel="noopener noreferrer">Prepare a recording package locally ↗</a></p>
    <p class="acquisition-feedback" role="status" data-acquisition-feedback></p>
  </section>`;
}

function detail(label, value) { return `<div><dt>${e(label)}</dt><dd>${value}</dd></div>`; }

function basicMetadata(descriptor) {
  if (!descriptor) return '';
  const streams = Array.isArray(descriptor.streams) ? descriptor.streams : [];
  return `<section class="acquisition-metadata" aria-labelledby="acquisition-metadata-heading"><h3 id="acquisition-metadata-heading">Recording metadata</h3>
    <dl class="details-list">${descriptor.rig ? detail('Rig', e(`${descriptor.rig.id} · ${descriptor.rig.revision}`)) : ''}
      ${descriptor.purpose ? detail('Declared purpose', e(descriptor.purpose)) : ''}
      ${detail('Independent reference', streams.some(stream => stream.kind === 'reference_pose') ? 'Included for evaluation only' : 'Not included')}
    </dl>${streams.length ? `<div class="acquisition-streams">${streams.map(stream => {
      const samples = stream.segments.reduce((sum, segment) => sum + segment.sample_count, 0);
      return `<div><strong>${e(stream.id)}</strong><span>${e(stream.kind?.replaceAll('_', ' '))}</span><span>${count(samples)} ${samples === 1 ? 'sample' : 'samples'}</span><span>${count(stream.segments.length)} ${stream.segments.length === 1 ? 'segment' : 'segments'}</span></div>`;
    }).join('')}</div>` : ''}
    <p class="muted">Counts and declared metadata do not establish calibration accuracy, synchronization quality or estimator readiness.</p></section>`;
}

export function renderAcquisitionDetail(project, view) {
  const acquisition = view.acquisition;
  const path = acquisitionPath(project.id, acquisition.id);
  const active = ['receiving', 'validating'].includes(acquisition.state);
  const ready = acquisition.state === 'ready' && acquisition.files_available;
  const deleted = acquisition.state === 'ready' && acquisition.availability === 'deleted';
  return `<section class="acquisitions acquisition-detail" aria-labelledby="acquisition-heading">
    <a class="back-link" href="${acquisitionRoute(project.id)}">← All acquisitions</a>
    <div class="section-heading"><div><p class="eyebrow">Private acquisition</p><h2 id="acquisition-heading">${e(acquisition.name)}</h2></div>${statusMarkup(acquisition)}</div>
    <p class="muted">${e(acquisition.filename)} · ${formatBytes(acquisition.size_bytes)}</p>
    <div data-acquisition-status>
      ${acquisition.state === 'receiving' ? `${progressMarkup(acquisition)}<p>Reselect the same archive to continue. Every received chunk is checked before any remaining bytes are sent.</p>${!acquisition.cancel_requested ? '<form data-acquisition-resume><label>Archive to resume<input type="file" name="archive" accept=".tar,.tar.gz,.tgz,.zip" required></label><button type="submit" class="primary">Resume upload</button></form>' : ''}` : ''}
      ${acquisition.state === 'validating' ? '<p>The archive is being checked and its sensor data prepared. You can leave this page; the saved status survives refresh.</p>' : ''}
      ${ready ? '<p>The recording passed import checks and is ready for inspection. Calibration validity and estimator readiness have not been assessed. Private-recording VIO execution is not available yet.</p>' : ''}
      ${deleted ? '<p>This acquisition was deleted. Its saved identity remains in the project, while hosted files are unavailable. Copies already downloaded remain usable.</p>' : ''}
      ${acquisition.state === 'cancelled' ? '<p>This upload was cancelled. Start a new acquisition to import the recording again.</p>' : ''}
      ${acquisition.failure ? `<div class="acquisition-failure" role="alert"><p>${e(acquisition.failure.message || 'The recording could not be imported.')}</p><p class="mono">${e(acquisition.failure.code || '')}</p></div>` : ''}
      ${acquisition.state === 'rejected' ? '<p>Correct the reported issue and upload a new package. Your local original remains unchanged.</p>' : ''}
      ${acquisition.cancel_requested && active ? '<p>Cancellation is pending. Files remain unavailable while the service stops validation.</p>' : ''}
      ${acquisition.state === 'ready' && !acquisition.files_available && !deleted ? '<p class="section-note">The retained files are unavailable. This acquisition cannot be downloaded or reused.</p>' : ''}
    </div>
    <p class="acquisition-feedback" role="status" data-acquisition-feedback></p>
    <button type="button" data-acquisition-action="pause" hidden>Pause transfer</button>
    <dl class="details-list acquisition-identity">
      ${detail('Acquisition ID', `<span class="mono">${e(acquisition.id)}</span>`)}
      ${detail('Original SHA-256', acquisition.sha256 ? `<span class="mono">${e(acquisition.sha256)}</span>` : 'Available after the complete archive has been verified')}
      ${acquisition.expires_at && acquisition.state === 'receiving' ? detail('Resume before', e(new Date(acquisition.expires_at).toLocaleString('en'))) : ''}
    </dl>
    ${ready ? `<div class="acquisition-downloads"><a class="button primary" href="${acquisitionRoute(project.id, acquisition.id)}/inspection">Inspect sensor data</a><a class="button" href="/api/v1${path}/original" download>Download original archive</a><button type="button" data-acquisition-action="descriptor">Download descriptor</button><button type="button" data-acquisition-action="report">Download import report</button></div>` : ''}
    ${ready ? basicMetadata(view.descriptor) : ''}
    <div class="acquisition-actions">${active && !acquisition.cancel_requested ? '<button type="button" class="danger" data-acquisition-action="cancel">Cancel acquisition</button>' : ''}
      ${!active && !deleted ? '<button type="button" class="danger" data-acquisition-action="delete">Delete acquisition</button>' : ''}
      <button type="button" data-acquisition-action="refresh">Refresh status</button></div>
  </section>`;
}

function uploadDialog() {
  return `<dialog class="acquisition-dialog" aria-labelledby="upload-heading"><h2 id="upload-heading">Upload a private recording</h2>
    <p>Choose a Meridian recording package (.tar, .tar.gz or .zip). Its original archive is retained in your account.</p>
    <form data-acquisition-create><fieldset>
      <label>Acquisition name<input name="name" maxlength="100" required autocomplete="off"></label>
      <label>Recording archive<input type="file" name="archive" accept=".tar,.tar.gz,.tgz,.zip" required></label>
      <details><summary>Preparation options</summary><label>IMU stream ID <span class="muted">Required only when the package contains multiple IMUs.</span><input name="imu_stream_id" maxlength="80" autocomplete="off" placeholder="For example: imu0"></label><label class="acquisition-checkbox"><input type="checkbox" name="include_reference" checked>Include the independent reference in prepared data</label></details>
      <button type="submit" class="primary">Start upload</button>
    </fieldset></form>
    <p class="acquisition-feedback" role="status" data-upload-feedback></p>
    <div class="form-actions"><button type="button" data-acquisition-action="close-upload">Close</button><button type="button" data-acquisition-action="pause" hidden>Pause transfer</button></div>
  </dialog>`;
}

function actionDialog(acquisition, action) {
  const cancel = action === 'cancel';
  return `<dialog class="acquisition-dialog" aria-labelledby="acquisition-action-heading"><h2 id="acquisition-action-heading">${cancel ? 'Cancel' : 'Delete'} ${e(acquisition.name)}?</h2>
    <p>${cancel ? 'Stop this transfer or validation. Importing this recording later requires a new acquisition.' : 'Remove hosted access to this recording, its original archive and prepared files. Physical cleanup follows asynchronously; backup copies follow the retention policy. Archives already downloaded remain usable.'}</p>
    <form data-acquisition-confirm="${action}"><fieldset><p class="form-error" role="alert"></p><div class="form-actions"><button type="button" data-acquisition-action="close-action">Keep acquisition</button><button type="submit" class="danger">${cancel ? 'Cancel acquisition' : 'Delete acquisition'}</button></div></fieldset></form></dialog>`;
}

export function mountAcquisitions(container, { api, project, view, onNavigate, onSessionExpired }) {
  if (!container) return null;
  let live = true;
  let current = view.acquisition || null;
  let transfer = null;
  let timer = null;
  let createAttempt = null;
  let createController = null;
  let transferring = false;
  const controller = new AbortController();
  const feedback = () => container.querySelector('[data-upload-feedback]') || container.querySelector('[data-acquisition-feedback]');
  const reportError = error => {
    if (!live || error.name === 'AbortError') return;
    if (error.status === 401) { void onSessionExpired(); return; }
    const target = feedback();
    if (target) {
      target.classList.add('error');
      target.textContent = error instanceof ApiError || error instanceof TypeError ? errorMessage(error) : error.message;
    }
  };
  function setBusy(busy) {
    transferring = busy;
    container.querySelectorAll('[data-acquisition-create] fieldset, [data-acquisition-resume] input, [data-acquisition-resume] button').forEach(element => { element.disabled = busy; });
    container.querySelectorAll('[data-acquisition-action="pause"]').forEach(button => { button.hidden = !busy; });
  }
  function stopTransfer() {
    createController?.abort();
    createController = null;
    transfer?.destroy();
    transfer = null;
    setBusy(false);
  }
  function refresh() {
    if (live) onNavigate(current?.id, true);
  }
  function startTransfer(file, acquisition) {
    stopTransfer();
    current = acquisition;
    setBusy(true);
    const target = feedback();
    if (target) { target.classList.remove('error'); target.textContent = 'Checking saved upload progress…'; }
    transfer = createAcquisitionTransfer({
      api, projectId: project.id, acquisitionId: acquisition.id,
      onState(value) { if (live) current = value; },
      onProgress(value) {
        if (!live) return;
        const element = feedback();
        if (element) element.textContent = `${value.phase === 'verifying' ? 'Checking received bytes' : 'Uploading'}: ${formatBytes(value.completed)} of ${formatBytes(value.total)}.${value.phase === 'uploading' ? ' Progress is saved on the server.' : ''}`;
      },
      onDone() { if (live) { setBusy(false); onNavigate(current.id); } },
      onError(error) { if (live) { setBusy(false); reportError(error); } },
    });
    void transfer.start(file);
  }
  async function poll() {
    if (!live) return;
    try {
      if (!transferring && document.visibilityState !== 'hidden') {
        const path = acquisitionPath(project.id, view.acquisition?.id);
        const next = await api.request(path, { signal: controller.signal });
        if (!live) return;
        const before = view.acquisition ? { state: view.acquisition.state, cancel_requested: view.acquisition.cancel_requested, bytes_received: view.acquisition.bytes_received, files_available: view.acquisition.files_available }
          : view.acquisitions.map(item => [item.id, item.state, item.bytes_received, item.cancel_requested]);
        const after = view.acquisition ? { state: next.acquisition.state, cancel_requested: next.acquisition.cancel_requested, bytes_received: next.acquisition.bytes_received, files_available: next.acquisition.files_available }
          : next.acquisitions.map(item => [item.id, item.state, item.bytes_received, item.cancel_requested]);
        if (JSON.stringify(before) !== JSON.stringify(after) && !container.querySelector('dialog[open]')) { refresh(); return; }
      }
    } catch (error) { reportError(error); }
    if (live) timer = setTimeout(poll, 2500);
  }
  const submit = async event => {
    const form = event.target;
    if (!form.matches('[data-acquisition-create], [data-acquisition-resume], [data-acquisition-confirm]')) return;
    event.preventDefault();
    event.stopPropagation();
    try {
      if (form.matches('[data-acquisition-confirm]')) {
        form.querySelector('fieldset').disabled = true;
        const action = form.dataset.acquisitionConfirm;
        const path = acquisitionPath(project.id, current.id);
        await api.request(action === 'cancel' ? `${path}/cancel` : path, { method: action === 'cancel' ? 'POST' : 'DELETE',
          ...(action === 'cancel' ? { body: { schema_version: 1 } } : {}), signal: controller.signal });
        if (!live) return;
        onNavigate(action === 'delete' ? undefined : current.id, true);
      } else {
        const file = form.querySelector('input[type="file"]').files[0];
        if (!file || transferring) return;
        if (form.matches('[data-acquisition-resume]')) { startTransfer(file, current); return; }
        const data = new FormData(form);
        if (view.limits?.upload_bytes && file.size > view.limits.upload_bytes) {
          throw new Error(`The archive exceeds the ${formatBytes(view.limits.upload_bytes)} upload limit.`);
        }
        const values = { schema_version: 1, name: data.get('name').trim(), filename: file.name, size_bytes: file.size,
          include_reference: data.get('include_reference') === 'on',
          ...(data.get('imu_stream_id').trim() ? { imu_stream_id: data.get('imu_stream_id').trim() } : {}) };
        if (!createAttempt || createAttempt.file !== file || JSON.stringify(createAttempt.values) !== JSON.stringify(values)) {
          createAttempt = { file, values, key: globalThis.crypto.randomUUID() };
        }
        setBusy(true);
        createController = new AbortController();
        const creationSignal = createController.signal;
        const next = await api.request(acquisitionPath(project.id), { method: 'POST', body: { ...values, idempotency_key: createAttempt.key }, signal: creationSignal });
        aborted(creationSignal);
        if (live) startTransfer(file, next.acquisition);
      }
    } catch (error) {
      if (!live) return;
      setBusy(false);
      const fieldset = form.querySelector('fieldset');
      if (fieldset) fieldset.disabled = false;
      const local = form.querySelector('.form-error');
      if (local && error.status !== 401) local.textContent = errorMessage(error);
      else reportError(error);
    }
  };
  const click = async event => {
    const button = event.target.closest('[data-acquisition-action]');
    if (!button || !container.contains(button)) return;
    const action = button.dataset.acquisitionAction;
    if (action === 'descriptor' || action === 'report') {
      button.disabled = true;
      try {
        const result = await api.request(`${acquisitionPath(project.id, current.id)}/${action}`, { signal: controller.signal });
        if (!live) return;
        const downloaded = action === 'descriptor' ? result.descriptor : result.report;
        if (!downloaded || typeof downloaded !== 'object') throw new ApiError(502, null);
        const blob = new Blob([JSON.stringify(downloaded, null, 2) + '\n'], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const link = globalThis.document.createElement('a');
        link.href = url;
        link.download = action === 'descriptor' ? 'acquisition.json' : 'import-report.json';
        globalThis.document.body.append(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      } catch (error) { reportError(error); }
      finally { if (live) button.disabled = false; }
    } else if (action === 'new') {
      container.insertAdjacentHTML('beforeend', uploadDialog());
      container.querySelector('dialog').showModal();
    } else if (action === 'close-upload') {
      stopTransfer();
      container.querySelector('dialog')?.remove();
      onNavigate(current?.id, true);
    } else if (action === 'pause') {
      stopTransfer();
      const target = feedback();
      if (target) target.textContent = 'Transfer paused. Received bytes are retained; reselect the same archive to resume.';
      if (current) onNavigate(current.id);
    } else if (action === 'refresh') refresh();
    else if (action === 'close-action') container.querySelector('dialog')?.remove();
    else if (action === 'cancel' || action === 'delete') {
      stopTransfer();
      container.insertAdjacentHTML('beforeend', actionDialog(current, action));
      container.querySelector('dialog').showModal();
    }
  };
  const close = event => {
    if (!event.target.matches('dialog')) return;
    stopTransfer();
    event.target.remove();
    if (current && !view.acquisition) onNavigate(current.id);
  };
  container.addEventListener('submit', submit);
  container.addEventListener('click', click);
  container.addEventListener('close', close, true);
  if (view.acquisition ? ['receiving', 'validating'].includes(view.acquisition.state)
    : view.acquisitions?.some(item => ['receiving', 'validating'].includes(item.state))) timer = setTimeout(poll, 2500);
  return { destroy() {
    live = false;
    controller.abort();
    createController?.abort();
    clearTimeout(timer);
    transfer?.destroy();
    container.removeEventListener('submit', submit);
    container.removeEventListener('click', click);
    container.removeEventListener('close', close, true);
    container.querySelectorAll('dialog').forEach(dialog => dialog.remove());
  } };
}
