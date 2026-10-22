"""Protect private backups, publication integrity and service recovery."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from uuid import uuid4

import pytest


spec = importlib.util.spec_from_file_location(
    "workspace_backup",
    Path(__file__).parents[1] / "deploy/workspace/backup-workspace.py",
)
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


@pytest.fixture(autouse=True)
def private_umask():
    previous = os.umask(0o077)
    yield
    os.umask(previous)


def write(path, data):
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o600)
    return {"size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def bundle(tmp_path):
    root = tmp_path / "backup"
    root.mkdir(mode=0o700)
    job, attempt = str(uuid4()), str(uuid4())
    data = b"timestamp_ns,px,py,pz\n1,0,0,0\n"
    declared = {"schema_version": 1, "files": {
        "trajectory.csv": {"size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()},
    }}
    row = {"id": job, "published_attempt": attempt, "artifact_manifest": declared}
    files = {"database.dump": write(root / "database.dump", b"fake dump fixture")}
    prefix = f"artifacts/{job}/{attempt}"
    files[prefix + "/trajectory.csv"] = write(root / prefix / "trajectory.csv", data)
    files[prefix + "/publication-manifest.json"] = write(
        root / prefix / "publication-manifest.json", backup.canonical(declared),
    )
    manifest = {"format": backup.FORMAT, "schema_version": 1,
                "files": files, "database_state": {"retained_jobs": [row]}}
    write(root / "manifest.json", backup.canonical(manifest))
    return root, manifest


def runtime(identity="1" * 64):
    return {"reference": backup.DATABASE_IMAGE, "distribution_digest": backup.DATABASE_DIGEST,
            "image_id": "sha256:" + identity, "architecture": "amd64", "os": "linux"}


def bind_runtime(manifest, monkeypatch):
    value = runtime()
    manifest["database_image_id"] = value["image_id"]
    manifest["database_runtime"] = value
    monkeypatch.setattr(backup, "database_runtime", lambda: value)


def test_complete_backup_verifies_every_member_against_database_publication(tmp_path):
    root, manifest = bundle(tmp_path)
    assert backup.verify(root) == manifest


@pytest.mark.parametrize("name", ["../secret", "/etc/passwd", "a/../b", "a//b", "a\\b", "a/./b", "", None])
def test_backup_members_reject_ambiguous_or_traversing_paths(name):
    with pytest.raises(ValueError):
        backup.relative(name)


@pytest.mark.parametrize("change", ["corrupt", "extra", "missing", "mode", "link", "hardlink"])
def test_backup_verification_rejects_corruption_extras_links_and_nonprivate_data(tmp_path, change):
    root, manifest = bundle(tmp_path)
    target = root / next(k for k in manifest["files"] if k.endswith("trajectory.csv"))
    if change == "corrupt":
        target.write_bytes(b"corrupted")
    elif change == "extra":
        write(root / "unexpected", b"private")
    elif change == "missing":
        target.unlink()
    elif change == "mode":
        target.chmod(0o644)
    elif change == "link":
        target.unlink()
        target.symlink_to(root / "database.dump")
    else:
        os.link(target, tmp_path / "hardlink")
    with pytest.raises((ValueError, OSError)):
        backup.verify(root)


def test_backup_rehash_cannot_override_original_database_artifact_identity(tmp_path):
    root, manifest = bundle(tmp_path)
    name = next(k for k in manifest["files"] if k.endswith("trajectory.csv"))
    manifest["files"][name] = write(root / name, b"replacement")
    write(root / "manifest.json", backup.canonical(manifest))
    with pytest.raises(ValueError, match="publication manifest"):
        backup.verify(root)


def test_copy_rejects_parent_symlink_and_content_identity_mismatch(tmp_path):
    source = tmp_path / "source"
    source.mkdir(mode=0o700)
    write(source / "original", b"content")
    (source / "linked").symlink_to(source, target_is_directory=True)
    destination = tmp_path / "destination"
    destination.mkdir(mode=0o700)
    with pytest.raises(OSError):
        backup.copy_file(source, "linked/original", destination)
    with pytest.raises(ValueError, match="manifest"):
        backup.copy_file(source, "original", destination,
                         expected={"size_bytes": 7, "sha256": "0" * 64})


def test_restore_rejects_existing_name_before_creating_output(tmp_path, monkeypatch):
    root, manifest = bundle(tmp_path)
    bind_runtime(manifest, monkeypatch)
    write(root / "manifest.json", backup.canonical(manifest))
    monkeypatch.setattr(backup, "command", lambda *a, **k: SimpleNamespace(stdout=b"existing"))
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="already exists"):
        backup.restore(argparse.Namespace(backup=root, output=output, name="meridian-restore-test"))
    assert not output.exists()


def test_backup_failure_restarts_only_originally_running_services(tmp_path, monkeypatch):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    calls = []
    services = {}
    for key in ("api", "worker", "db"):
        services[key] = {
            "Id": key, "State": {"Running": key != "worker"}, "Image": runtime()["image_id"],
            "Config": {"Image": backup.DATABASE_IMAGE,
                       "Labels": {"com.docker.compose.project": "meridian-workspace-test",
                                  "com.docker.compose.service": key}},
            "Mounts": [{"Source": str(artifacts), "Type": "bind"}],
        }
    monkeypatch.setattr(backup, "inspect", lambda name: services[name.split("-")[-2]])
    monkeypatch.setattr(backup, "database_runtime", runtime)
    monkeypatch.setattr(backup, "require_idle", lambda _: None)
    monkeypatch.setattr(backup, "command", lambda args, **kw: calls.append(args))
    def failed_database(_):
        raise RuntimeError("simulated database failure")
    monkeypatch.setattr(backup, "database_state", failed_database)
    with pytest.raises(RuntimeError, match="simulated"):
        backup.backup(argparse.Namespace(project="meridian-workspace-test",
                                       artifacts=artifacts, output=tmp_path / "backup"))
    assert calls == [["docker", "stop", "--time", "45", "api"], ["docker", "start", "api"]]
    assert not (tmp_path / "backup/manifest.json").exists()


def test_busy_queue_is_refused_before_any_service_stops(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "sql", lambda *a, **kw: "1")
    with pytest.raises(ValueError, match="empty queue"):
        backup.require_idle("database")


def test_retained_inventory_rejects_duplicate_jobs_and_absolute_artifacts(tmp_path):
    _, manifest = bundle(tmp_path)
    row = manifest["database_state"]["retained_jobs"][0]
    with pytest.raises(ValueError, match="Duplicate"):
        backup.artifact_members([row, row])
    row["artifact_manifest"]["files"]["/private"] = row["artifact_manifest"]["files"].pop("trajectory.csv")
    with pytest.raises(ValueError):
        backup.artifact_members([row])


def test_failed_restore_creation_cannot_remove_a_concurrent_container(tmp_path, monkeypatch):
    root, manifest = bundle(tmp_path)
    bind_runtime(manifest, monkeypatch)
    write(root / "manifest.json", backup.canonical(manifest))
    removed = []
    def invoke(args, **kwargs):
        if args[:2] == ["docker", "ps"]:
            return SimpleNamespace(stdout=b"")
        raise subprocess.CalledProcessError(125, args, stderr=b"name already in use")
    monkeypatch.setattr(backup, "command", invoke)
    monkeypatch.setattr(backup, "inspect", lambda _: {
        "Id": "not-ours", "Config": {"Labels": {"meridian.restore-token": "another-invocation"}},
    })
    monkeypatch.setattr(backup.subprocess, "run", lambda args, **kwargs: removed.append(args))
    output = tmp_path / "restore"
    with pytest.raises(subprocess.CalledProcessError):
        backup.restore(argparse.Namespace(backup=root, output=output, name="meridian-restore-test"))
    assert removed == []
    assert not (output / "restore.env").exists()


@pytest.mark.parametrize("local_id", [
    "sha256:589f742565e4f6d22149176b032974f55f8201a8a37b71ed587ec5c6902e689f",
    backup.DATABASE_DIGEST,
])
def test_pinned_postgres_resolves_both_classic_and_containerd_image_ids(local_id, monkeypatch):
    calls = []
    def invoke(args):
        calls.append(args)
        return SimpleNamespace(stdout=json.dumps([{
            "Id": local_id, "RepoDigests": ["postgres@" + backup.DATABASE_DIGEST],
            "Architecture": "amd64", "Os": "linux",
        }]).encode())
    monkeypatch.setattr(backup, "command", invoke)
    observed = backup.database_runtime()
    assert observed["image_id"] == local_id
    assert calls == [["docker", "image", "inspect", backup.DATABASE_IMAGE]]


@pytest.mark.parametrize("change", ["digest", "platform"])
def test_pinned_postgres_refuses_other_distribution_or_platform(change, monkeypatch):
    value = {"Id": "sha256:" + "1" * 64,
             "RepoDigests": ["postgres@" + backup.DATABASE_DIGEST],
             "Architecture": "amd64", "Os": "linux"}
    if change == "digest":
        value["RepoDigests"] = ["postgres@sha256:" + "0" * 64]
    else:
        value["Architecture"] = "arm64"
    monkeypatch.setattr(backup, "command", lambda _: SimpleNamespace(stdout=json.dumps([value]).encode()))
    with pytest.raises(ValueError, match="pinned Linux amd64"):
        backup.database_runtime()


def test_cross_store_restore_keeps_distribution_platform_and_original_identity():
    original, resolved = runtime("1" * 64), runtime("2" * 64)
    manifest = {"database_runtime": original, "database_image_id": original["image_id"]}
    backup.compatible_database(manifest, resolved)
    assert manifest["database_image_id"] == "sha256:" + "1" * 64
    manifest["database_runtime"] = {**original, "distribution_digest": "sha256:" + "0" * 64}
    with pytest.raises(ValueError, match="different PostgreSQL"):
        backup.compatible_database(manifest, resolved)


def test_restore_cannot_mutate_its_source_backup_by_nesting_output(tmp_path):
    root, manifest = bundle(tmp_path)
    with pytest.raises(ValueError, match="separate from the verified backup"):
        backup.restore(argparse.Namespace(backup=root, output=root / "restored",
                                          name="meridian-restore-test"))
    assert backup.verify(root) == manifest


def acquisition_bundle(tmp_path):
    root, manifest = bundle(tmp_path)
    acquisition, attempt = str(uuid4()), str(uuid4())
    prefix = f"acquisitions/{acquisition}"
    original = write(root / prefix / "original", b"original archive fixture")
    payload = {"acquisition/acquisition.json": b'{"kind":"fixture"}\n',
               "prepared/sensors/imu/data.csv": b"sample_id,timestamp\n0,1234567890123456789\n"}
    published = f"{prefix}/published/{attempt}"
    files = {name: write(root / published / name, value) for name, value in payload.items()}
    publication = {"schema_version": 1, "files": files}
    row = {"id": acquisition, "published_attempt": attempt, "manifest": publication,
           **original}
    manifest["schema_version"] = 2
    manifest["database_state"]["retained_acquisitions"] = [row]
    manifest["files"][prefix + "/original"] = original
    manifest["files"].update({published + "/" + name: value for name, value in files.items()})
    manifest["files"][published + "/publication-manifest.json"] = write(
        root / published / "publication-manifest.json", backup.canonical(publication))
    write(root / "manifest.json", backup.canonical(manifest))
    return root, manifest


def test_v2_backup_binds_original_and_prepared_files_to_database(tmp_path):
    root, manifest = acquisition_bundle(tmp_path)
    assert backup.verify(root) == manifest
    assert backup.member_limit("acquisitions/any/original") == 4 * 1024**3
    assert backup.member_limit("artifacts/any/trajectory.csv") == 32 * 1024**2


def revision_bundle(tmp_path):
    root, manifest = acquisition_bundle(tmp_path)
    revision, attempt = str(uuid4()), str(uuid4())
    prefix = f"acquisitions/revisions/{revision}/published/{attempt}"
    publication = {"schema_version": 1, "files": {
        "sensors/imu/data.csv": write(root / prefix / "sensors/imu/data.csv", b"corrected vector\n"),
        "evidence/calibration.json": write(root / prefix / "evidence/calibration.json", b"supplied parameters\n"),
    }}
    manifest["schema_version"] = 3
    manifest["database_state"]["retained_input_revisions"] = [{
        "id": revision, "published_attempt": attempt, "manifest": publication,
    }]
    manifest["files"].update({prefix + "/" + key: value for key, value in publication["files"].items()})
    manifest["files"][prefix + "/publication-manifest.json"] = write(
        root / prefix / "publication-manifest.json", backup.canonical(publication))
    write(root / "manifest.json", backup.canonical(manifest))
    return root, manifest


def test_v3_backup_retains_original_and_independent_corrected_revision(tmp_path):
    root, manifest = revision_bundle(tmp_path)
    assert backup.verify(root) == manifest
    rows = manifest["database_state"]["retained_input_revisions"]
    with pytest.raises(ValueError, match="Duplicate"):
        backup.acquisition_members([], rows * 2)


@pytest.mark.parametrize("member", ["sensors/imu/data.csv", "evidence/calibration.json", "publication-manifest.json"])
def test_v3_rehashed_correction_cannot_replace_original_publication(tmp_path, member):
    root, manifest = revision_bundle(tmp_path)
    path = next(name for name in manifest["files"] if name.startswith("acquisitions/revisions/") and name.endswith(member))
    manifest["files"][path] = write(root / path, b"changed correction")
    write(root / "manifest.json", backup.canonical(manifest))
    with pytest.raises(ValueError, match="database publication"):
        backup.verify(root)


def test_legacy_backup_cannot_silently_claim_revision_coverage(tmp_path):
    root, manifest = revision_bundle(tmp_path)
    manifest["schema_version"] = 2
    write(root / "manifest.json", backup.canonical(manifest))
    with pytest.raises(ValueError, match="Legacy backups"):
        backup.verify(root)


def test_active_revision_refuses_backup(monkeypatch):
    answers = iter(["0", "t", "0", "t", "1"])
    monkeypatch.setattr(backup, "sql", lambda *args, **kwargs: next(answers))
    with pytest.raises(ValueError, match="input revisions"):
        backup.require_idle("database")


@pytest.mark.parametrize("suffix", ["/original", "/data.csv", "/publication-manifest.json"])
def test_v2_rehashed_input_cannot_replace_database_publication(tmp_path, suffix):
    root, manifest = acquisition_bundle(tmp_path)
    name = next(name for name in manifest["files"] if name.startswith("acquisitions/") and name.endswith(suffix))
    manifest["files"][name] = write(root / name, b"changed file with updated backup hash")
    write(root / "manifest.json", backup.canonical(manifest))
    with pytest.raises(ValueError, match="database publication"):
        backup.verify(root)


@pytest.mark.parametrize("change", ["missing", "extra", "symlink", "hardlink"])
def test_v2_inputs_reject_missing_extra_and_aliased_files(tmp_path, change):
    root, manifest = acquisition_bundle(tmp_path)
    name = next(name for name in manifest["files"] if name.endswith("/original"))
    target = root / name
    if change == "missing":
        target.unlink()
    elif change == "extra":
        write(target.parent / "unfinished-upload", b"not published")
    elif change == "symlink":
        target.unlink()
        target.symlink_to(root / "database.dump")
    else:
        os.link(target, tmp_path / "linked-original")
    with pytest.raises((ValueError, OSError)):
        backup.verify(root)


def test_v2_acquisition_manifest_cannot_traverse_or_duplicate(tmp_path):
    _, manifest = acquisition_bundle(tmp_path)
    row = manifest["database_state"]["retained_acquisitions"][0]
    with pytest.raises(ValueError, match="Duplicate"):
        backup.acquisition_members([row, row])
    row["manifest"]["files"]["../../original"] = row["manifest"]["files"].pop("acquisition/acquisition.json")
    with pytest.raises(ValueError, match="Unsafe"):
        backup.acquisition_members([row])


def test_v2_restore_copies_both_original_and_preparation_before_database_creation(tmp_path, monkeypatch):
    root, manifest = acquisition_bundle(tmp_path)
    bind_runtime(manifest, monkeypatch)
    write(root / "manifest.json", backup.canonical(manifest))
    def invoke(args, **kwargs):
        if args[:2] == ["docker", "ps"]:
            return SimpleNamespace(stdout=b"")
        raise subprocess.CalledProcessError(125, args)
    monkeypatch.setattr(backup, "command", invoke)
    monkeypatch.setattr(backup, "inspect", lambda _: {"Config": {"Labels": {}}})
    output = tmp_path / "restore"
    with pytest.raises(subprocess.CalledProcessError):
        backup.restore(argparse.Namespace(backup=root, output=output, name="meridian-restore-inputs"))
    for name, expected in manifest["files"].items():
        if name.startswith("acquisitions/"):
            assert backup.identity(output, name, backup.member_limit(name)) == expected


def test_active_upload_refuses_backup_before_stopping_services(monkeypatch):
    answers = iter(["0", "t", "1"])
    monkeypatch.setattr(backup, "sql", lambda *args, **kwargs: next(answers))
    with pytest.raises(ValueError, match="completed or cancelled transfers"):
        backup.require_idle("database")


@pytest.mark.parametrize("version", [1, 2])
def test_database_fingerprints_hash_rows_before_aggregation_and_preserve_v1(version, monkeypatch):
    queries = []
    def execute(_container, query, **_kwargs):
        queries.append(query)
        if "to_regclass" in query:
            return "f"
        if "count(*)" in query:
            return "0"
        if "jsonb_agg(to_jsonb(t)" in query:
            return "[]"
        if "encode(sha256(convert_to(to_jsonb(t)::text" in query:
            return "0" * 64 + "\n" + "1" * 64
        return "[]"
    monkeypatch.setattr(backup, "sql", execute)
    result = backup.database_state("db", fingerprint_version=version)
    if version == 2:
        assert result["fingerprint_version"] == 2
        assert not any("jsonb_agg(to_jsonb(t)" in query for query in queries)
        expected = hashlib.sha256(("0" * 64 + "\n" + "1" * 64).encode()).hexdigest()
    else:
        assert "fingerprint_version" not in result
        assert any("jsonb_agg(to_jsonb(t)" in query for query in queries)
        expected = hashlib.sha256(b"[]").hexdigest()
    assert set(result["table_content_sha256"].values()) == {expected}


def test_v2_outer_manifest_preserves_long_unicode_without_ascii_expansion(tmp_path):
    root, manifest = acquisition_bundle(tmp_path)
    row = manifest["database_state"]["retained_acquisitions"][0]
    name = "acquisition/originals/" + "\U0001f680" * 55
    identity = {"size_bytes": 0, "sha256": hashlib.sha256(b"").hexdigest()}
    value = {"manifest": {"files": {name: identity}}}
    encoded = backup.backup_manifest_bytes(value)
    assert json.loads(encoded) == value
    assert "\U0001f680".encode() in encoded
    assert len(encoded) < len(backup.canonical(value))
    assert backup.canonical(row["manifest"]) == (root / next(
        member for member in manifest["files"] if member.startswith("acquisitions/") and member.endswith("/publication-manifest.json"))).read_bytes()


@pytest.mark.parametrize("capacity", [0, 128, 8193, True])
def test_restore_rejects_unbounded_database_capacity_before_creating_output(tmp_path, capacity):
    root, _ = bundle(tmp_path)
    output = tmp_path / "restore"
    with pytest.raises(ValueError, match="between 256 and 8192"):
        backup.restore(argparse.Namespace(backup=root, output=output, name="meridian-restore-limits", database_mib=capacity))
    assert not output.exists()


def test_restore_requires_capacity_for_measured_database_before_creating_output(tmp_path):
    root, manifest = acquisition_bundle(tmp_path)
    manifest["database_size_bytes"] = 200 * 1024**2
    write(root / "manifest.json", backup.backup_manifest_bytes(manifest))
    output = tmp_path / "restore"
    with pytest.raises(ValueError, match="--database-mib at least 464"):
        backup.restore(argparse.Namespace(backup=root, output=output, name="meridian-restore-limits"))
    assert not output.exists()


def test_restore_applies_selected_database_and_container_memory_limits(tmp_path, monkeypatch):
    root, manifest = acquisition_bundle(tmp_path)
    manifest["database_size_bytes"] = 200 * 1024**2
    bind_runtime(manifest, monkeypatch)
    write(root / "manifest.json", backup.backup_manifest_bytes(manifest))
    calls = []
    def invoke(args, **kwargs):
        calls.append(args)
        if args[:2] == ["docker", "ps"]:
            return SimpleNamespace(stdout=b"")
        raise subprocess.CalledProcessError(125, args)
    monkeypatch.setattr(backup, "command", invoke)
    monkeypatch.setattr(backup, "inspect", lambda _: {"Config": {"Labels": {}}})
    with pytest.raises(subprocess.CalledProcessError):
        backup.restore(argparse.Namespace(backup=root, output=tmp_path / "restore", name="meridian-restore-limits", database_mib=512))
    launch = next(command for command in calls if command[:2] == ["docker", "run"])
    assert launch[launch.index("--memory") + 1] == "768m"
    assert launch[launch.index("--memory-swap") + 1] == "768m"
    assert "/var/lib/postgresql/data:rw,uid=999,gid=999,mode=0700,size=512m" in launch


def test_v2_backup_failure_restarts_importer_and_only_originally_running_writers(tmp_path, monkeypatch):
    artifacts, acquisitions = tmp_path / "artifacts", tmp_path / "acquisitions"
    artifacts.mkdir()
    acquisitions.mkdir()
    services, calls = {}, []
    for key in ("api", "worker", "importer", "db"):
        services[key] = {
            "Id": key, "State": {"Running": key != "worker"}, "Image": runtime()["image_id"],
            "Config": {"Image": backup.DATABASE_IMAGE,
                       "Labels": {"com.docker.compose.project": "meridian-workspace-test", "com.docker.compose.service": key}},
            "Mounts": [{"Source": str(path), "Type": "bind"} for path in (artifacts, acquisitions)],
        }
    monkeypatch.setattr(backup, "inspect", lambda name: services[name.split("-")[-2]])
    monkeypatch.setattr(backup, "database_runtime", runtime)
    monkeypatch.setattr(backup, "require_idle", lambda _: None)
    monkeypatch.setattr(backup, "command", lambda args, **kwargs: calls.append(args))
    def failed_database(_):
        raise RuntimeError("simulated snapshot failure")
    monkeypatch.setattr(backup, "database_state", failed_database)
    with pytest.raises(RuntimeError, match="simulated snapshot"):
        backup.backup(argparse.Namespace(project="meridian-workspace-test", artifacts=artifacts,
                                       acquisitions=acquisitions, output=tmp_path / "backup"))
    assert calls == [["docker", "stop", "--time", "45", "api"], ["docker", "stop", "--time", "45", "importer"],
                     ["docker", "start", "api"], ["docker", "start", "importer"]]
