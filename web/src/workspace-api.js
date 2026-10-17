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
  return {
    setCsrfToken(value) { csrfToken = typeof value === 'string' ? value : ''; },
    async request(path, { method = 'GET', body, signal, query } = {}) {
      // API paths are application-owned. Never send session state to another origin.
      if (!path.startsWith('/') || path.startsWith('//') || /[\\?#]/.test(path)) throw new TypeError('Invalid API path');
      if (query && Object.values(query).some(value => !Number.isSafeInteger(value) || value < 1)) throw new TypeError('Query selectors must be positive integers');
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
  };
}

export function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
}

export function parseRoute(hash) {
  if (!hash || hash === '#' || hash === '#projects') return { page: 'projects' };
  const uuid = '[a-f\\d]{8}-[a-f\\d]{4}-[a-f\\d]{4}-[a-f\\d]{4}-[a-f\\d]{12}';
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
