# Independent experiment exports

Export schema version 1 packages one successful, retained VIO execution. Its
configuration, estimates and evaluation evidence remain useful without a
Meridian account. A saved comparison is a distinct object; this format exports
one exact execution, not a benchmark campaign or averaged comparison.

## Hosted access and retention

`GET /api/v1/projects/{project}/jobs/{job}/export?schema_version=1` requires the
owning authenticated session and matching project/job relationship. Only
successful executions whose complete artifacts remain retained can be exported.
Unknown formats return 400, expired artifacts 410, deleted/unowned records 404,
and invalid or unavailable evidence 503. Metadata availability is only a hint;
the download checks access, retention and integrity again.

Responses provide a ZIP attachment with content length and SHA-256, `no-store`
and `Vary: Cookie`. The service creates no persistent export cache. It reads a
bounded allowlist of verified artifacts and curated source, and rechecks source
job deletion/expiry before sending. Deletion revokes future hosted downloads;
bytes already received or in flight cannot be recalled from a recipient.

## Archive contract

`export-manifest.json` declares format `meridian-vio-experiment`, integer schema
version 1, execution IDs/timestamps, the original immutable specification hash,
reproduction tolerances and a complete SHA-256/byte-size file inventory.

`artifacts/` contains the exact original experiment specification, execution
manifest, result, native status, trajectory, evaluation, binary/dependency
provenance and five effective configuration files. The original result manifest
also retains hashes of omitted artifacts; those omissions are intentional, not
missing exported files. `source/` includes the bounded standalone Python
dependency closure, numerical evaluator, OpenVINS adapter/build recipe, baseline
configuration, full GPL text/notices, protocol and reproduction instructions.
`reproduce.py` is the standalone entry point.

Logs, account fields, project/experiment names, raw camera images, IMU samples,
investigation payloads and host workspace paths are excluded. Input identities,
the public dataset URL, archive hash and preparation rules are retained in the
original execution manifest and source recipe. Dataset samples and compiled
native binaries are acquired/built separately. Upstream OpenVINS source is
fetched at its exact public commit by the supplied build recipe.

The verifier rejects unsupported versions, duplicate JSON keys, nonfinite
values, unlisted/missing files, mismatched hashes, inconsistent specification or
result binding, links, unsafe paths and bounded-size violations. ZIPs have
deterministic metadata. Limits are 16 MiB compressed/uncompressed in total,
8 MiB per file and 64 members. Archive inspection does not execute build recipes
or estimator code.
An archive's own verifier and hashes are not a signature: preserve the external
download digest and inspect source before running it.

## Independent execution and numerical acceptance

Follow the [packaged reproduction recipe](../integrations/reproduction/README.md).
Linux x86-64, Python 3.12/NumPy 2.5.3 and Docker are the supported environment.
No Django, database, worker, browser session or private endpoint is required.
The immutable input manifest is verified before and after replay. The estimator
receives only camera/IMU inputs, effective configuration and output; independent
reference poses are read by the evaluator after native execution.

The original qualified image is identified by content, but is not distributed
in this source archive. `exact` mode requires that image independently available.
The source build route uses explicit `rebuilt` mode: upstream/adapter version,
adapter/build sources and installed dependency manifest must match; its actual
image and binary hashes are retained as a new runtime. The original experiment
specification remains unchanged and is linked as source provenance rather than
misrepresented as the new runtime's specification. A failed build, unavailable
package pin or mismatched dependency inventory is reported, never silently
replaced by an unqualified image.

Acceptance requires identical estimated timestamps, evaluated timestamps,
coverage/support counts and missing intervals. Maximum direct position
difference is 0.001 m and direct orientation difference is 0.001 degrees, without
refitting either trajectory. Position ATE RMSE may differ by at most 0.001 m and
orientation RMSE by 0.001 degrees. Both evaluations use the unchanged global
SE(3) alignment, scale fixed to one, full recording and original reference
interpolation limits. Resource times and RSS are recorded observations with no
reproduction performance guarantee.

The report separately records byte identity, actual numerical differences,
support checks, runtime provenance and the original integration-gate outcome.
Reproducing an output does not establish estimator accuracy, covariance
consistency, flight validity or cross-platform bit identity. TUM-VI room2 remains
reserved for the later complete acceptance increment.
