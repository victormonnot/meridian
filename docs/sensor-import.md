# Local sensor import and deterministic preparation

**Implemented in increment 2.2.** These tools verify actual payloads against the
[sensor contracts](sensor-contract.md), retain original observations and create
separate prepared inputs. They do not run an estimator, fit calibration or
enable hosted uploads. An accepted import establishes supported syntax and
declared content identity, not physical accuracy, clock synchronization or VIO
readiness.

## Commands and output

Use the locked Python environment from the [README](../README.md). Pillow
12.3.0 is already locked and is now an explicit dependency. DataFlash additionally
uses the existing optional `pymavlink` dependency.

```sh
# Exact Meridian transfer package: directory, tar/tar.gz or ZIP.
python -m meridian.sensor_import package recording.tar outputs/recording-import

# Verify a materialized acquisition without modifying it.
python -m meridian.sensor_import validate outputs/recording-import/acquisition

# Separate prepared payloads; preserve images unless conversion is requested.
python -m meridian.sensor_import prepare \
  outputs/recording-import/acquisition outputs/recording-prepared
```

Every output must be new. Successful results are never overwritten. Failed new
outputs are removed; the source remains unchanged. These are synchronous local
operations: consume outputs after successful return, with no concurrent mutation
of source or destination trees. Atomic hosted admission, cancellation, retention
and storage quotas belong to 2.3.

The package importer creates:

```text
recording-import/
  acquisition/
    acquisition.json
    sensors/...
    references/...          # optional
    originals/...           # if inventoried by the acquisition
    evidence/...            # if inventoried by the acquisition
  original-transfer         # exact container bytes for an archive import
  import-report.json
```

The inner acquisition is an exact transfer package. Its descriptor and file
bytes are preserved. The outer import report records the acquisition identity,
verified inventory, actual stream counts and segments, reference availability,
limits, and original container identity. It has no execution time, absolute
machine path or randomly generated identity.

CLI commands emit JSON. Rejections use exit code 2 with `status: rejected` and a
finding; payload findings include a stable code and, when available, relative
path, stream and CSV row. There is no partial success or silently repaired row.
The `validate` command emits the full payload report for adapter outputs too.
Its nested metadata assessment still describes what metadata alone can prove;
the outer `payload_verification: verified` records the additional file checks.

## What is checked

The package must contain `acquisition.json` and exactly its inventoried files.
Additional files, unused sensor/reference payloads and unlisted empty
directories are rejected. SHA-256 and sizes are verified before interpreting
CSV or images.

Directory traversal, absolute paths, drive/URL paths, control characters,
symbolic/hard links, devices and other special entries are rejected. Archives
are streamed through explicitly created regular files, never `extractall`.
Duplicate members, malformed headers, inconsistent sizes, sparse files,
encrypted/multipart ZIPs and ZIP64 are unsupported. ZIP supports stored and
DEFLATE members. Extended tar metadata is bounded.

CSV headers are exact. The validator checks UTF-8, bounded fields/lines, finite
decimal measurements, strictly increasing sample IDs, source segment order and
strictly increasing timestamps within each segment. IDs may have gaps, retaining
the interleaving of DataFlash instances. Segment counts and endpoints must match
the actual rows. An explicitly declared reset can decrease timestamps between
segments. No sorting, sample deletion, interpolation or gap filling occurs.

Every camera row resolves to a distinct inventoried PNG. Dimensions and bit
depth must match its declaration: noninterlaced grayscale 8 or 16 bits only.
Animated PNGs are rejected.
Checks include all chunk CRCs, bounded DEFLATE expansion, complete image data
and full Pillow decoding. Merely reading an image header is insufficient:
[`Image.open` is lazy](https://pillow.readthedocs.io/en/stable/reference/Image.html#PIL.Image.open).
Declared exact stereo pairing is checked against every actual timestamp.

Reference poses remain optional. Present references require finite positions
and unit Hamilton xyzw quaternions within the convention helper's norm tolerance
of `1e-9`; they are never normalized automatically. Declared coverage is not
converted into an accuracy metric or proof of gap-free support.

## TUM-VI adapter

```sh
python -m meridian.sensor_import tumvi \
  dataset-room1_512_16.tar outputs/room1-acquisition \
  --id room1-acquisition --sequence room1
python -m meridian.sensor_import validate outputs/room1-acquisition
python -m meridian.sensor_import prepare \
  outputs/room1-acquisition outputs/room1-prepared
```

The adapter accepts a local **calibrated EuRoC `512_16`** directory or uncompressed
tar. A directory may contain `mav0` directly or one matching
`dataset-SEQUENCE_512_16` root. Selecting the adapter declares this profile; it
does not authenticate arbitrary bytes as a TUM recording.

The archive or complete input directory is retained under `originals/`.
Camera PNG16 bytes are copied unchanged. Camera and IMU CSVs add stable sample
and segment IDs while retaining integer ns timestamps and stripped numerical
decimal text. Optional mocap conversion only reorders wxyz to xyzw. Inputs
without `mocap0/data.csv` are supported; `--without-reference` excludes its
canonical reference stream while preserving the original container.

The profile supports cam0, cam1, imu0 and optional mocap0. Additional sensor
streams are rejected. The published archive's two exact DSO image-directory
symlink aliases are recorded and omitted from extraction; their bytes remain in
the original archive. Other links are rejected, including all directory-source
symlinks. This exception does not apply to the generic package importer.

`evidence/tumvi-source.json` records source identities, omitted aliases,
publisher/license URLs and transformations. Attribution and the SPDX license
are in `acquisition.json` provenance. Supplied calibration/configuration
files remain original evidence; they are not executed, fitted or silently
translated into a new calibration. Published IMU/time corrections are already
represented in the source data. Physical IMU/world axis descriptions and clock
epoch remain unknown here. See the
[source record](sensor-data-sources.md) for preprocessing constraints and
reservations; the importer does not download or select evaluation data.

## DataFlash snapshots

```sh
python -m meridian.sensor_import dataflash \
  recording.bin outputs/snapshot-acquisition --metadata session.json
python -m meridian.sensor_import prepare \
  outputs/snapshot-acquisition outputs/imu0-prepared --imu-stream imu0
```

`session.json` supplies `acquisition_id`, `rig`, `provenance`, `instances` and
optionally `gap_threshold_us` (default 200000). The first three follow the
[contract](sensor-contract.md). `instances` maps canonical numeric keys such as
`"0"` to:

```json
{
  "sensor_instance": "recorded-imu-0",
  "frame_id": "imu0-frame",
  "axes": null,
  "calibration": "unknown",
  "filtering": "unknown",
  "clock_correction": "unknown",
  "description": "Physical axes and frontend processing await session evidence."
}
```

Declare every decoded instance exactly once. Unknown physical metadata stays
unknown; firmware names do not establish axes or processing. The existing
decoder must verify logged units through FMTU/UNIT/MULT. All decoded IMU
snapshots and their global record indices survive unchanged, with native
integer microseconds, rad/s and m/s². Batch IMU and other message types remain
in the retained binary; this adapter does not pretend to decode them as streams.

Duplicate/backward timestamps and per-instance gaps strictly above the declared
threshold start a new segment, with an explicit reason in
`evidence/dataflash-import.json`. No snapshot is removed. The replay convention
remains previous-snapshot hold over the next logged interval, with no propagation
across segments; import does not execute that replay.

Nonfinite snapshots, conflicting units, decoder diagnostics or unparsed trailing
bytes reject the import. The original binary may contain information not present
in the selected evidence; it is retained locally in full. The
[historical-log regression](../results/sensor-import/README.md) documents a real
rejection rather than trimming the source or claiming a successful import.

## Prepared inputs

Preparation creates `source-acquisition.json`, `prepared-input.json`,
`preparation-report.json`, `sensors/` and optional `references/`. The
`prepared_input` binds the source descriptor hash and inventories every output
payload. Sample IDs, segment IDs, source order and observation values remain
linked to their originals.

- Source ticks become ns using exact integer multiplication. This changes units,
  not clock domains; no offset or drift is inferred.
- Non-SI IMU units become SI with explicit operation records. Conversion from
  `g` requires the supplied gravity scale. Already-SI decimal values remain
  unchanged, including their textual representation.
- Images keep original bytes by default. `--image-encoding mono8` records a
  mono16 conversion, each source/output hash, dimensions and encoding. For
  every uint16 value `n`, `(n + 128) // 257` implements nearest-integer scaling
  by `255/65535`, checked against the qualified runner's OpenCV `convertTo`.
  There are no half-integer ties. Output PNGs use explicit uncompressed DEFLATE
  blocks for deterministic encoding.
- `--without-reference` excludes prepared reference payloads. Their original
  identity remains in the source acquisition; they are never moved into sensors.

The initial preparation profiles are two exactly paired cameras plus one
instantaneous IMU, or one filtered-snapshot IMU. Multiple recorded IMUs require
an explicit `--imu-stream` choice. All selected segments are retained. Other
topologies remain importable when their declared streams are supported, but
cannot be prepared into these profiles.

Applying a supplied calibration, changing frames or correcting clocks belongs
to 2.5. Prepared candidates cannot be submitted to the existing hosted runner.

## Resource limits and evidence

Default local limits are 8 GiB materialized package bytes, 4 GiB per file,
100000 members, two million aggregate rows, 32 MiB descriptor, 512 MiB per CSV,
32768 characters per physical CSV line, 64 MiB per PNG and 16 million pixels
per image. Archive metadata is bounded at 64 MiB. Preparation also bounds its
output payload bytes, including image expansion. Python callers can supply
explicit smaller `PackageLimits`.

These are per-package limits, **not a total scratch, memory, CPU or hosted
storage quota**. A transfer archive and its extracted acquisition coexist.
TUM import can temporarily retain archive, extraction and canonical payloads.
The DataFlash decoder additionally limits input to 128 MiB and checks two
million records after decoding; its eager parser can consume substantially
more memory than the file size.

The [regression report](../results/sensor-import/README.md) records repeatability,
full room1 compatibility and the historical DataFlash limitation. Synthetic
tests independently exercise exact epoch arithmetic, known unit conversions,
all 65536 image intensities, optional truth, malformed payloads and bounded
failure cleanup. None of this establishes physical calibration accuracy.
