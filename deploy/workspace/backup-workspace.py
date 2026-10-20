#!/usr/bin/env python3
"""Create, verify and rehearse a private Meridian database/artifact backup.

This operator tool requires local Docker access. It never overwrites a database
or an output directory. A backup briefly stops the exact API and worker after
refusing a busy queue; a restore creates a new network-isolated PostgreSQL
container. Backup contents include password hashes and session records.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import resource
import secrets
import stat
import subprocess
import time
from uuid import UUID


FORMAT = "meridian-private-backup"
MAX_FILE = 32 * 1024 * 1024
MAX_DATABASE = 512 * 1024 * 1024
MAX_TOTAL = 7 * 1024 * 1024 * 1024
MAX_FILES = 16384
MAX_ACQUISITION_FILE = 4 * 1024**3
MAX_ACQUISITION_FILES = 240000
MAX_COMPLETE_TOTAL = 32 * 1024**3
MAX_MANIFEST = 256 * 1024**2
DATABASE_DIGEST = "sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826"
DATABASE_IMAGE = "postgres:17.11-bookworm@" + DATABASE_DIGEST
TABLES = (
    "auth_user", "django_session", "workspace_project", "workspace_experiment",
    "workspace_experimentrevision", "workspace_job", "workspace_jobattempt",
    "workspace_runcomparison", "workspace_authwindow", "workspace_computequeue",
)
ACQUISITION_TABLES = ("workspace_acquisition", "workspace_uploadchunk")


def command(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, **kwargs)


def inspect(name):
    return json.loads(command(["docker", "inspect", name]).stdout)[0]


def database_runtime():
    """Bind the pinned distribution digest to this Docker store's local ID."""
    image = json.loads(command(["docker", "image", "inspect", DATABASE_IMAGE]).stdout)[0]
    allowed = {"postgres@" + DATABASE_DIGEST, "docker.io/library/postgres@" + DATABASE_DIGEST}
    if (not allowed.intersection(image.get("RepoDigests") or [])
            or image.get("Architecture") != "amd64" or image.get("Os") != "linux"
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", image.get("Id", ""))):
        raise ValueError("PostgreSQL image does not match the pinned Linux amd64 distribution.")
    return {"reference": DATABASE_IMAGE, "distribution_digest": DATABASE_DIGEST,
            "image_id": image["Id"], "architecture": "amd64", "os": "linux"}


def compatible_database(manifest, actual):
    original = manifest.get("database_runtime") or {}
    if (original.get("reference") != DATABASE_IMAGE
            or original.get("distribution_digest") != DATABASE_DIGEST
            or original.get("architecture") != "amd64" or original.get("os") != "linux"
            or original.get("image_id") != manifest.get("database_image_id")
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", original.get("image_id", ""))
            or any(actual[key] != original[key]
                   for key in ("reference", "distribution_digest", "architecture", "os"))):
        raise ValueError("Backup requires a different PostgreSQL image; review compatibility.")


def sql(container, query, *, user="meridian_admin", database="meridian"):
    return command(
        ["docker", "exec", container, "psql", "-X", "-v", "ON_ERROR_STOP=1",
         "-U", user, "-d", database, "-Atc", query], timeout=60,
    ).stdout.decode().strip()


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def backup_manifest_bytes(value):
    """Use UTF-8 for the outer v2 report, keeping path lengths bounded in bytes.

    Publication markers retain their original ASCII canonical encoding; only
    the backup envelope avoids multiplying non-ASCII path lengths.
    """
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def relative(name):
    if not isinstance(name, str):
        raise ValueError("Unsafe backup member path.")
    path = PurePosixPath(name)
    if (not name or path.is_absolute()
            or any(part in {"", ".", ".."} for part in name.split("/"))
            or "\\" in name or "\x00" in name or str(path) != name):
        raise ValueError("Unsafe backup member path.")
    return path


def private_directory(path, *, new=False):
    path = Path(path).absolute()
    if new:
        if path.parent.resolve(strict=True) != path.parent:
            raise ValueError("Output parent must not contain symbolic links.")
        path.mkdir(mode=0o700, exist_ok=False)
    if (path.resolve(strict=True) != path or not path.is_dir()
            or stat.S_IMODE(path.stat().st_mode) & 0o077):
        raise ValueError("Backup directories must be private real directories.")
    return path


def checked_file(root, name, limit):
    """Open a regular private file without following links at any level."""
    parts = relative(name).parts
    descriptors = []
    try:
        descriptors.append(os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
        for part in parts[:-1]:
            descriptors.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                       dir_fd=descriptors[-1]))
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=descriptors[-1])
        stream = os.fdopen(descriptor, "rb")
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                or not 0 <= metadata.st_size <= limit):
            stream.close()
            raise ValueError("Invalid backup file type or size.")
        return stream, metadata
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def copy_file(source, name, destination, *, limit=MAX_FILE, expected=None):
    stream, before = checked_file(source, name, limit)
    target = destination / name
    parent = destination
    for part in relative(name).parts[:-1]:
        parent = parent / part
        parent.mkdir(mode=0o700, exist_ok=True)
        if parent.is_symlink() or stat.S_IMODE(parent.stat().st_mode) & 0o077:
            stream.close()
            raise ValueError("Backup destination contains an unsafe directory.")
    digest, size = hashlib.sha256(), 0
    with stream, target.open("xb") as output:
        os.chmod(target, 0o600)
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            if size > limit:
                raise ValueError("Backup file exceeded its limit.")
            digest.update(chunk)
            output.write(chunk)
        after = os.fstat(stream.fileno())
        output.flush()
        os.fsync(output.fileno())
    identity = {"size_bytes": size, "sha256": digest.hexdigest()}
    if ((before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or size != before.st_size or expected is not None and identity != expected):
        raise ValueError("Backup source changed or does not match its manifest.")
    return identity


def identity(root, name, limit):
    stream, metadata = checked_file(root, name, limit)
    digest, size = hashlib.sha256(), 0
    with stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(stream.fileno())
    if ((metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)
            != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or size != metadata.st_size or size > limit):
        raise ValueError("Backup member changed during verification.")
    return {"size_bytes": size, "sha256": digest.hexdigest()}


def artifact_members(rows):
    members = {}
    for row in rows:
        job, attempt = str(UUID(row["id"])), str(UUID(row["published_attempt"]))
        manifest = row["artifact_manifest"]
        if set(manifest) != {"schema_version", "files"} or manifest["schema_version"] != 1:
            raise ValueError("Unsupported publication manifest.")
        if type(manifest["files"]) is not dict or len(manifest["files"]) > 64:
            raise ValueError("Invalid publication inventory.")
        for name, item in manifest["files"].items():
            if len(relative(name).parts) != 1 or name == "publication-manifest.json":
                raise ValueError("Invalid published artifact name.")
            if (set(item) != {"size_bytes", "sha256"} or type(item["size_bytes"]) is not int
                    or not 0 <= item["size_bytes"] <= MAX_FILE
                    or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])):
                raise ValueError("Invalid published artifact identity.")
            member = f"{job}/{attempt}/{name}"
            if member in members:
                raise ValueError("Duplicate published artifact.")
            members[member] = item
        publication = canonical(manifest)
        members[f"{job}/{attempt}/publication-manifest.json"] = {
            "size_bytes": len(publication),
            "sha256": hashlib.sha256(publication).hexdigest(),
        }
    if len(members) > MAX_FILES or sum(v["size_bytes"] for v in members.values()) > MAX_TOTAL:
        raise ValueError("Backup exceeds its aggregate budget.")
    return members


def acquisition_members(rows):
    """Bind retained originals and derived inputs to the same database snapshot."""
    members = {}
    for row in rows:
        acquisition, attempt = str(UUID(row["id"])), str(UUID(row["published_attempt"]))
        manifest = row["manifest"]
        if (set(manifest) != {"schema_version", "files"} or manifest["schema_version"] != 1
                or type(manifest["files"]) is not dict or not manifest["files"]):
            raise ValueError("Invalid acquisition publication manifest.")
        entries = {f"{acquisition}/original": {
            "size_bytes": row["size_bytes"], "sha256": row["sha256"],
        }}
        prefix = f"{acquisition}/published/{attempt}/"
        for name, item in manifest["files"].items():
            relative(name)
            if name == "publication-manifest.json":
                raise ValueError("Acquisition publication cannot inventory itself.")
            entries[prefix + name] = item
        publication = canonical(manifest)
        entries[prefix + "publication-manifest.json"] = {
            "size_bytes": len(publication), "sha256": hashlib.sha256(publication).hexdigest(),
        }
        for name, item in entries.items():
            if (type(item) is not dict or set(item) != {"size_bytes", "sha256"}
                    or type(item["size_bytes"]) is not int
                    or not 0 <= item["size_bytes"] <= MAX_ACQUISITION_FILE
                    or not isinstance(item["sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])):
                raise ValueError("Invalid acquisition file identity.")
            if name in members:
                raise ValueError("Duplicate retained acquisition.")
            members[name] = item
    # Originals and publication records add two files per acquisition.
    if (len(members) > MAX_ACQUISITION_FILES + 200
            or sum(v["size_bytes"] for v in members.values()) > 24 * 1024**3):
        raise ValueError("Acquisition backup exceeds its aggregate budget.")
    return members


def member_limit(name):
    if name == "database.dump":
        return MAX_DATABASE
    return MAX_ACQUISITION_FILE if name.startswith("acquisitions/") else MAX_FILE


def database_state(container, *, user="meridian_admin", fingerprint_version=2):
    has_acquisitions = sql(container, "SELECT to_regclass('workspace_acquisition') IS NOT NULL",
                           user=user) == "t"
    tables = TABLES + (ACQUISITION_TABLES if has_acquisitions else ())
    counts = {table: int(sql(container, "SELECT count(*) FROM " + table, user=user))
              for table in tables}
    jobs = json.loads(sql(container, """
        SELECT coalesce(json_agg(row_to_json(j)), '[]') FROM (
          SELECT id, published_attempt, artifact_manifest, artifacts_expire_at
          FROM workspace_job WHERE artifacts_available ORDER BY id
        ) j
        """, user=user))
    migrations = json.loads(sql(container, """
        SELECT coalesce(json_agg(row_to_json(m)), '[]') FROM
          (SELECT app,name FROM django_migrations ORDER BY app,name) m
        """, user=user))
    fingerprints = {}
    if fingerprint_version not in {1, 2}:
        raise ValueError("Unsupported database fingerprint version.")
    for table in tables:
        if fingerprint_version == 1:
            # Historical backups fingerprinted one aggregate JSON array.
            raw = sql(container, "SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY "
                      "to_jsonb(t)::text), '[]'::jsonb) FROM " + table + " t", user=user)
        else:
            # Hash each row before sorting: only fixed-size digests cross the
            # process boundary, even for large acquisition descriptors.
            raw = sql(container, "SELECT encode(sha256(convert_to(to_jsonb(t)::text, 'UTF8')), 'hex') "
                      "FROM " + table + " t ORDER BY 1", user=user)
        fingerprints[table] = hashlib.sha256(raw.encode()).hexdigest()
    result = {"counts": counts, "retained_jobs": jobs, "migrations": migrations,
              "table_content_sha256": fingerprints}
    if fingerprint_version == 2:
        result["fingerprint_version"] = 2
    if has_acquisitions:
        result["retained_acquisitions"] = json.loads(sql(container, """
            SELECT coalesce(json_agg(row_to_json(a)), '[]') FROM (
              SELECT id, published_attempt, manifest, size_bytes, sha256
              FROM workspace_acquisition WHERE files_available ORDER BY id
            ) a
            """, user=user))
    return result


def require_idle(container):
    if int(sql(container, "SELECT count(*) FROM workspace_job "
                         "WHERE state IN ('queued','running','evaluating')")):
        raise ValueError("Backup requires an empty queue and no active attempts.")
    if (sql(container, "SELECT to_regclass('workspace_acquisition') IS NOT NULL") == "t"
            and int(sql(container, "SELECT count(*) FROM workspace_acquisition "
                        "WHERE state IN ('receiving','validating') OR current_attempt IS NOT NULL"))):
        raise ValueError("Backup requires completed or cancelled transfers and no active imports.")
    for component in ("vio-job", "sensor-import"):
        children = command(["docker", "ps", "--all", "--filter",
                            "label=meridian.component=" + component, "--format", "{{.ID}}"]).stdout
        if children.strip():
            raise ValueError("Numerical/import child containers must be absent before backup.")


def backup(args):
    if not re.fullmatch(r"meridian-workspace(?:-[a-z0-9-]+)?", args.project):
        raise ValueError("Expected an explicit Meridian Compose project.")
    writers = ("api", "worker", "importer") if getattr(args, "acquisitions", None) is not None else ("api", "worker")
    services = {key: inspect(f"{args.project}-{key}-1") for key in (*writers, "db")}
    for key, container in services.items():
        labels = container["Config"].get("Labels") or {}
        if (labels.get("com.docker.compose.project") != args.project
                or labels.get("com.docker.compose.service") != key):
            raise ValueError("Service does not belong to the requested Compose project.")
    db = services["db"]["Id"]
    if not services["db"]["State"]["Running"]:
        raise ValueError("Database must already be running.")
    runtime = database_runtime()
    if (services["db"]["Image"] != runtime["image_id"]
            or services["db"]["Config"].get("Image") != DATABASE_IMAGE):
        raise ValueError("Source database differs from the pinned PostgreSQL image.")
    require_idle(db)
    artifacts = Path(args.artifacts).absolute()
    if artifacts.resolve(strict=True) != artifacts or not artifacts.is_dir():
        raise ValueError("Artifact root must be a real directory.")
    intended = Path(args.output).absolute()
    if intended.is_relative_to(artifacts) or artifacts.is_relative_to(intended):
        raise ValueError("Backup output must be separate from active artifact storage.")
    for key in ("api", "worker"):
        mounts = [m for m in services[key]["Mounts"]
                  if m["Source"] == str(artifacts)]
        if len(mounts) != 1 or mounts[0]["Type"] != "bind":
            raise ValueError("Artifact root differs from the actual service mount.")
    acquisitions = getattr(args, "acquisitions", None)
    if acquisitions is not None:
        acquisitions = Path(acquisitions).absolute()
        if (acquisitions.resolve(strict=True) != acquisitions or not acquisitions.is_dir()
                or intended.is_relative_to(acquisitions) or acquisitions.is_relative_to(intended)
                or acquisitions.is_relative_to(artifacts) or artifacts.is_relative_to(acquisitions)):
            raise ValueError("Acquisition root must be real and separate from backup/artifact storage.")
        for key in ("api", "importer"):
            mounts = [m for m in services[key]["Mounts"] if m["Source"] == str(acquisitions)]
            if len(mounts) != 1 or mounts[0]["Type"] != "bind":
                raise ValueError("Acquisition root differs from the actual service mount.")
    output = private_directory(args.output, new=True)
    (output / "artifacts").mkdir(mode=0o700)
    started, pause_started = datetime.now(timezone.utc).isoformat(), time.monotonic()
    stopped = []
    report = None
    try:
        # API first prevents new submissions while the idle worker shuts down.
        for key in writers:
            container = services[key]
            if container["State"]["Running"]:
                # Register before calling stop so a timeout still attempts recovery.
                stopped.append(container["Id"])
                command(["docker", "stop", "--time", "45", container["Id"]], timeout=60)
        require_idle(db)
        before = database_state(db)
        database_size_bytes = int(sql(db, "SELECT pg_database_size(current_database())"))
        members = artifact_members(before["retained_jobs"])
        if "retained_acquisitions" in before and acquisitions is None:
            raise ValueError("This release requires --acquisitions, including for an empty store.")
        input_members = acquisition_members(before.get("retained_acquisitions", []))
        if acquisitions is not None:
            (output / "acquisitions").mkdir(mode=0o700)
        descriptor = os.open(output / "database.dump", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            subprocess.run(
                ["docker", "exec", db, "pg_dump", "-U", "meridian_admin",
                 "--format=custom", "--no-owner", "--no-acl", "--dbname=meridian"],
                stdout=stream, stderr=subprocess.PIPE, timeout=120, check=True,
                preexec_fn=lambda: resource.setrlimit(
                    resource.RLIMIT_FSIZE, (MAX_DATABASE, MAX_DATABASE)),
            )
            stream.flush()
            os.fsync(stream.fileno())
        files = {"database.dump": identity(output, "database.dump", MAX_DATABASE)}
        for name, expected in members.items():
            files["artifacts/" + name] = copy_file(
                artifacts, name, output / "artifacts", expected=expected,
            )
        for name, expected in input_members.items():
            files["acquisitions/" + name] = copy_file(
                acquisitions, name, output / "acquisitions", limit=MAX_ACQUISITION_FILE,
                expected=expected,
            )
        if sum(item["size_bytes"] for item in files.values()) > MAX_COMPLETE_TOTAL:
            raise ValueError("Backup exceeds its complete size budget.")
        if database_state(db) != before:
            raise ValueError("Database changed while services were stopped.")
        report = {
            "format": FORMAT, "schema_version": 2, "started_at_utc": started,
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "database_image_id": services["db"]["Image"],
            "database_runtime": runtime,
            "application_image_ids": {key: services[key]["Image"] for key in writers},
            "database_state": before, "database_size_bytes": database_size_bytes, "files": files,
            "scope": "Database metadata, published job artifacts and retained private acquisition originals/derived inputs; no environment secrets or public reference recordings.",
            "sensitive_contents": "Includes password hashes, session records and private project data.",
        }
    finally:
        failures = []
        for container in stopped:
            try:
                command(["docker", "start", container], timeout=60)
            except subprocess.SubprocessError:
                failures.append(container)
        if failures:
            raise RuntimeError("Service restart failed; inspect the original API/worker immediately.")
    report["write_pause_seconds"] = time.monotonic() - pause_started
    encoded_report = backup_manifest_bytes(report)
    if len(encoded_report) > MAX_MANIFEST:
        raise ValueError("Backup manifest exceeds its metadata budget.")
    (output / "manifest.json").write_bytes(encoded_report)
    os.chmod(output / "manifest.json", 0o600)
    verify(output)
    return {"backup_verified": True, "files": len(report["files"]),
            "bytes": sum(x["size_bytes"] for x in report["files"].values()),
            "retained_jobs": len(report["database_state"]["retained_jobs"]),
            "retained_acquisitions": len(report["database_state"].get("retained_acquisitions", [])),
            "write_pause_seconds": report["write_pause_seconds"]}


def verify(directory):
    root = private_directory(directory)
    stream, _ = checked_file(root, "manifest.json", MAX_MANIFEST)
    with stream:
        manifest = json.load(stream)
    if manifest.get("format") != FORMAT or manifest.get("schema_version") not in {1, 2}:
        raise ValueError("Unsupported backup format.")
    files = manifest.get("files")
    maximum_files = MAX_FILES + 1 + (MAX_ACQUISITION_FILES + 200 if manifest["schema_version"] == 2 else 0)
    if not isinstance(files, dict) or not 1 <= len(files) <= maximum_files or "database.dump" not in files:
        raise ValueError("Invalid backup inventory.")
    required = {"database.dump", *("artifacts/" + name for name in
                                   artifact_members(manifest["database_state"]["retained_jobs"]))}
    input_members = acquisition_members(manifest["database_state"].get("retained_acquisitions", []))
    if manifest["schema_version"] == 1 and input_members:
        raise ValueError("Legacy backups cannot contain private acquisitions.")
    required.update("acquisitions/" + name for name in input_members)
    if set(files) != required:
        raise ValueError("Backup inventory differs from retained publication metadata.")
    actual = set()
    for path in root.rglob("*"):
        if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ValueError("Backup contains a link or a non-private member.")
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
        elif not path.is_dir():
            raise ValueError("Backup contains a nonregular member.")
    if actual != set(files) | {"manifest.json"}:
        raise ValueError("Backup contains missing or unexpected files.")
    total = 0
    for name, expected in files.items():
        observed = identity(root, name, member_limit(name))
        if observed != expected:
            raise ValueError("Backup member integrity check failed.")
        total += observed["size_bytes"]
    expected_artifacts = artifact_members(manifest["database_state"]["retained_jobs"])
    if any(files["artifacts/" + name] != expected for name, expected in expected_artifacts.items()):
        raise ValueError("Backup artifacts differ from their database publication manifest.")
    if any(files["acquisitions/" + name] != expected for name, expected in input_members.items()):
        raise ValueError("Backup acquisitions differ from their database publication manifest.")
    if total > (MAX_COMPLETE_TOTAL if manifest["schema_version"] == 2 else MAX_TOTAL):
        raise ValueError("Backup exceeds its total size budget.")
    return manifest


def restore(args):
    manifest = verify(args.backup)
    database_mib = getattr(args, "database_mib", 256)
    if type(database_mib) is not int or not 256 <= database_mib <= 8192:
        raise ValueError("Restore --database-mib must be between 256 and 8192 MiB.")
    database_bytes = manifest.get("database_size_bytes")
    if database_bytes is not None:
        if type(database_bytes) is not int or database_bytes < 0:
            raise ValueError("Invalid source database size metadata.")
        required = 2 * database_bytes + 64 * 1024**2
        if database_mib * 1024**2 < required:
            minimum = (required + 1024**2 - 1) // 1024**2
            raise ValueError(f"Restore database capacity is insufficient; choose --database-mib at least {minimum} (maximum 8192) on a host with sufficient memory.")
    memory_mib = max(512, database_mib + 256)
    source = Path(args.backup).absolute()
    destination = Path(args.output).absolute()
    if destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError("Restore output must be separate from the verified backup.")
    if not re.fullmatch(r"meridian-restore-[a-z0-9][a-z0-9-]{0,40}", args.name):
        raise ValueError("Restore container name must begin with meridian-restore-.")
    runtime = database_runtime()
    compatible_database(manifest, runtime)
    if command(["docker", "ps", "--all", "--filter", f"name=^/{args.name}$",
                "--format", "{{.ID}}"]).stdout.strip():
        raise ValueError("Restore container already exists; refusing replacement.")
    output = private_directory(args.output, new=True)
    for name, expected in manifest["files"].items():
        if name.startswith(("artifacts/", "acquisitions/")):
            copy_file(source, name, output, limit=member_limit(name), expected=expected)
    (output / "artifacts").mkdir(mode=0o700, exist_ok=True)
    if manifest["schema_version"] == 2:
        (output / "acquisitions").mkdir(mode=0o700, exist_ok=True)
    # No published ports, network, named volume, source environment or application
    # secret is reused. PostgreSQL data are disposable and bounded in this drill.
    environment = output / "restore.env"
    application_password = secrets.token_hex(32)
    environment.write_text("POSTGRES_USER=restore_admin\nPOSTGRES_DB=meridian\n"
                           "POSTGRES_PASSWORD=" + secrets.token_hex(32) + "\n"
                           "MERIDIAN_RESTORE_DATABASE_PASSWORD=" + application_password + "\n")
    os.chmod(environment, 0o600)
    restore_token = secrets.token_hex(24)
    try:
        command([
            "docker", "run", "--detach", "--pull", "never", "--name", args.name,
            "--label", "meridian.component=restore-drill",
            "--label", "meridian.restore-token=" + restore_token, "--network", "none",
            "--read-only", "--user", "999:999", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--memory", f"{memory_mib}m",
            "--memory-swap", f"{memory_mib}m", "--cpus", "0.5", "--pids-limit", "128",
            "--tmpfs", f"/var/lib/postgresql/data:rw,uid=999,gid=999,mode=0700,size={database_mib}m",
            "--tmpfs", "/var/run/postgresql:rw,uid=999,gid=999,mode=0770,size=8m",
            "--tmpfs", "/tmp:rw,mode=1777,size=16m", "--env-file", str(environment),
            runtime["image_id"], "postgres", "-c", "shared_buffers=32MB",
        ], timeout=60)
        if inspect(args.name)["Image"] != runtime["image_id"]:
            raise ValueError("Restored database container uses a different image.")
        deadline = time.monotonic() + 40
        while True:
            # The entrypoint's temporary initialization server accepts Unix
            # sockets before database creation finishes. TCP becomes available
            # only after that server exits and normal PostgreSQL startup begins.
            check = subprocess.run(["docker", "exec", args.name, "pg_isready",
                                    "-h", "127.0.0.1", "-U", "restore_admin", "-d", "meridian"],
                                   capture_output=True)
            if check.returncode == 0:
                break
            if time.monotonic() > deadline:
                raise RuntimeError("Restored PostgreSQL did not become ready.")
            time.sleep(0.5)
        if json.loads(sql(args.name, "SELECT json_build_array(current_database(),current_user)",
                          user="restore_admin")) != ["meridian", "restore_admin"]:
            raise ValueError("Restoration connected to an unexpected database or role.")
        # The initdb bootstrap role must remain a superuser on PostgreSQL 17.
        # Create the restricted application owner separately, as in production.
        # The fresh password is sent over stdin, never a process argument/log.
        command(["docker", "exec", "-i", args.name, "psql", "-X", "-v", "ON_ERROR_STOP=1",
                 "-U", "restore_admin", "-d", "meridian"], input=(
            "CREATE ROLE meridian LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
            "PASSWORD '" + application_password + "';\n"
            "ALTER DATABASE meridian OWNER TO meridian;\n"
        ).encode(), timeout=60)
        with (source / "database.dump").open("rb") as dump:
            command(["docker", "exec", "-i", args.name, "pg_restore", "-U", "meridian",
                     "--dbname=meridian", "--no-owner", "--no-acl",
                     "--exit-on-error", "--single-transaction"], stdin=dump, timeout=120)
        fingerprint_version = manifest["database_state"].get("fingerprint_version", 1)
        actual = database_state(args.name, user="meridian", fingerprint_version=fingerprint_version)
        if actual != manifest["database_state"]:
            raise ValueError("Restored database metadata differs from the backup.")
        sessions = actual["counts"]["django_session"]
        sql(args.name, "DELETE FROM django_session", user="meridian")
        role_flags = json.loads(sql(args.name, """
            SELECT json_build_object('superuser',rolsuper,'create_database',rolcreatedb,
                                     'create_role',rolcreaterole,'replication',rolreplication)
            FROM pg_roles WHERE rolname=current_user
            """, user="meridian"))
        if any(role_flags.values()):
            raise ValueError("Restored application role has excessive privileges.")
        for name, expected in manifest["files"].items():
            if name.startswith(("artifacts/", "acquisitions/")) and identity(output, name, member_limit(name)) != expected:
                raise ValueError("Restored publication differs from the backup.")
        report = {
            "schema_version": 1, "backup_manifest_sha256": hashlib.sha256(
                (source / "manifest.json").read_bytes()).hexdigest(),
            "database_metadata_verified_before_session_clear": True,
            "restored_artifacts_verified": True, "copied_sessions_cleared": sessions,
            "restored_acquisitions_verified": manifest["schema_version"] == 2,
            "database_image_id": runtime["image_id"],
            "source_database_image_id": manifest["database_image_id"],
            "database_runtime": runtime,
            "same_pinned_distribution_and_platform": True,
            "application_role_flags": role_flags,
            "restored_counts": database_state(args.name, user="meridian")["counts"],
            "container": args.name, "network": "none", "database_storage": "disposable tmpfs",
            "database_storage_mib": database_mib, "container_memory_mib": memory_mib,
            "source_database_size_bytes": database_bytes,
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        (output / "restore-report.json").write_bytes(canonical(report))
        os.chmod(output / "restore-report.json", 0o600)
        return report
    except BaseException:
        # A concurrent process could claim the unused name before `docker run`.
        # Remove only a container with this invocation's unguessable ownership tag.
        try:
            candidate = inspect(args.name)
            if (candidate["Config"].get("Labels") or {}).get("meridian.restore-token") == restore_token:
                subprocess.run(["docker", "rm", "--force", candidate["Id"]], capture_output=True)
        except (subprocess.SubprocessError, KeyError, ValueError):
            pass
        raise
    finally:
        environment.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    create = sub.add_parser("backup", help="Briefly stop idle API/worker; restart them in finally.")
    create.add_argument("--project", required=True)
    create.add_argument("--artifacts", required=True, type=Path)
    create.add_argument("--acquisitions", type=Path,
                        help="Required for releases with private acquisitions, even when empty.")
    create.add_argument("--output", required=True, type=Path)
    check = sub.add_parser("verify")
    check.add_argument("--backup", required=True, type=Path)
    recovery = sub.add_parser("restore", help="Create a NEW disposable isolated PostgreSQL drill.")
    recovery.add_argument("--backup", required=True, type=Path)
    recovery.add_argument("--output", required=True, type=Path)
    recovery.add_argument("--name", required=True)
    recovery.add_argument("--database-mib", type=int, default=256,
                       help="Disposable database tmpfs capacity, 256..8192 MiB; memory limit is capacity plus 256 MiB.")
    args = parser.parse_args()
    os.umask(0o077)
    if args.action == "backup":
        result = backup(args)
    elif args.action == "restore":
        result = restore(args)
    else:
        manifest = verify(args.backup)
        result = {"backup_verified": True, "files": len(manifest["files"])}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
