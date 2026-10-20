# Synthetic sensor-contract examples

These tiny, deterministic fixtures exercise the
[version 1 contract](../../docs/sensor-contract.md). They are not physical
recordings, calibration results or meaningful VIO input. The two-pair 8 × 6
images have no claim of visual observability.

| Descriptor | Purpose |
| --- | --- |
| `stereo-no-reference.json` | Stereo plus instantaneous IMU; independent reference absent. |
| `stereo-partial-reference.json` | Same synthetic sensors with one independent synthetic pose; partial coverage. |
| `imu-snapshots.json` | Processed logger snapshots in microseconds, with an explicit backward clock reset between two segments. Not an actual DataFlash file. |
| `unknown-metadata.json` | Unknown IMU axes/timestamp/calibration semantics remain representable. |
| `replay-acquisition.json` | Separate acquisition identity using the same declared rig, to exercise calibration transfer. |
| `calibration.json` | Analytic pinhole/stereo geometry; supplied, without a physical validation report. |
| `clock-mapping.json` | Exact +2 ns source-to-target mapping with explicit origins and bounds. |
| `prepared-input.json` | Immutable source/calibration/mapping chain and changed IMU timestamps; source bytes remain intact. |

Example descriptors inventory the included CSV/image payloads. The descriptor
validator does not inspect those payloads; the fixture tests independently check
their byte identities and declared row boundaries. Multiple acquisition variants
share synthetic observations and are not independent evaluation sessions.
The zero reference uncertainty belongs only to this analytic synthetic example.

```sh
python -m meridian.sensor_contract examples/sensor-contracts/stereo-no-reference.json
python examples/sensor-contracts/generate.py /tmp/meridian-contract-examples-new
```

The output directory must not exist. Generation uses only the Python standard
library and does not access the network. JSON/CSV files are deterministic;
PNG uses an explicit uncompressed DEFLATE block rather than a version-dependent
compression decision. The public metadata records fixture licensing as
`unspecified`; this example introduces no repository-wide license grant.
