# Versioned VIO experiments

An experiment belongs to a private project and records a supported OpenVINS
configuration for its selected TUM-VI dataset. Increment 1.3 adds creation,
editing, duplication, history and configuration differences. Hosted execution
is implemented by the [durable job service](hosted-vio-jobs.md); saving an
experiment does not queue or run it. [Private-input VIO](private-vio.md) adds a
separate profile selecting an exact owned input revision and camera interval.
The room1 profile and its historical specifications remain unchanged.

## Supported configuration

The initial profile is `openvins-tumvi-room1-v1`, for
`tumvi-room1-512-16-v1` only. It uses the existing
[qualified reference](vio-reference.md) and the whole recording. Cropping,
alternative calibration, other datasets, uploaded code and runtime selection
are not supported by this profile.

| Parameter | Default | Allowed values | Meaning |
| --- | ---: | --- | --- |
| `num_pts` | 200 | Integers 100, 200, 400 | Total target feature budget divided between the two cameras after initialization; 200 targets 100 per camera. |

Changing this budget can trade computation against visual support. Initialization
has its own fixed settings. These choices are supported configuration values,
not a claim that every variant meets a navigation requirement. Only the original
200-point baseline has the repeated qualification evidence from increment 1.1.

The server supplies the profile's labels, units, choices and defaults to the UI.
It independently validates submitted values: booleans, floats, numeric strings,
unsupported integers and unknown parameter names are rejected. The project
dataset must match the pinned profile. User input cannot supply commands, paths,
fetch URLs, images or arbitrary estimator configuration.

## Identity and revision history

An experiment has an opaque UUID and a sequence of numbered revisions starting
at one. Each revision stores its name, description, creation time, normalized
specification and SHA-256 identity. The specification pins dataset and calibration
identities, the full recording interval, native estimator/adapter/runtime,
configuration, scheduling and output conventions, resource limits, evaluation
protocol and reproducibility assumptions.

The numerical specification is separate from the experiment's title and notes:
renaming an experiment creates a history entry but does not change the numerical
specification hash. The hash uses canonical JSON with sorted keys and fixed
separators. It identifies content and grants no access permission.

Editing appends a new revision. Older revisions remain readable and cannot be
edited through the API. Future jobs must reference a specific immutable revision;
the current experiment summary is not an execution input. Unchanged saves return
the existing version without consuming another revision.

An edit supplies `base_version`, the version the editor opened. The server checks
it under a database lock and returns `409 version_conflict` if another edit has
already advanced the experiment. The browser keeps the unsaved draft for review.
It does not silently replace a newer version or discard the draft.

Duplication copies a selected historical specification into version one of a new
experiment in the same project, with a new name and a server-recorded source
revision identity. It does not rebuild an old specification with current defaults.
Deleting a source experiment does not change the copied specification. A revision
comparison reports exact before/after values for the name, description and
supported parameters; it is not a comparison of measured VIO results.

## Access and lifecycle

All endpoints require the existing session authentication; mutations also require
CSRF protection. Every lookup traverses the owning project. Substituting another
project, experiment or revision identifier returns 404. UUIDs and content hashes
are not authorization mechanisms.

Each project permits 20 experiments, each with up to 50 revisions. Transactions
serialize mutations against project/experiment rows so simultaneous requests
cannot overrun the quotas or create two versions with the same number. Deleting
an experiment removes its revisions; deleting a project removes its experiments.
Submitting a [VIO job](hosted-vio-jobs.md) copies the selected immutable revision.
Active jobs block deletion of their parent experiment or project with 409.
Terminal metadata cascades with its parent; the worker subsequently removes
orphaned files. Account and backup limitations follow the
[workspace contract](hosted-workspace.md).

## API

All responses retain the workspace's `schema_version: 1` envelope and structured
errors. Paths below are relative to `/api/v1`.

| Endpoint | Behavior |
| --- | --- |
| `GET /configuration-profile` | Read the supported configuration and parameter metadata. |
| `GET /projects/{project}/experiments` | List current experiment summaries and limits. |
| `POST /projects/{project}/experiments` | Validate structured input and create version one. |
| `GET /projects/{project}/experiments/{experiment}` | Read the current summary and latest revision. |
| `PATCH /projects/{project}/experiments/{experiment}` | Append a revision using `base_version`. |
| `DELETE /projects/{project}/experiments/{experiment}` | Delete the experiment and its history. |
| `GET /projects/{project}/experiments/{experiment}/revisions` | List revision summaries. |
| `GET /projects/{project}/experiments/{experiment}/revisions/{version}` | Read one unchanged specification. |
| `POST /projects/{project}/experiments/{experiment}/duplicate` | Copy `source_version` under a new `name`. |
| `GET /projects/{project}/experiments/{experiment}/compare?from=1&to=2` | Report exact revision differences. |

Creation takes `schema_version`, `name`, optional `description`, `dataset_id` and
`parameters`. Editing takes `schema_version`, `base_version`, `name`, optional
`description` and `parameters`; dataset identity is fixed. Duplication takes
`schema_version`, `source_version` and `name`. Unsupported fields are rejected.

## Use the same specification locally

The API and local reference runner share the standard-library-only
[`vio_specification.py`](../src/meridian/vio_specification.py) contract. The API
image includes the standard-library contracts and access/inspection helpers;
it does not install numerical dependencies or receive a VIO runtime or Docker
access. The acquisition overlay mounts retained private inputs for owned reads
and bounded compatibility checks. The local runner below supports the original
room1 specification; private-input jobs use their separate worker handoff.

The local runner accepts the stored revision's `specification` object unchanged
through `--specification`. It checks the pinned prepared inputs, runtime and
evaluation semantics, then renders only the allowlisted parameter into a
separate configuration directory. Original prepared inputs remain unchanged.
The result retains the supplied specification, its hash, the effective
configuration files and the ordinary execution provenance. Use the
[reference guide](vio-reference.md) for preparation and execution commands.

Acceptance evidence belongs in a separate validation report. A valid stored
configuration is not proof of successful initialization, accuracy, hosted
availability, embedded performance or flight behavior.
