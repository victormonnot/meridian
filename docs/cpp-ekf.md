# C++ EKF and Python agreement contract

The C++ port implements the existing [vector EKF model](vector-ekf.md): the same
two states, force observation, Jacobian, constant-bias prediction, per-interval
gyro noise, and Joseph covariance correction. It is an independent implementation
of that model; matching Python does not establish physical accuracy or remove the
known initialization, translation, varying-bias, and delay limitations.

## Implementation choices

The core uses C++17, Eigen matrices of fixed size 2, and `double` arithmetic.
Eigen expresses the matrix operations and supplies an LDLT solve for the symmetric
innovation covariance. NumPy uses its general linear-system solver. Neither code
forms an explicit inverse. Floating-point evaluation orders can differ, so the
comparison uses declared tolerances rather than byte equality across languages.

The CMake build requires CMake 3.20+ and Eigen 3.4+. It uses the imported
`Eigen3::Eigen` target described in the [Eigen CMake guide](https://libeigen.gitlab.io/eigen/docs-3.4/TopicCMakeGuide.html).
Eigen is provided by the build environment, with no network download in CMake.
The initial verified environment has GCC 13.3, CMake 3.28.3 and Eigen 3.4.0 on Linux.
Python dependencies and C++ dependencies remain separate.

The core returns state and covariance by value, accepts explicit prediction
intervals, and has no file, plotting, vehicle, or web dependencies. Invalid input
and numerical failures throw exceptions before committing a new state/covariance.
Numerically unusable LDLT factors are rejected. Failure behavior at extreme
ill-conditioning need not match NumPy's different solver; the agreement report
only covers the stated test cases and numeric range.

## Build and test

Install a C++17 compiler, CMake and Eigen development headers using the host's
package manager. For example, Debian/Ubuntu packages are `g++`, `cmake`, and
`libeigen3-dev`. After creating the Python environment from the main README:

```sh
cmake -S . -B build/cpp -DCMAKE_BUILD_TYPE=Release -DMERIDIAN_TEST_PYTHON="$PWD/.venv/bin/python"
cmake --build build/cpp --parallel 2
ctest --test-dir build/cpp --output-on-failure
python -m meridian.cpp_parity --binary build/cpp/meridian_ekf_replay --output outputs/cpp-parity
```

Choose a new output directory. Build files under `build/` and generated runs under
`outputs/` are ignored. Native unit tests require no Python. Setting
`MERIDIAN_TEST_PYTHON` adds a CTest job that runs the CLI and Python agreement tests
with the exact built executable; the tests do not silently skip that configured
binary. Ordinary Python-only pytest runs skip C++ integration tests unless
`MERIDIAN_CPP_BINARY` names an executable. CMake does not install Python packages.

For a standalone core/replay build, omit `MERIDIAN_TEST_PYTHON`. On generators
with multiple configurations, use `cmake --build build/cpp --config Release`
and `ctest --test-dir build/cpp -C Release --output-on-failure`, then select the
corresponding executable path. Other operating systems and compilers require
their own verification; a successful Linux build is not a portability guarantee.

## Event replay protocol

The small replay executable separates files from the numerical core. Its input
CSV has exactly three columns:

```text
operation,value0,value1
predict,0.12,0.01
update,-0.15,-9.8
```

For `predict`, values are the measured interval-mean gyro rate in rad/s and dt in
seconds. For `update`, they are body y/z specific force in m/s². Events execute
in file order. The caller supplies time ordering through the event sequence and
prediction durations; the protocol is not a general sensor-log format. There is
no truth, hidden acquisition time, gap filling, or automatic delayed update.

Pass `--input` and a new `--output` path plus all seven numeric settings:
`--initial-angle-rad`, `--initial-bias-rad-s`, `--initial-angle-std-rad`,
`--initial-bias-std-rad-s`, `--gyro-noise-std-rad-s`, `--accel-noise-std-m-s2`,
and `--gravity-m-s2`. `--help` describes the protocol; `--version` reports the
compiler, build configuration, Eigen version, C++ standard and protocol version.

Output contains one initial record and one record after each operation. Each
record includes step, operation, state and all four covariance entries. Update
records also include the prior vector innovation and all four entries of S;
these fields are empty on initial/prediction records. Floating-point values use
round-trip decimal precision and a fixed C locale. Malformed input, unknown or
duplicate options, invalid filter operations, and existing output paths are
refused. Input is replayed successfully before output creation.

## Comparison declared before evaluation

Python serializes the inputs once, reads back those decimal values, and runs
the reference on the same numbers consumed by C++. Configuration is explicit.
Compare initialization, every prediction, and every available correction, rather
than only final state or aggregate accuracy. Trace row counts, step indices,
operations and diagnostic availability must agree exactly.

For each scalar entry, require `abs(cpp - python) <= atol + rtol * abs(python)`:

| Compared values | Absolute tolerance (atol) | Relative tolerance (rtol) |
| --- | ---: | ---: |
| Roll (rad), bias (rad/s) | 1e-10 in the respective unit | 1e-10 |
| P entries (rad², rad²/s, rad²/s²) | 1e-12 in the respective unit | 1e-10 |
| Innovation components (m/s²) | 1e-10 | 1e-10 |
| S entries (m²/s⁴) | 1e-10 | 1e-10 |

These tolerances are declared before running the port and are numerical agreement
criteria, not sensor accuracy specifications. Check the eight
[controlled cases](controlled-scenarios.md) plus the existing translation pulse,
each at seed 42 and seeds 0–19: **189 complete traces**. No model parameters are
retuned and no physical accuracy requirement is added to the difficult cases.

Native C++ tests separately check poses/Jacobians, analytical prediction and
correction, covariance behavior, failure atomicity, and local zero/opposite-force
limitations. Agreement between two implementations complements these independent
checks; it cannot prove that their shared physical assumptions are correct.
No hardware deployment, execution-time guarantee, allocation benchmark, float32
assessment, embedded integration, or flight validation is claimed by this port.
