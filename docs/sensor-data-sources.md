# Sensor-data sources and recording reservations

**Increment 2.1 selection record — 2026-10-20.**

This record selects sources and assigns recording roles before calibration
development. It does not qualify an importer, calibration tool or estimator.
Only publisher documentation, directory listings and checksum sidecars were
retrieved for the new selections. No selected recording payload was downloaded,
decoded, plotted or fitted, and no new result is claimed.

## Source, license and scope

The first source is the **TUM VI Benchmark**, by David Schubert, Thore Goll,
Nikolaus Demmel, Vladyslav Usenko, Joerg Stueckler and Daniel Cremers (2018).
Its [publisher page](https://cvg.cit.tum.de/data/datasets/visual-inertial-dataset)
licenses the dataset under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
Distributed inputs and derived packages must retain attribution, the source and
license links, and identify modifications. The dataset license does not select
or license the future calibration-tool implementation.

Use the calibrated EuRoC-format `512_16` exports for the initial camera/IMU
profile. This reuses the established resolution and topology, while adding
recordings with declared calibration roles. This is a Meridian scope choice,
not evidence that the new records pass the existing runner.

## Reservation manifest

All rows below have `acquisition_status: not_acquired`,
`measured_sha256: null`, `measured_size_bytes: null` and
`content_validation: not_performed`. Published MD5 values identify the intended
download; they are not locally verified SHA-256 identities or authenticity
proofs. Record the actual bytes, SHA-256, size, retrieval URL and redirect
destination when acquisition is authorized in the corresponding increment.

The download links follow the publisher's
[EuRoC export directory](https://cdn3.vision.in.tum.de/tumvi/exported/euroc/512_16/)
and [static-IMU directory](https://cdn3.vision.in.tum.de/tumvi/imu_static/).
During this metadata check, `cdn3` redirected to `cdn2`; that redirect is not
itself a new dataset version.

| Recording group / selected source | Assigned role | Reservation state | Published MD5 |
| --- | --- | --- | --- |
| `tumvi-calib-cam1` — [dataset-calib-cam1_512_16.tar](https://cdn3.vision.in.tum.de/tumvi/exported/euroc/512_16/dataset-calib-cam1_512_16.tar) | Camera intrinsics/stereo fitting and development. | Available for later calibration development; not opened in 2.1. | `7a90893513eda0d345ef3ae032302614` ([sidecar](https://cdn3.vision.in.tum.de/tumvi/exported/euroc/512_16/dataset-calib-cam1_512_16.tar.md5)) |
| `tumvi-calib-cam2` — [dataset-calib-cam2_512_16.tar](https://cdn3.vision.in.tum.de/tumvi/exported/euroc/512_16/dataset-calib-cam2_512_16.tar) | Camera/stereo validation on a separate recording. | Reserved; exclude from fitting and candidate selection. | `958e82f852aec438b97956b05d254d46` ([sidecar](https://cdn3.vision.in.tum.de/tumvi/exported/euroc/512_16/dataset-calib-cam2_512_16.tar.md5)) |
| `tumvi-calib-imu2` — [dataset-calib-imu2_512_16.tar](https://cdn3.vision.in.tum.de/tumvi/exported/euroc/512_16/dataset-calib-imu2_512_16.tar) | Camera–IMU extrinsic and residual temporal calibration development. | Available for later fitting after input-model qualification. | `31ce65051aa03db6f4af68b9bd8b5aef` ([sidecar](https://cdn3.vision.in.tum.de/tumvi/exported/euroc/512_16/dataset-calib-imu2_512_16.tar.md5)) |
| `tumvi-calib-imu3` — [dataset-calib-imu3_512_16.tar](https://cdn3.vision.in.tum.de/tumvi/exported/euroc/512_16/dataset-calib-imu3_512_16.tar) | Camera–IMU calibration validation on a separate recording. | Reserved; exclude from fitting and candidate selection. | `38bbfd33e243ed5d4d67e3bda011dc74` ([sidecar](https://cdn3.vision.in.tum.de/tumvi/exported/euroc/512_16/dataset-calib-imu3_512_16.tar.md5)) |
| `tumvi-room3` — [dataset-room3_512_16.tar](https://cdn3.vision.in.tum.de/tumvi/exported/euroc/512_16/dataset-room3_512_16.tar) | Final navigation evaluation for the room-recording profile. | Reserved; no sensor/reference-content inspection or candidate tuning before protocol freeze. | `f38c8f3e30f06a1fb28e6b47e8702fc8` ([sidecar](https://cdn3.vision.in.tum.de/tumvi/exported/euroc/512_16/dataset-room3_512_16.tar.md5)) |
| `tumvi-corridor1` — [dataset-corridor1_512_16.tar](https://cdn3.vision.in.tum.de/tumvi/exported/euroc/512_16/dataset-corridor1_512_16.tar) | Final navigation evaluation with expected partial reference coverage. | Reserved under the same rule; no replacement by a favorable recording. | `05fb167d21b21fb68af6f674cd5afe9f` ([sidecar](https://cdn3.vision.in.tum.de/tumvi/exported/euroc/512_16/dataset-corridor1_512_16.tar.md5)) |
| `tumvi-imu-static2` — [dataset-calib-imu-static2.npy](https://cdn3.vision.in.tum.de/tumvi/imu_static/dataset-calib-imu-static2.npy) | IMU noise-characterization development. | One development recording; no independent static validation session identified. | `a1c62269c620a77b7c4b902887a26f54` ([sidecar](https://cdn3.vision.in.tum.de/tumvi/imu_static/dataset-calib-imu-static2.npy.md5)) |

The `room` and `corridor` choices use the publisher's stated acquisition
categories: room trajectories remain in the motion-capture room; corridor
references cover start/end portions. These descriptions are not assertions of
gap-free reference samples. Preserve the observed room2 coverage failure and
measure the new coverage only at the declared evaluation stage.
[Publisher sequence descriptions](https://cvg.cit.tum.de/data/datasets/visual-inertial-dataset#dataset_sequences).

Room1 and room2 are consumed regression recordings. Their frozen identities and
results remain in the [existing qualification](vio-reference.md) and
[room2 report](../results/hosted-acceptance/README.md). Neither is restored to an
untouched evaluation role by importing a different encoding or resolution.

## Separation and release rules

- The reservation unit is the complete physical recording group. Its raw bag,
  calibrated bag, EuRoC export, resized images, cropped intervals and injected
  fault variants inherit its role. They are not independent recordings.
- Freeze the candidate, calibration assumptions, allowed online adaptation,
  selection/exclusion rules, metrics and thresholds before opening each reserved
  recording for evaluation. Record content access and its purpose.
- Integrity/precondition failure is a recorded outcome. Do not choose new
  intervals, retune thresholds or replace a reserved recording after seeing
  estimator errors. A changed candidate requires a new evaluation reservation;
  previously used data can remain development or regression evidence.
- These are separate recordings of the same published rig, not independent
  sensors, vehicles or demonstrated recording days. Publisher preprocessing
  and supplied calibrations were not created under Meridian's split. Claims
  must state that shared provenance.
- Optional-reference and intentionally missing-reference variants must preserve
  their recording group. Removing truth creates an input-contract test, not
  another independent accuracy trial.

## Sensor and preprocessing constraints

The paper describes synchronized global-shutter IDS stereo cameras, a Bosch
BMI160 IMU, nominal camera/IMU rates of 20/200 Hz, and image timestamps at the
exposure center. Camera-calibration sequences use slower acquisition.
The released imagery is monochrome; the selected exports use 512 × 512,
16-bit images. The paper's transform convention maps coordinates from the
second named frame into the first.
[TUM VI paper, sensor setup and format](https://cvg.cit.tum.de/_media/spezial/bib/schubert2018vidataset.pdf).

The publisher supplies camera models and an
[AprilGrid target package](https://cvg.cit.tum.de/_media/data/datasets/visual-inertial-dataset/april_6x6_80x80cm.zip),
including a
[pinhole/equidistant 512 calibration package](https://cvg.cit.tum.de/_media/data/datasets/visual-inertial-dataset/pinhole-equi-512.zip).
Before fitting, verify target dimensions, tag layout, uncertainty and the chosen
camera-model convention from the actual metadata. Those packages are references,
not newly measured Meridian calibrations.

Processed data already correct IMU axes/scales and reference frame/time.
The publisher identifies `calib-imu1` as the source of its joint preprocessing
calibration. Thus `calib-imu2/3` separate Meridian fitting/validation observations
from that named source, but do not remove upstream calibration dependence.
[Publisher calibration and raw-data notes](https://cvg.cit.tum.de/data/datasets/visual-inertial-dataset#raw_data).

Even the raw release is not an uncorrected clock experiment: the paper reports
a 5.3 ms camera–IMU readout correction applied to both raw and calibrated
releases. A fit on the selected processed exports estimates a remaining offset,
not the original sensor delay. Do not add published offsets a second time or
infer their sign from their numerical value. The paper and current website
describe different correction stages; qualify their application against the
exact release before consuming either.
[TUM VI paper, IMU and calibration sections](https://cvg.cit.tum.de/_media/spezial/bib/schubert2018vidataset.pdf).

If a later workflow needs unscaled IMU values, the publisher also lists
[raw calib-imu2](https://cdn3.vision.in.tum.de/tumvi/raw/dataset-calib-imu2.bag)
with published MD5 `232577e564ede901b26f7fca7aae8f9d`
([sidecar](https://cdn3.vision.in.tum.de/tumvi/raw/dataset-calib-imu2.bag.md5))
and [raw calib-imu3](https://cdn3.vision.in.tum.de/tumvi/raw/dataset-calib-imu3.bag)
with `afe2a496bdb725403685b4522d41b03d`
([sidecar](https://cdn3.vision.in.tum.de/tumvi/raw/dataset-calib-imu3.bag.md5)).
These are unacquired alternate encodings of the same reserved groups.
Supporting their conversion requires an explicit adapter decision; this record
does not add a general ROS-bag importer or change the initial processed profile.

## IMU characterization and missing acquisitions

The publisher describes `imu-static2` as 111 hours of stationary IMU data,
with timestamp, gyro, acceleration and temperature columns in the NumPy export.
The [static directory](https://cdn3.vision.in.tum.de/tumvi/imu_static/) lists
approximately 4.8 G for that file and 28 G for its bag counterpart; these are
rounded listing sizes, not measured acquisition sizes.
[Publisher static-sequence description](https://cvg.cit.tum.de/data/datasets/visual-inertial-dataset#calibration_sequences).

The NumPy representation therefore needs a bounded conversion decision before
2.7; its dtype, timestamp unit/precision, calibration state and effective
filtering are not inferred from the filename. Do not load it as trusted pickle
data. Fix subsampling, fitting ranges and resource budgets before inspecting
noise fits. Any contiguous-window split remains evidence from one session,
not an independent repeat.

No documented, independently referenced multi-pose static acquisition has been
selected for accelerometer bias/diagonal-scale validation. Moving AprilGrid
sequences do not establish such a protocol. No second stationary IMU session
has been selected for noise-model validation either. These remain explicit data
requirements for 2.7, to be satisfied by a suitable separately documented source
or a new physical acquisition; no existing TUM result calibrates the drone IMU.

Public recordings do not complete the [physical bench requirement](bench-acquisition.md).
Actual controller configuration, axes, timestamp semantics, reference
uncertainty and acquisition conditions must still be recorded on the available
hardware. A camera–IMU bench requires its own compatible rig.
