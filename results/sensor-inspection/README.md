# Sensor inspection regression

Increment 2.4 inspects validated sensor inputs before estimation. The consumed
TUM-VI room2 recording reproduces its known reference-support gaps through the
new inspection artifacts and full-resolution window queries. **Evidence
verification passes; the historical scientific qualification remains failed.**
No calibration, clock fitting, estimator execution or new evaluation occurs.

## Room2 observations

The [recorded regression](room2-regression.json) binds the inspection to the
original calibrated `512_16` archive, the Meridian transfer archive and the
acquisition descriptor. The original archive is 1,759,395,840 bytes with SHA-256
`70343c5bbd9d674d0a2790bf934ee7d9cc08c1dea7cd77747a5557e4c94973f6`, matching the
[frozen acquisition](../hosted-acceptance/room2/acquisition.json).

| Stream | Observations | Mean observed rate | Gaps under the declared policy |
| --- | ---: | ---: | ---: |
| Each camera | 2,882 | 19.9994 Hz | 0 |
| IMU | 28,732 | 199.3722 Hz | 0 |
| Independent mocap reference | 15,477 | 107.4225 Hz | 49 |

Rates include gaps and are not configured nominal sensor rates. Camera and IMU
gaps use intervals strictly greater than three times their segment median;
reference gaps use the fixed 20 ms support limit. The longest reference gap is
2.491667 s. All 2,882 declared stereo pairs match exactly.

For either camera, 2,573 timestamps have reference support, 308 lie inside
reference brackets wider than 20 ms, and one lies after the final reference
pose: **89.2783% reference coverage**. Independent integer bracketing of the
source CSVs reproduces the inspection's counts and the
[historical reference-support audit](../hosted-acceptance/room2/reference-support-audit.json).
All 49 reference gaps are listed in a full-window query, and both endpoints of
each gap remain in its reduced display. Statistics use every observation before
display reduction. IMU support is reported separately: all 2,882 observations
of each camera have an exact sample or valid IMU bracket (100%); no IMU-support
gap or out-of-range observation is substituted for the reference gaps.

Every retained inspection file matches its size and SHA-256 in the actual child
outcome manifest: 5,963 files totaling 258,656,742 bytes. The summary binds to the
canonical acquisition identity, and each source index matches its acquisition
inventory. Historical acceptance and preparation identities remain unchanged.
The original 95% reference-coverage gate still fails, as does the historical
evaluated-coverage gate; inspection availability does not replace either result.

## Resource observation

The actual local import sandbox completes validation, preparation and inspection
in **218.514 s**, producing 17,506 result files totaling 5,522,056,577 bytes.
The measured container retains one CPU, 1 GiB memory without additional swap,
64 processes, no network, a read-only root and original archive, and dropped
capabilities. The configured timeout is 900 s with an 8 GiB disk scratch area.
The image identity and exact limits are in the regression JSON.

The observed cgroup memory peak reaches 1 GiB and includes filesystem cache and
all contained processes; it is not Python resident memory. The stopwatch excludes
upload, durable publication and copying the evidence for inspection. This
single development-host measurement does not establish throughput, real-time or
embedded performance.

## Reproduction and limits

Use the [local import guide](../../docs/sensor-import.md) to adapt the already
retained calibrated archive, then inspect its acquisition in a new directory:

```sh
python -m meridian.sensor_import tumvi \
  dataset-room2_512_16.tar outputs/room2-acquisition \
  --id room2-inspection-regression --sequence room2
python -m meridian.sensor_inspection \
  outputs/room2-acquisition outputs/room2-inspection
```

The standalone command reproduces inspection artifacts, not the Docker resource
measurement. The [inspection guide](../../docs/sensor-inspection.md) documents
the hosted route, explicit policies and resource limits. Analytic regression
cases are reproducible with:

```sh
python -m pytest tests/test_sensor_inspection.py -q
```

The 22 focused cases cover exact large timestamps, chunk-boundary gaps,
full-resolution statistics, gap-preserving reduction, fixed reference boundaries,
stereo ties and tolerance, independent clocks/reset eras, all source IMUs and SI
conversion, missing reference, preview evidence, numerical overflow and bounded
failure cleanup. These are generated cases, not physical sensor experiments.

Room2 was already consumed in increment 1.8; this regression does not reopen a
reserved evaluation. Images, IMU and reference payloads are not redistributed.
The TUM VI Benchmark authors and CC BY 4.0 license remain recorded in the
[source registry](../../docs/sensor-data-sources.md). Calibration suitability,
estimator compatibility and estimate support remain explicitly unassessed or
absent. There is no new bench, onboard or flight validation.

## Hosted and recovery verification

The [qualification record](qualification.json) reports 1,760 Python tests with
C++ integration, one native C++ test, 216 PostgreSQL workspace tests and 269 Web
tests. The 216 workspace tests also pass in the minimal API image without
Pillow or NumPy. The production Web build and recorded-study export check pass;
this increment adds no database migration.

Real browser checks pass locally (36 inspection, 33 acquisition lifecycle,
49 workspace) and over the canonical HTTPS deployment (36 inspection,
49 workspace), with no uncaught JavaScript or CSP errors. The analytic cases
verify separate 13/17 reference and 15/17 IMU support, a cross-chunk gap, empty
intervals inside gaps, exact image provenance, absent reference, reset ambiguity,
independent clocks, owner boundaries, deletion and logout. Dark, light and
390-pixel mobile layouts were inspected. Extreme numeric signal ranges produce
an explicit unavailable chart instead of nonfinite SVG coordinates.

The deployed release binds 126 source files to its bundle digest. Runtime
sources were checked in the API, worker and importer, and the database container,
image, mounts and original start time were preserved. All five services remain
healthy. Existing neighboring applications and the retained native runtime are
preserved; no hosted VIO job was started.

A quiescent hosted backup containing three synthetic acquisitions was verified
and transferred off server: 368 files, 1,073,838 bytes, with a measured write pause
of 16.264 s. A separate PostgreSQL restore uses no network, 256 MiB database
storage and a 512 MiB memory limit. Its database/file identities and restricted
application role match; copied sessions are cleared. **64 authenticated access
checks pass**, including byte-identical source-browser summary, window and
preview responses, original/prepared inputs and independent support counts.
The transferred API configuration and every filesystem layer match the active
API runtime despite differing OCI provenance identifiers.

QA accounts, retained synthetic uploads, disposable restore containers, dumps
and transferred runtime copies were removed after verification. Preexisting
application rows and shared authentication budgets were preserved. Public
evidence contains only aggregates, check descriptions and identities; it excludes
private accounts, database dumps and sensor payloads. This small manual recovery
exercise is not a full-quota benchmark or a scheduled backup service.

For an authorized test deployment, generate all four analytic browser inputs:

```sh
mkdir -p outputs/inspection-qa
python scripts/make-inspection-fixture.py outputs/inspection-qa/faulty.zip
python scripts/make-inspection-fixture.py outputs/inspection-qa/no-reference.zip --no-reference
python scripts/make-inspection-fixture.py outputs/inspection-qa/reset.zip --reset
python scripts/make-inspection-fixture.py outputs/inspection-qa/foreign-clock.zip --foreign-clock
node web/scripts/check-workspace-inspection-browser.mjs \
  http://127.0.0.1:8090/workspace.html outputs/inspection-qa/browser outputs/inspection-qa
```

The browser script requires Playwright/Chromium configured as in the existing
[workspace checks](../../docs/hosted-workspace.md). It creates temporary QA
accounts and retained synthetic recordings for recovery checks; operators must
remove those exact identities after completing their verification.
