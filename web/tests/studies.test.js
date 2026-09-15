import assert from 'node:assert/strict';
import { readFile, stat } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { tmpdir } from 'node:os';
import { resolve } from 'node:path';
import test from 'node:test';
import { STUDY_SPECS, studyStatistics, validateStudies } from '../src/studies.js';
import { buildStudies, extractStudyPhase, serializeStudies, STUDIES_OUTPUT } from '../scripts/export-studies.js';

const bundledText = await readFile(STUDIES_OUTPUT, 'utf8');
const bundled = JSON.parse(bundledText);
const clone = () => structuredClone(bundled);
const rPhase = data => data.studies.r.phases.evaluation;
const biasPhase = data => data.studies.bias.phases.evaluation;
const first = phase => phase.cases.nominal.trials[0];
const root = fileURLToPath(new URL('../../', import.meta.url));

async function original(id, phase = 'evaluation') {
  const path = new URL(`../../results/${STUDY_SPECS[id].directory}/${phase}-summary.json`, import.meta.url);
  const bytes = await readFile(path);
  return { summary: JSON.parse(bytes), bytes };
}

test('bundled study artifact reproduces exactly from four public summaries', async () => {
  assert.equal(serializeStudies(await buildStudies()), bundledText);
  assert.strictEqual(validateStudies(bundled), bundled);
});

test('check command works outside the repository and does not rewrite artifact', async () => {
  const before = await stat(STUDIES_OUTPUT);
  const result = execFileSync(process.execPath, [resolve(root, 'web/scripts/export-studies.js'), '--check'], { cwd: tmpdir(), encoding: 'utf8' });
  assert.match(result, /matches the four recorded summaries/);
  const after = await stat(STUDIES_OUTPUT);
  assert.equal(after.mtimeMs, before.mtimeMs);
  assert.equal(after.size, before.size);
});

test('export preserves every per-seed metric and fingerprints the original bytes', async () => {
  for (const [id, spec] of Object.entries(STUDY_SPECS)) {
    for (const phaseName of ['exploration', 'evaluation']) {
      const { summary, bytes } = await original(id, phaseName);
      const phase = bundled.studies[id].phases[phaseName];
      assert.equal(phase.provenance.summary_sha256, createHash('sha256').update(bytes).digest('hex'));
      assert.deepEqual(phase.protocol, summary.protocol);
      assert.deepEqual(phase.limitations, summary.limitations);
      for (const caseId of spec.cases) {
        phase.cases[caseId].trials.forEach((trial, index) => {
          const source = summary.scenarios[caseId].trials[index];
          assert.equal(trial.seed, source.seed);
          assert.equal(trial.measurement_sha256, source[id === 'r' ? 'measurement_sha256' : 'input_sha256']);
          for (const key of spec.settings) assert.deepEqual(trial.metrics[key], source.settings[key].metrics);
          if (id === 'r') assert.deepEqual(trial.baselines, source.baselines);
          assert.equal(Object.hasOwn(trial, 'stream_seeds'), false);
        });
      }
    }
  }
  assert.doesNotMatch(bundledText, /\.personal|AGENTS\.md|\/home\/|\/Users\/|outputs\/|stream_seeds/);
});

test('historical source fingerprints are not gated against current source files', () => {
  const data = clone();
  for (const phase of Object.values(data.studies.r.phases)) {
    phase.provenance.source_sha256['ekf.py'] = 'a'.repeat(64);
  }
  assert.doesNotThrow(() => validateStudies(data));
});

test('paired statistics match the reported nominal R tradeoff and reference zeros', () => {
  const stats = studyStatistics(rPhase(bundled), 'nominal', '0.6', 'angle_rmse_deg', '0.2');
  assert.equal(stats.values.length, 20);
  assert.deepEqual([stats.delta.negative, stats.delta.zero, stats.delta.positive], [7, 0, 13]);
  assert.ok(Math.abs(stats.mean - 0.2527260071501013) < 1e-14);
  assert.ok(Math.abs(stats.delta.mean - (stats.mean - stats.reference_mean)) < 1e-14);
  stats.values.forEach(row => assert.equal(row.delta, row.value - row.reference));
  const ref = studyStatistics(rPhase(bundled), 'nominal', '0.2', 'angle_rmse_deg', '0.2');
  assert.deepEqual(ref.delta, { mean: 0, min: 0, max: 0, negative: 0, zero: 20, positive: 0 });
});

test('bias-ramp tracking and constant-bias fluctuation remain separate metrics', () => {
  const phase = biasPhase(bundled);
  const tracking = studyStatistics(phase, 'bias_ramp', '0.1', 'ramp_bias_rmse_deg_s', '0');
  assert.equal(tracking.delta.negative, 20);
  const fluctuation = studyStatistics(phase, 'nominal', '0.1', 'late_bias_temporal_std_deg_s', '0');
  assert.equal(fluctuation.delta.positive, 20);
  assert.ok(tracking.mean > 0.26 && tracking.mean < 0.27);
});

test('statistics rejects missing metrics, reference settings and scenarios', () => {
  assert.throws(() => studyStatistics(rPhase(bundled), 'nominal', '0.6', 'absent', '0.2'), /Invalid parameter/);
  assert.throws(() => studyStatistics(rPhase(bundled), 'nominal', '0.6', 'mean_nis', 'absent'), /Invalid parameter/);
  assert.throws(() => studyStatistics(rPhase(bundled), 'absent', '0.6', 'mean_nis', '0.2'), /Invalid parameter/);
});

const corruptions = [
  ['schema version', d => { d.schema_version = 2; }],
  ['unknown top-level field', d => { d.private_note = 'unexpected'; }],
  ['non-simulation source', d => { d.data_source = 'hardware'; }],
  ['missing phase', d => { delete d.studies.r.phases.exploration; }],
  ['swapped phases', d => { d.studies.r.phases.evaluation = d.studies.r.phases.exploration; }],
  ['changed reference', d => { d.studies.bias.reference_setting = '0.1'; }],
  ['changed setting grid', d => { d.studies.r.settings[0] = '0.3'; }],
  ['duplicate phase seed', d => { rPhase(d).seeds[1] = rPhase(d).seeds[0]; }],
  ['missing seed', d => { rPhase(d).seeds.pop(); }],
  ['wrong trial seed', d => { first(rPhase(d)).seed = 17; }],
  ['missing trial', d => { rPhase(d).cases.nominal.trials.pop(); }],
  ['extra case', d => { rPhase(d).cases.extra = rPhase(d).cases.nominal; }],
  ['extra setting', d => { first(rPhase(d)).metrics['9'] = first(rPhase(d)).metrics['0.2']; }],
  ['missing metric', d => { delete first(rPhase(d)).metrics['0.2'].mean_nis; }],
  ['NaN metric', d => { first(rPhase(d)).metrics['0.2'].mean_nis = NaN; }],
  ['infinite metric', d => { first(rPhase(d)).metrics['0.2'].angle_rmse_deg = Infinity; }],
  ['negative RMSE', d => { first(rPhase(d)).metrics['0.2'].angle_rmse_deg = -1; }],
  ['nonfinite baseline', d => { first(rPhase(d)).baselines.gyro.angle_rmse_deg = null; }],
  ['unknown baseline', d => { first(rPhase(d)).baselines.ekf = first(rPhase(d)).baselines.gyro; }],
  ['measurement fingerprint', d => { first(rPhase(d)).measurement_sha256 = 'invalid'; }],
  ['summary fingerprint', d => { rPhase(d).provenance.summary_sha256 = 'invalid'; }],
  ['recorded protocol fingerprint', d => { rPhase(d).provenance.protocol_sha256 = '0'.repeat(64); }],
  ['private summary path', d => { rPhase(d).provenance.summary_file = '.personal/report.json'; }],
  ['wrong phase path', d => { rPhase(d).provenance.summary_file = 'results/r-sensitivity/exploration-summary.json'; }],
  ['source fingerprint', d => { rPhase(d).provenance.source_sha256['ekf.py'] = 'invalid'; }],
  ['source private path', d => { rPhase(d).provenance.source_sha256['.personal/source.py'] = '0'.repeat(64); }],
  ['private limitation', d => { rPhase(d).limitations.push('/home/person/notes'); }],
  ['extra protocol field', d => { rPhase(d).protocol.extra = 'unexpected'; }],
  ['changed covariance tolerance', d => { biasPhase(d).protocol.covariance_tolerance_si = 1; }],
  ['changed parity tolerance', d => { biasPhase(d).protocol.parity_tolerances.p00.atol = 1; }],
  ['changed phase source fingerprint', d => { rPhase(d).provenance.source_sha256['ekf.py'] = 'a'.repeat(64); }],
  ['changed phase environment', d => { rPhase(d).provenance.environment.numpy = 'different'; }],
  ['NIS dimension', d => { rPhase(d).protocol.nis_dimension = 1; }],
  ['wrong R units', d => { rPhase(d).protocol.r_diagonal_m2_s4['0.2'] = 0.2; }],
  ['changed window', d => { biasPhase(d).protocol.closed_windows_s.late = [20, 30]; }],
  ['changed model', d => { biasPhase(d).protocol.q_bias = 'diagonal only'; }],
  ['wrong generated noise', d => { rPhase(d).protocol.scenarios[1].accel_noise_std_m_s2 = 0.2; }],
  ['C++ fingerprint', d => { biasPhase(d).provenance.cpp_binary.sha256 = null; }],
  ['C++ protocol version', d => { biasPhase(d).provenance.cpp_binary.protocol_version = 2; }],
];
for (const [name, mutate] of corruptions) {
  test(`runtime rejects ${name}`, () => {
    const data = clone(); mutate(data);
    assert.throws(() => validateStudies(data), /Invalid parameter study data/);
  });
}

const sourceCorruptions = [
  ['r', 'phase mismatch', s => { s.phase = 'exploration'; }],
  ['r', 'protocol text mutation', s => { s.protocol.frame = 'changed'; }],
  ['r', 'unpassed numerical checks', s => { s.scenarios.nominal.trials[0].settings['0.2'].numerical_checks_passed = false; }],
  ['r', 'changed paired delta', s => { s.scenarios.nominal.trials[0].settings['0.6'].delta_from_default.angle_rmse_deg += 0.1; }],
  ['r', 'changed aggregate', s => { s.scenarios.nominal.aggregates.settings['0.6'].metrics.mean_nis.mean += 0.1; }],
  ['r', 'changed paired count', s => { s.scenarios.nominal.aggregates.settings['0.6'].rmse_delta_counts.angle_rmse_deg.negative = 8; }],
  ['r', 'changed baseline aggregate', s => { s.scenarios.nominal.aggregates.baselines.gyro.angle_rmse_deg.mean += 1; }],
  ['bias', 'wrong diffusion units', s => { s.scenarios.nominal.trials[0].settings['0.1'].configuration.bias_random_walk_std_rad_s_per_sqrt_s = 0.1; }],
  ['bias', 'failed parity', s => { s.scenarios.nominal.trials[0].settings['0'].comparison.passed = false; }],
  ['bias', 'failed parity quantity', s => { s.scenarios.nominal.trials[0].settings['0'].comparison.quantities.p11.passed = false; }],
  ['bias', 'parity tolerance exceeded', s => { s.scenarios.nominal.trials[0].settings['0'].comparison.quantities.p11.max_tolerance_ratio = 1.1; }],
  ['bias', 'missing operation records', s => { s.scenarios.nominal.trials[0].settings['0'].comparison.quantities.p11.compared_values = 3000; }],
  ['bias', 'changed ramp delta', s => { s.scenarios.bias_ramp.trials[0].settings['0.1'].delta_from_constant.ramp_bias_rmse_deg_s += 0.1; }],
];
for (const [id, name, mutate] of sourceCorruptions) {
  test(`export rejects ${name}`, async () => {
    const { summary, bytes } = await original(id); mutate(summary);
    assert.throws(() => extractStudyPhase(summary, id, 'evaluation', bytes), /Invalid parameter study data/);
  });
}
