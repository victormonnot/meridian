# Independent replay after workspace restoration

**Passed:** the actual HTTPS experiment download was restored from the workspace
backup, then reproduced outside the checkout and hosted backend after its
original source job was deleted. The reproduced trajectory is byte-identical;
timestamps, evaluation support, missing regions and reported errors match.

The browser downloaded the successful room1 100-point candidate. Off-server
restoration recovered the same ZIP bytes: **681,997 bytes**, SHA-256
`d7243ecdc428c1e50f80eafee3179817a2530ba38fb2556619ef89f6ebf53af0`.
Authenticated deletion returned 200, and subsequent job and export requests
returned 404 at **10:13:24 UTC on 20 October 2026**. Independent replay began
at 10:15:35 UTC and exited successfully at 10:16:31 UTC, taking **56.71634 s**
overall, including **32.76455 s** of native execution.

## Environment and reuse

The archive was copied and extracted into a new directory outside the repository,
with a new reproduction output directory. The replay used the independent
Python 3.12 environment established in [increment 1.7](../../exports/README.md),
containing only NumPy 2.5.3 and pip 24.0. All eight imported Meridian modules
came from this archive's source tree. The subprocess environment contained only
`PATH`, `LANG`, `PYTHONNOUSERSITE` and `PYTHONDONTWRITEBYTECODE`.

The previously independently downloaded and prepared room1 inputs and the
source-built native image were reused explicitly. This was not another cold
build, a fresh dependency installation or an additional physical machine.
The image identity was
`sha256:213dad232a508955fcaf80452dbf10f696981eb1562e5ac5eb9f8faae75bb82d`,
distinct from the original qualified configuration identity `73e1213d…`.
The original experiment specification remains unchanged; replay records the
different runtime rather than relabeling it as the original image.

All **24 exported source files** match the accepted repository sources. Only
`src/meridian/vio_dataset.py` differs from the retained increment 1.7 archive,
adding the explicit room2 preparation path while preserving room1 identities.
The native adapter/build recipe, evaluator and runner are unchanged. The replay
checks the reused image's adapter/build sources, version and installed
dependencies against this export before execution.

No Meridian account, database, worker or backend request was needed for the
independent calculation. A live observation of its native container confirmed
read-only sensor/configuration mounts, separate writable output, a non-root user,
no network or reference/socket mount, and the original resource limits.

## Verification

| Check | Observed outcome |
| --- | --- |
| Standalone verifier under Python `-I -S` | Pass; no NumPy needed for inspection |
| Archive inventory | All 38 payload files verified |
| Prepared recording | All 5,651 files verified, 1,696,791,755 bytes |
| Prepared manifest | Original `e55f0480…8ba6d` identity preserved |
| Immutable specification | Original `98e780ba…e5f73` identity preserved |
| Trajectory | Byte-identical; SHA-256 `5640a2349c71c67c1ffeda44a59b0226cb8d5f9ea86ec58d068a78b30660462e` |
| Output/evaluation timestamps and support | Identical |
| Maximum direct position/orientation difference | 0 m / 0° |
| Position ATE RMSE | 0.051132041394887494 m in both |
| Orientation RMSE | 1.532136616154553° in both |
| Numerical qualification and reproduction tolerances | Pass |

The original 1 mm / 0.001° tolerances and unit-scale global SE(3) evaluation
protocol were retained. Direct poses were compared without fitting another
alignment. The independent audit rehashed evidence and prepared inputs,
recomputed direct pose differences and recomputed RMSE from retained per-sample
errors. It is **not a second implementation of the complete SE(3) evaluator**.

The six JSON records below are exact copies of observed evidence; private
import-path logs and the raw archive are not published:

- [Execution, deletion ordering and declared reuse](execution.json)
- [Standalone verifier](standalone-verify.json)
- [Archive versus original HTTP response and source audit](export-audit.json)
- [Reproduction result and original/runtime identities](reproduction-report.json)
- [Independent artifact and numerical audit](reproduction-audit.json)
- [Live native container observation](native-isolation.json)

This establishes the tested archive/restoration/replay path on the supported
Linux/Python/Docker environment. It does not establish cross-platform bit
identity, cold-build availability, statistical confidence, real-time deadlines
or flight performance. See the [complete acceptance report](../README.md) for
the hosted workflow, backup scope and the separate failed room2 coverage gates.
