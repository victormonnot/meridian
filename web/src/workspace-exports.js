import { escapeHtml as e, errorMessage } from './workspace-api.js';

export function renderRunExport(job) {
  const available = job.state === 'succeeded' && job.export_available === true;
  const reason = job.export_unavailable_reason || (['queued', 'running', 'evaluating'].includes(job.state)
    ? 'The reproduction archive becomes available after a successful run with complete retained evidence.'
    : job.state === 'cancelled' ? 'Cancelled runs have no complete reproduction archive.'
      : job.state === 'failed' ? 'This failed run has no complete reproduction archive.'
        : 'The complete evidence needed for reproduction is not available.');
  return `<section class="job-export" aria-labelledby="job-export-heading"><div class="section-heading"><div><p class="eyebrow">Take this experiment with you</p><h3 id="job-export-heading">Independent reproduction</h3></div>${available ? '<button type="button" data-action="download-export">Download experiment</button>' : ''}</div>
    ${available ? '<p>The archive contains this exact configuration, estimates, evaluation evidence, source, checksums and reproduction instructions.</p><p class="muted">Reproduction runs on your own Linux machine with Docker and the public TUM-VI recording. Download the recording and build the runtime separately using the included instructions; no Meridian account is needed.</p><p class="muted">Save the archive before the retained files expire. Deleting this run revokes future hosted access; archives you already saved are unaffected.</p>'
      : `<p class="muted export-unavailable">${e(reason)}</p>`}
    <p class="job-feedback" id="export-status" role="status"></p></section>`;
}

// A late private response must never start a download after navigation or logout.
export function createExportDownload({ read, save, onStart, onSuccess, onError, onFinish }) {
  const controller = new AbortController();
  let stopped = false;
  let pending = false;
  return {
    async start() {
      if (stopped || pending) return;
      pending = true;
      onStart?.();
      try {
        const archive = await read(controller.signal);
        if (stopped) return;
        save(archive);
        onSuccess?.(archive);
      } catch (error) {
        if (!stopped && error.name !== 'AbortError') onError?.(error);
      } finally {
        pending = false;
        if (!stopped) onFinish?.();
      }
    },
    destroy() { stopped = true; controller.abort(); },
  };
}

export function mountRunExport(container, { api, job, onSessionExpired }) {
  const button = container?.querySelector('[data-action="download-export"]');
  if (!button) return null;
  const status = container.querySelector('#export-status');
  const downloader = createExportDownload({
    read: signal => api.download(`/projects/${job.project_id}/jobs/${job.id}/export`, { signal }),
    save({ blob, filename }) {
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = filename;
      link.hidden = true;
      document.body.append(link);
      link.click();
      link.remove();
      // Give the browser time to consume the attachment before releasing memory.
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    },
    onStart() {
      button.disabled = true;
      status.classList.remove('error');
      status.textContent = 'Preparing and verifying the reproduction archive…';
    },
    onSuccess({ bytes, sha256 }) {
      status.textContent = `Archive verified; download requested (${new Intl.NumberFormat('en').format(bytes)} bytes). SHA-256: ${sha256}`;
    },
    onError(error) {
      if (error.status === 401) { void onSessionExpired(); return; }
      status.classList.add('error');
      status.textContent = errorMessage(error);
    },
    onFinish() { button.disabled = false; },
  });
  const start = () => { void downloader.start(); };
  button.addEventListener('click', start);
  return { destroy() { downloader.destroy(); button.removeEventListener('click', start); } };
}
