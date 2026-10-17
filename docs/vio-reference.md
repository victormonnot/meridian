# Reference visual-inertial replay

**Status: local reference implementation, increment 1.1.** This document records
the execution contract and protocol fixed before the first reference execution.
The [qualification report](../results/vio-reference/README.md) records the actual
results. See also the [part 1 plan](part-1-hosted-vio-workspace.md).

## Reference and separation

The reference is OpenVINS revision
`69488123ed9362dd44b6f28e7f4680abbff1442b`, executed without ROS through the Meridian
adapter, on the calibrated TUM-VI `room1` EuRoC-format `512_16` recording. The
original archive contains 16-bit images; conversion to the estimator input format
must be specified by the adapter. Dataset preprocessing has already corrected
IMU scaling/alignment and synchronized the clocks. Do not apply raw-recording
calibration corrections a second time.

The estimator container receives only camera/IMU files, its configuration, and an
output directory. Reference poses are prepared outside these mounts and read by
the independent evaluator after execution. The native runner has no ground-truth
initialization option. The image is selected by its content identity, with no
runtime network, a read-only root filesystem, a non-root user, and explicit CPU,
memory, process, temporary-storage, and wall-time limits. This is a local
qualification runner for a maintained implementation, not a hosted job service.

TUM-VI is a handheld benchmark. Its motion-capture poses evaluate a GNSS-free
estimate but are never estimator inputs. `room2` remains reserved and is not
downloaded or evaluated in this increment.

## Frozen initial evaluation protocol

Protocol version 1 uses integer nanosecond timestamps, position in metres, and
unit Hamilton quaternions in `qx,qy,qz,qw` order representing IMU-to-world rotation.
The normalized trajectory columns are
`timestamp_ns,px_m,py_m,pz_m,qx,qy,qz,qw`. Original sensor timestamps and their
relationship to the estimator clock are retained.

The expected output timeline is the full paired camera timeline. Associate each
estimate with at most one camera timestamp within 1 microsecond; do not
interpolate missing estimates. Interpolate reference positions linearly and
orientations by shortest-arc quaternion interpolation only inside bracketing
reference samples at most 20 ms apart. Do not extrapolate or optimize time offset.

Fit one least-squares rigid SE(3) alignment over the evaluated positions with
**scale fixed to one**. Apply that same rotation to estimated orientations. This
removes a constant world-frame translation and rotation, including pitch/roll;
the resulting orientation error is not an independent absolute-gravity check.
Reject degenerate position support. Report position ATE RMSE, orientation RMSE,
support counts, reference coverage, estimate coverage, evaluated coverage,
initialization delay (first emitted pose relative to the first camera sample),
and missing intervals. This delay differs from an internal initialization event
or wall-clock latency. These scores describe the aligned
trajectory on the stated support and do not establish consistency of covariance.

The following integration sanity gates are fixed before examining room1 results:

| Quantity | Required value |
| --- | --- |
| Native completion | Complete and initialized, with validated outputs |
| Evaluated samples | At least 100 |
| Estimate coverage of full camera timeline | At least 90% |
| Evaluated coverage of full camera timeline | At least 90% |
| Reference coverage of full camera timeline | At least 95% |
| Initialization delay from first camera sample | At most 10 s |
| Aligned position ATE RMSE | At most 0.5 m |
| Aligned orientation RMSE | At most 15 degrees |

These deliberately loose criteria reject integration failures; they are not
drone navigation requirements or competitive accuracy targets. Known-error
synthetic fixtures separately check evaluator behavior, including scale, time,
frame conversion, and missing-data handling. All failed runs and any subsequent
protocol changes must remain identified in the qualification record.

Three identical executions characterize repeatability under the declared runtime.
Require identical output timestamps and configuration/input identities. Report
byte identity as well as maximum direct position and orientation differences;
the initial tolerances are 1 mm and 0.001 degrees without refitting alignment.
These checks characterize this host and runtime, not all CPU platforms.

Record estimator elapsed/CPU time and peak resident memory, outer container
elapsed time, input/artifact sizes, container resource limits, runtime image
identity, dependency versions, configuration/data hashes, adapter revision, and
host CPU information. Offline wall-time ratios do not establish real-time
deadline behavior or embedded performance.

## Configuration and available diagnostics

The checked-in [OpenVINS configuration](../integrations/openvins/config/estimator_config.yaml)
uses stereo KLT tracking, `num_pts: 200`, 11 pose clones, up to 50 SLAM features,
RK4 integration, first-estimate Jacobians, and static initialization. Camera
intrinsics, camera/IMU extrinsics, and camera/IMU time offset are estimated online;
their initial values come from the provided calibration, with initial time offset
zero. IMU intrinsic calibration and gravity magnitude are fixed. Threading is
limited to one, and every paired camera observation is processed subject to
available IMU bracketing. There is no ROS callback-frequency throttle.

The selected continuous-time IMU noise settings are:

| Setting | Value | Unit |
| --- | ---: | --- |
| Gyroscope white noise density | 0.00016 | (rad/s)/√Hz |
| Accelerometer white noise density | 0.0028 | (m/s²)/√Hz |
| Gyroscope bias random walk | 0.000022 | (rad/s)/√s |
| Accelerometer bias random walk | 0.00086 | (m/s²)/√s |

These are the reference configuration's assumptions, including the dataset's
documented noise inflation; they are not new Meridian sensor measurements or
per-sample variances. Camera calibration is pinhole/equidistant at 512 × 512,
with the upstream masks. The native adapter converts uint16 image values to
uint8 using the fixed factor `255/65535` before tracking.

Pose, input/processed counts, first-output delay, initialization state, and
resource measurements are exported. Velocity, biases, full covariance, visual
tracks, and innovations are not yet exported; their availability in OpenVINS
does not make them Meridian outputs. `num_pts` is the selected parameter for the
first later configuration comparison: an integer feature budget divided between
the two cameras after initialization (200 gives a target of 100 per camera).
The proposed initial choices are 100, 200 and 400; only the 200 baseline is
qualified here. Changing the budget can trade computation against visual support.
No variant or tuning campaign is claimed by this qualification.

## Reproduce the qualification

Use the existing Python environment from the [README](../README.md) and a Linux
Docker engine. Build and acquisition need network access; estimator execution
does not. The archive is about 1.71 GB and the prepared inputs about 1.70 GB;
allow additional space for the image/build cache and outputs. Commands refuse
existing destinations so previous evidence is not overwritten.

```sh
mkdir -p outputs/vio-reference-data
docker build -t meridian-openvins:reference integrations/openvins
python integrations/openvins/smoke_test.py meridian-openvins:reference
python -m meridian.vio_reference download \
  --output outputs/vio-reference-data/dataset-room1_512_16.tar
python -m meridian.vio_reference prepare \
  --archive outputs/vio-reference-data/dataset-room1_512_16.tar \
  --config integrations/openvins/config \
  --output outputs/vio-reference-data/prepared \
  > outputs/vio-reference-data/preparation-report.json
python -m meridian.vio_reference run \
  --prepared outputs/vio-reference-data/prepared \
  --output outputs/vio-reference/room1-run-1
python -m meridian.vio_reference run \
  --prepared outputs/vio-reference-data/prepared \
  --output outputs/vio-reference/room1-run-2
python -m meridian.vio_reference run \
  --prepared outputs/vio-reference-data/prepared \
  --output outputs/vio-reference/room1-run-3
python -m meridian.vio_reference compare-repeats \
  --runs outputs/vio-reference/room1-run-1 outputs/vio-reference/room1-run-2 \
         outputs/vio-reference/room1-run-3 \
  --output outputs/vio-reference/repeatability.json
```

Preparation checks the exact archive hash, stream conventions, image metadata,
configuration copies, and all individual file hashes. It recognizes but does not
extract the archive's two known DSO image symlink aliases. Every other link or
unsafe archive path is rejected. The independent reference is normalized by
reordering quaternion components without altering its timestamps or poses.

Each run verifies the prepared inputs before and after execution, resolves the
image tag to a content ID, and records a versioned `run-spec.json`. The output
includes `trajectory.csv`, `status.json`, `evaluation.json`, `result.json`, logs,
installed dependency versions, and native binary hashes. `result.json` binds
artifact hashes to the run specification. Invalid/missing native outputs remain
failed attempts. A completed computation with failed numerical gates returns
CLI exit code 1; execution or validation errors return code 2.

Default execution limits are 2 CPUs, 4096 MiB memory with no additional swap,
128 processes, 128 MiB `/tmp`, and 900 seconds wall time. These are caps, not
measured requirements. Native RSS excludes container-wide filesystem cache;
workspace artifact size and the temporary-storage cap are recorded separately.
The adapter does not continuously instrument temporary storage; a separate
[resource probe](../results/vio-reference/README.md#repetition-and-resource-measurements)
samples `/tmp` occupancy during an additional execution with the same inputs
and limits. Shorter transients can be missed. Output bind-mount storage is
measured after execution, not quota-enforced by this local runner; hosted
job/storage quotas belong to increment 1.4.

The [native integration guide](../integrations/openvins/README.md) describes build
pins, scheduling and output conventions. The runtime image deliberately excludes
upstream benchmark references and Git objects, retaining only the required
estimator source directories, adapter, dependencies, binaries and notices. Direct
package versions and source commits are pinned; future apt repository changes
can prevent a rebuild. Preserve the recorded image for exact runtime reuse.

Run the focused checks without the acquired dataset or Docker:

```sh
python -m pytest -q tests/test_vio_dataset.py tests/test_vio_evaluation.py \
  tests/test_vio_reference.py
```

## Sources and attribution

- [OpenVINS installation and ROS-free API](https://docs.openvins.com/gs-installing-free.html).
- [Pinned OpenVINS source](https://github.com/rpng/open_vins/tree/69488123ed9362dd44b6f28e7f4680abbff1442b), licensed under GPL-3.0-or-later; preserve its source and notices with the adapter runtime.
- [TUM-VI dataset](https://cvg.cit.tum.de/data/datasets/visual-inertial-dataset), data licensed under CC BY 4.0. David Schubert, Thore Goll, Nikolaus Demmel, Vladyslav Usenko, Jörg Stückler, and Daniel Cremers, *The TUM VI Benchmark for Evaluating Visual-Inertial Odometry*, IROS 2018.
- [Room1 calibrated 512_16 archive](https://vision.in.tum.de/tumvi/exported/euroc/512_16/dataset-room1_512_16.tar). Normalization and sensor/reference separation are Meridian transformations; original archive hashes are retained.
