# OpenVINS sensor-only reference adapter

This native, ROS-free adapter runs the pinned OpenVINS EKF/MSCKF implementation
on prepared synchronized stereo images and IMU samples. It provides the native
execution component of Meridian's first VIO reference; it is not a general
dataset importer, a hosted service, or an onboard integration.

The upstream revision is `69488123ed9362dd44b6f28e7f4680abbff1442b`. Source and
build integration are GPL-3.0-or-later; see [NOTICE.md](NOTICE.md) and [LICENSE](LICENSE).
The adapter uses a separate CMake project and does not change Meridian's
single-axis C++ build or OpenVINS estimator equations.

## Build

From the repository root, with Docker and network access for dependencies:

```sh
docker build -t meridian-openvins:reference integrations/openvins
docker run --rm --network none meridian-openvins:reference --version
python3 integrations/openvins/smoke_test.py
```

The Dockerfile pins the Ubuntu base digest, direct development package versions
and OpenVINS commit. Ceres 2.2 requires C++17. Compilation uses two jobs, `-O2`,
and disables ROS and ArUco support. The source selection follows upstream's
ROS-free library, excluding test/simulator executables. Transitive Ubuntu
packages are resolved by apt: a complete installed package/version manifest is
retained, but this recipe does not promise byte-identical rebuilds from future
package repositories. A package disappearing causes a build failure; versions
must not be silently substituted.

The `git-man` package is explicitly pinned alongside `git`: a later package-index
refresh otherwise selected newer manual pages while leaving the estimator
binaries unchanged. This pin preserves the qualified dependency inventory for the
observed rebuild; it does not freeze every transitive package indefinitely.

The image retains `/opt/meridian/build-info.json`, `dependency-versions.txt`,
`binary-sha256.txt`, all adapter sources and the upstream estimator source
directories used by this build. `--version`
returns JSON including the upstream revision and compiled `runner.cpp` hash.
Preserve the image content ID as well as these files for each evaluated run.
The smoke check runs a generated static uint16 stereo/IMU sequence without any
reference poses under the same container restrictions, expects explicit failure
to initialize without motion, and verifies malformed stereo/IMU rejection.

## Input and invocation

The only accepted CLI is:

```text
meridian-openvins --dataset /sensors --config /config/estimator_config.yaml --output /output
```

The output directory must already exist and contain neither `trajectory.csv`
nor `status.json`. The prepared dataset layout is:

```text
/sensors/mav0/cam0/data.csv       # timestamp_ns,filename
/sensors/mav0/cam0/data/*.png
/sensors/mav0/cam1/data.csv       # timestamp_ns,filename
/sensors/mav0/cam1/data/*.png
/sensors/mav0/imu0/data.csv       # timestamp_ns,wx,wy,wz,ax,ay,az
```

Comments starting with `#` are accepted. Timestamp values must be nonnegative
integer nanoseconds and strictly increasing within each stream. Camera rows
must pair exactly in timestamp and count; no synchronization is guessed. IMU
units are rad/s and m/s² in the calibrated IMU frame. The TUM-VI calibrated
sequence already corrects its clocks and IMU calibration: do not apply raw-data
corrections a second time. The included calibration is specific to TUM-VI
512 × 512 images; other datasets require their own verified configuration.

The parser accepts only monochrome uint8/uint16 PNG images matching calibrated
dimensions. uint16 images are converted by OpenCV `convertTo` with scale
`255/65535`, matching the ROS
[`cv_bridge` MONO16-to-MONO8 full-range policy](https://github.com/ros-perception/vision_opencv/blob/1.16.2/cv_bridge/src/cv_bridge.cpp#L300-L304).
There is no per-frame intensity normalization. Camera masks and calibration
are read from the configuration directory. Camera downsampling is rejected.

The runner feeds IMU samples through the first sample strictly beyond each
camera time plus the current estimated camera-to-IMU offset, then processes
the stereo pair synchronously. It subtracts an integer timestamp origin before
conversion to seconds. Frames lacking an IMU bracket are counted and skipped.
The adapter processes every eligible camera pair; no wall-clock pacing or
callback frequency throttle is applied. OpenCV, initialization and numerical
library threads are limited to one in the reference execution.

The adapter exposes no ground-truth argument and never reads reference files
or calls OpenVINS's ground-truth initializer. The
runtime image excludes upstream's bundled `ov_data` reference trajectories,
evaluation examples and Git objects using a fresh final stage. The
process must be run with only sensor, configuration and output mounts; the
parent evaluator separately mounts or reads the independent reference. For
example, substitute absolute paths and create an empty output directory:

```sh
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --user "$(id -u):$(id -g)" \
  --cpus 2 --memory 4g --pids-limit 128 --tmpfs /tmp \
  --mount type=bind,src=/absolute/sensors,dst=/sensors,readonly \
  --mount type=bind,src=/absolute/config,dst=/config,readonly \
  --mount type=bind,src=/absolute/output,dst=/output \
  meridian-openvins:reference \
  --dataset /sensors --config /config/estimator_config.yaml --output /output
```

Only `/output` and `/tmp` need writable storage. This narrow prepared-data
adapter is not an upload security boundary; the orchestrator must validate
dataset paths and mounted contents before execution.

## Outputs and limitations

`trajectory.csv` has the exact header:

```text
timestamp_ns,px_m,py_m,pz_m,qx,qy,qz,qw
```

Positions in metres describe the IMU origin in OpenVINS's local world. The
Hamilton quaternion in xyzw order rotates IMU/body vectors into that world.
It is constructed from the transpose of OpenVINS's world-to-IMU rotation
matrix and normalized. Absolute position and heading are not observed by
camera/IMU alone; evaluation needs an explicitly reported alignment. Output
timestamps are exact camera input nanoseconds and are written only when the
current estimate actually has that camera time. No stale pose is relabelled,
no missing poses are filled, and no reference data influence initialization.

`status.json` schema version 1 records `state` (`complete`, `uninitialized`, or
`failed`), error text, input/processed counts, initial/final camera and estimate
timestamps, initializer timestamp, first-output delay, wall and CPU seconds,
and peak self RSS in KiB. Input camera timestamps include the full sequence;
the initialized timestamp identifies the initializer's state time, while
`initialization_delay_s` measures first output minus first input camera time.
Completion means at least one pose was produced, not that accuracy or coverage
criteria passed. Those independent acceptance gates belong to the evaluator.
Exit codes are 0 for completion, 3 for no initialized output and 2 for an
adapter exception. A crash, kill or upstream `exit()` can prevent status from
being written; the orchestrator must treat missing status as failure.

Covariance, feature tracks, innovations, relocalization and online resets are
not exported in this increment. TUM-VI was acquired by hand; successful replay
does not establish drone, embedded, real-time or flight validation.
