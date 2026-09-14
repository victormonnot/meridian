# Recorded IMU comparison between Python and C++

`meridian.imu_cpp_parity` runs the existing [snapshot replay](imu-replay.md),
converts its selected measurements into explicit EKF operations, and compares
Python with the native [C++ executable](cpp-ekf.md) after every operation.
The C++ core and protocol are unchanged. The driver retains source provenance,
logged-time mapping, full traces, and a separate check against the snapshot replay.
The [historical result](../results/imu-cpp-parity/README.md) reports numerical
agreement on recorded data; it does not measure physical estimation accuracy.

## Run

Install the Python environment and optional DataFlash decoder as described in the
[replay guide](imu-replay.md), build the native executable using the
[C++ guide](cpp-ekf.md), and audit the recording before choosing a segment:

```sh
python -m meridian.imu_cpp_parity /path/to/recording.bin \
  --binary build/cpp/meridian_ekf_replay \
  --instance 0 --segment 5 --output outputs/imu-cpp-comparison
```

The installed equivalent is `meridian-imu-cpp-parity`. IDs above are examples;
select an instance and segment from the audit, with the same gap threshold.
The five replay options are also accepted: `--gyro-noise-std-deg-s`,
`--angle-noise-std-deg`, `--initial-bias-std-deg-s`, `--complementary-tau-s`,
and `--max-gap-s`. Defaults and their interpretation are unchanged. The
complementary setting applies only to the retained baseline replay.

The source and executable must exist, and the output directory must be new.
Exit status is 0 when both numerical comparisons pass, 1 for numerical
disagreement, and 2 for invalid input, decoder, native process or artifact errors.
A failed operation can leave partial diagnostic files; an absent top-level
`summary.json` is not a completed comparison. A numerical failure retains a
summary with `passed: false`. Existing outputs are never overwritten.

## Operation and timestamp contract

For `N` selected rows, the event trace has `2N - 1` records:

| Step | Operation | Input | State timestamp |
| --- | --- | --- | --- |
| 0 | Initialization | First accelerometer tilt, zero bias, declared prior covariance | `TimeUS[0]` |
| `2k - 1` | Prediction | `gyro_x[k-1]`, `(TimeUS[k] - TimeUS[k-1]) / 1e6` | `TimeUS[k]` |
| `2k` | Correction | Unnormalized `accel_y[k]`, `accel_z[k]` | `TimeUS[k]` |

Here `k = 1 … N-1`. Timestamp subtraction uses integers before conversion to
seconds. The first tilt initializes once, with no correction at step 0; the last
gyro has no following interval. This is a previous-snapshot hold, not a measured
interval mean or verified sensor synchronization. `TimeUS` remains logger time.

`timeline.csv` gives each trace step's operation, endpoint IMU record index and
integer `TimeUS`, plus the input record index and integer `TimeUS`. A prediction's
input comes from the previous row while its state belongs to the current endpoint.
The native three-column event file contains rates/durations or force components;
timestamp provenance remains in this sidecar and `python_replay/measurements.csv`.

## Two comparisons

Both implementations decode the same event CSV at round-trip decimal precision.
The first comparison links the Python event trace's initialization and corrected
endpoints to `ekf_estimates.csv`, and its prior innovations/S to
`ekf_innovations.csv`. Row counts, operation order and logged times are checked.
This guards against comparing two implementations on an incorrectly mapped stream.
Snapshot CSVs store symmetric upper triangles of P and S; the linkage reconstructs
their lower triangles. It does not recover unexported asymmetry or intermediate
prediction states from those files.

The second comparison checks initialization, every prediction and every correction
between Python and C++: two state entries, all four P entries, both prior
innovation components, and all four S entries. Innovations/S exist only on update
rows. The same [predeclared per-quantity tolerances](cpp-ekf.md#comparison-declared-before-evaluation)
apply to both comparisons without adjustment to the recording. The summary gives
counts, maximum absolute differences and maximum ratios to allowed tolerance.
NIS is not emitted by the native protocol; innovation and S are compared directly.

## Retained evidence

| Artifact | Content |
| --- | --- |
| `python_replay/` | Seven unchanged outputs from the four-method Python snapshot replay, including source metadata and model limitations. |
| `events.csv` | Exact prediction/update input numbers read by both implementations. |
| `timeline.csv` | Operation-to-record mapping with integer logged timestamps. |
| `python_trace.csv`, `cpp_trace.csv` | Full state, P and available innovation/S after every operation. |
| `summary.json` | Source/binary fingerprints, compiler/Eigen/Python versions, selection, SI configuration, units, fixed tolerances, both comparisons and hashes of the other eleven files. |

The driver checks the recording and executable fingerprints before/after execution,
and verifies that generated inputs and retained evidence stayed unchanged during
comparison. Source paths are not included in summaries. Outputs remain local under
`outputs/`; raw recordings and real traces are not distributed with the repository.
To check repeatability, run twice into different directories and compare every
file byte for byte in the same environment. Cross-compiler equality is assessed
by tolerances, not promised byte equality.

Public tests use synthetic snapshots with changing rates, unequal intervals and
large integer timestamps; they require no historical recording. Configured CTest
runs include `tests/test_imu_cpp_parity.py` with the exact built executable.

This comparison exercises the port on recorded sensor values and timing. Shared
model errors, correlated snapshots, unknown sensor delays and absence of independent
truth remain. A new documented acquisition with known poses and stated reference
uncertainty remains necessary. No embedded, real-time or flight validation is claimed.
