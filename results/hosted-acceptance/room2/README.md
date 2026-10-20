# Reserved room2 evaluation

**Outcome: all three runs fail the original qualification gates.** Native
execution completes, and the three trajectories are byte-identical, but reference
coverage and evaluated coverage are below their fixed thresholds. The gates,
reference-interpolation rule, baseline configuration and estimator remain
unchanged. This is an explicit negative result from the reserved recording,
separate from the [hosted workspace acceptance](../README.md).

The [room2 protocol](../../../docs/room2-acceptance.md) was committed and pushed
in [`f3c7ff6`](https://github.com/victormonnot/meridian/commit/f3c7ff6)
before acquisition or evaluation on 20 October 2026. It specifies exactly three
full-recording executions of the original OpenVINS baseline with `num_pts: 200`.
No 100-point variant, parameter search, interval selection or replacement run was
performed.

## Numerical results

Each run produces the same 2,778 estimated poses from 2,882 camera pairs.
The first 104 camera timestamps have no estimate during initialization; there
are no missing interior or trailing estimates. Independent evaluation uses
2,469 poses with valid reference support under the fixed 20 ms bracket limit.

| Run | Native outcome | Position ATE RMSE | Orientation RMSE | Qualification |
| --- | --- | ---: | ---: | --- |
| 1 | Complete | 0.0926172211 m | 2.3407484491° | Fail: reference and evaluated coverage |
| 2 | Complete | 0.0926172211 m | 2.3407484491° | Fail: reference and evaluated coverage |
| 3 | Complete | 0.0926172211 m | 2.3407484491° | Fail: reference and evaluated coverage |

The following observations are identical in all three runs:

| Gate | Observed | Required | Outcome |
| --- | ---: | ---: | --- |
| Native execution and evaluator | Complete, initialized; evaluator `ok` | Valid completion and `ok` | Pass |
| Evaluated samples | 2,469 | At least 100 | Pass |
| Estimate coverage | 96.3914% | At least 90% | Pass |
| Evaluated coverage | 85.6697% | At least 90% | **Fail** |
| Reference coverage | 89.2783% | At least 95% | **Fail** |
| First emitted pose delay | 5.200233439 s | At most 10 s | Pass |
| Position ATE RMSE | 0.0926172211 m | At most 0.5 m | Pass |
| Orientation RMSE | 2.3407484491° | At most 15° | Pass |

The position maximum on evaluated support is 0.300915242 m; the orientation
maximum is 5.717733580°. These errors use one global SE(3) alignment with scale
fixed to one. They do not describe accuracy on unsupported intervals. The same
alignment rotation applies to orientations, so their errors do not independently
validate the gravity direction. Full precision is retained in
[the acceptance record](acceptance-report.json).

## Why coverage fails

An [independent timestamp audit](reference-support-audit.json), using Python
integer arithmetic and `bisect` without importing the Meridian evaluator, finds
49 adjacent motion-capture gaps wider than 20 ms. The longest is 2.491667 s.
There are 308 camera timestamps within these unsupported brackets and one
after the last reference timestamp. Thus only 2,573 of 2,882 camera timestamps
have valid reference support: 89.2783%.

This independently reproduces the evaluator's support count. Reference coverage
fails before estimator accuracy is considered; it is not itself a measure of
tracking loss. The initialization interval removes a further 104 timestamps
from evaluated support, leaving 2,469. The maintained full-recording denominator,
20 ms interpolation limit and no-extrapolation rule are preserved.

The [dataset description](https://cvg.cit.tum.de/data/datasets/visual-inertial-dataset)
identifies room recordings as motion-capture sequences. That description does
not imply uninterrupted reference support under Meridian's stricter association
protocol. No missing reference poses were invented, interpolated across wider
gaps or removed from the denominator to turn this outcome into a pass.

## Frozen inputs and repeatability

Acquisition started at 09:21:27 UTC and finished at 09:27:32 UTC, after the
protocol commit. Before preparation or estimator execution, the downloaded
archive matched the publisher's MD5 and the previously observed byte count;
its independently computed SHA-256 was recorded and pinned. Preparation and
input identity were recorded before native execution began.

| Input or evidence | Identity |
| --- | --- |
| Calibrated archive | `dataset-room2_512_16.tar`, 1,759,395,840 bytes |
| Archive SHA-256 | `70343c5bbd9d674d0a2790bf934ee7d9cc08c1dea7cd77747a5557e4c94973f6` |
| Publisher MD5 | `412ec1596f2973a5cefac6a351a43486` |
| Prepared manifest SHA-256 | `edef18aca9b0672e7580ca10e2f6fd9d4a4f2ad8d0bcdb223d24d229fcfa39ba` |
| Normalized reference SHA-256 | `a1bd2bb90947100e3606031859e0bcf4f3c20611438ad2e0145e6b5b56a8b117` |
| Trajectory SHA-256, each run | `8d48fe0892e4b116e1a418ea3b55faa7bab3c6dabeea9d5086f09346addf4093` |
| Run specification SHA-256, each run | `b6892c7b035e4cabe4adbd4fc1608ebff94c7a13716b609c1d343fd654a9fa0f` |

The prepared input contains 5,773 files totaling 1,749,020,182 bytes:
2,882 paired-camera samples, 28,732 IMU samples and 15,477 reference poses,
plus the exact original configuration and masks. Input camera duration is
144.054327316 s. No second correction was applied to the already calibrated and
synchronized export.

All executions use the original qualified runtime configuration ID
`sha256:73e1213d073ce52a9a509decb15628a7c043e0690609171c4b9382ed0c2730da`.
The native adapter, reference runner, evaluator, calibration, noise assumptions
and baseline settings retain the hashes specified in the frozen protocol.
Existing room1 specifications for 100, 200 and 400 points remain unchanged.
[Actual room1 verification](room1-regression.json) also checks all 5,651 prepared
files and its original manifest identity after adding the explicit room2
preparation path.

Repeatability passes the original 1 mm / 0.001° tolerances without another
alignment. Timestamps and trajectory bytes match exactly. The unchanged
reference comparison reports 0 m and approximately 0.000002415° maximum
difference; the tiny nonzero angle is floating-point rounding in its
dot-product/arccos calculation for identical stored quaternions. The independent
relative-quaternion calculation returns zero direct position and orientation
difference.

## Resource and isolation observations

The local host is Linux x86-64 under WSL2, with an Intel Core i5-12400F,
12 logical CPUs, Python 3.12.3 and NumPy 2.5.3. Each native container retains the
original limits: 2 CPUs, 4,096 MiB memory without additional swap, 128 processes,
128 MiB temporary storage and 900 s wall time.

| Run | Native wall time | Native CPU time | Native peak RSS | Outer container time | Output bytes |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 45.2119 s | 45.2285 s | 81,116 KiB | 45.5554 s | 1,737,449 |
| 2 | 44.9622 s | 44.9680 s | 80,752 KiB | 45.3072 s | 1,737,449 |
| 3 | 44.7203 s | 44.7764 s | 81,344 KiB | 45.0606 s | 1,737,443 |

These are observations on one development host, not performance comparisons.
Application image builds finished before the acceptance executions; local
API/worker activation and metadata verification overlapped the first run.
The host was not an isolated performance benchmark. RSS is the native process
measurement and does not include all container filesystem cache. Offline
execution times do not establish real-time deadlines or embedded performance.

[Live container inspection](native-isolation.json) observed all three native
executions with a non-root user, read-only root filesystem, no network, dropped
capabilities and the declared resource caps. The only mounts were read-only
sensors, read-only configuration and writable output. No reference or Docker
socket was mounted. This local experiment evaluates the reference separately
in the trusted host Python process after native execution; hosted evaluator
isolation belongs to the overall hosted report.

## Checks and reproduction

The focused dataset/reference/evaluation/specification/export/reproduction/
acceptance suite passes **294 tests**, including 22 new acceptance tests and
eight new dataset cases. They cover fixed runtime/configuration checks, three
attempts without tuning controls, explicit failures and interruption records,
input changes between attempts, rejection of unsafe output destinations,
room2/source mismatches and unchanged room1 identities.

The [evidence audit](evidence-audit.json) independently verifies all 5,773
prepared files, seven recorded artifacts per run, result/run bindings,
timestamp equality, direct pose differences and RMSE recomputed from retained
per-sample errors. Its `status: passed` means the evidence is internally
consistent; `scientific_qualification_passed: false` preserves the experiment's
negative outcome. This audit is not a second implementation of SE(3) evaluation.

Using the repository's pinned Python environment and the **already retained
qualified native image**, reproduce in new directories:

```sh
python -m meridian.vio_acceptance download \
  --output outputs/room2-acceptance/dataset-room2_512_16.tar
python -m meridian.vio_acceptance prepare \
  --archive outputs/room2-acceptance/dataset-room2_512_16.tar \
  --config integrations/openvins/config \
  --output outputs/room2-acceptance/prepared
python -m meridian.vio_acceptance run \
  --prepared outputs/room2-acceptance/prepared \
  --output outputs/room2-acceptance/runs
```

The last command runs exactly three attempts and returns exit code **1** for
this completed but failed qualification. Preflight/setup errors return 2;
failures within the attempts retain their error categories, and interruptions
preserve an incomplete report. The exact runtime must already be
available: a new source build is a different identity and is not silently
substituted in this frozen acceptance experiment.

The seven JSON records are unchanged copies of observed evidence:
[acquisition](acquisition.json), [prepared identity](prepared-identity.json),
[room1 regression](room1-regression.json), [acceptance](acceptance-report.json),
[native isolation](native-isolation.json),
[reference support](reference-support-audit.json), and
[evidence audit](evidence-audit.json). Raw camera images, IMU, reference poses and
logs are not redistributed here. The public dataset remains attributable to
the TUM VI Benchmark authors under CC BY 4.0, as recorded in prepared metadata.

Room2 remains a local reserved-recording case, not a selectable hosted profile.
Together with the development room1 sequence, it is a two-recording handheld
indoor exercise. It establishes neither general drone robustness nor visual-loss
recovery, onboard or flight validation, covariance consistency, or demonstrated
market value.
