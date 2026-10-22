# Supplied calibration and constant time mappings

Meridian imports structured [sensor contracts](sensor-contract.md), checks their
compatibility with an acquisition, and prepares a new immutable input revision.
It does not fit parameters or establish physical accuracy. The original
observations, source inspection and earlier revisions remain unchanged.
Private-input VIO execution is a separate increment.

The [qualification report](../results/sensor-calibration/README.md) records
independently expected synthetic corrections, lifecycle checks and restoration
of the resulting immutable revisions.

## Select parameters and apply corrections

The acquisition's **Calibration & timing** page accepts a calibration or clock
mapping as a JSON file. Imports preserve the submitted text and compute the
authoritative contract identity on the server. Duplicate JSON keys, unsupported
fields and nonfinite numbers are rejected. JavaScript parsing is not used to
compute identity: a numeric representation such as `4.0` participates in the
existing Python canonical contract serialization.

Artifacts belong to one account and project. Their source and validation
acquisition identities must resolve to exact owned acquisitions in that project.
A contract ID alone is insufficient; bindings include the acquisition UUID and
descriptor SHA-256. Calibration fitting observations and declared validation
observations remain distinct. An artifact can be archived to prevent new
selection while retaining its immutable provenance.

Creating an input revision freezes the original acquisition publication, artifact
identities and operation choices. Selecting a different candidate creates a new
revision. The revision contains its own prepared observations, selected artifact
descriptors, dependency descriptors, preparation report and file inventory.

| Selection | Current behavior |
| --- | --- |
| Camera intrinsics | Bind pinhole parameters, four radtan or equidistant coefficients, sensor/frame and exact resolution; preserve image pixels. |
| Stereo and camera–IMU geometry | Retain directed rigid transforms for a future supported consumer; no rectification or implicit inversion. |
| IMU intrinsics | Optionally subtract supplied SI bias and apply positive diagonal gains. |
| IMU frame | Optionally apply one explicitly selected direct rotation with zero translation. |
| IMU noise | Retain declared continuous white-noise densities, bias diffusion and assumptions; do not derive sample variances or change estimator defaults. |
| Constant clock mapping | Apply one explicitly selected integer mapping per source stream/reset segment. |

The calibration carries supplied/estimated provenance, method/version,
applicability and already-applied information. `estimated` is an imported claim,
not evidence that Meridian ran a solver. `validation.status = reported` retains
the external report identity and declared observations; this increment does not
verify report payloads or independently assess the physical calibration.
Structural validity and successful application never imply independent accuracy.

## Numerical conventions

IMU corrections execute after conversion to rad/s and m/s², in this order:

```text
omega_corrected = gyro_scale * (omega_SI - gyro_bias_rad_s)
force_corrected = accel_scale * (force_SI - accel_bias_m_s2)
omega_target = R_target_source @ omega_corrected
force_target = R_target_source @ force_corrected
```

The multiplication by each scale is componentwise in the original sensor frame.
Rotation then expresses both vectors in the selected target frame. The declared
rotation must be proper and orthonormal within `1e-9`; it is never repaired.
Transforms use `p_target = R_target_source @ p_source + t_target_source`.
Only a direct transform matching that direction is executable for IMU vectors,
and its translation must be exactly zero. Lever-arm compensation, transform-graph
inference and full axis-misalignment calibration are unsupported.

Numeric IMU calibration requires `processing.calibration = not_applied` and
`applicability.imu_intrinsics = candidate_not_applied`. Already-corrected and
unknown sources are rejected for that operation. Their compatible camera/geometry
parameters can still be selected without applying IMU corrections again.

## Exact time and reset segments

Every source tick is converted exactly to an integer nanosecond before applying:

```text
t_target_ns = target_origin_ns + (t_source_ns - source_origin_ns) + offset_ns
```

All timestamps and interval bounds are signed 64-bit integer strings. Arithmetic
uses exact integer intermediates, checks the resulting range and performs no
rounding. A positive offset increases the target timestamp.

A **clock-domain mapping** names a distinct, declared target clock. A **sensor
time offset** preserves the clock and uses equal source/target origins, so its
only effect is the signed added offset. The mapping applies to the timestamp of
the named source stream, after tick conversion and before output publication;
it does not shift another stream or change timestamp semantics implicitly.

Each mapping must cover the complete selected source segment with inclusive
bounds. Reset segments remain separate. The current v1 sensor contract permits one
constant mapping per stream/segment: domain mapping and sensor offset cannot be
composed on the same segment. Drift, interpolation, extrapolation and resampling
are unsupported. Output timestamps must remain ordered within each segment and
the declared stereo pair must remain exactly paired.

Clock execution requires `processing.clock_correction = not_applied`. The v1
flag does not distinguish which prior correction was applied, so `applied` and
`unknown` both refuse another correction. In particular, the calibrated TUM-VI
adapter declares upstream corrections and cannot silently receive them twice.

## Local reproduction

```sh
python -m meridian.sensor_import prepare original-acquisition new-prepared-input \
  --calibration supplied-calibration.json \
  --dependency-acquisition calibration-observations/acquisition.json \
  --clock-mapping imu-clock.json \
  --apply-imu-intrinsics --imu-target-frame body
```

Omit each operation that is not intended. `--clock-mapping` and
`--dependency-acquisition` may be repeated. Both required calibration provenance
and any declared validation acquisition must resolve. No arbitrary estimator
YAML is accepted.

Preparation without selected calibration/time artifacts retains method version 1.
Selection uses method version 2. Its default prepared ID derives from the source,
artifacts and explicit choices; repeated preparation is deterministic within the
qualified environment. Output directories must be new. Sources and prior
prepared directories are never overwritten, and a prepared input cannot be
passed off as an original acquisition.

## Hosted lifecycle and limits

Revisions reuse the acquisition import worker and shared compute slot. Each child
receives only the exact source acquisition directory read-only and its own
bounded scratch directory: no network, database credentials or Docker socket.
The envelope remains 1 CPU, 1 GiB memory, 64 processes and 900 seconds within the
dedicated 8 GiB import filesystem.

Each revision reserves at most 2 GiB and 30,000 files, including publication
metadata. Its retained files count toward the shared 12 GiB/account and 24 GiB
global acquisition storage quotas. It copies selected images rather than sharing
mutable links or duplicating the original transfer archive. Queued/running
preparations protect required source files against deletion. A completed
revision remains self-contained after source-file deletion; source metadata and
provenance remain available. Deleting revision files retains a metadata tombstone
and explicitly marks downloads unavailable.

[Private-input VIO](private-vio.md) can consume supported revisions after a
separate compatibility check. Queued and active VIO jobs protect their exact
revision files. Deleting files after completion leaves retained numerical job
evidence readable, while associated image reads become unavailable.

Calibration and clock descriptors are limited to 64 KiB each; imports use a
256 KiB request limit. A frozen execution request is limited to 2 MiB and may
include up to 16 clock mappings and 16 dependency acquisitions. Oversized
combinations are rejected rather than dropping provenance.

Publication is fenced to its current attempt and becomes visible atomically.
Cancellation, abandoned attempts and scratch collection use the same isolation
principles as acquisitions. [Backup format 3](workspace-backup.md) fingerprints
artifact/revision metadata and retains exact revision publication files.
Original source inspection is not a diagnostic of corrected values; inspect the
revision report and output CSVs for its actual operations and measurements.
