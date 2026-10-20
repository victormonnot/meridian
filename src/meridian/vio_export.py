"""Bounded, source-only VIO experiment archives, using the standard library.

Hashes detect altered contents relative to a manifest, not a malicious party
replacing the archive and manifest together. The download SHA-256 is the external
identity; there is no signature or authenticity claim.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import stat
import zipfile

from meridian.vio_specification import specification_sha256, validate_specification
from meridian.vio_runtime_identity import CONFIGURATION_ID, OCI_MANIFEST_ID


EXPORT_SCHEMA_VERSION = 1
EXPORT_FORMAT = "meridian-vio-experiment"
MAX_EXPORT_BYTES = 16 * 1024 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_FILES = 64
CONFIGURATION_NAMES = (
    "estimator_config.yaml", "kalibr_imu_chain.yaml", "kalibr_imucam_chain.yaml",
    "mask_tumvi0.png", "mask_tumvi1.png",
)
REQUIRED_ARTIFACTS = (
    "experiment-specification.json", "run-spec.json", "result.json",
    "trajectory.csv", "status.json", "evaluation.json",
    "binary-sha256.txt", "dependency-versions.txt",
    *(f"config-{name}" for name in CONFIGURATION_NAMES),
)
SOURCE_FILES = (
    "src/meridian/__init__.py",
    "src/meridian/vio_dataset.py",
    "src/meridian/vio_evaluation.py",
    "src/meridian/vio_reference.py",
    "src/meridian/vio_specification.py",
    "src/meridian/vio_runtime_identity.py",
    "src/meridian/vio_export.py",
    "src/meridian/vio_reproduction.py",
    "integrations/openvins/Dockerfile",
    "integrations/openvins/CMakeLists.txt",
    "integrations/openvins/runner.cpp",
    "integrations/openvins/LICENSE",
    "integrations/openvins/NOTICE.md",
    "integrations/openvins/README.md",
    "integrations/openvins/smoke_test.py",
    *(f"integrations/openvins/config/{name}" for name in CONFIGURATION_NAMES),
    "integrations/reproduction/README.md",
    "integrations/reproduction/requirements.lock",
    "docs/vio-reference.md",
    "docs/vio-exports.md",
)
TOLERANCES = {
    "maximum_direct_position_difference_m": 0.001,
    "maximum_direct_orientation_difference_deg": 0.001,
    "maximum_ate_rmse_difference_m": 0.001,
    "maximum_orientation_rmse_difference_deg": 0.001,
    "output_timestamps": "identical",
    "evaluation_support_timestamps": "identical",
    "support_counts_and_missing_intervals": "identical",
    "direct_pose_alignment": "none",
}
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_EXECUTION_KEYS = {
    "job_id", "experiment_id", "revision_version", "state", "created_at",
    "started_at", "finished_at", "specification_sha256",
}
_LAUNCHER = b'''"""Run the exported verifier/reproducer without installing Meridian."""
from pathlib import Path
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent / "source" / "src"))
from meridian.vio_reproduction import main
raise SystemExit(main())
'''


def _json_bytes(value) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True,
                       allow_nan=False) + "\n").encode("ascii")


def _canonical_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("ascii")).hexdigest()


def strict_json(content: bytes) -> dict:
    """Reject ambiguous keys, nonfinite constants and non-object documents."""
    def pairs(items):
        value = {}
        for key, entry in items:
            if key in value:
                raise ValueError("Duplicate JSON key.")
            value[key] = entry
        return value

    def invalid(_):
        raise ValueError("Nonfinite JSON value.")

    try:
        value = json.loads(content, object_pairs_hook=pairs, parse_constant=invalid)
        # Also rejects finite-looking exponents that overflow into infinity.
        _json_bytes(value)
    except (TypeError, UnicodeError, RecursionError) as error:
        raise ValueError("Invalid export JSON.") from error
    if type(value) is not dict:
        raise ValueError("Export JSON must be an object.")
    return value


def identity(content: bytes) -> dict:
    return {"sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)}


def _safe_name(name: str) -> None:
    if (type(name) is not str or not name or "\\" in name or "\x00" in name
            or any(ord(character) < 32 for character in name)
            or name != str(PurePosixPath(name)) or PurePosixPath(name).is_absolute()
            or any(part in {".", ".."} for part in name.split("/"))
            or ":" in name):
        raise ValueError("Unsafe export member name.")


def _check_identity(value) -> None:
    if (type(value) is not dict or set(value) != {"sha256", "size_bytes"}
            or type(value["sha256"]) is not str or not _DIGEST.fullmatch(value["sha256"])
            or type(value["size_bytes"]) is not int
            or not 0 <= value["size_bytes"] <= MAX_FILE_BYTES):
        raise ValueError("Invalid export file identity.")


def _private_strings(value) -> None:
    """Reject host locations/credentials in the allowlisted JSON evidence."""
    if isinstance(value, dict):
        for key, item in value.items():
            if re.search(r"password|secret|token|cookie|authorization", key, re.I):
                raise ValueError("Private fields are not exportable.")
            _private_strings(item)
    elif isinstance(value, list):
        for item in value:
            _private_strings(item)
    elif isinstance(value, str):
        if (re.search(r"(?:^|[\s\"=])/(?:home|Users|root|tmp|srv|var|app|run|mnt|media)/", value)
                or re.search(r"(?:^|\s)[A-Za-z]:[\\/]", value)
                or value.startswith("file:")):
            raise ValueError("Host filesystem locations are not exportable.")


def _validate_payload(files: dict[str, bytes], manifest: dict) -> None:
    if (set(manifest) != {"format", "schema_version", "execution", "specification_sha256",
                          "tolerances", "distribution", "files"}
            or manifest.get("format") != EXPORT_FORMAT
            or type(manifest.get("schema_version")) is not int
            or manifest["schema_version"] != EXPORT_SCHEMA_VERSION):
        raise ValueError("Unsupported export format or schema version.")
    expected_names = {
        *(f"artifacts/{name}" for name in REQUIRED_ARTIFACTS),
        *(f"source/{name}" for name in SOURCE_FILES),
        "reproduce.py",
    }
    if set(files) != expected_names or set(manifest["files"]) != expected_names:
        raise ValueError("Export inventory does not match the supported format.")
    if manifest["tolerances"] != TOLERANCES:
        raise ValueError("Unsupported reproduction tolerance contract.")
    if manifest["distribution"] != {
        "kind": "source_and_result_evidence",
        "native_binaries_included": False,
        "dataset_samples_included": False,
        "runtime_rebuild": "explicit_new_runtime_identity",
    }:
        raise ValueError("Unsupported export distribution.")
    total = 0
    for name, content in files.items():
        _safe_name(name)
        if type(content) is not bytes:
            raise ValueError("Export contents must be bytes.")
        recorded = manifest["files"][name]
        _check_identity(recorded)
        if identity(content) != recorded:
            raise ValueError("Export member failed its integrity check.")
        total += len(content)
    if total > MAX_EXPORT_BYTES:
        raise ValueError("Export exceeds its uncompressed size limit.")
    if files["reproduce.py"] != _LAUNCHER:
        raise ValueError("Unsupported export launcher.")
    spec = strict_json(files["artifacts/experiment-specification.json"])
    validate_specification(spec)
    spec_hash = specification_sha256(spec)
    execution = manifest["execution"]
    if (type(execution) is not dict or set(execution) != _EXECUTION_KEYS
            or execution.get("state") != "succeeded"
            or execution.get("specification_sha256") != spec_hash
            or type(execution.get("revision_version")) is not int
            or execution["revision_version"] < 1):
        raise ValueError("Export requires a complete successful execution identity.")
    for key in ("job_id", "experiment_id"):
        if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", str(execution[key])):
            raise ValueError("Invalid exported execution identifier.")
    for key in ("created_at", "started_at", "finished_at"):
        if type(execution[key]) is not str or not re.fullmatch(r"[0-9T:.+Z-]{20,40}", execution[key]):
            raise ValueError("Invalid exported execution timestamp.")
    if manifest["specification_sha256"] != spec_hash:
        raise ValueError("Export specification identity differs.")
    result = strict_json(files["artifacts/result.json"])
    run_spec = strict_json(files["artifacts/run-spec.json"])
    status = strict_json(files["artifacts/status.json"])
    evaluation = strict_json(files["artifacts/evaluation.json"])
    for value in (execution, spec, result, run_spec, status, evaluation):
        _private_strings(value)
    if set(result) - {
        "schema_version", "state", "run_spec_sha256", "host", "error",
        "experiment_specification_sha256", "container_wall_seconds", "returncode",
        "native_status", "qualification", "artifacts", "cleanup_error",
    } or set(result.get("host", {})) - {
        "system", "machine", "kernel", "cpu_model", "logical_cpus", "python", "numpy",
    }:
        raise ValueError("Unexpected result metadata is not exportable.")
    if set(run_spec) - {
        "schema_version", "dataset_manifest_sha256", "runtime_image_id", "runtime_version",
        "limits", "runtime_provenance_sha256", "evaluation_protocol_version",
        "qualification_criteria", "evaluator_sha256", "runner_sha256", "input_manifest",
        "experiment_specification_sha256", "rendered_configuration_files", "runtime_identity",
    }:
        raise ValueError("Unexpected execution metadata is not exportable.")
    if (type(result.get("schema_version")) is not int or result["schema_version"] != 1
            or result.get("state") != "complete"
            or result.get("error") is not None or result.get("returncode") != 0
            or result.get("experiment_specification_sha256") != spec_hash
            or result.get("run_spec_sha256") != _canonical_hash(run_spec)
            or result.get("native_status") != status
            or status.get("state") != "complete" or status.get("error") is not None
            or type(status.get("schema_version")) is not int or status["schema_version"] != 1
            or type(evaluation.get("schema_version")) is not int or evaluation["schema_version"] != 1):
        raise ValueError("Exported result is not a consistent completed execution.")
    if (type(run_spec.get("schema_version")) is not int or run_spec["schema_version"] != 1
            or run_spec.get("experiment_specification_sha256") != spec_hash
            or run_spec.get("dataset_manifest_sha256") != spec["dataset"]["prepared_manifest_sha256"]
            or run_spec.get("evaluator_sha256") != spec["evaluation"]["evaluator_sha256"]
            or run_spec.get("evaluation_protocol_version") != 1
            or run_spec.get("rendered_configuration_files") != spec["configuration"]["rendered_files"]
            or run_spec.get("runtime_image_id") not in {CONFIGURATION_ID, OCI_MANIFEST_ID}
            or run_spec.get("runtime_version") != spec["runtime"]["version"]
            or run_spec.get("runtime_provenance_sha256") != spec["runtime"]["provenance_sha256"]
            or run_spec.get("limits") != spec["resources"]
            or run_spec.get("qualification_criteria") != spec["evaluation"]["qualification_criteria"]
            or type(run_spec.get("input_manifest")) is not dict
            or identity(_json_bytes(run_spec["input_manifest"]))["sha256"] != spec["dataset"]["prepared_manifest_sha256"]
            or run_spec.get("runner_sha256") != identity(files["source/src/meridian/vio_reference.py"])["sha256"]):
        raise ValueError("Exported execution does not bind the pinned protocol.")
    for name in REQUIRED_ARTIFACTS:
        if name != "result.json" and result.get("artifacts", {}).get(name) != identity(files[f"artifacts/{name}"]):
            raise ValueError("Exported artifact differs from the original result manifest.")
    for name, recorded in spec["configuration"]["rendered_files"].items():
        if identity(files[f"artifacts/config-{name}"]) != recorded:
            raise ValueError("Exported effective configuration differs from the specification.")
    for name, recorded in spec["configuration"]["baseline_files"].items():
        if identity(files[f"source/integrations/openvins/config/{name}"]) != recorded:
            raise ValueError("Exported baseline configuration differs from the specification.")
    for name, digest in spec["runtime"]["provenance_sha256"].items():
        if identity(files[f"artifacts/{name}"])["sha256"] != digest:
            raise ValueError("Exported runtime provenance differs from the specification.")
    for name, digest in (
        ("src/meridian/vio_evaluation.py", spec["evaluation"]["evaluator_sha256"]),
        ("integrations/openvins/runner.cpp", spec["estimator"]["adapter_sha256"]),
    ):
        if identity(files[f"source/{name}"])["sha256"] != digest:
            raise ValueError("Exported numerical source differs from the specification.")


def build_export(specification: dict, *, artifacts: dict[str, bytes],
                 publication_manifest: dict, source_files: dict[str, bytes],
                 execution: dict) -> bytes:
    """Build a deterministic ZIP from already authorized, descriptor-read inputs.

    The API owns access and retention checks. Only the named successful-result
    files are accepted here. Raw logs, images, investigation/IMU data, account and
    free-text workspace fields are deliberately outside the archive contract.
    """
    validate_specification(specification)
    if set(artifacts) != set(REQUIRED_ARTIFACTS) or set(source_files) != set(SOURCE_FILES):
        raise ValueError("Missing or unexpected export inputs.")
    if (type(publication_manifest) is not dict
            or publication_manifest.get("schema_version") != 1
            or type(publication_manifest.get("files")) is not dict):
        raise ValueError("Unsupported published artifact manifest.")
    for name, content in artifacts.items():
        if publication_manifest["files"].get(name) != identity(content):
            raise ValueError("Published artifact failed its integrity check.")
    if strict_json(artifacts["experiment-specification.json"]) != specification:
        raise ValueError("Published specification differs from the selected execution.")
    files = {**{f"artifacts/{name}": content for name, content in artifacts.items()},
             **{f"source/{name}": content for name, content in source_files.items()},
             "reproduce.py": _LAUNCHER}
    manifest = {
        "format": EXPORT_FORMAT, "schema_version": EXPORT_SCHEMA_VERSION,
        "execution": execution, "specification_sha256": specification_sha256(specification),
        "tolerances": TOLERANCES,
        "distribution": {"kind": "source_and_result_evidence", "native_binaries_included": False,
                         "dataset_samples_included": False, "runtime_rebuild": "explicit_new_runtime_identity"},
        "files": {name: identity(content) for name, content in sorted(files.items())},
    }
    _validate_payload(files, manifest)
    files["export-manifest.json"] = _json_bytes(manifest)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, content in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content, compresslevel=6)
    result = output.getvalue()
    if len(result) > MAX_EXPORT_BYTES:
        raise ValueError("Export exceeds its compressed size limit.")
    return result


def verify_export(archive: bytes | Path) -> dict:
    """Read a bounded ZIP without extraction and reject ambiguous/unsafe members."""
    if isinstance(archive, Path):
        if archive.is_symlink() or not archive.is_file() or archive.stat().st_size > MAX_EXPORT_BYTES:
            raise ValueError("Export must be a bounded regular file.")
        archive = archive.read_bytes()
    if type(archive) is not bytes or len(archive) > MAX_EXPORT_BYTES:
        raise ValueError("Export must be bounded bytes.")
    files = {}
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            infos = zipped.infolist()
            if len(infos) > MAX_FILES:
                raise ValueError("Export has too many members.")
            total = 0
            for info in infos:
                _safe_name(info.filename)
                mode = info.external_attr >> 16
                if (info.filename in files or info.is_dir()
                        or stat.S_IFMT(mode) not in (0, stat.S_IFREG)
                        or info.flag_bits & 1
                        or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                        or info.file_size > MAX_FILE_BYTES):
                    raise ValueError("Unsupported or duplicate export member.")
                total += info.file_size
                if total > MAX_EXPORT_BYTES:
                    raise ValueError("Export exceeds its uncompressed size limit.")
                with zipped.open(info) as stream:
                    content = stream.read(MAX_FILE_BYTES + 1)
                if len(content) != info.file_size or len(content) > MAX_FILE_BYTES:
                    raise ValueError("Invalid export member size.")
                files[info.filename] = content
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as error:
        raise ValueError("Invalid export ZIP.") from error
    if "export-manifest.json" not in files:
        raise ValueError("Export manifest is missing.")
    manifest = strict_json(files.pop("export-manifest.json"))
    _validate_payload(files, manifest)
    return manifest


def verify_directory(directory: Path) -> dict:
    """Verify an already extracted directory; never follow links or execute code."""
    root = Path(directory)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Export directory must be a regular directory.")
    files = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("Export directory must not contain symlinks.")
        if path.is_dir():
            continue
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("Invalid extracted export file.")
        name = path.relative_to(root).as_posix()
        _safe_name(name)
        if len(files) >= MAX_FILES or sum(map(len, files.values())) + path.stat().st_size > MAX_EXPORT_BYTES:
            raise ValueError("Extracted export exceeds its limits.")
        files[name] = path.read_bytes()
    if "export-manifest.json" not in files:
        raise ValueError("Export manifest is missing.")
    manifest = strict_json(files.pop("export-manifest.json"))
    _validate_payload(files, manifest)
    return manifest
