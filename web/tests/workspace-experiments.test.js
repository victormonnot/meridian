import assert from 'node:assert/strict';
import test from 'node:test';
import { revisionDraft, draftChanged, experimentPayload, renderComparison, renderConflict, createExperimentDialog } from '../src/workspace-experiments.js';

const profile = {
  estimator: 'OpenVINS',
  parameters: { num_pts: { label: 'Tracked features per camera', type: 'integer', default: 200, minimum: 100, maximum: 400, choices: [100, 200, 400], unit: 'features/camera', description: 'Tracking budget.' } },
};

test('new and historical drafts use server defaults and the selected immutable parameters', () => {
  assert.deepEqual(revisionDraft(null, profile), { name: '', description: '', num_pts: '200' });
  const historical = { version: 1, name: 'Initial', description: 'Saved description', specification: { parameters: { num_pts: 100 } } };
  assert.deepEqual(revisionDraft(historical, profile), { name: 'Initial', description: 'Saved description', num_pts: '100' });
  assert.equal(revisionDraft(null, { ...profile, parameters: { num_pts: { ...profile.parameters.num_pts, default: 400 } } }).num_pts, '400');
});

test('dirty checking accounts for each editable field without changing the saved baseline', () => {
  const original = { name: 'Reference', description: 'Indoor', num_pts: '200' };
  assert.equal(draftChanged(original, { ...original }), false);
  for (const key of Object.keys(original)) assert.equal(draftChanged(original, { ...original, [key]: `${original[key]} changed` }), true);
  assert.deepEqual(original, { name: 'Reference', description: 'Indoor', num_pts: '200' });
});

test('creation sends only structured values and the selected dataset identity', () => {
  const payload = experimentPayload({ name: 'Reference', description: '', num_pts: '100', command: 'untrusted', path: '/host/file' }, profile, { datasetId: 'qualified-dataset' });
  assert.deepEqual(payload, { schema_version: 1, dataset_id: 'qualified-dataset', name: 'Reference', description: '', parameters: { num_pts: 100 } });
});

test('editing includes the optimistic base version and cannot retarget the dataset', () => {
  const draft = { name: 'Edited', description: 'New description', num_pts: '400' };
  assert.deepEqual(experimentPayload(draft, profile, { datasetId: 'ignored', baseVersion: 3 }), {
    schema_version: 1, base_version: 3, name: 'Edited', description: 'New description', parameters: { num_pts: 400 },
  });
  for (const num_pts of ['150', '200.5', '', 'NaN', 'Infinity', '400; execute']) {
    assert.throws(() => experimentPayload({ ...draft, num_pts }, profile, { baseVersion: 3 }), /supported/);
  }
});

test('the form takes choices, defaults and units from the profile instead of hardcoding controls', () => {
  const alternate = { estimator: 'Test estimator', parameters: { num_pts: { ...profile.parameters.num_pts, choices: [120, 240], default: 240, unit: 'custom unit' } } };
  const html = createExperimentDialog({ dataset_id: 'dataset' }, alternate, revisionDraft(null, alternate));
  assert.match(html, /value="120"/);
  assert.match(html, /value="240" selected/);
  assert.match(html, /custom unit/);
  assert.doesNotMatch(html, /value="200"/);
});

test('exact comparison values retain multiline text and escape markup from saved metadata', () => {
  const html = renderComparison({ from_version: 1, to_version: 2, changes: [
    { field: 'description', before: '<img src=x>', after: 'line one\nline two & more' },
    { field: 'parameters.num_pts', before: 200, after: 400 },
  ] });
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /&lt;img src=x&gt;/);
  assert.match(html, /line one\nline two &amp; more/);
  assert.match(html, /parameters\.num_pts/);
  assert.match(html, /<td>200<\/td><td>400<\/td>/);
});

test('conflict review keeps draft values distinct from the newer saved revision', () => {
  const draft = { name: 'My draft', description: '<script>draft</script>', num_pts: '100' };
  const snapshot = structuredClone(draft);
  const html = renderConflict({ version: 4, name: 'Other edit', description: 'Saved', parameters: { num_pts: 400 } }, draft, profile);
  assert.match(html, /Saved v4/);
  assert.match(html, /My draft/);
  assert.match(html, /&lt;script&gt;draft&lt;\/script&gt;/);
  assert.match(html, /<span>400<\/span>/);
  assert.match(html, /<span>100<\/span>/);
  assert.deepEqual(draft, snapshot);
});
