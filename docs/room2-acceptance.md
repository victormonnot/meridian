# Reserved recording acceptance protocol

Protocol version 1, fixed before acquiring or evaluating TUM-VI room2 in
increment 1.8. The associated
[hosted acceptance protocol](hosted-acceptance-protocol.md) exercises the complete
online room1 workflow. This document defines a separate, local evaluation of the
reserved recording. It does not add room2 to the hosted catalog, configuration
profile, comparison service or downloadable experiment format.

## Fixed experiment

Run the full calibrated TUM-VI EuRoC `room2_512_16` recording exactly three times
with the existing OpenVINS baseline, `num_pts: 200`. Use the same five
configuration and mask files, calibration, noise densities, online-calibration
settings, static initialization, scheduling and image conversion described in
[the reference contract](vio-reference.md). There is no parameter search,
100-point variant, interval selection, new calibration or result-dependent
change. Room1 remains the development sequence; room2 is the reserved evaluation
sequence. All three outcomes, including failures, belong in the report.

Use the existing qualified native runtime, with configuration identity
`sha256:73e1213d073ce52a9a509decb15628a7c043e0690609171c4b9382ed0c2730da`
or its verified OCI manifest identity
`sha256:54863f14d9f92d2805f5fddcfb78d146c6aac2ad77dc348e304059e2252a400a`.
Record the identity actually used. A newly built image is not substituted for
this frozen experiment.

| Frozen component | SHA-256 or revision |
| --- | --- |
| OpenVINS revision | `69488123ed9362dd44b6f28e7f4680abbff1442b` |
| Native adapter source | `a577f358c893ac9171cad6a71bc2a65a49b9d90ab3dc426865d89b6ad2c8b782` |
| Python reference runner | `d840c4400675a16a6ed22873086a8267f3547b750a5d62fbbf1c03279bd51a09` |
| Evaluator | `539f07a0e3dbabe5fd3b4874b42218db1938e5f3733d8ac3bd9c714900a8cca6` |
| Baseline estimator YAML | `10c37c00c52a07ba5a86ce3d6d2846c2430397d6fa6a9e190dfdc47aa10c2045` |
| Installed dependency inventory | `92d76ee986a1ff4508a96fb3f2c26439d99cb446a546f8e0d795f56d69e7023e` |
| Native binary inventory | `7b66a083c7d5c69c6e85721e3357def42fbc1a125a5b13735f3e55ee3ad28070` |

The acceptance entry point verifies these identities and every baseline
configuration file before execution. It records a room2 run manifest, without
mislabeling it as a stored room1 experiment specification. Existing room1
specifications and prepared-manifest bytes must remain unchanged.

## Acquisition before execution

Use the official calibrated archive
[`dataset-room2_512_16.tar`](https://vision.in.tum.de/tumvi/exported/euroc/512_16/dataset-room2_512_16.tar).
Its [publisher checksum](https://vision.in.tum.de/tumvi/exported/euroc/512_16/dataset-room2_512_16.tar.md5)
was retrieved over HTTPS before acquisition: MD5
`412ec1596f2973a5cefac6a351a43486`. The HTTPS HEAD response reported
1,759,395,840 bytes. Preserve the source response, final URL, byte count, MD5
and independently computed SHA-256. MD5 is a publisher transfer check, not a
modern authenticity guarantee.

Freeze this protocol in Git before downloading the archive. After download,
check the publisher MD5 and exact size, compute SHA-256, and record that identity
before preparation or estimator execution. Preparation records the full
per-file SHA-256 inventory, normalized-reference identity, complete camera/IMU
timestamps and counts, and exact configuration copies. No estimate or error
metric is inspected during this identity-acquisition step.

The [dataset maintainers](https://cvg.cit.tum.de/data/datasets/visual-inertial-dataset)
describe the calibrated export as already time-aligned and IMU-corrected, with
reference poses expressed in the IMU frame. Do not apply the raw-recording
corrections again. Keep the CC BY 4.0 attribution in all prepared metadata.
Preparation keeps camera/IMU input apart from the motion-capture reference.
The estimator receives only sensor files, configuration and its writable output;
the independent evaluator receives the reference after native execution.

## Unchanged numerical protocol and gates

Apply evaluator protocol 1 to the complete paired-camera timeline. Retain
integer nanoseconds, positions in metres and unit Hamilton `qx,qy,qz,qw`
quaternions representing IMU-to-world rotation. Match each estimate to at most
one camera timestamp within 1 microsecond. Never interpolate missing estimates.
Reference interpolation uses linear position and shortest-arc quaternion SLERP
only within brackets no wider than 20 ms. Do not extrapolate or fit a time
offset.

Fit one global least-squares SE(3) transform over all evaluated positions,
holding scale at one. Apply its rotation to orientations. Reject degenerate
alignment support. Report the whole-recording coverage denominator, missing
regions, support counts, aligned position ATE RMSE, aligned orientation RMSE,
and first-emitted-pose delay from the first camera timestamp. Aligned orientation
error does not independently validate the gravity direction.

| Required check | Fixed threshold |
| --- | --- |
| Native completion | Complete, initialized and structurally valid output |
| Evaluator status | `ok` |
| Evaluated samples | At least 100 |
| Estimate coverage | At least 90% of all camera pairs |
| Evaluated coverage | At least 90% of all camera pairs |
| Reference coverage | At least 95% of all camera pairs |
| First emitted pose delay | At most 10 s |
| Aligned position ATE RMSE | At most 0.5 m |
| Aligned orientation RMSE | At most 15 degrees |

These are the original integration sanity gates, not drone navigation
requirements. An incomplete run or unavailable metric fails its applicable
check; a zero process exit status alone is insufficient.

For the three identical executions, require identical input/configuration/run
identities and output timestamps. Report trajectory byte identity and maximum
direct position and orientation differences without any additional alignment.
Require maximum position difference at most 1 mm and maximum orientation
difference at most 0.001 degrees. Preserve the reference runner's comparison
implementation; do not adjust its tolerances after viewing results.

## Resources, failures and interpretation

Use the original limits: 2 CPUs, 4,096 MiB memory with no additional swap,
128 processes, 128 MiB temporary storage and 900 seconds wall time per native
execution. Use the existing non-root, read-only, network-disabled container
configuration and single-thread estimator settings. Record host CPU, platform,
Python/NumPy, native elapsed/CPU time and RSS, container elapsed time, prepared
and output bytes, and limits. Offline elapsed-time ratios do not prove real-time
or onboard behavior.

Allocate new run directories and retain attempt records. A failed numerical
gate remains a failed qualification even if the execution completed. Exceptions,
timeouts, malformed outputs and interrupted attempts retain their actual failure
category. Any repeatable numerical failure is reported without tuning or
replacement by a more favorable run. A proven infrastructure interruption may
be rerun under the identical protocol, with both attempts and the reason
retained. No protocol revision may silently replace this original record.

The report distinguishes scientific qualification from the hosted workflow
acceptance. It states whether all three room2 executions meet the original gates
and whether repeatability passes, without pooling room1 and room2 into a
performance ranking. These two handheld indoor recordings do not establish
general robustness, recovery after visual loss, outdoor or drone performance,
flight safety, covariance consistency, product value, or replacement of an
estimation engineer.
