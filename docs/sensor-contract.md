# Sensor acquisition, calibration and clock contracts

**Implemented in increment 2.1, schema version 1.**

The standard-library [contract validator](../src/meridian/sensor_contract.py)
checks descriptors and their identity relationships. The
[convention helpers](../src/meridian/sensor_conventions.py) check exact integer
time arithmetic, units and rigid geometry. Neither imports recordings, decodes
images, verifies referenced payloads, fits calibration nor starts an estimator.
The hosted service continues to use its existing room1 specification.

## Validate a descriptor

```sh
python -m meridian.sensor_contract examples/sensor-contracts/stereo-no-reference.json
```

Use `loads_contract(text)` for strict JSON parsing, `validate_contract(value)`
for a single object, `contract_sha256(value)` for its identity and
`validate_bundle(documents)` to verify a complete set of linked descriptors.
Unknown keys and versions, duplicate JSON keys, nonfinite numbers and incorrect
types are rejected. Booleans are not integers or physical measurements.
The CLI reads the supplied JSON only; paths and URLs inside it are never opened.

Four record kinds share the required envelope:

```json
{
  "format": "meridian-sensor-contract",
  "schema_version": 1,
  "kind": "acquisition",
  "id": "example-recording"
}
```

The snippet is the envelope, not a complete acquisition. IDs begin with an ASCII
letter, then contain letters, digits, `.`, `_` or `-`, with at most 80 characters.
All fields described below are mandatory; nullable values are explicit. No
validator fills defaults, changes conventions or upgrades a version.

Each identity reference is `{id, sha256}`. The hash is SHA-256 of validated,
sorted-key, compact ASCII JSON with nonfinite values forbidden. Object-key order
and whitespace do not affect it; array order, numeric representation and values
do. This is a Python serialization contract, not a claim of RFC 8785 compliance.
Changing a descriptor creates a new identity; dependent records must bind that
identity explicitly. Hashes establish content identity, not origin authenticity,
ownership or permission.

## Acquisition

Required fields beyond the envelope are `purpose`, `rig`, `provenance`, `frames`,
`clocks`, `files`, `streams` and `stereo_pairing`.

| Field | Contract |
| --- | --- |
| `purpose` | `replay`, `calibration`, `evaluation` or `fixture`. This declares intended use, not proof that a split is independent. |
| `rig` | `{id, revision}` identifies the declared physical sensor assembly. Change revision when mounting, sensor instance, focus or other calibration-relevant conditions change. |
| `provenance` | `{source, license, attribution, session_id, synthetic}`. Text names source and acquisition session; `synthetic` is Boolean. Unknown licensing is recorded as `unspecified`, not inferred as permission to redistribute. |
| `frames` | Distinct `{id, axes}` records. Axes are three descriptions in x/y/z order, or `null` when unknown. Known frames use right-handed conventions; the validator does not infer physical axis correctness from words. |
| `clocks` | Distinct `{id, unit, epoch}` records. Integer tick units are `s`, `ms`, `us`, `ns`; epoch is `unix`, `boot`, `device` or `unknown`. An equal epoch label does not establish synchronization. |
| `files` | Distinct `{path, sha256, size_bytes, role}` records. Role is `original`, `sensor`, `reference` or `evidence`. No bytes are verified by this metadata validator. |
| `streams` | Typed records described below. All frame/clock/index-file references resolve within the acquisition. |
| `stereo_pairing` | `null`, or `{camera_ids, policy}` with two distinct camera streams and policy `exact_timestamp_pairs` or `unknown`. Exact pairing requires matching declared clocks, segment bounds and counts; actual image timestamps still require payload inspection. |

Paths are relative POSIX paths with no absolute root, drive/URL colon,
backslash, control character, empty component, `.` or `..`. This does not replace
filesystem containment, symlink checks and archive extraction controls in an
importer. Those are part of 2.2–2.3.

Each stream has `id`, `kind`, `sensor_instance`, `frame_id`, `clock_id`,
`timestamp_semantics`, `measurement_semantics`, `index_path`, `segments`,
`processing` and exactly one kind-specific object:

- `camera`: `{resolution, encoding, shutter}`. Resolution is `[width, height]`
  in pixels; encoding is `mono8` or `mono16`; shutter is `global`, `rolling` or
  `unknown`. Measurement semantics is `image`.
- `imu`: `{angular_velocity_unit, specific_force_unit, local_gravity_m_s2}`.
  Rates use `rad/s` or `deg/s`; specific force uses `m/s^2` or `g`. The latter
  requires the explicit positive gravity scale that the source uses; it must
  never silently mean a fitted accelerometer magnitude. Measurement semantics is
  `instantaneous_imu` or `filtered_snapshot`. These are separate models.
- `reference`: `{parent_frame_id, position_unit, orientation, use, coverage,
  position_uncertainty_m, orientation_uncertainty_rad}` for kind `reference_pose`.
  Position uses metres; orientation is `Hamilton_xyzw_frame_to_parent`; use is
  always `independent_evaluation_only`. Coverage is `full`, `partial` or `unknown`
  as declared, not measured. Uncertainties are nonnegative scalars or `null`;
  the scalars are descriptive bounds/scale declarations, not full covariance.
  Measurement semantics is `pose`.

Timestamp semantics is `sample`, `exposure_start`, `exposure_midpoint`,
`exposure_end`, `message_write` or `unknown`. A common logger timestamp does not
establish simultaneous IMU sampling. Exposure semantics do not model
rolling-shutter row time.

Each segment is `{id, first_tick, last_tick, sample_count}` with closed bounds,
positive count and `first_tick <= last_tick`. Ticks are canonical signed-int64
decimal **strings**, including when absolute values exceed JavaScript's exact
Number range. `+1`, `01`, `-0`, decimal/exponent notation and numeric JSON values
are invalid. Conversion to ns must also fit signed int64. Segment order is
original recording order; a reset can make the next segment's ticks smaller.
Segment IDs are local to a stream. No sorting or gap filling is implied.

`processing` contains `calibration` and `clock_correction` states (`applied`,
`not_applied`, `unknown`), `filtering` (`applied`, `none`, `unknown`) and a
nonempty `description`. These describe corrections already represented in the
stored observations. Imported TUM-VI corrections and DataFlash frontend
processing must not be applied again.

## Transfer package layout

The [local importer](sensor-import.md) consumes one acquisition descriptor named
`acquisition.json` plus exactly inventoried payload files. Optional linked
calibration, clock and preparation descriptors remain separate JSON records;
original acquisition identity does not change when a calibration is added.
The checked examples use descriptive filenames so several cases can coexist.

Camera and IMU index paths begin with `sensors/`; reference indices begin with
`references/` and have role `reference`. Raw source containers can be retained
under `originals/`. Derived artifacts retain their own inventory.

CSV data use UTF-8, one header, comma delimiters and decimal numbers. No implicit
units, timestamp conversions or row reorder are permitted. Headers are:

| Stream | Exact columns |
| --- | --- |
| Camera | `sample_id,segment_id,timestamp,filename` |
| IMU | `sample_id,segment_id,timestamp,wx,wy,wz,fx,fy,fz` |
| Reference pose | `sample_id,segment_id,timestamp,px,py,pz,qx,qy,qz,qw` |

`sample_id` is a canonical nonnegative integer string, increasing in original
file order, scoped to its stream. `segment_id` resolves to the declared segment;
`timestamp` uses that stream's clock ticks. Duplicate/backward timestamps within
a declared continuous segment require a reported import outcome; they are not
silently removed. Index counts and boundaries must match payloads before a
prepared input is admitted. IMU vectors are angular velocity and **specific
force**, not gravity-subtracted translational acceleration.

Image filenames are safe relative paths from their index directory and must
resolve to inventoried sensor files. The initial encodings are grayscale PNG,
8 or 16 bits; image bytes/dimensions and camera-pair timestamps need independent
inspection. Reference rows are never part of the estimator sensor mount.
The separate importer introduced in 2.2 verifies these CSV and payload rules.
The metadata-only validator described here still does not open those files.

## Calibration

Fields are `acquisition`, `state`, `method`, `cameras`, `imu`, `transforms`,
`applicability` and `validation`. The source acquisition reference identifies the
observations or provenance supporting the artifact. `state` is `supplied` or
`estimated`; `method` is `{name, version}` with exact source/tool identity text.
Neither state means physically validated.

Camera entries are `{sensor_id, frame_id, resolution, projection, distortion,
intrinsics, coefficients}`. Initial representation supports pinhole projection
and four-coefficient `radtan` or `equidistant` distortion:
`intrinsics = [fx, fy, cx, cy]` in pixels, positive focal lengths;
`radtan = [k1, k2, p1, p2]`, `equidistant = [k1, k2, k3, k4]`.
Projection and distortion evaluation/qualification are future calibration work.
Resolution and sensor/frame identities must agree with the linked acquisition.

Optional `imu` has `sensor_id`, `frame_id`, `gyro_bias_rad_s`,
`accel_bias_m_s2`, `gyro_scale`, `accel_scale` and `noise`. Bias vectors contain
three finite values; scale vectors contain three positive diagonal gains.
The declared correction is `corrected = scale * (measurement - bias)`, after
unit conversion, in the declared sensor frame. It does not identify
misalignment, temperature compensation, gyro g-sensitivity or lever-arm dynamics.

Optional `noise` describes `continuous_white_noise_and_bias_diffusion` with
`gyro_density_rad_s_per_sqrt_hz`, `accel_density_m_s2_per_sqrt_hz`,
`gyro_bias_diffusion_rad_s_per_sqrt_s`, `accel_bias_diffusion_m_s2_per_sqrt_s`
and `assumptions`. Values are nonnegative. Continuous densities and bias
diffusion are distinct from sample variance; the contract does not infer a
discrete covariance or fit these parameters to filtered snapshots.

Each direct transform has `{source_frame, target_frame, rotation,
translation_m}` and means:

```text
p_target = R_target_source p_source + t_target_source
T_source_target = (R_target_source^T, -R_target_source^T t_target_source)
```

Rows/columns of R must be orthonormal and determinant +1 within absolute
`1e-9`. Reflections and scale/shear are rejected; the validator never projects an
arbitrary matrix onto SO(3). Translation is the source origin expressed in the
target frame, in metres. Do not add that translation to a gyro or force vector.
Only direct transforms are described; no graph inference or redundant-cycle
solution is performed.

`applicability` has `description` and `imu_intrinsics` (`already_applied`,
`candidate_not_applied`, `not_applicable`). The bundle validator rejects an
IMU-calibration step when full corrections are already represented in either
the acquisition or calibration declaration.

`validation` contains `status`, `acquisitions` and `report`.
`not_provided` requires no validation acquisitions and a null report.
`reported` requires distinct acquisition identities and a report file identity
`{path, sha256, size_bytes}`. It records a claim with supporting identities;
the validator does not read the report or certify its conclusions. Different
hashes/IDs do not by themselves prove different physical sessions.
The [source reservation record](sensor-data-sources.md) supplies that separate
experimental-design constraint.

Calibration observations and replay acquisitions can differ. Applying an
artifact to another acquisition requires the same declared rig identity/revision
and compatible sensor instances, frames and camera resolution. The source
acquisition remains in the identity chain; it is never rewritten to pretend that
fitting occurred on the replay data. These labels check declared compatibility,
not whether a physical rig was actually unchanged.

## Clock mapping

Fields are `acquisition`, `source_stream_id`, `source_segment_id`,
`target_clock_id`, `purpose`, `model`, `source_origin_ns`, `target_origin_ns`,
`offset_ns`, `first_source_ns`, `last_source_ns` and `evidence`.

The initial model is `constant_offset_ns`. All five ns fields are canonical
signed-int64 strings. The source interval is closed and must be contained within
the linked source segment after exact tick-to-ns conversion.

```text
t_target_ns = target_origin_ns + (t_source_ns - source_origin_ns) + offset_ns
```

All arithmetic is integer, including origin subtraction. A positive offset
increases the target timestamp. Input/output values must fit signed int64;
intermediate Python integers need not. For an epoch timestamp ending in `626`,
adding 2 ns yields `628` exactly, without a float round trip.

`purpose` is `clock_domain_mapping` or `sensor_time_offset`; these are separate
physical explanations even when represented by the same algebra.
`evidence` contains `status` (`declared` or `estimated`), `description` and
optional `report` identity. A constant-offset fit cannot establish clock
stability, latency origin or simultaneity. Drift models are unsupported in
version 1; unknown relationships remain explicit. Each reset segment needs
its own mapping. Mappings are applied exactly once at a declared preparation
step; no offset is fitted from evaluation truth.

## Prepared-input lineage

Fields are `acquisition`, `calibration` (nullable), `clock_mappings`, `method`,
`selections`, `operations`, `outputs`, `files` and `target_profile`. All linked artifacts
use immutable identity references. `method = {name, version}` identifies the
preparation implementation. `selections` contain `{stream_id, segment_id,
first_tick, last_tick}` in original source-clock ticks, within the selected
source segment. Version 1 allows one contiguous interval per stream/segment;
selected segments retain their original order within each stream, including
across clock resets. Files declare exact output identities.
Each distinct selected stream has one output descriptor
`{stream_id, index_path, frame_id, clock_id, timestamp_unit}`. Its index resolves
to an inventoried file with the correct sensor/reference role. Prepared
timestamps always use `ns`, through exact integer scaling from source ticks
before any declared clock correction; output measurement units are SI.
Output frames/clocks must match the declared operations. Sensor observation
identity and row order remain linked to their originals.

Operations are ordered `{kind, stream_id, artifact, description, parameters}`
records. Their representations are deliberately limited:

| Kind | Artifact | Parameters |
| --- | --- | --- |
| `unit_conversion` | `null` | `{quantity, source_unit, target_unit, local_gravity_m_s2}`; `angular_velocity`: deg/s → rad/s, or `specific_force`: g → m/s² with explicit positive scale. |
| `frame_transform` | Selected calibration | `{source_frame, target_frame}` identifying a direct zero-translation transform for IMU vectors. |
| `image_conversion` | `null` | `{source_encoding: "mono16", target_encoding: "mono8", scale: 255/65535, offset: 0, rounding: "opencv_convertTo"}`. |
| `imu_calibration` | Selected calibration | `{model: "diagonal_scale_after_bias_subtraction"}`. |
| `clock_mapping` | A selected mapping | `{}`; the mapping supplies all parameters. |

This record describes a preparation, not an executable shell/YAML/URL recipe.
Increment 2.2 executes a bounded subset: SI/ns conversion and optional image
conversion. [Method version 2](sensor-calibration.md) additionally executes
explicit supplied IMU calibration, direct zero-translation IMU rotations and
one constant clock correction per selected source segment. The contract schema
remains version 1; stricter application eligibility is checked by the executor.
Image conversion records the existing OpenCV convention; its actual bytes require the identified implementation,
including rounding/saturation behavior. Exact duplicate declarations and
repeated conversion of the same measurement quantity are rejected. The bounded
per-stream measurement order is unit conversion, then any IMU calibration,
then at most one frame rotation. Clock operations concern timestamps separately.
IMU frame preparation applies R to angular velocity and specific force;
nonzero translation would require unsupported lever-arm dynamics. Image and
reference-pose frame transformations are unsupported preparation operations
in v1; general rigid extrinsics can still be stored in a calibration artifact.
This is declaration consistency checking, not a pipeline executor.

Targets are `stereo_imu_candidate_v1` and `imu_snapshot_candidate_v1`.
The stereo target requires two declared paired cameras with matching selected
intervals and one IMU. The snapshot target requires one filtered-snapshot IMU.
Selected clock mappings must cover their selected source intervals; overlapping
mappings within the same source stream/reset segment are rejected. No implicit
mapping composition is supported.
“Candidate” is intentional: neither can be submitted to the current hosted
runner. Bundle checks establish identity and metadata consistency only.

## Evidence states, limits and acceptance

`assess_acquisition` always reports payload verification `not_performed` and
execution readiness `not_assessed`. Unknown axes, timestamp or processing
semantics remain valid metadata with explicit limitations. A missing reference
is not a sensor-data error and does not establish an execution blocker.
Declared full/partial reference coverage must not be presented as measured
coverage or an accuracy score.

Descriptor parser bounds are 32 MiB of UTF-8 JSON, depth 32, two million JSON
nodes, strings at most 4096 characters and paths at most 512 characters.
Per acquisition: at most 256 frames, 128 clocks, 128 streams, 100,000 files and
10,000 segments per stream. These bound metadata parsing, not future upload
storage, decoder memory or VIO/calibration budgets.

The [synthetic examples](../examples/sensor-contracts/README.md) cover stereo
without truth, partial reference, processed IMU snapshots with clock reset,
unknown metadata, supplied calibration, a known +2 ns mapping and prepared
lineage. Independent tests check 90° rotations, inverse transforms, unit scale,
epoch precision, offset sign, reflection/non-unit rejection, invalid identities
and separation of reference/input roles. Existing frozen room1 specification,
dataset and export tests remain regression evidence.

No new real recording, sensor accuracy, fitted calibration or hosted upload
capability follows from these tests. The [source selection](sensor-data-sources.md)
reserves real calibration/validation recordings without inspecting their
payloads. [Import/preparation](sensor-import.md) is implemented separately in 2.2.
