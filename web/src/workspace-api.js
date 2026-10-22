// Session credentials stay in HttpOnly cookies; only the CSRF token is held here.
export class ApiError extends Error {
  constructor(status, payload) {
    super(payload?.error?.message || (status === 401 ? 'Your session has expired. Sign in again.' : 'The request could not be completed.'));
    this.name = 'ApiError';
    this.status = status;
    this.code = payload?.error?.code || 'request_failed';
    this.fields = payload?.error?.fields || {};
  }
}

export function createApiClient(fetchImpl = globalThis.fetch) {
  let csrfToken = '';
  const checkPath = path => {
    if (!path.startsWith('/') || path.startsWith('//') || /[\\?#]/.test(path)) throw new TypeError('Invalid API path');
  };
  return {
    setCsrfToken(value) { csrfToken = typeof value === 'string' ? value : ''; },
    async request(path, { method = 'GET', body, signal, query } = {}) {
      // API paths are application-owned. Never send session state to another origin.
      checkPath(path);
      if (query && Object.entries(query).some(([key, value]) => {
        if (['first_ns', 'last_ns'].includes(key)) return typeof value !== 'string'
          || !/^(0|-?[1-9]\d{0,18})$/.test(value)
          || BigInt(value) < -(1n << 63n) || BigInt(value) > (1n << 63n) - 1n;
        if (['stream_id', 'segment_id'].includes(key)) return typeof value !== 'string'
          || !/^[A-Za-z][A-Za-z0-9._-]{0,79}$/.test(value);
        if (['start_ns', 'end_ns'].includes(key)) return typeof value !== 'string'
          || !/^(0|[1-9]\d{0,19})$/.test(value);
        return !Number.isSafeInteger(value) || value < 1;
      })) throw new TypeError('Invalid query selector');
      const headers = { Accept: 'application/json' };
      if (method !== 'GET') {
        headers['Content-Type'] = 'application/json';
        headers['X-CSRFToken'] = csrfToken;
      }
      const suffix = query ? `?${new URLSearchParams(query)}` : '';
      const response = await fetchImpl(`/api/v1${path}${suffix}`, {
        method, headers, credentials: 'same-origin', cache: 'no-store', signal,
        ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
      });
      let payload = null;
      if (response.status !== 204) {
        try { payload = await response.json(); } catch {
          throw new ApiError(response.ok ? 502 : response.status, null);
        }
      }
      if (!response.ok) throw new ApiError(response.status, payload);
      if (response.status !== 204 && payload?.schema_version !== 1) throw new ApiError(502, null);
      return payload;
    },
    async upload(path, { offset, sha256, body, signal } = {}) {
      checkPath(path);
      if (!Number.isSafeInteger(offset) || offset < 0 || !/^[a-f\d]{64}$/.test(sha256 || '')
        || !(body instanceof ArrayBuffer) || body.byteLength < 1 || body.byteLength > 4 * 1024 * 1024) {
        throw new TypeError('Invalid upload chunk');
      }
      const response = await fetchImpl(`/api/v1${path}`, {
        method: 'PUT', credentials: 'same-origin', cache: 'no-store', signal, body,
        headers: { Accept: 'application/json', 'Content-Type': 'application/octet-stream',
          'X-CSRFToken': csrfToken, 'Upload-Offset': String(offset), 'X-Chunk-SHA256': sha256 },
      });
      let payload;
      try { payload = await response.json(); } catch { throw new ApiError(response.ok ? 502 : response.status, null); }
      if (!response.ok) throw new ApiError(response.status, payload);
      if (payload?.schema_version !== 1) throw new ApiError(502, null);
      return payload;
    },
    async image(path, { signal } = {}) {
      checkPath(path);
      const response = await fetchImpl(`/api/v1${path}`, { credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'image/png' }, signal });
      if (!response.ok) {
        let payload;
        try { payload = await response.json(); } catch { /* Proxy errors need not be JSON. */ }
        throw new ApiError(response.status, payload);
      }
      if (response.headers.get('Content-Type')?.split(';')[0] !== 'image/png') throw new ApiError(502, null);
      return response.blob();
    },
    async download(path, { signal } = {}) {
      checkPath(path);
      const response = await fetchImpl(`/api/v1${path}?schema_version=1`, {
        credentials: 'same-origin', cache: 'no-store',
        headers: { Accept: 'application/zip' }, signal,
      });
      if (!response.ok) {
        let payload;
        try { payload = await response.json(); } catch { /* Proxy errors need not be JSON. */ }
        throw new ApiError(response.status, payload);
      }
      const disposition = /^attachment; filename="(meridian-run-[a-f\d]{8}(?:-[a-f\d]{4}){3}-[a-f\d]{12}-v1\.zip)"$/.exec(response.headers.get('Content-Disposition') || '');
      const sha256 = response.headers.get('X-Content-SHA256');
      const length = response.headers.get('Content-Length');
      if (response.headers.get('Content-Type')?.split(';')[0] !== 'application/zip'
        || !disposition || !/^[a-f\d]{64}$/.test(sha256 || '')
        || !/^[1-9]\d*$/.test(length || '') || !Number.isSafeInteger(Number(length))
        || Number(length) > 16 * 1024 * 1024) {
        throw new ApiError(502, { error: { message: 'The export response could not be verified. Try again.' } });
      }
      const blob = await response.blob();
      if (signal?.aborted) throw new DOMException('Request aborted', 'AbortError');
      if (blob.size > 16 * 1024 * 1024 || blob.size !== Number(length)) {
        throw new ApiError(502, { error: { message: 'The export download failed its integrity check. Try again.' } });
      }
      const digest = await globalThis.crypto.subtle.digest('SHA-256', await blob.arrayBuffer());
      const actual = Array.from(new Uint8Array(digest), value => value.toString(16).padStart(2, '0')).join('');
      if (signal?.aborted) throw new DOMException('Request aborted', 'AbortError');
      if (actual !== sha256) {
        throw new ApiError(502, { error: { message: 'The export download failed its integrity check. Try again.' } });
      }
      return { blob, filename: disposition[1], sha256, bytes: blob.size };
    },
  };
}

export function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
}

export function parseRoute(hash) {
  if (!hash || hash === '#' || hash === '#projects') return { page: 'projects' };
  const uuid = '[a-f\\d]{8}-[a-f\\d]{4}-[a-f\\d]{4}-[a-f\\d]{4}-[a-f\\d]{12}';
  const calibration = new RegExp(`^#project/(${uuid})/data/acquisitions/(${uuid})/calibration(?:/revisions/(${uuid}))?$`, 'i').exec(hash);
  if (calibration) return { page: 'project', id: calibration[1], section: 'data', acquisitionId: calibration[2],
    calibration: true, ...(calibration[3] ? { inputRevisionId: calibration[3] } : {}) };
  const acquisition = new RegExp(`^#project/(${uuid})/data/acquisitions/(${uuid})(/inspection)?$`, 'i').exec(hash);
  if (acquisition) return { page: 'project', id: acquisition[1], section: 'data', acquisitionId: acquisition[2],
    ...(acquisition[3] ? { inspection: true } : {}) };
  const comparison = new RegExp(`^#project/(${uuid})/results/comparisons/(${uuid})$`, 'i').exec(hash);
  if (comparison) return { page: 'project', id: comparison[1], section: 'results', comparisonId: comparison[2] };
  const job = new RegExp(`^#project/(${uuid})/results/(${uuid})(/investigation)?$`, 'i').exec(hash);
  if (job) return { page: 'project', id: job[1], section: 'results', jobId: job[2], ...(job[3] ? { investigation: true } : {}) };
  const experiment = new RegExp(`^#project/(${uuid})/experiments/(${uuid})(?:/revisions/([1-9]\\d*))?$`, 'i').exec(hash);
  if (experiment) {
    const version = experiment[3] === undefined ? undefined : Number(experiment[3]);
    if (version !== undefined && !Number.isSafeInteger(version)) return { page: 'not-found' };
    return { page: 'project', id: experiment[1], section: 'experiments', experimentId: experiment[2], ...(version === undefined ? {} : { version }) };
  }
  const match = new RegExp(`^#project/(${uuid})/(data|experiments|results|settings)$`, 'i').exec(hash);
  return match ? { page: 'project', id: match[1], section: match[2] } : { page: 'not-found' };
}

export function errorMessage(error) {
  if (!(error instanceof ApiError)) return 'The service could not be reached. Check your connection and try again.';
  const fields = Object.entries(error.fields).flatMap(([field, messages]) =>
    (Array.isArray(messages) ? messages : [messages]).map(message => `${field.replaceAll('_', ' ')}: ${message}`));
  return [error.message, ...fields].join(' ');
}

export function safeExternalLink(value) {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' ? url.href : null;
  } catch { return null; }
}
