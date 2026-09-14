# Python/C++ agreement on recorded IMU snapshots

The unchanged C++ EKF was replayed on the same historical segment as the
[four-method Python experiment](../imu-replay/README.md). All **9,913 initial,
prediction and correction records** passed the existing numerical tolerances.
The serialized Python trace also reproduced every exported snapshot endpoint
and prior innovation/S value exactly in the recorded environment.

This is implementation agreement on recorded inputs. There is no independent
roll-angle or gyroscope-bias reference, and the model/tuning were not adjusted
to obtain agreement. This result does not establish estimation accuracy or
complete the required documented bench acquisition.

## Input and execution

| Quantity | Recorded value |
| --- | --- |
| Source SHA-256 | `47b6476bc77cc1ccd9c1248a31eeaeec8866c77062c176127411192d84987191` |
| Source size | 8,127,996 bytes |
| Selection | IMU instance 0, segment 5 at the 0.2 s gap threshold |
| Snapshots / duration | 4,957 / 198.268980 s |
| Predictions / corrections | 4,956 / 4,956 |
| Compared scalar values | 89,214 across state, P, innovation and S |
| Native build | GNU 13.3.0, Release, C++17, Eigen 3.4.0, protocol 1, Linux |
| Executable SHA-256 | `782c85691878f66e419fca604ed3d2259630e86ac177eacaaf8f8162d445e4e7` |
| Python environment | Python 3.12.3, NumPy 2.5.3, Matplotlib 3.11.2, pymavlink 2.4.49 |

The executable fingerprint identifies the tested build, not a portable expected
hash for every rebuild. Decoder metadata still reports 37 unparsed trailing
bytes and no parser diagnostics. Historical recording conditions have not been
recertified. The source, full traces and machine paths are not distributed.

Initialization and tuning match the preceding Python experiment: first tilt
−0.716461429°, zero bias, initial angle/bias standard deviations 2° and 1°/s,
gyro standard deviation 1°/s, fixed `g = 9.80665 m/s²`, and force component
standard deviation `g × radians(2) = 0.34231666218140383 m/s²`.
The previous gyro snapshot is held over each integer-timestamp interval, then
the current unnormalized body y/z force corrects the filter. There is no
correction at initialization and no propagation using the final gyro snapshot.

## Numerical differences

Tolerances remain `atol + rtol × abs(Python)`: `rtol = 1e-10`, `atol = 1e-12`
for P and `1e-10` for the other quantities in their respective SI units.
Each state/P entry has 9,913 comparisons; each innovation/S entry has 4,956.

| Quantity | Maximum absolute C++−Python difference | Unit |
| --- | ---: | --- |
| Roll | 3.46945e−18 | rad |
| Bias | 2.10064e−19 | rad/s |
| P00 | 9.48677e−20 | rad² |
| P01 and P10, each | 4.06576e−20 | rad²/s |
| P11 | 4.06576e−20 | rad²/s² |
| Innovation y | 4.16334e−17 | m/s² |
| Innovation z | 1.77636e−15 | m/s² |
| S00 | 2.77556e−17 | m²/s⁴ |
| S01 and S10, each | 1.15196e−19 | m²/s⁴ |
| S11 | 0 | m²/s⁴ |

The largest ratio of difference to allowed tolerance was 1.60205e−5, below the
pass limit of 1. Full event traces compare all four P/S entries separately.
The snapshot linkage reconstructs symmetric lower triangles from the original
CSV layouts; its maximum difference was zero for every compared quantity.
NIS is not a native output field; its innovation and covariance inputs are checked.

## Reproduce and interpret

Follow the [recorded comparison guide](../../docs/imu-cpp-parity.md) after
installing the optional decoder, building C++ and obtaining the matching source:

```sh
python -m meridian.imu_cpp_parity /path/to/recording.bin \
  --binary build/cpp/meridian_ekf_replay \
  --instance 0 --segment 5 --output outputs/imu-cpp-comparison
meridian-imu-cpp-parity /path/to/recording.bin \
  --binary build/cpp/meridian_ekf_replay \
  --instance 0 --segment 5 --output outputs/imu-cpp-comparison-repeat
```

Two runs produced all twelve artifacts byte for byte identically in this
environment. Every nested Python replay file remained byte-identical to the
preceding seven-file baseline, and source/executable fingerprints stayed intact.
Repeatability was checked by comparing the files; each invocation alone does
not perform the second run automatically.

Synthetic public tests exercise irregular intervals, interleaved instances,
timestamps near the int64 limit, exact step/source mapping, snapshot linkage,
native disagreement, malformed version responses, process failure and artifact
changes. They do not need access to the historical log. The native core tests
and Python suite with native integration enabled also pass.

Agreement preserves the limitations of the [historical replay](../imu-replay/README.md):
unknown sensor delay/correlation, illustrative noise, single-axis assumptions
and no independent truth. Matching calculations cannot certify those assumptions,
sensor synchronization or recording conditions. The web explorer remains based
on simulations. No embedded, real-time or flight validation is claimed.
