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
