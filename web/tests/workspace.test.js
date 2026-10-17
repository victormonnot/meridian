import assert from 'node:assert/strict';
import test from 'node:test';
import { ApiError, createApiClient, errorMessage, escapeHtml, parseRoute, safeExternalLink } from '../src/workspace-api.js';

function response(payload, status = 200) {
  return new Response(status === 204 ? null : JSON.stringify(payload), {
    status, headers: { 'Content-Type': 'application/json' },
  });
}

test('workspace requests use same-origin cookies and never cache private data', async () => {
  const calls = [];
  const client = createApiClient(async (...args) => { calls.push(args); return response({ schema_version: 1, projects: [] }); });
  const signal = new AbortController().signal;
  assert.deepEqual(await client.request('/projects', { signal }), { schema_version: 1, projects: [] });
  assert.equal(calls[0][0], '/api/v1/projects');
  assert.equal(calls[0][1].credentials, 'same-origin');
  assert.equal(calls[0][1].cache, 'no-store');
  assert.equal(calls[0][1].signal, signal);
  assert.equal(calls[0][1].headers['X-CSRFToken'], undefined);
});

test('mutations use the current CSRF token after session rotation', async () => {
  const calls = [];
  const client = createApiClient(async (...args) => { calls.push(args); return response({ schema_version: 1 }); });
  client.setCsrfToken('before-login');
  await client.request('/auth/login', { method: 'POST', body: { username: 'engineer', password: 'example password' } });
  client.setCsrfToken('after-login');
  await client.request('/projects', { method: 'POST', body: { name: '<private project>' } });
  client.setCsrfToken(null);
  await client.request('/auth/logout', { method: 'POST', body: {} });
  assert.deepEqual(calls.map(([, options]) => options.headers['X-CSRFToken']), ['before-login', 'after-login', '']);
  assert.deepEqual(JSON.parse(calls[1][1].body), { name: '<private project>' });
  assert.equal(calls[1][1].headers['Content-Type'], 'application/json');
});

test('API client rejects external and ambiguous paths before sending credentials', async () => {
  let calls = 0;
  const client = createApiClient(async () => { calls += 1; });
  for (const path of ['https://example.com', '//example.com', '/\\example.com', 'projects', '/projects?redirect=other', '/projects#secret']) {
    await assert.rejects(client.request(path), TypeError);
  }
  assert.equal(calls, 0);
});

test('comparison selectors are encoded on the existing same-origin API path', async () => {
  let calledUrl;
  const client = createApiClient(async url => { calledUrl = url; return response({ schema_version: 1, changes: [] }); });
  await client.request('/projects/id/experiments/id/compare', { query: { from: 1, to: 7 } });
  assert.equal(calledUrl, '/api/v1/projects/id/experiments/id/compare?from=1&to=7');
  for (const value of [0, -1, 1.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1, 'https://other.example/', {}, null]) {
    await assert.rejects(client.request('/projects/id/experiments/id/compare', { query: { from: value, to: 1 } }), TypeError);
  }
});

test('API errors retain status and validation fields for honest inline feedback', async () => {
  const payload = { schema_version: 1, error: { code: 'validation_error', message: 'Invalid project.', fields: { name: ['This field is required.'] } } };
  const client = createApiClient(async () => response(payload, 400));
  await assert.rejects(client.request('/projects'), error => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.status, 400);
    assert.equal(error.code, 'validation_error');
    assert.equal(errorMessage(error), 'Invalid project. name: This field is required.');
    return true;
  });
});

test('session expiry and CSRF rejection stay distinguishable even with HTML error responses', async () => {
  for (const status of [401, 403]) {
    const client = createApiClient(async () => new Response('<html>Denied</html>', { status }));
    await assert.rejects(client.request('/projects'), error => error instanceof ApiError && error.status === status);
  }
});

test('unexpected successful response schema or HTML is an actionable service error', async () => {
  for (const makeResponse of [() => response({ schema_version: 2 }), () => new Response('<html>Proxy page</html>')]) {
    const client = createApiClient(async () => makeResponse());
    await assert.rejects(client.request('/session'), error => error instanceof ApiError && error.status === 502);
  }
});

test('empty deletion responses are supported without parsing a nonexistent body', async () => {
  const client = createApiClient(async () => response(null, 204));
  assert.equal(await client.request('/projects/id', { method: 'DELETE' }), null);
});

test('transport errors and aborted navigation are not disguised as API authorization responses', async () => {
  const offline = new TypeError('Failed to fetch');
  const client = createApiClient(async () => { throw offline; });
  await assert.rejects(client.request('/session'), error => error === offline);
  assert.match(errorMessage(offline), /connection/);
  const aborted = new DOMException('Request aborted', 'AbortError');
  const abortClient = createApiClient(async () => { throw aborted; });
  await assert.rejects(abortClient.request('/projects'), error => error.name === 'AbortError');
});

test('project deep links accept only UUID identifiers and available sections', () => {
  const id = '12345678-abcd-abcd-abcd-123456789abc';
  for (const section of ['data', 'experiments', 'results', 'settings']) {
    assert.deepEqual(parseRoute(`#project/${id}/${section}`), { page: 'project', id, section });
  }
  for (const hash of ['', '#', '#projects']) assert.deepEqual(parseRoute(hash), { page: 'projects' });
  for (const hash of ['#project/../../secret/data', '#project/<script>/data', `#project/${id}/run`, `#project/${id}/data?owner=someone`, '#unknown']) {
    assert.deepEqual(parseRoute(hash), { page: 'not-found' });
  }
});

test('experiment and immutable revision deep links preserve exact identities', () => {
  const id = '12345678-abcd-abcd-abcd-123456789abc';
  const experimentId = '87654321-abcd-abcd-abcd-987654321abc';
  const base = `#project/${id}/experiments/${experimentId}`;
  assert.deepEqual(parseRoute(base), { page: 'project', id, section: 'experiments', experimentId });
  assert.deepEqual(parseRoute(`${base}/revisions/3`), { page: 'project', id, section: 'experiments', experimentId, version: 3 });
  for (const suffix of ['/revisions/0', '/revisions/-1', '/revisions/01', '/revisions/1.5', '/revisions/9007199254740992', '/revisions/3/extra', '/run', '?version=1']) {
    assert.deepEqual(parseRoute(base + suffix), { page: 'not-found' });
  }
});

test('user-supplied project text is escaped in both markup and attribute contexts', () => {
  assert.equal(escapeHtml('<img src=x onerror="alert(1)"> & \'name\''), '&lt;img src=x onerror=&quot;alert(1)&quot;&gt; &amp; &#39;name&#39;');
  assert.equal(escapeHtml(null), '');
  assert.equal(escapeHtml('line one\nline two'), 'line one\nline two');
});

test('catalog external references only allow absolute HTTPS URLs', () => {
  assert.equal(safeExternalLink('https://cvg.cit.tum.de/data/datasets/visual-inertial-dataset'), 'https://cvg.cit.tum.de/data/datasets/visual-inertial-dataset');
  for (const value of ['javascript:alert(1)', 'data:text/html,test', '//example.org', '/results/file', 'Repository root', 'http://example.org']) {
    assert.equal(safeExternalLink(value), null);
  }
});
