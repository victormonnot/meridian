import { createHash } from 'node:crypto';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import {
  STUDY_SPECS, expectedStudySeeds, exactStudyKeys, studyCheck, studyStatistics,
  summarizeStudyValues, validateStudies, validateStudyMetrics, validateStudyProtocol,
} from '../src/studies.js';

const ROOT = fileURLToPath(new URL('../../', import.meta.url));
export const STUDIES_OUTPUT = resolve(ROOT, 'web/public/data/parameter-studies.json');
const PHASES = ['exploration', 'evaluation'];
// JS-normalized, sorted protocol fingerprints complement the original Python
// fingerprints. These are different serializations of the same recorded objects.
const NORMALIZED_PROTOCOL_HASHES = {
  r: '98660957c3bfb275b368696d796f143798c3962c2d851676e0a66e43e757de17',
  bias: 'ec166e1b145e702ae41f562916c5219ff8a16c5ba60998902755897b7ddb8f66',
};

function fingerprint(bytes) { return createHash('sha256').update(bytes).digest('hex'); }
function sorted(value) {
  if (Array.isArray(value)) return value.map(sorted);
  if (value !== null && typeof value === 'object') {
    return Object.fromEntries(Object.keys(value).sort().map(key => [key, sorted(value[key])]));
  }
  return value;
}
function close(actual, expected, message) {
  studyCheck(Number.isFinite(actual) && Number.isFinite(expected)
    && Math.abs(actual - expected) <= 1e-12 + 1e-12 * Math.abs(expected), message);
}
function checkStatistics(actual, expected, message) {
  studyCheck(exactStudyKeys(actual, ['mean', 'min', 'max']), `${message} fields`);
  for (const key of ['mean', 'min', 'max']) close(actual[key], expected[key], `${message} ${key}`);
}

function validateParity(setting, key) {
  const configuration = setting.configuration;
  const expected = {
    initial_angle_rad: 0, initial_bias_rad_s: 0, initial_angle_std_rad: 0,
    initial_bias_std_rad_s: Math.PI / 180, gyro_noise_std_rad_s: Math.PI / 180,
    accel_noise_std_m_s2: 0.2, gravity_m_s2: 9.80665,
    bias_random_walk_std_rad_s_per_sqrt_s: Number(key) * Math.PI / 180,
  };
  studyCheck(exactStudyKeys(configuration, Object.keys(expected)), 'bias configuration fields');
  for (const [name, value] of Object.entries(expected)) close(configuration[name], value, `bias configuration ${name}`);
  const comparison = setting.comparison;
  const quantities = ['angle_rad', 'bias_rad_s', 'p00', 'p01', 'p10', 'p11', 'v0', 'v1', 's00', 's01', 's10', 's11'];
  studyCheck(exactStudyKeys(comparison, ['record_count', 'quantities', 'passed'])
    && comparison.record_count === 3301 && comparison.passed === true, 'C++ parity result');
  studyCheck(exactStudyKeys(comparison.quantities, quantities), 'C++ parity quantities');
  for (const [name, result] of Object.entries(comparison.quantities)) {
    studyCheck(exactStudyKeys(result, ['compared_values', 'max_abs_difference', 'max_tolerance_ratio', 'passed']), 'parity quantity fields');
    studyCheck(result.compared_values === (name.startsWith('v') || name.startsWith('s') ? 300 : 3301)
      && result.passed === true && Number.isFinite(result.max_abs_difference) && result.max_abs_difference >= 0
      && Number.isFinite(result.max_tolerance_ratio) && result.max_tolerance_ratio >= 0 && result.max_tolerance_ratio <= 1,
    `C++ parity ${name}`);
  }
}

export function extractStudyPhase(summary, id, phaseName, rawBytes) {
  const spec = STUDY_SPECS[id];
  studyCheck(spec && PHASES.includes(phaseName), 'requested study or phase');
  studyCheck(exactStudyKeys(summary, ['schema_version', 'experiment', 'data_source', 'phase', 'seeds',
    'representative_seed', 'protocol', 'protocol_sha256', 'source_sha256', 'environment', 'scenarios',
    'limitations', ...(id === 'r' ? ['measurement_fingerprint_format'] : ['cpp_binary'])]), 'summary fields');
  studyCheck(summary.schema_version === 1 && summary.experiment === spec.experiment
    && summary.data_source === 'simulation' && summary.phase === phaseName, 'summary identity');
  studyCheck(JSON.stringify(summary.seeds) === JSON.stringify(expectedStudySeeds(id, phaseName))
    && summary.representative_seed === summary.seeds[0], 'summary seeds');
  validateStudyProtocol(summary.protocol, id);
  studyCheck(summary.protocol_sha256 === spec.protocolHash
    && fingerprint(JSON.stringify(sorted(summary.protocol))) === NORMALIZED_PROTOCOL_HASHES[id], 'recorded protocol fingerprint');
  studyCheck(exactStudyKeys(summary.scenarios, spec.cases), 'summary cases');
  const phase = {
    seeds: summary.seeds,
    protocol: summary.protocol,
    limitations: summary.limitations,
    provenance: {
      summary_file: `results/${spec.directory}/${phaseName}-summary.json`,
      // Hash the actual file bytes, never JSON.stringify(parsedSummary). In
      // particular, original uint64 child seeds cannot round-trip through JS.
      summary_sha256: fingerprint(rawBytes),
      protocol_sha256: summary.protocol_sha256,
      source_sha256: summary.source_sha256,
      environment: summary.environment,
      ...(id === 'bias' ? { cpp_binary: summary.cpp_binary } : {}),
    },
    cases: {},
  };
  const deltaKey = id === 'r' ? 'delta_from_default' : 'delta_from_constant';
  for (const caseId of spec.cases) {
    const scenario = summary.scenarios[caseId];
    studyCheck(exactStudyKeys(scenario, ['trials', 'aggregates', ...(id === 'r' ? ['simulated_accel_std_m_s2'] : [])]), 'summary case fields');
    if (id === 'r') studyCheck(scenario.simulated_accel_std_m_s2 === (caseId === 'nominal' ? 0.2 : 0.6), 'generated noise');
    studyCheck(Array.isArray(scenario.trials) && scenario.trials.length === 20, 'summary trial count');
    phase.cases[caseId] = { trials: scenario.trials.map((trial, i) => {
      studyCheck(exactStudyKeys(trial, ['seed', 'stream_seeds', 'settings',
        ...(id === 'r' ? ['measurement_sha256', 'baselines'] : ['input_sha256'])]), 'summary trial fields');
      studyCheck(trial.seed === phase.seeds[i] && exactStudyKeys(trial.settings, spec.settings), 'summary trial seeds or settings');
      // Child seeds are intentionally omitted from the compact artifact. The
      // original summary and its raw-byte fingerprint retain that provenance.
      studyCheck(exactStudyKeys(trial.stream_seeds, ['gyro', 'accelerometer', 'timing']), 'source stream fields');
      const metrics = {};
      for (const key of spec.settings) {
        const setting = trial.settings[key];
        studyCheck(exactStudyKeys(setting, ['metrics', deltaKey, ...(id === 'r'
          ? ['numerical_checks_passed'] : ['configuration', 'comparison'])]), 'summary setting fields');
        validateStudyMetrics(setting.metrics, id);
        studyCheck(exactStudyKeys(setting[deltaKey], spec.metrics), 'paired delta fields');
        for (const metric of spec.metrics) {
          close(setting[deltaKey][metric], setting.metrics[metric] - trial.settings[spec.reference].metrics[metric], 'paired delta');
        }
        if (id === 'r') studyCheck(setting.numerical_checks_passed === true, 'numerical checks failed');
        else validateParity(setting, key);
        metrics[key] = setting.metrics;
      }
      return { seed: trial.seed, measurement_sha256: id === 'r' ? trial.measurement_sha256 : trial.input_sha256,
        metrics, ...(id === 'r' ? { baselines: trial.baselines } : {}) };
    }) };
    const aggregates = id === 'r' ? scenario.aggregates.settings : scenario.aggregates;
    if (id === 'r') studyCheck(exactStudyKeys(scenario.aggregates, ['settings', 'baselines']), 'aggregate groups');
    studyCheck(exactStudyKeys(aggregates, spec.settings), 'aggregate settings');
    for (const key of spec.settings) {
      const aggregate = aggregates[key];
      const rmseNames = spec.metrics.filter(name => name.includes('rmse'));
      studyCheck(exactStudyKeys(aggregate, ['metrics', deltaKey, 'rmse_delta_counts'])
        && exactStudyKeys(aggregate.metrics, spec.metrics) && exactStudyKeys(aggregate[deltaKey], spec.metrics)
        && exactStudyKeys(aggregate.rmse_delta_counts, rmseNames), 'aggregate metric fields');
      for (const metric of spec.metrics) {
        const stats = studyStatistics(phase, caseId, key, metric, spec.reference);
        checkStatistics(aggregate.metrics[metric], stats, 'aggregate value');
        checkStatistics(aggregate[deltaKey][metric], stats.delta, 'aggregate delta');
        if (rmseNames.includes(metric)) {
          const counts = aggregate.rmse_delta_counts[metric];
          studyCheck(exactStudyKeys(counts, ['negative', 'zero', 'positive'])
            && ['negative', 'zero', 'positive'].every(sign => counts[sign] === stats.delta[sign]), 'paired RMSE counts');
        }
      }
    }
    if (id === 'r') {
      studyCheck(exactStudyKeys(scenario.aggregates.baselines, ['gyro', 'complementary']), 'baseline aggregate methods');
      for (const method of ['gyro', 'complementary']) {
        const metricNames = Object.keys(phase.cases[caseId].trials[0].baselines[method]);
        studyCheck(exactStudyKeys(scenario.aggregates.baselines[method], metricNames), 'baseline aggregate fields');
        for (const metric of metricNames) {
          const values = phase.cases[caseId].trials.map(trial => trial.baselines[method][metric]);
          checkStatistics(scenario.aggregates.baselines[method][metric], summarizeStudyValues(values), 'baseline aggregate');
        }
      }
    }
  }
  return phase;
}

export async function buildStudies() {
  const result = { schema_version: 1, data_source: 'simulation', studies: {} };
  for (const [id, spec] of Object.entries(STUDY_SPECS)) {
    const phases = {};
    for (const phaseName of PHASES) {
      const bytes = await readFile(resolve(ROOT, `results/${spec.directory}/${phaseName}-summary.json`));
      phases[phaseName] = extractStudyPhase(JSON.parse(bytes.toString('utf8')), id, phaseName, bytes);
    }
    result.studies[id] = { reference_setting: spec.reference, settings: spec.settings, phases };
  }
  return validateStudies(result);
}

export function serializeStudies(data) { return `${JSON.stringify(data, null, 2)}\n`; }

async function main() {
  const args = process.argv.slice(2);
  studyCheck(args.length === 0 || (args.length === 1 && args[0] === '--check'), 'usage: export-studies.js [--check]');
  const text = serializeStudies(await buildStudies());
  if (args[0] === '--check') {
    studyCheck(await readFile(STUDIES_OUTPUT, 'utf8') === text, 'bundled artifact differs; run node web/scripts/export-studies.js');
    process.stdout.write('Parameter study artifact matches the four recorded summaries.\n');
  } else {
    await mkdir(dirname(STUDIES_OUTPUT), { recursive: true });
    await writeFile(STUDIES_OUTPUT, text, 'utf8');
    process.stdout.write('Exported web/public/data/parameter-studies.json from four recorded summaries.\n');
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch(error => { process.stderr.write(`${error.message}\n`); process.exitCode = 1; });
}
