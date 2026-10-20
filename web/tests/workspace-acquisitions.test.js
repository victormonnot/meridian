import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import test from 'node:test';
import { ApiError, createApiClient, parseRoute } from '../src/workspace-api.js';
import { acquisitionPath, acquisitionRoute, acquisitionStatus, createAcquisitionTransfer,
  formatBytes, hashChunk, renderAcquisitionDetail, renderAcquisitionLimits,
  renderAcquisitionList, verifyReceivedPrefix } from '../src/workspace-acquisitions.js';

const projectId = '12345678-abcd-abcd-abcd-123456789abc';
const acquisitionId = '87654321-abcd-abcd-abcd-987654321abc';
const project = { id: projectId };
const base = { id: acquisitionId, project_id: projectId, name: 'Bench recording', filename: 'sensors.zip',
  state: 'receiving', phase: 'receiving', size_bytes: 6, bytes_received: 0, chunk_bytes: 4,
  cancel_requested: false, files_available: false, sha256: null, failure: null };
const digest = bytes => createHash('sha256').update(bytes).digest('hex');
const file = () => new File(['abcdef'], 'sensors.zip');
const chunk = { offset: 0, size_bytes: 4, sha256: digest('abcd') };

test('acquisition links retain project scope and reject ambiguous identities', () => {
  assert.equal(acquisitionPath(projectId, acquisitionId), `/projects/${projectId}/acquisitions/${acquisitionId}`);
  const route = acquisitionRoute(projectId, acquisitionId);
  assert.deepEqual(parseRoute(route), { page: 'project', id: projectId, section: 'data', acquisitionId });
  for (const suffix of ['/extra', '?owner=someone', '/..', '#secret']) assert.deepEqual(parseRoute(route + suffix), { page: 'not-found' });
  for (const invalid of ['', '../secret', '<script>', 'https://other.example']) assert.throws(() => acquisitionPath(projectId, invalid), TypeError);
});

test('states distinguish stored progress, physical readiness and deleted availability', () => {
  assert.equal(acquisitionStatus(base), 'Receiving');
  assert.equal(acquisitionStatus({ ...base, state: 'validating', cancel_requested: true }), 'Cancellation requested');
  assert.equal(acquisitionStatus({ ...base, state: 'ready', files_available: true }), 'Ready to inspect');
  assert.equal(acquisitionStatus({ ...base, state: 'ready' }), 'Files unavailable');
  assert.equal(acquisitionStatus({ ...base, state: 'ready', availability: 'deleted' }), 'Deleted');
  assert.equal(acquisitionStatus({ ...base, state: 'cancelled', cancel_requested: true }), 'Cancelled');
  assert.equal(acquisitionStatus({ ...base, state: 'cancelled', availability: 'deleted' }), 'Cancelled');
  assert.equal(acquisitionStatus({ ...base, state: 'rejected', availability: 'deleted' }), 'Rejected');
});

test('list escapes private text and explains server quotas and resume policy', () => {
  const html = renderAcquisitionList(project, { acquisitions: [{ ...base, name: '<img onerror=1>', filename: '<script>.zip' }],
    limits: { upload_bytes: 4 * 1024 ** 3, unpacked_bytes: 4 * 1024 ** 3, prepared_bytes: 2 * 1024 ** 3,
      account_bytes: 12 * 1024 ** 3, upload_inactivity_hours: 24 },
    storage: { reserved_bytes: 6 * 1024 ** 3 }, service: { enabled: true, worker_available: false } });
  assert.doesNotMatch(html, /<img|<script>/);
  for (const text of ['&lt;img', '4.0 GiB', '2.0 GiB', '12.0 GiB', '6.0 GiB', '24 hours', 'waiting for the preparation service']) assert.ok(html.includes(text), text);
  assert.match(html, /VIO execution is not available yet/);
  assert.doesNotMatch(html, /data-action="launch-job"/);
});

test('disabled service and reached project limit hide creation without hiding records', () => {
  const disabled = renderAcquisitionList(project, { acquisitions: [base], service: { enabled: false } });
  assert.doesNotMatch(disabled, /data-acquisition-action="new"/);
  assert.match(disabled, /Bench recording/);
  const full = renderAcquisitionList(project, { acquisitions: [base], limits: { per_project: 1 } });
  assert.doesNotMatch(full, /data-acquisition-action="new"/);
  assert.match(full, /saved acquisition limit/);
});

test('detail exposes owner-scoped files only for ready retained recordings', () => {
  const ready = { ...base, state: 'ready', files_available: true, sha256: 'a'.repeat(64) };
  const html = renderAcquisitionDetail(project, { acquisition: ready, descriptor: { rig: { id: 'rig', revision: 'v1' },
    purpose: 'fixture', streams: [{ id: 'imu0', kind: 'imu', segments: [{ sample_count: 3 }, { sample_count: 2 }] }] } });
  assert.match(html, new RegExp(`/api/v1/projects/${projectId}/acquisitions/${acquisitionId}/original`));
  assert.match(html, /Download descriptor/);
  assert.match(html, /5 samples/);
  assert.match(html, /2 segments/);
  assert.match(html, /Not included/);
  assert.match(html, /have not been assessed/);
  for (const acquisition of [base, { ...base, state: 'validating' }, { ...base, state: 'rejected' },
    { ...base, state: 'cancelled' }, { ...ready, files_available: false }]) {
    assert.doesNotMatch(renderAcquisitionDetail(project, { acquisition }), /Download original|Download descriptor|\/original"/);
  }
});

test('rejection text is escaped and deleted metadata never suggests the files are usable', () => {
  const rejected = renderAcquisitionDetail(project, { acquisition: { ...base, state: 'rejected',
    failure: { code: '<script>', message: '<img src=x onerror=1>' } } });
  assert.doesNotMatch(rejected, /<img|<script>/);
  assert.match(rejected, /&lt;img/);
  const deleted = renderAcquisitionDetail(project, { acquisition: { ...base, state: 'ready', availability: 'deleted' } });
  assert.match(deleted, /was deleted/);
  assert.doesNotMatch(deleted, /ready for inspection|Download |data-acquisition-action="delete"/);
});

test('unknown sizes are not displayed as NaN or a fabricated zero', () => {
  for (const value of [undefined, NaN, -1, 1.5, Number.MAX_SAFE_INTEGER + 1]) assert.equal(formatBytes(value), '—');
  assert.equal(formatBytes(0), '0 B');
  assert.equal(formatBytes(1024), '1.0 KiB');
  assert.doesNotMatch(renderAcquisitionLimits(), /undefined|NaN/);
});

test('chunk SHA matches an independent implementation without reading an entire file', async () => {
  const bytes = new TextEncoder().encode('abc').buffer;
  assert.equal(await hashChunk(bytes), 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
  const original = file();
  original.arrayBuffer = () => { throw new Error('Whole-file reads forbidden'); };
  const progress = [];
  assert.equal(await verifyReceivedPrefix(original, { ...base, bytes_received: 4 }, [chunk],
    { onProgress: value => progress.push(value) }), 4);
  assert.deepEqual(progress, [{ phase: 'verifying', completed: 4, total: 4 }]);
});

test('resume verifies all acknowledged chunks including a short final chunk', async () => {
  const manifest = [chunk, { offset: 4, size_bytes: 2, sha256: digest('ef') }];
  assert.equal(await verifyReceivedPrefix(file(), { ...base, bytes_received: 6 }, manifest), 6);
  const bad = new File(['abcdeg'], 'sensors.zip');
  await assert.rejects(verifyReceivedPrefix(bad, { ...base, bytes_received: 6 }, manifest), /differs from the received bytes/);
});

test('resume rejects incorrect size, mismatched bytes and incomplete or reordered manifests', async () => {
  await assert.rejects(verifyReceivedPrefix(new File(['abc'], 'sensors.zip'), base, []), /size/);
  await assert.rejects(verifyReceivedPrefix(new File(['xxxxef'], 'sensors.zip'), { ...base, bytes_received: 4 }, [chunk]), /differs/);
  for (const manifest of [[], [{ ...chunk, offset: 1 }], [{ ...chunk, size_bytes: 3 }],
    [{ ...chunk, sha256: 'wrong' }], [chunk, chunk]]) {
    await assert.rejects(verifyReceivedPrefix(file(), { ...base, bytes_received: 4 }, manifest), /manifest/);
  }
  await assert.rejects(verifyReceivedPrefix(file(), { ...base, bytes_received: 0 }, Array(1025).fill(chunk)), /manifest/);
});

test('prefix verification suppresses progress after abort during hashing', async () => {
  const controller = new AbortController();
  let finish;
  const progress = [];
  const pending = verifyReceivedPrefix(file(), { ...base, bytes_received: 4 }, [chunk], {
    signal: controller.signal, onProgress: value => progress.push(value),
    digest: () => new Promise(resolve => { finish = resolve; }),
  });
  while (!finish) await new Promise(resolve => setTimeout(resolve, 0));
  controller.abort();
  finish(chunk.sha256);
  await assert.rejects(pending, error => error.name === 'AbortError');
  assert.deepEqual(progress, []);
});

function transferHarness({ received = 0, manifest, upload, read } = {}) {
  let acquisition = { ...base, bytes_received: received };
  const calls = [], errors = [], states = [], completed = [], progress = [];
  const api = {
    async request(path, options = {}) {
      calls.push({ path, options });
      if (read) return read(path, options);
      if (path.endsWith('/chunks')) return { chunks: manifest || (received ? [chunk] : []) };
      if (path.endsWith('/complete')) {
        acquisition = { ...acquisition, state: 'validating' };
        return { acquisition };
      }
      return { acquisition };
    },
    async upload(path, options) {
      calls.push({ path, options });
      assert.equal(options.sha256, digest(Buffer.from(options.body)));
      if (upload) return upload(path, options);
      acquisition = { ...acquisition, bytes_received: options.offset + options.body.byteLength };
      return { acquisition };
    },
  };
  const transfer = createAcquisitionTransfer({ api, projectId, acquisitionId,
    onError: error => errors.push(error), onState: value => states.push(value),
    onDone: value => completed.push(value), onProgress: value => progress.push(value) });
  return { transfer, calls, errors, states, completed, progress };
}

test('new transfer uses sequential acknowledged offsets and completes only after every byte', async () => {
  const run = transferHarness();
  await run.transfer.start(file());
  const uploads = run.calls.filter(call => call.path.endsWith('/content'));
  assert.deepEqual(uploads.map(call => call.options.offset), [0, 4]);
  assert.deepEqual(uploads.map(call => Buffer.from(call.options.body).toString()), ['abcd', 'ef']);
  assert.equal(run.calls.at(-1).path.endsWith('/complete'), true);
  assert.equal(run.completed[0].state, 'validating');
  assert.equal(run.errors.length, 0);
});

test('resumed transfer verifies the server prefix and uploads only the suffix', async () => {
  const run = transferHarness({ received: 4 });
  await run.transfer.start(file());
  assert.deepEqual(run.calls.filter(call => call.path.endsWith('/content')).map(call => call.options.offset), [4]);
  assert.equal(run.progress[0].phase, 'verifying');
  assert.equal(run.completed.length, 1);
});

test('already received archive can retry completion without retransmitting any chunk', async () => {
  const run = transferHarness({ received: 6, manifest: [chunk, { offset: 4, size_bytes: 2, sha256: digest('ef') }] });
  await run.transfer.start(file());
  assert.equal(run.calls.filter(call => call.path.endsWith('/content')).length, 0);
  assert.equal(run.completed.length, 1);
});

test('wrong resume file cannot send suffix bytes or complete the acquisition', async () => {
  const run = transferHarness({ received: 4 });
  await run.transfer.start(new File(['xxxxef'], 'sensors.zip'));
  assert.match(run.errors[0].message, /differs/);
  assert.equal(run.calls.some(call => call.path.endsWith('/content') || call.path.endsWith('/complete')), false);
});

test('unexpected acknowledgements and network loss stop without claiming accepted progress', async () => {
  const invalid = transferHarness({ upload: async () => ({ acquisition: { ...base, bytes_received: 6 } }) });
  await invalid.transfer.start(file());
  assert.match(invalid.errors[0].message, /expected chunk/);
  assert.equal(invalid.completed.length, 0);
  const lost = transferHarness({ upload: async () => { throw new TypeError('offline'); } });
  await lost.transfer.start(file());
  assert.equal(lost.progress.length, 0);
  assert.equal(lost.completed.length, 0);
  assert.equal(lost.errors[0].message, 'offline');
});

test('late private responses after navigation produce no updates, upload or completion', async () => {
  let finish;
  const run = transferHarness({ read: () => new Promise(resolve => { finish = resolve; }) });
  const pending = run.transfer.start(file());
  run.transfer.destroy();
  finish({ acquisition: base });
  await pending;
  assert.equal(run.calls.length, 1);
  assert.deepEqual(run.errors, []);
  assert.deepEqual(run.states, []);
  assert.deepEqual(run.completed, []);
  await run.transfer.start(file());
  assert.equal(run.calls.length, 1);
});

test('binary API sends bounded raw bytes with current CSRF and no private cache', async () => {
  const calls = [];
  const api = createApiClient(async (...args) => { calls.push(args); return Response.json({ schema_version: 1, acquisition: base }); });
  api.setCsrfToken('current-session');
  const body = new TextEncoder().encode('abcd').buffer;
  const signal = new AbortController().signal;
  await api.upload(acquisitionPath(projectId, acquisitionId) + '/content', { offset: 0, sha256: digest('abcd'), body, signal });
  const [path, options] = calls[0];
  assert.match(path, /^\/api\/v1\/projects\//);
  assert.equal(options.body, body);
  assert.equal(options.method, 'PUT');
  assert.equal(options.signal, signal);
  assert.equal(options.credentials, 'same-origin');
  assert.equal(options.cache, 'no-store');
  assert.equal(options.headers['Content-Type'], 'application/octet-stream');
  assert.equal(options.headers['X-CSRFToken'], 'current-session');
  assert.equal(options.headers['Upload-Offset'], '0');
  assert.equal(options.headers['X-Chunk-SHA256'], digest('abcd'));
});

test('binary API rejects ambiguous paths, unbounded bodies and malformed chunk identity before fetch', async () => {
  let calls = 0;
  const api = createApiClient(async () => { calls += 1; });
  const valid = { offset: 0, sha256: digest('abcd'), body: new ArrayBuffer(4) };
  for (const path of ['https://other.example', '//other.example', '/content?owner=other']) await assert.rejects(api.upload(path, valid), TypeError);
  for (const change of [{ offset: -1 }, { offset: 1.5 }, { sha256: 'bad' }, { body: 'text' },
    { body: new ArrayBuffer(0) }, { body: new ArrayBuffer(4 * 1024 * 1024 + 1) }]) {
    await assert.rejects(api.upload('/content', { ...valid, ...change }), TypeError);
  }
  assert.equal(calls, 0);
});

test('binary API retains authentication and conflict errors and rejects wrong success schemas', async () => {
  const options = { offset: 0, sha256: digest('a'), body: new ArrayBuffer(1) };
  for (const status of [401, 403, 409]) {
    const api = createApiClient(async () => Response.json({ error: { code: 'denied', message: 'Denied' } }, { status }));
    await assert.rejects(api.upload('/content', options), error => error instanceof ApiError && error.status === status);
  }
  const invalid = createApiClient(async () => Response.json({ schema_version: 2 }));
  await assert.rejects(invalid.upload('/content', options), error => error.status === 502);
});
