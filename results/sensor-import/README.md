# Sensor import and preparation regression

Increment 2.2 adds local import and preparation for versioned sensor packages,
calibrated TUM-VI exports and DataFlash snapshots. This record checks file
preservation, numerical representation and deterministic lineage. It does not
claim a new calibration, VIO trajectory, physical acquisition or flight result.

## TUM-VI room1

The regression uses the already-consumed, pinned calibrated `512_16` room1
archive and the verified preparation from the
[original qualification](../vio-reference/README.md). No new reserved recording
was opened. Source attribution and CC BY 4.0 licensing are recorded in the
[source registry](../../docs/sensor-data-sources.md).

The acceptance script checks the source archive's frozen SHA-256 and size,
verifies the entire legacy preparation, imports twice into distinct directories,
and prepares both imports with PNG16 preservation. It compares every resulting
file identity and checks all camera timestamps and image bytes, IMU timestamp
and measurement decimal strings, and reference position/quaternion strings
against the legacy preparation. It re-verifies the source and legacy payloads
afterwards.

**Full repeated comparison: passed.** Both imports contain the same 5649
files byte for byte, as do both preparations. Every compared camera/IMU/reference
value matches the frozen input. The original archive and all legacy payloads
remain unchanged. [Recorded identities and comparisons](room1-regression.json).

| Stream | Samples in each verified import/preparation |
| --- | ---: |
| Left camera | 2821 |
| Right camera | 2821 |
| IMU | 28122 |
| Optional mocap reference | 16541 |

The new package format adds row/segment identifiers and explicit descriptors.
It does not promise byte-identical CSV wrappers to the older format; the
acceptance compares the retained numerical columns and images exactly.

Reproduce with already-acquired pinned inputs and a new output directory:

```sh
python scripts/verify-sensor-import.py \
  outputs/vio-reference-data/dataset-room1_512_16.tar \
  outputs/vio-reference-data/prepared \
  outputs/sensor-import-check
```

The script performs no download or estimator execution. Large original and
derived payloads remain local; the final report contains aggregate comparisons
and hashes only.

## Image conversion

The [OpenCV probe](../../scripts/check-image-scaling.cpp) compares the integer
conversion `(n + 128) / 257` with the existing runner's
`Mat.convertTo(..., 255/65535)` for every uint16 value. The
[recorded result](image-scaling.json) is **65536 values, zero mismatches** under
OpenCV 4.6.0 from the qualified native image.

The Python test independently decodes the generated PNG, compares every
intensity with nearest-integer scaling, and verifies identical output bytes
across repeated encodes. This validates the representation conversion, not
feature-tracking or estimator quality.

With the qualified image from the [integration guide](../../integrations/openvins/README.md):

```sh
docker run --rm --network none \
  --mount "type=bind,source=$PWD/scripts,target=/input,readonly" \
  --entrypoint /bin/sh meridian-openvins:reference -c \
  'g++ -std=c++17 -O2 -I/usr/include/opencv4 /input/check-image-scaling.cpp -lopencv_core -o /tmp/image-scaling && /tmp/image-scaling'
```

## Historical DataFlash: rejected

The existing historical source is 8127996 bytes with SHA-256
`47b6476bc77cc1ccd9c1248a31eeaeec8866c77062c176127411192d84987191`.
It yields 9788 decoded IMU snapshots from instance 0, zero parser diagnostics
and **37 unparsed trailing bytes**. Both new strict import attempts reject it
identically and remove their partial outputs. The original hash is unchanged.
[Recorded rejection evidence](dataflash-regression.json).

The source was not trimmed and the admission rule was not relaxed to manufacture
a success. Therefore no successful real-log package or prepared-input
repeatability claim is made for this source. Its
[earlier selected-segment replay](../imu-replay/README.md) retains its original
scope and limitations. Successful binary-fixture tests exercise the adapter,
all instances, resets, duplicates, gaps, units and reference-free preparation.

## Automated validation and limits

The complete Python suite with C++ integration passes **1690 tests**, including
189 new import/package/adapter tests. New cases cover exact inventories,
unsafe/duplicate archive members, symlinks and special files, expansion limits,
CSV semantics, malformed/animated PNGs, quaternion validity, optional truth,
source mutation, output conflicts and failure cleanup. Existing frozen VIO
specifications and legacy import/replay tests remain unchanged.

The [import guide](../../docs/sensor-import.md) defines the supported formats,
resource limits and output structure. Local package limits are not hosted
CPU/memory/scratch quotas. Uploads and hosted execution are still unavailable
for these new packages. Valid payload syntax does not establish calibration
accuracy, sensor simultaneity or estimator readiness; room2's existing coverage
failure remains part of the original evaluation record.
