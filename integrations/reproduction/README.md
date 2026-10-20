# Independent VIO reproduction

The downloaded ZIP contains result evidence and source. It contains no native
binary or dataset samples. Its manifest identifies every file by SHA-256 and
size. Keep the separately supplied download SHA-256: a manifest is not a
signature and cannot authenticate an archive replaced together with its files.
Inspect downloaded source before executing it.

Supported environment: Linux x86-64, Python 3.12, a Docker engine, and enough disk
for the 1.71 GB dataset archive, 1.70 GB prepared input, native image and build
cache. Runtime caps remain 2 CPUs, 4 GiB, 128 processes and 900 seconds. Building
the native image has separate Docker build resource requirements.

From a new directory containing the extracted archive:

```sh
python3.12 reproduce.py verify --bundle .
python3.12 -m venv ../reproduction-venv
../reproduction-venv/bin/python -m pip install -r source/integrations/reproduction/requirements.lock
docker build -t meridian-reproduction:export source/integrations/openvins
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=source/src ../reproduction-venv/bin/python \
  -m meridian.vio_reference download --output ../dataset-room1_512_16.tar
../reproduction-venv/bin/python reproduce.py reproduce \
  --bundle . --work ../reproduced-experiment \
  --archive ../dataset-room1_512_16.tar \
  --image meridian-reproduction:export --runtime-mode rebuilt
```

The dataset command acquires only the public calibrated TUM-VI room1 archive,
then verifies SHA-256, MD5 and size. An already acquired archive may replace the
download. `--prepared` may replace `--archive` only for an exactly verified
prepared recording; the original manifest hash must match.

The build recipe pins the Ubuntu base digest, direct package versions, OpenVINS
commit and adapter sources. The runtime retains the needed upstream estimator
sources and notices. It excludes benchmark reference data. Third-party source is
acquired from the pinned public Git revision during building. No private registry,
Meridian account or API request is needed. Package repositories and source hosts
are external dependencies; unavailable pins or changed installed dependencies
fail explicitly. Direct package pins do not make an indefinitely reproducible
binary build. The recipe also pins `git-man` to the recorded version, preventing
its independently updated package from changing the dependency inventory.

`rebuilt` explicitly uses a new runtime image identity. Adapter/version, build
source and installed dependency identities must match; binary/image hashes are
recorded separately from the original runtime. This mode never overwrites or
relabels the exported immutable specification. If the original qualified image
is already independently available, choose `--runtime-mode exact` with its
content ID; an arbitrary replacement cannot satisfy that mode.

The command leaves `reproduction-report.json` and actual run evidence in the new
work directory. Exit 0 means the stated reproduction checks passed; exit 1 means
completed evidence failed those checks; exit 2 means invalid inputs or execution
failure. Output directories must be new. There is no tolerance on host timings.

Required agreement: identical output timestamps, evaluated support, coverage
counts and missing regions; at most 1 mm direct position difference, 0.001 degree
direct orientation difference, 1 mm ATE RMSE difference and 0.001 degree
orientation RMSE difference. Direct poses are compared without aligning them
again. Evaluation retains the original full-recording, unit-scale SE(3) protocol.
These are engineering reproduction tolerances, not flight accuracy requirements.

The adapter, its build integration and copied OpenVINS calibration/masks carry
GPL-3.0-or-later; see `source/integrations/openvins/LICENSE` and `NOTICE.md`.
The source export does not redistribute Ubuntu/OpenVINS binaries. If you
subsequently redistribute a built image, preserve its applicable notices and
corresponding source obligations.

TUM VI Benchmark: David Schubert, Thore Goll, Nikolaus Demmel, Vladyslav Usenko,
Jörg Stückler and Daniel Cremers (2018), data under CC BY 4.0.
[Dataset and original attribution](https://cvg.cit.tum.de/data/datasets/visual-inertial-dataset).
Normalization reorders reference quaternions and separates evaluation reference
poses from estimator sensor inputs; it does not retime or rescale the recording.
