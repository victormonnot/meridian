# C++ and Python vector-EKF agreement

The C++17/Eigen port implements the same roll/bias EKF as the Python reference.
This report checks implementation agreement on serialized input operations,
including the difficult cases where both filters can be physically wrong.
The [model and comparison contract](../../docs/cpp-ekf.md) defines the numerical
thresholds before evaluation and documents the standalone C++ API and event CLI.

**All 189 complete traces pass the declared agreement criteria.** The comparison
covers nine cases at displayed seed 42 and seeds 0–19: the eight
[controlled scenarios](../controlled-scenarios/README.md) and the earlier
[translation pulse](../ekf-comparison/README.md). There are 567,000 predictions,
55,650 corrections and 189 initializations: **622,839 records**, with
**4,070,934 scalar comparisons** across states, covariances and innovations.

## Reproduce

After preparing the main README's Python environment and installing a C++17
compiler, CMake 3.20+ and Eigen 3.4+:

```sh
cmake -S . -B build/cpp -DCMAKE_BUILD_TYPE=Release -DMERIDIAN_TEST_PYTHON="$PWD/.venv/bin/python"
cmake --build build/cpp --parallel 2
ctest --test-dir build/cpp --output-on-failure
python -m meridian.cpp_parity --binary build/cpp/meridian_ekf_replay --output outputs/cpp-parity
```

The equivalent Python CLI entry point is `meridian-cpp-parity`. Use a new output
directory. These commands were verified on Linux with GCC 13.3.0, CMake 3.28.3,
Eigen 3.4.0, Python 3.12.3, and NumPy 2.5.3. The executable reports its build
configuration as Release. Its name, SHA-256 and compiler/Eigen metadata are
retained in the [selected summary](summary.json).

Two runs, through the Python module and installed entry point using the same
Release binary, produced **28 byte-identical artifacts**. The selected JSON was
copied unchanged from that run. Cross-language traces are compared numerically;
Python and C++ trace bytes are not claimed identical. Rebuilding elsewhere can
change the binary hash and numerical details; exact bytes across environments
are not promised.

## Observed differences

For every finite scalar entry, the requirement is
`abs(cpp-python) <= atol + 1e-10 * abs(python)`.
The final column is the largest observed ratio of the difference to that full
tolerance, over the whole suite; it must be at most 1. A maximum difference and
maximum ratio can occur at different records. Covariance entries are listed
separately because they have different units.

| Quantity | Unit | Maximum absolute difference | Absolute tolerance | Maximum tolerance ratio |
| --- | --- | ---: | ---: | ---: |
| Roll | rad | 2.997602e-14 | 1e-10 | 2.479245e-04 |
| Bias | rad/s | 6.099288e-15 | 1e-10 | 5.830333e-05 |
| P angle variance | rad^2 | 6.166400e-19 | 1e-12 | 6.132921e-07 |
| P angle/bias covariance | rad^2/s | 1.456897e-19 | 1e-12 | 1.455576e-07 |
| P bias/angle covariance | rad^2/s | 1.456897e-19 | 1e-12 | 1.455576e-07 |
| P bias variance | rad^2/s^2 | 2.710505e-19 | 1e-12 | 2.700227e-07 |
| Innovation y | m/s^2 | 2.859935e-13 | 1e-10 | 1.084054e-03 |
| Innovation z | m/s^2 | 6.750156e-14 | 1e-10 | 4.776834e-04 |
| S yy | m^2/s^4 | 5.273559e-16 | 1e-10 | 4.891352e-06 |
| S yz | m^2/s^4 | 1.080733e-15 | 1e-10 | 1.071094e-05 |
| S zy | m^2/s^4 | 1.080733e-15 | 1e-10 | 1.071094e-05 |
| S zz | m^2/s^4 | 5.412337e-16 | 1e-10 | 5.193568e-06 |

State differences by scenario, maxima over all 21 seeds and every record:

| Scenario | Maximum roll difference (rad) | Maximum bias difference (rad/s) |
| --- | ---: | ---: |
| `nominal` | 1.942890e-16 | 4.857226e-17 |
| `initial_offset` | 2.997602e-14 | 6.099288e-15 |
| `initial_overconfident` | 6.578071e-15 | 2.359224e-15 |
| `bias_ramp` | 3.330669e-16 | 6.071532e-17 |
| `accel_dropout` | 2.220446e-16 | 4.857226e-17 |
| `timing_jitter` | 1.110223e-16 | 4.163336e-17 |
| `accel_noise_mismatch` | 3.330669e-16 | 8.673617e-17 |
| `accel_delay` | 2.220446e-16 | 8.153200e-17 |
| `translation_pulse` | 6.661338e-16 | 1.665335e-16 |

These differences are well below the declared bounds. No tolerance or filter
setting was relaxed after evaluating the port. They describe agreement between
implementations, not angle accuracy against a physical reference. The
initialization, varying-bias, noise, dropout, and delay limitations observed in
the original Python results remain unchanged.

## What is checked

Both implementations consume the same decimal input values after full-precision
serialization. Gyro dt is passed directly; it is not reconstructed from a running
time sum. Missing accelerometer observations produce no update event. Delayed
observations retain the earlier experiment's uncompensated arrival schedule.
No truth, true bias, or hidden acquisition time enters the C++ executable.

Every initial state, prediction and correction is compared. Both off-diagonal
entries of P and S are checked independently, along with roll, bias, and the two
innovation components. Trace lengths, step numbers, operation ordering, finite
values, and diagnostic availability are checked separately; initial/prediction
records have empty innovation fields. This would detect an intermediate mismatch
that disappears before the last state.

Native C++ tests separately verify analytical propagation, a scalar tangent
correction oracle, finite-difference Jacobians, covariance behavior, copied
state ownership, invalid inputs and atomic numerical failures. They retain
zero-force and opposite-vector local failure cases. The checks remain active
in Release builds. Python agreement supplements those checks rather than serving
as the sole mathematical oracle.

Validation executed for this snapshot:

- Release CTest: both the native core test and Python/C++ integration job pass.
- Full pytest with `MERIDIAN_CPP_BINARY` explicitly set: **490 passed**, with no
  skipped C++ tests (44 tests added in this brick).
- Debug native CTest built with `-fsanitize=undefined -fno-sanitize-recover=all`:
  passes without an UndefinedBehaviorSanitizer diagnostic. This is a separate
  native test run, not the complete 189-trace report under sanitization.
- The native executable performs 21,607 active numerical/contract checks; this
  count is distinct from pytest test cases and CTest jobs.

To run the full Python suite with C++ enabled on Linux:

```sh
MERIDIAN_CPP_BINARY="$PWD/build/cpp/meridian_ekf_replay" python -m pytest -q
```

Without that variable, ordinary Python-only pytest skips the 25 C++ integration
cases. The 19 Python-side serialization/comparison checks still run. CTest sets
the executable explicitly when configured with `MERIDIAN_TEST_PYTHON`.

## Artifacts and boundaries

Each displayed seed-42 scenario retains three files under the local output:
`events.csv`, `python_trace.csv`, and `cpp_trace.csv`. The JSON stores configuration,
input hashes and per-quantity differences for all selected and repeat trials.
Repeat trace files are temporary and removed after each comparison; their
summary records remain. The one summary plus nine sets of three files gives
28 artifacts. Only the selected summary and this report are stored publicly.

The core uses fixed-size double matrices and Eigen LDLT with positive finite
pivot checks; NumPy uses a different linear solver. Both apply one prior
linearization and Joseph covariance correction without an explicit inverse.
Extreme ill-conditioned failure domains can differ. This comparison does not
cover arbitrary inputs, every compiler/CPU, float32 arithmetic, or unmodeled
sensor effects.

The native replay tool buffers its output until successful input processing and
refuses existing paths. It is an offline adapter for explicit filter operations,
not a new raw-IMU/DataFlash decoder or a flight-controller task. No execution-time
or allocation guarantee, embedded deployment, or flight validation is established.
New documented bench acquisition and replay with a physical reference remain
required for the broader study.
