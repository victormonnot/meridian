"""Private, versioned configuration metadata shared with queued executions."""
import json
import re

from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.db import transaction

from meridian.vio_specification import build_specification, configuration_profile, specification_sha256

from .api import endpoint, error, not_found, response
from .models import Experiment, ExperimentRevision, Project


def limits():
    return {"experiments": getattr(settings, "MERIDIAN_EXPERIMENT_LIMIT", 20),
            "revisions": getattr(settings, "MERIDIAN_REVISION_LIMIT", 50)}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError("Non-finite JSON number")


def payload(request, allowed, required):
    if request.content_type != "application/json":
        return None, error("invalid_request", "Send an application/json object.", 400)
    try:
        data = json.loads(request.body, object_pairs_hook=unique_object, parse_constant=reject_constant)
    except (ValueError, UnicodeDecodeError, RequestDataTooBig):
        return None, error("invalid_request", "The request body is invalid or too large.", 400)
    if not isinstance(data, dict) or set(data) - allowed or required - set(data):
        return None, error("invalid_request", "The request contains missing or unsupported fields.", 400)
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        return None, error("invalid_request", "Unsupported request schema version.", 400)
    for key in ("name", "description", "dataset_id"):
        if key not in data:
            continue
        value = data[key]
        if not isinstance(value, str) or "\x00" in value:
            return None, error("invalid_request", "Text fields must contain valid Unicode text.", 400)
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return None, error("invalid_request", "Text fields must contain valid Unicode text.", 400)
    for key in ("base_version", "source_version"):
        if key in data and (type(data[key]) is not int or not 1 <= data[key] <= limits()["revisions"]):
            return None, error("invalid_request", "Revision numbers must be positive integers within the revision limit.", 400)
    if "name" in data:
        data["name"] = data["name"].strip()
        if not 1 <= len(data["name"]) <= 100:
            return None, error("validation_error", "Check the experiment details.", 400,
                               {"name": ["Use between 1 and 100 characters."]})
    if len(data.get("description", "")) > 2000:
        return None, error("validation_error", "Check the experiment details.", 400,
                           {"description": ["Use at most 2000 characters."]})
    return data, None


def specification(dataset_id, parameters, schema_version):
    try:
        value = build_specification(dataset_id, parameters, schema_version=schema_version)
        digest = specification_sha256(value)
    except ValueError as exc:
        return None, None, error("validation_error", str(exc), 400)
    return value, digest, None


def experiment_payload(value):
    return {"id": str(value.pk), "project_id": str(value.project_id), "name": value.name,
            "description": value.description, "version": value.current_version,
            "parameters": value.parameters, "specification_sha256": value.specification_sha256,
            "created_at": value.created_at.isoformat(), "updated_at": value.updated_at.isoformat()}


def revision_payload(value, summary=False):
    result = {"version": value.version, "name": value.name, "description": value.description,
              "specification_sha256": value.specification_sha256, "created_at": value.created_at.isoformat(),
              "copied_from": value.copied_from}
    result["parameters" if summary else "specification"] = value.specification["parameters"] if summary else value.specification
    return result


def detail(value, revision=None, **extra):
    revision = revision or value.revisions.get(version=value.current_version)
    return {"experiment": experiment_payload(value), "revision": revision_payload(revision), **extra}


def owned_project(request, project_id, lock=False):
    query = Project.objects.filter(pk=project_id, owner=request.user)
    return (query.select_for_update() if lock else query).first()


def owned_experiment(project, experiment_id, lock=False):
    query = Experiment.objects.filter(pk=experiment_id, project=project)
    return (query.select_for_update() if lock else query).first()


def create_snapshot(project, name, description, spec, digest, copied_from=None):
    value = Experiment.objects.create(project=project, name=name, description=description,
                                      parameters=spec["parameters"], specification_sha256=digest)
    revision = ExperimentRevision.objects.create(experiment=value, version=1, name=name,
                                                description=description, specification=spec,
                                                specification_sha256=digest, copied_from=copied_from)
    return value, revision


@endpoint(["GET"])
def profile(request):
    return response({"profile": configuration_profile()})


@endpoint(["GET", "POST"])
def experiments(request, project_id):
    with transaction.atomic():
        project = owned_project(request, project_id, lock=request.method == "POST")
        if project is None:
            return not_found(request)
        if request.method == "GET":
            return response({"experiments": [experiment_payload(item) for item in project.experiments.all()],
                             "limits": limits()})
        fields = {"schema_version", "name", "description", "dataset_id", "parameters"}
        data, failure = payload(request, fields, fields - {"description"})
        if failure is not None:
            return failure
        if data["dataset_id"] != project.dataset_id:
            return error("validation_error", "The experiment dataset must match its project.", 400)
        spec, digest, failure = specification(project.dataset_id, data["parameters"], data["schema_version"])
        if failure is not None:
            return failure
        if project.experiments.count() >= limits()["experiments"]:
            return error("experiment_limit", "This project has reached its experiment limit.", 409)
        value, revision = create_snapshot(project, data["name"], data.get("description", ""), spec, digest)
        return response(detail(value, revision), 201)


@endpoint(["GET", "PATCH", "DELETE"])
def experiment(request, project_id, experiment_id):
    with transaction.atomic():
        lock = request.method != "GET"
        project = owned_project(request, project_id, lock=lock)
        value = owned_experiment(project, experiment_id, lock=lock) if project else None
        if value is None:
            return not_found(request)
        if request.method == "GET":
            snapshot = value.revisions.filter(version=value.current_version).first()
            return response(detail(value, snapshot)) if snapshot else not_found(request)
        if request.method == "DELETE":
            if value.jobs.filter(state__in=["queued", "running", "evaluating"]).exists():
                return error("active_jobs", "Cancel active executions and wait for them to stop before deleting this experiment.", 409)
            value.delete()
            return response({"deleted": True})
        fields = {"schema_version", "base_version", "name", "description", "parameters"}
        data, failure = payload(request, fields, fields - {"description"})
        if failure is not None:
            return failure
        if data["base_version"] != value.current_version:
            return error("version_conflict", "This experiment changed. Review its latest revision before saving.", 409)
        spec, digest, failure = specification(project.dataset_id, data["parameters"], data["schema_version"])
        if failure is not None:
            return failure
        description = data.get("description", "")
        if (data["name"], description, digest) == (value.name, value.description, value.specification_sha256):
            return response(detail(value, unchanged=True))
        if value.current_version >= limits()["revisions"]:
            return error("revision_limit", "This experiment has reached its revision limit.", 409)
        value.name, value.description = data["name"], description
        value.current_version += 1
        value.parameters, value.specification_sha256 = spec["parameters"], digest
        revision = ExperimentRevision.objects.create(experiment=value, version=value.current_version,
                                                    name=value.name, description=value.description,
                                                    specification=spec, specification_sha256=digest)
        value.save(update_fields=["name", "description", "current_version", "parameters", "specification_sha256", "updated_at"])
        return response(detail(value, revision))


@endpoint(["GET"])
def revisions(request, project_id, experiment_id):
    project = owned_project(request, project_id)
    value = owned_experiment(project, experiment_id) if project else None
    if value is None:
        return not_found(request)
    return response({"revisions": [revision_payload(item, summary=True) for item in value.revisions.all()]})


@endpoint(["GET"])
def revision(request, project_id, experiment_id, version):
    project = owned_project(request, project_id)
    value = owned_experiment(project, experiment_id) if project else None
    snapshot = value.revisions.filter(version=version).first() if value else None
    return response({"revision": revision_payload(snapshot)}) if snapshot else not_found(request)


@endpoint(["POST"])
def duplicate(request, project_id, experiment_id):
    with transaction.atomic():
        project = owned_project(request, project_id, lock=True)
        source = owned_experiment(project, experiment_id, lock=True) if project else None
        if source is None:
            return not_found(request)
        fields = {"schema_version", "source_version", "name"}
        data, failure = payload(request, fields, fields)
        if failure is not None:
            return failure
        snapshot = source.revisions.filter(version=data["source_version"]).first()
        if snapshot is None:
            return not_found(request)
        if project.experiments.count() >= limits()["experiments"]:
            return error("experiment_limit", "This project has reached its experiment limit.", 409)
        copied_from = {"experiment_id": str(source.pk), "version": snapshot.version,
                       "specification_sha256": snapshot.specification_sha256}
        value, new_revision = create_snapshot(project, data["name"], snapshot.description, snapshot.specification,
                                             snapshot.specification_sha256, copied_from)
        return response(detail(value, new_revision), 201)


@endpoint(["GET"])
def compare(request, project_id, experiment_id):
    project = owned_project(request, project_id)
    value = owned_experiment(project, experiment_id) if project else None
    if value is None:
        return not_found(request)
    if set(request.GET) != {"from", "to"}:
        return error("invalid_request", "Select exactly two revision numbers: from and to.", 400)
    versions = []
    for key in ("from", "to"):
        entries = request.GET.getlist(key)
        if len(entries) != 1 or re.fullmatch(r"[1-9][0-9]{0,2}", entries[0]) is None:
            return error("invalid_request", "Revision numbers must be positive decimal integers.", 400)
        versions.append(int(entries[0]))
    snapshots = {item.version: item for item in value.revisions.filter(version__in=versions)}
    if any(version not in snapshots for version in versions):
        return not_found(request)
    before, after = [snapshots[version] for version in versions]
    changes = []
    for field in ("name", "description"):
        left, right = getattr(before, field), getattr(after, field)
        if left != right:
            changes.append({"field": field, "before": left, "after": right})
    for key in sorted(before.specification["parameters"]):
        left, right = before.specification["parameters"][key], after.specification["parameters"][key]
        if left != right:
            changes.append({"field": "parameters." + key, "before": left, "after": right})
    return response({"from_version": versions[0], "to_version": versions[1], "changes": changes})
