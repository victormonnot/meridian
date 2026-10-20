"""Archive integrity and privacy checks; fixtures are synthetic, not VIO results."""

import hashlib
import io
import json
from pathlib import Path
import stat
import subprocess
import sys
import zipfile

import pytest

from meridian.vio_export import (
    MAX_FILE_BYTES, REQUIRED_ARTIFACTS, SOURCE_FILES, _canonical_hash, _json_bytes,
    build_export, identity, strict_json, verify_directory, verify_export,
)
from meridian.vio_specification import build_specification, specification_sha256


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def inputs(monkeypatch):
    from meridian import vio_specification
    original_manifest_hash = vio_specification._PINNED["dataset"]["prepared_manifest_sha256"]
    input_manifest = {"schema_version": 1, "synthetic_fixture": True}
    fixture_manifest_hash = identity(_json_bytes(input_manifest))["sha256"]
    monkeypatch.setitem(vio_specification._PINNED["dataset"], "prepared_manifest_sha256", fixture_manifest_hash)
    spec = build_specification("tumvi-room1-512-16-v1", {"num_pts": 200})
    sources = {name: (ROOT / name).read_bytes() for name in SOURCE_FILES}
    sources["src/meridian/vio_specification.py"] = sources["src/meridian/vio_specification.py"].replace(
        original_manifest_hash.encode(), fixture_manifest_hash.encode())
    run_spec = {
        "schema_version": 1, "experiment_specification_sha256": specification_sha256(spec),
        "dataset_manifest_sha256": spec["dataset"]["prepared_manifest_sha256"],
        "evaluator_sha256": spec["evaluation"]["evaluator_sha256"],
        "evaluation_protocol_version": 1,
        "rendered_configuration_files": spec["configuration"]["rendered_files"],
        "runtime_image_id": spec["runtime"]["image_id"],
        "runtime_version": spec["runtime"]["version"],
        "runtime_provenance_sha256": spec["runtime"]["provenance_sha256"],
        "limits": spec["resources"],
        "qualification_criteria": spec["evaluation"]["qualification_criteria"],
        "input_manifest": input_manifest,
        "runner_sha256": identity(sources["src/meridian/vio_reference.py"])["sha256"],
    }
    status = {"schema_version": 1, "state": "complete", "error": None}
    artifacts = {
        "experiment-specification.json": _json_bytes(spec),
        "run-spec.json": _json_bytes(run_spec), "status.json": _json_bytes(status),
        "evaluation.json": _json_bytes({"schema_version": 1, "status": "ok"}),
        "trajectory.csv": b"timestamp_ns,px_m,py_m,pz_m,qx,qy,qz,qw\n1,0,0,0,0,0,0,1\n",
        "dependency-versions.txt": (ROOT / "results/vio-reference/dependency-versions.txt").read_bytes(),
        # These recorded binary hashes identify the original output, not a
        # binary distributed with the fixture or export.
        "binary-sha256.txt": (
            b"6ee3d0ffe11cac76a59eefbb41eb60cdbae73383f24d9a7d8aac106ec8158e93  /usr/local/bin/meridian-openvins\n"
            b"d9f474faa016c6eb97cae8cd0895adf9f5805e2ea9d71828700ccaee3a54b8e1  /usr/local/lib/libmeridian_openvins.so\n"
        ),
    }
    for name in spec["configuration"]["rendered_files"]:
        artifacts["config-" + name] = sources["integrations/openvins/config/" + name]
    result = {
        "schema_version": 1, "state": "complete", "error": None, "returncode": 0,
        "experiment_specification_sha256": specification_sha256(spec),
        "run_spec_sha256": _canonical_hash(run_spec), "native_status": status,
        "artifacts": {name: identity(content) for name, content in artifacts.items()},
    }
    artifacts["result.json"] = _json_bytes(result)
    execution = {
        "job_id": "01234567-1234-1234-1234-012345678901",
        "experiment_id": "11234567-1234-1234-1234-012345678901",
        "revision_version": 1, "state": "succeeded",
        "created_at": "2026-10-20T00:00:00Z", "started_at": "2026-10-20T00:00:01Z",
        "finished_at": "2026-10-20T00:00:02Z",
        "specification_sha256": specification_sha256(spec),
    }
    return {"specification": spec, "artifacts": artifacts,
            "publication_manifest": {"schema_version": 1, "files": {
                name: identity(content) for name, content in artifacts.items()}},
            "source_files": sources, "execution": execution}


def republish(inputs, name, content):
    inputs["artifacts"][name] = content
    if name != "result.json":
        result = strict_json(inputs["artifacts"]["result.json"])
        result["artifacts"][name] = identity(content)
        inputs["artifacts"]["result.json"] = _json_bytes(result)
    inputs["publication_manifest"]["files"] = {
        name: identity(content) for name, content in inputs["artifacts"].items()}


def rewrite_zip(data, transform):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = [(info, archive.read(info)) for info in archive.infolist()]
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for info, content in transform(entries):
            archive.writestr(info, content)
    return output.getvalue()


def test_round_trip_preserves_exact_evidence_and_deterministic_archive(inputs):
    first = build_export(**inputs)
    assert first == build_export(**inputs)
    manifest = verify_export(first)
    assert len(manifest["files"]) == len(SOURCE_FILES) + len(REQUIRED_ARTIFACTS) + 1
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        assert archive.read("artifacts/trajectory.csv") == inputs["artifacts"]["trajectory.csv"]
        assert not any("runner.log" in name or ".personal" in name for name in archive.namelist())


@pytest.mark.parametrize("key,value", [
    ("schema_version", 2), ("schema_version", True), ("format", "unknown"),
    ("tolerances", {}), ("distribution", {}),
])
def test_unknown_contracts_are_not_silently_upgraded(inputs, key, value):
    def transform(entries):
        for info, content in entries:
            if info.filename == "export-manifest.json":
                manifest = strict_json(content)
                manifest[key] = value
                content = _json_bytes(manifest)
            yield info, content
    with pytest.raises(ValueError):
        verify_export(rewrite_zip(build_export(**inputs), transform))


@pytest.mark.parametrize("name", [
    "artifacts/trajectory.csv", "source/src/meridian/vio_reference.py",
    "source/integrations/openvins/NOTICE.md", "reproduce.py",
])
def test_tamper_is_rejected_before_execution(inputs, name):
    with pytest.raises(ValueError):
        verify_export(rewrite_zip(build_export(**inputs), lambda entries: [
            (info, content + b"x" if info.filename == name else content) for info, content in entries]))


@pytest.mark.parametrize("name", [
    "../escape", "/absolute", "source/../../escape", "source\\escape",
    "source//duplicate", "C:/windows", "source/./dot",
])
def test_archive_never_accepts_unsafe_paths(inputs, name):
    def transform(entries):
        yield from entries
        yield zipfile.ZipInfo(name), b"unsafe"
    with pytest.raises(ValueError):
        verify_export(rewrite_zip(build_export(**inputs), transform))


def test_duplicate_members_are_rejected(inputs):
    with pytest.warns(UserWarning), pytest.raises(ValueError):
        verify_export(rewrite_zip(build_export(**inputs), lambda entries: [*entries, entries[0]]))


@pytest.mark.parametrize("mode", [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR, stat.S_IFDIR])
def test_nonregular_members_are_rejected(inputs, mode):
    def transform(entries):
        info, content = entries[0]
        info.external_attr = (mode | 0o644) << 16
        yield info, content
        yield from entries[1:]
    with pytest.raises(ValueError):
        verify_export(rewrite_zip(build_export(**inputs), transform))


def test_compressed_size_bomb_is_bounded_before_inflate(inputs):
    def transform(entries):
        yield from entries
        info = zipfile.ZipInfo("bomb")
        info.compress_type = zipfile.ZIP_DEFLATED
        yield info, b"0" * (MAX_FILE_BYTES + 1)
    with pytest.raises(ValueError):
        verify_export(rewrite_zip(build_export(**inputs), transform))


@pytest.mark.parametrize("content", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}', b'[]'])
def test_json_ambiguity_and_nonfinite_values_fail(content):
    with pytest.raises(ValueError):
        strict_json(content)


@pytest.mark.parametrize("field,value", [
    ("state", "failed"), ("returncode", 1), ("error", {"message": "failure"}),
    ("experiment_specification_sha256", "0" * 64), ("run_spec_sha256", "0" * 64),
])
def test_completed_result_binding_is_required(inputs, field, value):
    result = strict_json(inputs["artifacts"]["result.json"])
    result[field] = value
    republish(inputs, "result.json", _json_bytes(result))
    with pytest.raises(ValueError):
        build_export(**inputs)


@pytest.mark.parametrize("field,value", [
    ("host_path", "/srv/private/artifact"), ("secret", "must not leave host"),
    ("home", "C:\\Users\\private"), ("path", "file:///private"),
])
def test_private_metadata_is_not_exported(inputs, field, value):
    result = strict_json(inputs["artifacts"]["result.json"])
    result[field] = value
    republish(inputs, "result.json", _json_bytes(result))
    with pytest.raises(ValueError):
        build_export(**inputs)


def test_public_https_dataset_location_is_allowed(inputs):
    from meridian.vio_export import _private_strings
    _private_strings({"url": "https://cvg.cit.tum.de/data/datasets/visual-inertial-dataset"})
    _private_strings({"url": "https://creativecommons.org/licenses/by/4.0/"})


@pytest.mark.parametrize("name", [
    "src/meridian/vio_evaluation.py", "integrations/openvins/runner.cpp",
    "integrations/openvins/config/kalibr_imucam_chain.yaml",
])
def test_pinned_numerical_sources_cannot_be_replaced(inputs, name):
    inputs["source_files"][name] += b"\n"
    with pytest.raises(ValueError):
        build_export(**inputs)


def test_original_published_artifacts_cannot_be_replaced(inputs):
    inputs["artifacts"]["trajectory.csv"] += b"\n"
    with pytest.raises(ValueError):
        build_export(**inputs)


def test_omitted_required_and_unrequested_files_fail(inputs):
    inputs["source_files"]["private-notes.txt"] = b"not public"
    with pytest.raises(ValueError):
        build_export(**inputs)
    del inputs["source_files"]["private-notes.txt"]
    del inputs["artifacts"]["evaluation.json"]
    with pytest.raises(ValueError):
        build_export(**inputs)


def test_stdlib_only_export_launcher_and_extracted_verification(inputs, tmp_path):
    data = build_export(**inputs)
    bundle = tmp_path / "bundle"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        archive.extractall(bundle)
    assert verify_directory(bundle)["schema_version"] == 1
    # -I -S excludes the checkout and installed NumPy/site-packages.
    check = subprocess.run([sys.executable, "-I", "-S", str(bundle / "reproduce.py"),
                            "verify", "--bundle", str(bundle)], capture_output=True, text=True)
    assert check.returncode == 0, check.stderr
    assert strict_json(check.stdout.encode())["passed"]
    assert not list(bundle.rglob("__pycache__"))
    (bundle / "extra.txt").write_text("unexpected")
    with pytest.raises(ValueError):
        verify_directory(bundle)


def test_extracted_links_fail(inputs, tmp_path):
    bundle = tmp_path / "bundle"
    with zipfile.ZipFile(io.BytesIO(build_export(**inputs))) as archive:
        archive.extractall(bundle)
    (bundle / "source-link").symlink_to(bundle / "source", target_is_directory=True)
    with pytest.raises(ValueError):
        verify_directory(bundle)
