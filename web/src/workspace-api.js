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
      if (query && Object.entries(query).some(([key, value]) => ['start_ns', 'end_ns'].includes(key)
        ? typeof value !== 'string' || !/^(0|[1-9]\d{0,19})$/.test(value)
        : !Number.isSafeInteger(value) || value < 1)) throw new TypeError('Invalid query selector');
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
  };
}

export function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
}

export function parseRoute(hash) {
  if (!hash || hash === '#' || hash === '#projects') return { page: 'projects' };
  const uuid = '[a-f\\d]{8}-[a-f\\d]{4}-[a-f\\d]{4}-[a-f\\d]{4}-[a-f\\d]{12}';
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
