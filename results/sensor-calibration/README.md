# Supplied calibration and time-mapping qualification

Increment 2.5 imports immutable supplied artifacts and prepares separate input
revisions. The analytic qualification verifies bias/scale/rotation order, exact
constant time mappings, retained provenance and prior-version preservation.
It does not fit a calibration or establish physical accuracy, independent
physical validation, private-input VIO performance or flight readiness.

## Analytic observations

The [fixture generator](../../scripts/make-calibration-fixture.py) creates three
synthetic acquisition identities, two supplied calibration candidates, a clock
domain mapping of +2 ns and a separate sensor offset of −2 ns. The three
acquisition identities exercise lineage and ownership; they do not represent
independent physical sessions. The external validation report is explicitly an
unverified supplied claim.

[analytic.json](analytic.json) records exact source/artifact identities and
expected rows computed without Meridian's preparation helpers. Candidate A
subtracts gyro bias `[0.1, 0.2, 0.3]` rad/s, applies diagonal gains `[2, 3, 4]`,
then rotates +90° about source Z. For the first observation `[1, 2, 3]` rad/s,
the result is `[-5.4, 1.8, 10.8]` rad/s. The supplied force bias/gains and the
same rotation produce `[-6, 1.5, 3]` m/s² from `[4, 5, 6]` m/s².

The first IMU timestamp changes from `1520530308194447626` to
`1520530308194447628` ns under the domain mapping. The separate −2 ns sensor
offset yields `1520530308194447624` ns. Both are beyond JavaScript's exact
`Number` integer range and are retained as decimal strings.

Candidate B changes the first gyro bias to 0.6 rad/s. After gain and rotation,
the first corrected observation becomes `[-5.4, 0.8, 10.8]` rad/s. Both complete
three-row IMU CSVs are 254 bytes and match their independently generated
expected bytes:

| Candidate | Corrected IMU CSV SHA-256 |
| --- | --- |
| A | `e5d933a8691379cfafb632510a64a27386ad876816ed4e6fe925ff77f866f529` |
| B | `92dba61a567e326ccdf0b3be6106319cecd710087508325612da5fd0200feca0` |

Changing the candidate creates a distinct prepared contract. Original pixels,
observations, references, source inspection and candidate A remain unchanged.
Camera geometry and noise parameters are retained separately from executed
measurement operations. Reported validation remains unverified.

## Lifecycle and ownership

[operations.json](operations.json) records 18 checks against actual local
isolated child containers. Cancellation of an intentionally paused revision
removes the sandbox, publishes no files and releases the reservation. A second
revision stays queued with zero attempts while the first owns the compute slot.
After an importer SIGKILL and lease expiry, recovery uses a new attempt; only
the second attempt publishes, and all 12 recovered files match their downloaded
bytes and hashes. Original source and pre-existing tracked records remain
unchanged. Across 33 observations, at most one global compute slot is active;
no estimator attempt is created.

The cancellation/recovery source includes a 128 MiB synthetic original-file
padding payload to create an observable fault window. It is neither sensor
evidence nor a throughput benchmark. Polling records observed concurrency;
it does not measure every instant of execution.

The [qualification record](qualification.json) separates automated tests,
local/HTTPS browser checks and backup restoration. The browser checks actual
artifact imports, compatibility refusal, immutable candidate creation,
independently expected CSVs, ownership, deletion and unavailable files. Backend
tests additionally cover PostgreSQL races between quota admission, source
deletion and preparation submission, and VIO/revision claims.

## Reproduce the analytic corrections

Use the locked Python environment and new output directories:

```sh
python scripts/make-calibration-fixture.py build/calibration-fixture
python -m meridian.sensor_import package \
  build/calibration-fixture/replay.zip build/calibration-replay
python -m meridian.sensor_import prepare \
  build/calibration-replay/acquisition build/calibration-candidate-a \
  --calibration build/calibration-fixture/calibration.json \
  --dependency-acquisition build/calibration-fixture/source-acquisition.json \
  --dependency-acquisition build/calibration-fixture/validation-acquisition.json \
  --clock-mapping build/calibration-fixture/clock-domain.json \
  --apply-imu-intrinsics --imu-target-frame imu-corrected
```

Compare `sensors/imu0/data.csv` with the generated manifest's
`expected_revision_a`. Select `calibration-alternative.json` and another new
directory for candidate B. Core and sandbox regression checks are:

```sh
python -m pytest -q tests/test_sensor_calibration.py \
  tests/test_sensor_hosted_revision.py tests/test_revision_execution.py
```

The repeatable [browser recipe](../../web/scripts/check-workspace-calibration-browser.mjs)
accepts a workspace URL, an output directory and the generated fixture directory.
It requires an operator-authorized test deployment and leaves exact QA identities
in its private report for backup/restoration and cleanup. Do not publish those
raw reports or backup contents.

The [implementation guide](../../docs/sensor-calibration.md) documents supported
geometry, time conventions, budgets and limitations. Drift, implicit composition
of clock corrections, calibration fitting, image rectification and private-input
estimator execution remain outside this increment. No reserved recording or new
physical acquisition was used; prior room2 qualification remains unchanged.
