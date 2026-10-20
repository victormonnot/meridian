# Part 2 — Sensor data, calibration, and timing

**Status: increments 2.1–2.2 implemented, 2026-10-20. Increments 2.3–2.13 remain planned.**

The intended outcome is a private, versioned acquisition with traceable sensor
semantics, calibration and timing, usable by a supported estimator and
reproducible outside Meridian. An engineer should be able to import recordings,
inspect their limitations, supply or estimate supported calibration parameters,
run the maintained VIO implementation, and deliver the inputs and evidence.
The [roadmap](professional-estimation-roadmap.md) defines the broader scope.

## Starting point verified in the repository

Part 1 provides hosted projects, immutable experiments, isolated OpenVINS jobs,
investigation, comparisons and independent exports. Its
[acceptance report](../results/hosted-acceptance/README.md) records the actual
workflow and the negative room2 coverage result.

| Area | Implemented boundary | Consequence for part 2 |
| --- | --- | --- |
| [Dataset preparation](../src/meridian/vio_dataset.py) | Pinned TUM-VI room1/room2 archives, 512 × 512 mono16 images, exact stereo pairs, mandatory mocap input. | User recordings require a separate versioned import contract and optional references. |
| [Specifications](../src/meridian/vio_specification.py) | The hosted room1 profile fixes inputs, calibration, timing, interval and evaluation; only feature budget varies. | Preserve v1 and add an explicit supported contract; do not weaken its identity checks. |
| [Workspace](../server/workspace/models.py) | Each project has one fixed dataset identifier; no private acquisition or calibration objects. | Allow multiple acquisitions per project and bind exact input versions to each experiment. |
| [Native adapter](../integrations/openvins/runner.cpp) | Two synchronized cameras, mono8/mono16 decoding, calibrated dimensions, explicit IMU scheduling. | Admission must check actual compatibility rather than treating every import as runnable. |
| [Native configuration](../integrations/openvins/config/estimator_config.yaml) | Online camera intrinsics, extrinsics and time-offset refinement enabled; only poses/status exported. | This is not an offline calibration product. Record the adaptation policy separately from supplied calibration. |
| [Comparisons](../src/meridian/vio_comparison.py) | Calibration/time/input differences are incompatible. | Introduce an explicit calibration-comparison policy, retaining the original one. |
| [Exports](vio-exports.md) and [backup](workspace-backup.md) | Exports reacquire public inputs; backups cover metadata and published job artifacts. | Private original inputs need portable delivery and coherent recovery coverage. |
| [DataFlash](dataflash-audit.md) and [bench](bench-acquisition.md) | Snapshot audit and roll replay exist; new physical acquisition and reference-window evaluation do not. | Preserve snapshot semantics and add actual bench evidence without implying a camera rig already exists. |

Increments 2.1–2.2 add local metadata contracts, convention checks and actual
file import/preparation. Existing room1 inputs provide regression evidence.
No new acquisition, calibration fit, VIO execution or deployment is claimed.

## Proposed boundaries

- Start with one documented camera/IMU transfer package: manifest, timestamped
  image files, IMU CSVs and optional independent references. Adapt the existing
  TUM-VI layout and DataFlash IMU snapshots through explicit supported profiles.
  Arbitrary ROS bags, MCAP, ULog, live streaming and high-rate DataFlash batches
  are not promised by this part.
- Preserve the initial VIO topology: synchronized stereo plus one IMU. Qualify
  an explicit camera-model and image-format allowlist. Mono VIO, asynchronous
  stereo, rolling-shutter correction and arbitrary rigs require separate
  evidence; importability does not establish execution support.
- Keep original bytes immutable. Unit/frame changes, clock mappings, image
  conversions, calibration application and interval selection create traceable
  derived versions. Preserve native sample identity and original order. Never
  silently sort, deduplicate, fill gaps, resample or apply calibration twice.
- Keep sensor measurements, calibration observations and evaluation references
  distinct. A recording without independent truth can be inspectable and runnable;
  its accuracy is unavailable. Missing reference coverage is not sensor loss.
- Continue using the existing web/API/PostgreSQL/worker boundaries. Add concrete
  import and calibration job types with measured budgets, not a universal
  plugin system. The shared VPS remains the target, subject to measured capacity.
- Use established calibration methods with pinned sources and reproducible
  execution. Kalibr is the proposed first candidate for the camera and camera–IMU
  workflows; its build, model compatibility and resource envelope still need
  qualification. Its documentation describes
  [camera-system calibration](https://github.com/ethz-asl/kalibr/wiki/multiple-camera-calibration)
  and [spatial/temporal camera–IMU calibration](https://github.com/ethz-asl/kalibr/wiki/camera-imu-calibration).
  A tool-specific intermediate recording can remain inside the worker; it does
  not require a general bag importer or ROS installation for browser users.
- The new 3D estimator, external-code execution, general VIO front-end development,
  localization recovery and large optimization campaigns remain in later parts.
  Target detection needed for calibration belongs here.

## Sequential increments

Every increment includes applicable interface states, bounded failures,
ownership checks, documentation and meaningful verification. Numerical thresholds
and supported resource limits must be fixed before the corresponding acceptance
runs. A failed or inconclusive calibration is a valid recorded outcome; it must
not become a validated artifact merely because a process exited successfully.

### 2.1 — Acquisition, calibration and clock contracts

**Implemented:** [contract and transfer format](sensor-contract.md),
[synthetic examples](../examples/sensor-contracts/README.md), and
[source reservations](sensor-data-sources.md). A standard-library validator
checks four record kinds and linked identities; independent tests exercise units,
rigid transforms, exact clocks, reference separation and invalid declarations.
The existing hosted v1 execution path remains unchanged. Payload import and
verification are implemented separately in 2.2.

**Dependency:** completed part 1.

**Deliverable:** versioned contracts for original acquisitions, sensor streams,
prepared inputs, calibration artifacts, clock mappings and optional references.
Describe sensor instance, SI units, axes, transform direction, quaternion
convention, sample/exposure/message timestamp meaning, clock resets, filtering,
already-applied corrections, provenance and validity. Separate calibration,
evaluation and replay recording roles. Define compatibility and evidence states.

Keep integer timestamps and explicit clock origins; JSON/browser transport must
not lose epoch-nanosecond precision. Unknown metadata stays unknown and can
restrict eligibility. Define the supported transfer package and a small fixture
matrix before implementing import. Select licensed calibration/evaluation
recordings and reserve independent sessions before fitting; room1 and room2 have
already been examined and are regression cases, not untouched final evidence.

**Acceptance:** reviewed examples cover stereo/IMU, IMU-only snapshots,
missing/partial reference and invalid conventions. Independent fixtures exercise
known units, rotations and clock offsets. Existing v1 specifications and input
identities remain valid and unchanged.

### 2.2 — Deterministic import and preparation

**Implemented:** [local import and preparation](sensor-import.md), bounded
payload validation, calibrated TUM-VI and DataFlash snapshot adapters. The
[regression record](../results/sensor-import/README.md) distinguishes successful
room1 compatibility from the historical DataFlash log's strict rejection.
Prepared candidates are not yet accepted by the hosted runner.

**Dependency:** 2.1.

**Deliverable:** a local importer for the declared package, a TUM-VI adapter and
integration of the existing DataFlash snapshot decoder. Preserve original files,
per-stream timestamps, instances, calibration provenance and hashes; produce an
immutable inventory and structured validation report. Parsing remains separate
from estimation. Reject unsupported models/streams explicitly.

DataFlash snapshots retain their already processed measurement semantics and
previous-sample-hold replay contract. Do not relabel them as raw high-rate IMU
samples. Image conversions record encoding, dimensions, scaling and source
identity. Optional reference preparation is separate from sensor preparation.

**Acceptance:** repeat import produces identical prepared data and semantic
identities; malformed values, broken images, duplicate members, unsafe paths and
inconsistent metadata have explicit outcomes. TUM-VI compatibility is checked
against the frozen preparation. A valid recording without truth imports.

### 2.3 — Private hosted recordings and upload lifecycle

**Dependency:** 2.2.

**Deliverable:** multiple private acquisitions per project, browser uploads,
durable import status and isolated preparation. Define receiving, validating,
ready, rejected and cancelled outcomes; publish a usable version atomically.
Handle interrupted transfers with a documented retry/resume policy.

Measure and enforce compressed/unpacked bytes, file/image counts, dimensions,
per-account/global storage, duration, scratch and CPU/memory limits. The current
8 KiB API body limit and small service tmpfs allocations cannot serve as upload
budgets. Keep expensive decoding outside HTTP. Select storage and transfer
mechanics from those measurements; no object-storage migration is assumed.

Separate originals, derived inputs and job artifacts in retention/deletion.
Prevent deletion races with active jobs, clean abandoned uploads, and extend
coherent backup/restore coverage to private inputs before hosted storage is
considered delivered. Every preview/file route resolves the owned acquisition.
Specify how source deletion affects retained calibration/experiment revisions,
derived results, backup retention and future replay. Missing inputs must have an
explicit availability state; already delivered complete packages remain usable.

**Acceptance:** browser import survives refresh; two accounts cannot exchange
IDs to access files or metadata. Interrupted uploads, corrupt archives,
expansion limits, quota races, cancellation and deletion are exercised. Restore
recovers an acquisition with matching hashes and access boundaries.

### 2.4 — Data-quality investigation before estimation

**Dependency:** 2.3.

**Deliverable:** a Data view linking images, all IMU axes and timing diagnostics
without requiring a VIO run. Show rates, interval distributions, gaps, duplicates,
resets, stereo pairing, IMU overlap and reference coverage. Trace findings to
exact streams/samples and display units, frames and timestamp meaning.

Distinguish import validity, inspection availability, calibration suitability,
estimator compatibility and evaluation availability. Saturation requires known
sensor limits; blur/exposure indicators and apparent stationarity are evidence,
not universal quality verdicts. Display reduction must preserve gap boundaries
and never replace full-resolution checks.

**Acceptance:** faulty fixtures yield the expected diagnoses, including separate
sensor/reference/estimate support. Room2's reference gaps are visible before
execution without altering its historical qualification. No-reference data can
remain eligible for supported estimation.

### 2.5 — Versioned supplied calibration and time mappings

**Dependency:** 2.4.

**Deliverable:** import, inspect, validate and select structured camera,
stereo, camera–IMU and supported IMU calibration artifacts. Record method/source,
sensor identities, camera model/resolution, transform direction, noise units,
validity conditions and corrections already applied. Distinguish supplied,
estimated and independently checked artifacts.

Represent clock-domain mapping separately from camera–IMU sensor offset. State
offset sign, application point, time origin and rounding. A declared drift model
requires supporting evidence and a validated consumer; resets create separate
segments. Unsupported or ambiguous clock relationships remain ineligible.
Known constant offsets are the first required correction case.

Applying a calibration/time mapping creates a derived input revision, with an
explicit chain back to original observations. Never edit old experiments or
paste arbitrary runtime YAML into the execution boundary.

**Acceptance:** known transforms and offsets give the declared results;
inverted frames, improper rotations, model/resolution mismatch, invalid noise
units and duplicate application are caught. Changing a candidate leaves prior
versions reproducible. A valid file alone does not establish physical accuracy.

### 2.6 — VIO execution on private acquisitions

**Dependency:** 2.5.

**Deliverable:** a new versioned execution profile selecting exact acquisition,
preparation, calibration, timing and interval versions. Resolve private inputs
per job rather than through the current global room1 path. Reuse durable
execution and investigation with explicit initialization, stereo pairing, IMU
bracketing and end-of-stream policies.

Support absent/partial references throughout execution, publication,
investigation and result retention: a successful estimation can have evaluation
unavailable. Sensor-only containers never receive evaluation truth. State
whether supplied calibration is fixed or refined online; preserve the original
room1 policy and do not present unexported internal refinements as calibration
outputs.

**Acceptance:** a real uploaded recording runs and can be investigated from the
browser, both with a suitable reference and without one. No-reference results
contain no invented ATE or accuracy qualification. Unsupported acquisitions
fail admission with reasons. The old room1 workflow/specifications still replay.

### 2.7 — IMU characterization and bounded calibration

**Dependency:** 2.5; hosted numerical work uses the execution controls of 2.6.

**Deliverable:** reproducible static-segment analysis, residual gyroscope bias
and repeatability, and supported noise characterization. Identify applied
filtering, sample rate, stationarity, temperature information and acquisition
duration. Distinguish continuous noise density, bias diffusion and per-sample
variance. Allan-deviation fitting requires adequate data and declared fitting
regions; short/filtered snapshots may support descriptive statistics only.

Support accelerometer bias/diagonal-scale estimation only with an adequate
documented multi-pose acquisition and reference assumptions. A single gravity
pose cannot identify full calibration; gyroscope scale, axis misalignment and
temperature compensation are outside the initial identification scope unless
their required observations are separately established.

**Acceptance:** known synthetic bias/scale/noise cases and independent validation
poses check supported calculations. Real recordings produce identified
parameters with their evidence or an explicit insufficient-data outcome.
Candidates are versioned and cannot silently replace estimator noise defaults.

### 2.8 — Camera intrinsics and stereo calibration

**Dependency:** 2.5–2.6.

**Deliverable:** execute a qualified, pinned calibration tool on target images
and measured target geometry. Produce supported intrinsics, distortion and
stereo extrinsics, detection/coverage diagnostics and reprojection residuals.
The proposed Kalibr integration must first pass a reproducible build, supported
camera-model and bounded-resource check.

Record target scale/uncertainty, resolution, focus and shutter assumptions.
Retain rejected images and failure reasons. Validate on images/recordings
excluded from fitting; expose uncertainty or conditioning only when the method
actually provides it.

**Acceptance:** synthetic known-geometry cases check conventions and scale;
real target images produce an estimator-compatible versioned artifact and a
separate validation report. Poor coverage or insufficient observations cannot
be presented as a validated calibration. Export usable parameters and the
reproduction recipe as part of this increment.

### 2.9 — Camera–IMU extrinsics and temporal calibration

**Dependency:** 2.7–2.8.

**Deliverable:** a bounded offline workflow estimating the supported rigid
camera–IMU transform and constant temporal offset from joint calibration
recordings, using the qualified camera and IMU models. Show excitation,
residuals, convergence/conditioning evidence and validity interval. Preserve
the distinction between clock offset, clock-rate drift, latency and exposure.

Use the existing clock mapping when justified; do not claim a constant-offset
solver estimates arbitrary clock drift. Identify resets and evidence of drift
only where clock/synchronization information or identifiable cross-sensor
observations support that diagnosis. Unknown or ambiguous relationships stay
explicit and can block eligibility; a constant-offset fit does not establish
clock stability. Evaluate application sign and scheduling independently. Any online
refinement allowed during subsequent VIO must be recorded.

**Acceptance:** known spatial/time fixtures verify sign, units and application
exactly once. Static or poorly excited sequences yield failure/inconclusive
results. A real joint recording and separate validation recording exercise the
full workflow. Ground-truth trajectory alignment cannot fit away timing error.

### 2.10 — Controlled calibration and timing comparisons

**Dependency:** 2.6–2.9.

**Deliverable:** an explicit comparison mode for supplied versus candidate
calibration/time versions. Bind the same original physical observations and,
when available, the same independent reference; list all permitted preparation, calibration and clock
differences. Keep the original strict configuration-comparison policy.

Use a small predeclared set of unit/frame/offset/calibration fault fixtures and
real candidate runs. Preserve common physical support despite timestamp
transformations, global alignment rules, missing intervals and initialization
loss. Fix the online-adaptation policy so it cannot silently compensate for the
parameter being tested. No automatic optimizer is introduced.

**Acceptance:** known faults are detectable under the stated conditions;
unrelated recordings or evaluation changes cannot masquerade as paired runs.
Show unfavorable and inconclusive outcomes, calibration residuals and trajectory
evidence separately. Without truth, compare availability/repeatability/residuals
without ranking physical trajectory accuracy.

### 2.11 — Portable private acquisitions and calibration exports

**Dependency:** 2.10; standalone parameter/recipe exports accompany earlier tools.

**Deliverable:** versioned export of a complete selected input chain and its
experiment, including original inputs or an independently deliverable verified
input bundle, calibration evidence, clock transforms, preprocessing, configuration,
source/runtime recipe and available evaluation. Sensor data must remain private;
public download URLs cannot substitute for private-input availability.

Separate a complete reproduction package from a manifest-only report. Large
data need measured streaming/bundle limits rather than expansion of the current
16 MiB in-memory export. Record all required data dependencies, including
calibration recordings when reproducing the fit is claimed. No service
credentials, temporary private URLs or internal host paths are required.

**Acceptance:** reproduce a private acquisition's preparation, selected
calibration fit and VIO run outside the service using the delivered bundles.
Verify hashes, timestamps/support, declared numerical tolerances and actual
runtime provenance. Repeat without a Meridian session and after hosted source
deletion. Reference-free reproduction remains valid with accuracy unavailable.

### 2.12 — Documented physical bench acquisition and replay

**Dependency:** complete hosted analysis/delivery uses 2.2, 2.4–2.7 and 2.11;
camera extensions also require 2.8–2.9. Protocol preparation, configuration
read-back and physical acquisition can proceed earlier when the required
hardware is available, using the existing audit/replay tools.

**Deliverable:** extend the [bench protocol](bench-acquisition.md) with
machine-readable independent static-reference windows, reference uncertainty
and timestamp mapping, then perform a new documented acquisition and replay.
Read back current controller/firmware/logging conditions, verify axes/units and
already-applied calibration, and record stationary periods, referenced poses
and slow manual motion as supported by the actual setup.

Keep the drone disarmed, propellers removed and estimator outside the control
loop. Retain the DataFlash snapshot model where that is the available stream.
Static reference windows are evaluator-only; another fused attitude is a
comparator. Report repeatability/agreement if no independent reference exists;
physical accuracy then remains unavailable. Independent accuracy is a completion
gate only if the declared acquisition protocol requires it.

**Acceptance:** a new physical recording, acquisition record, integrity/timing
audit and reproducible replay exist. Reference-window tests cover known errors,
uncertainty and gap-crossing windows. Independent static errors are reported
only where reference evidence supports them; dynamic accuracy remains unclaimed.
A camera–IMU bench result additionally requires an established compatible rig
and synchronization. If hardware is unavailable, keep this increment explicitly
pending; fixtures and public benchmarks do not complete it.

### 2.13 — Complete hosted acceptance and part assessment

**Dependency:** 2.1–2.12; unavailable hardware must remain a visible open item.

**Deliverable:** a frozen acceptance protocol and measured report for browser
upload → data inspection → supplied/estimated calibration → synchronization →
VIO → comparison → portable export. Exercise real licensed calibration data,
independent evaluation sessions, no/partial-reference recordings and declared
invalid cases. Record who actually evaluated the interface.

Verify deployed ownership, storage/compute quotas, cancellation, interrupted
imports, worker recovery, active-input retention, deletion and restoration of
metadata, original inputs, calibration and retained artifacts. Measure relevant
CPU, memory, storage and transfer costs on the shared host.

**Acceptance:** the supported workflow succeeds over HTTPS from a separate
client and independent reproduction passes. Keep the room1 regression and
room2 historical failed coverage gates unchanged. Document supported formats,
models, resource limits, failures and calibration validity. Distinguish
completed software acceptance from any pending physical acquisition; do not
declare all of part 2 complete while a required bench deliverable is pending.

## Completion and decisions to resolve within the increments

The proposed software order is 2.1 through 2.13. Physical acquisition in 2.12 can
start earlier when hardware is available, with integrated analysis/delivery
completed after its dependencies. Hardware availability can otherwise require a
pause; software checks cannot replace that acquisition. No duration or
performance improvement is promised.

Resolve exact transfer layout and fixtures in 2.1; upload/storage budgets in
2.3; supported calibration/clock models in 2.5; solver revision, runtime and
real calibration recordings before fitting in 2.7–2.9. If a tool cannot satisfy
its declared model or host budget, record that result and revise the proposal
explicitly. Do not silently drop calibration into a file-import-only feature.

Part 2 should deliver reusable acquisitions and calibration with evidence, not
claims of universal identifiability, drift-free VIO, real-time onboard operation
or flight validation. New estimator development remains in part 3, whose
increments are not specified here.
