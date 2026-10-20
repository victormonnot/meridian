import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import test from 'node:test';
import { ApiError, createApiClient } from '../src/workspace-api.js';
import { renderRunExport, createExportDownload } from '../src/workspace-exports.js';

const id = '87654321-abcd-abcd-abcd-987654321abc';
const path = `/projects/12345678-abcd-abcd-abcd-123456789abc/jobs/${id}/export`;
const bytes = Buffer.from('PK deterministic attachment fixture');
const sha256 = createHash('sha256').update(bytes).digest('hex');
const filename = `meridian-run-${id}-v1.zip`;
const headers = {
  'Content-Type': 'application/zip',
  'Content-Length': String(bytes.length),
  'Content-Disposition': `attachment; filename="${filename}"`,
  'X-Content-SHA256': sha256,
};
const response = (overrides = {}, body = bytes) => new Response(body, { headers: { ...headers, ...overrides } });

test('export transport preserves the explicit format version, same-origin session and verified bytes', async () => {
  let request;
  const signal = new AbortController().signal;
  const client = createApiClient(async (...args) => { request = args; return response(); });
  client.setCsrfToken('not-needed-for-read');
  const archive = await client.download(path, { signal });
  assert.equal(request[0], `/api/v1${path}?schema_version=1`);
  assert.deepEqual(request[1], { credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/zip' }, signal });
  assert.equal(archive.filename, filename);
  assert.equal(archive.sha256, sha256);
  assert.equal(archive.bytes, bytes.length);
  assert.deepEqual(Buffer.from(await archive.blob.arrayBuffer()), bytes);
});

test('export transport rejects unverifiable attachments and unsafe filenames', async () => {
  for (const override of [
    { 'Content-Type': 'text/html' }, { 'Content-Disposition': `inline; filename="${filename}"` },
    { 'Content-Disposition': 'attachment; filename="../../secret.zip"' },
    { 'Content-Disposition': `attachment; filename="${filename.replace('-v1', '-v2')}"` },
    { 'X-Content-SHA256': 'not-a-hash' }, { 'Content-Length': '0' },
    { 'Content-Length': '9007199254740992' }, { 'Content-Length': String(16 * 1024 * 1024 + 1) },
  ]) {
    await assert.rejects(createApiClient(async () => response(override)).download(path),
      error => error instanceof ApiError && error.status === 502);
  }
});

test('a corrupted or incomplete attachment is never offered as a verified download', async () => {
  for (const override of [{ 'X-Content-SHA256': '0'.repeat(64) }, { 'Content-Length': String(bytes.length + 1) }]) {
    await assert.rejects(createApiClient(async () => response(override)).download(path),
      error => error instanceof ApiError && /integrity check/.test(error.message));
  }
});

test('export failures preserve authorization, expiry and unavailable reasons', async () => {
  for (const status of [401, 404, 409, 410, 503]) {
    const client = createApiClient(async () => new Response(JSON.stringify({ schema_version: 1, error: { code: 'unavailable', message: 'Evidence is no longer retained.' } }), { status }));
    await assert.rejects(client.download(path), error => error instanceof ApiError && error.status === status && error.message === 'Evidence is no longer retained.');
  }
});

test('download refuses external paths and successful JSON before creating a file', async () => {
  let reads = 0;
  const client = createApiClient(async () => { reads += 1; return new Response('{"schema_version":1}'); });
  for (const value of ['https://other.example', '//other.example/export', '/jobs?schema_version=2', '/jobs#other']) await assert.rejects(client.download(value), TypeError);
  assert.equal(reads, 0);
  await assert.rejects(client.download(path), error => error.status === 502);
});

test('an aborted download is discarded even when a transport delivers the complete body', async () => {
  const controller = new AbortController();
  const client = createApiClient(async () => { controller.abort(); return response(); });
  await assert.rejects(client.download(path, { signal: controller.signal }), error => error.name === 'AbortError');
});

test('export UI uses server availability and distinguishes failed or expired evidence', () => {
  const available = renderRunExport({ state: 'succeeded', export_available: true });
  for (const text of ['Download experiment', 'exact configuration', 'Linux', 'Docker', 'no Meridian account', 'Deleting this run revokes']) assert.ok(available.includes(text), text);
  for (const state of ['queued', 'running', 'evaluating', 'failed', 'cancelled']) {
    assert.doesNotMatch(renderRunExport({ state, export_available: true }), /data-action="download-export"/);
  }
  const expired = renderRunExport({ state: 'succeeded', export_available: false, export_unavailable_reason: 'Artifact retention expired.' });
  assert.match(expired, /Artifact retention expired/);
  assert.doesNotMatch(expired, /data-action="download-export"/);
  assert.match(renderRunExport({ state: 'failed' }), /failed run/);
  assert.match(renderRunExport({ state: 'cancelled' }), /Cancelled runs/);
  assert.match(renderRunExport({ state: 'succeeded', export_unavailable_reason: '<img src=x>' }), /&lt;img/);
});

test('repeated clicks share one in-flight download and recoverable errors can be retried', async () => {
  let resolve;
  let reads = 0;
  const saved = [];
  const downloader = createExportDownload({ read: () => { reads += 1; return new Promise(done => { resolve = done; }); }, save: item => saved.push(item) });
  const first = downloader.start();
  await downloader.start();
  assert.equal(reads, 1);
  resolve({ sha256 });
  await first;
  assert.deepEqual(saved, [{ sha256 }]);
  let fail = true;
  const errors = [];
  const retry = createExportDownload({ read: async () => { if (fail) throw new Error('network'); return { sha256 }; }, save: item => saved.push(item), onError: error => errors.push(error.message) });
  await retry.start();
  fail = false;
  await retry.start();
  assert.deepEqual(errors, ['network']);
  assert.equal(saved.length, 2);
});

test('navigation or logout aborts a pending export and suppresses every late callback', async () => {
  let finish;
  let signal;
  const calls = [];
  const downloader = createExportDownload({
    read: value => { signal = value; return new Promise(resolve => { finish = resolve; }); },
    save: () => calls.push('save'), onSuccess: () => calls.push('success'),
    onError: () => calls.push('error'), onFinish: () => calls.push('finish'),
  });
  const pending = downloader.start();
  downloader.destroy();
  assert.equal(signal.aborted, true);
  finish({ sha256 });
  await pending;
  await downloader.start();
  assert.deepEqual(calls, []);
});
