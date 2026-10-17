"""Qualify a pinned VIO runtime on a prepared, sensor/reference-separated recording."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import subprocess
from tempfile import TemporaryDirectory
import time
import uuid

import numpy as np

from meridian.vio_dataset import (
    camera_timestamps_ns, download_dataset, prepare_dataset, verify_prepared,
)
from meridian.vio_evaluation import evaluate_trajectory, read_trajectory
from meridian.vio_specification import specification_sha256, validate_specification


UPSTREAM_REVISION = "69488123ed9362dd44b6f28e7f4680abbff1442b"
GATES = {
    "minimum_evaluated_samples": 100,
    "minimum_coverage_fraction": 0.90,
    "minimum_evaluated_fraction": 0.90,
    "minimum_reference_coverage_fraction": 0.95,
    "maximum_initialization_delay_s": 10.0,
    "maximum_ate_rmse_m": 0.5,
    "maximum_orientation_rmse_deg": 15.0,
}
REPEAT_POSITION_TOLERANCE_M = 0.001
REPEAT_ORIENTATION_TOLERANCE_DEG = 0.001


def _json(path: Path, value: dict) -> None:
    content = json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _parse_json(text: str) -> dict:
    def invalid_constant(value):
        raise ValueError(f"non-finite JSON value: {value}")

    value = json.loads(text, parse_constant=invalid_constant)
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _image_identity(image: str) -> str:
    if not image or image.startswith("-"):
        raise ValueError("image must be a local Docker image name or content ID")
    result = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", image],
                            check=True, capture_output=True, text=True, timeout=30)
    identity = result.stdout.strip()
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", identity):
        raise ValueError("Docker did not return a single content-addressed image identity")
    return identity


def _limits(cpus: float, memory_mb: int, timeout_s: float) -> dict:
    if not math.isfinite(cpus) or cpus <= 0:
        raise ValueError("cpus must be positive and finite")
    if type(memory_mb) is not int or memory_mb < 128:
        raise ValueError("memory_mb must be an integer of at least 128")
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError("timeout_s must be positive and finite")
    return {"cpus": cpus, "memory_mb": memory_mb, "timeout_s": timeout_s,
            "pids": 128, "tmp_mb": 128, "network": "none", "rootfs": "read-only"}


def _container_options(limits: dict) -> list[str]:
    return ["--rm", "--init", "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--user", f"{os.getuid()}:{os.getgid()}",
            "--cpus", str(limits["cpus"]), "--memory", f'{limits["memory_mb"]}m',
            "--memory-swap", f'{limits["memory_mb"]}m', "--pids-limit", str(limits["pids"]),
            "--tmpfs", f'/tmp:rw,noexec,nosuid,size={limits["tmp_mb"]}m',
            "--env", "OMP_NUM_THREADS=1", "--env", "OPENBLAS_NUM_THREADS=1"]


def _mount(source: Path, destination: str, *, readonly: bool) -> list[str]:
    if "," in str(source) or "\n" in str(source):
        raise ValueError("bind mount paths must not contain commas or newlines")
    return ["--mount", f"type=bind,src={source},dst={destination}" +
            (",readonly" if readonly else "")]


def container_command(prepared: Path, output: Path, image_id: str, name: str,
                      limits: dict, *, configuration: Path | None = None) -> list[str]:
    """Only sensor/config directories enter the runtime; the reference never does."""
    return (["docker", "run", "--name", name, *_container_options(limits)]
            + _mount(prepared / "sensors", "/sensors", readonly=True)
            + _mount(configuration if configuration is not None else prepared / "config", "/config", readonly=True)
            + _mount(output, "/output", readonly=False)
            + [image_id, "--dataset", "/sensors", "--config",
               "/config/estimator_config.yaml", "--output", "/output"])


def _runtime_version(image_id: str, limits: dict) -> dict:
    result = subprocess.run(["docker", "run", *_container_options(limits), image_id, "--version"],
                            check=True, capture_output=True, text=True, timeout=30)
    version = _parse_json(result.stdout)
    if (not isinstance(version, dict) or type(version.get("protocol_version")) is not int
            or version["protocol_version"] != 1
            or version.get("upstream_revision") != UPSTREAM_REVISION):
        raise ValueError("runtime does not implement the pinned VIO adapter protocol")
    return version


def _runtime_provenance(image_id: str, limits: dict) -> dict[str, str]:
    provenance = {}
    for name in ("dependency-versions.txt", "binary-sha256.txt"):
        result = subprocess.run(
            ["docker", "run", *_container_options(limits), "--entrypoint", "/bin/cat",
             image_id, f"/opt/meridian/{name}"],
            check=True, capture_output=True, text=True, timeout=30,
        )
        if not result.stdout.strip():
            raise ValueError("runtime provenance is missing")
        provenance[name] = result.stdout
    return provenance


def _host() -> dict:
    model = "unknown"
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        for line in cpuinfo.read_text().splitlines():
            if line.startswith("model name"):
                model = line.split(":", 1)[1].strip()
                break
    return {"system": platform.system(), "machine": platform.machine(),
            "kernel": platform.release(), "cpu_model": model,
            "logical_cpus": os.cpu_count(), "python": platform.python_version(),
            "numpy": np.__version__}


def qualification(evaluation: dict) -> dict:
    """Apply the integration criteria declared before the first recording replay."""
    def at_least(value, bound):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= bound

    def at_most(value, bound):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= bound

    metrics = evaluation.get("metrics") or {}
    checks = {
        "evaluation_ok": evaluation.get("status") == "ok",
        "evaluated_samples": at_least(evaluation.get("counts", {}).get("evaluated_camera_samples"), GATES["minimum_evaluated_samples"]),
        "estimate_coverage": at_least(evaluation.get("coverage_fraction"), GATES["minimum_coverage_fraction"]),
        "evaluated_coverage": at_least(evaluation.get("evaluated_fraction"), GATES["minimum_evaluated_fraction"]),
        "reference_coverage": at_least(evaluation.get("reference_coverage_fraction"), GATES["minimum_reference_coverage_fraction"]),
        "initialization_delay": at_most(evaluation.get("initialization_delay_s"), GATES["maximum_initialization_delay_s"]),
        "position_error": at_most(metrics.get("ate_rmse_m"), GATES["maximum_ate_rmse_m"]),
        "orientation_error": at_most(metrics.get("orientation_rmse_deg"), GATES["maximum_orientation_rmse_deg"]),
    }
    return {"passed": all(checks.values()), "criteria": GATES, "checks": checks}


def _validate_native_status(status: dict, trajectory) -> None:
    if (not isinstance(status, dict) or type(status.get("schema_version")) is not int
            or status["schema_version"] != 1 or status.get("state") != "complete"):
        raise ValueError("native VIO execution did not complete successfully")
    count = len(trajectory.timestamp_ns)
    if (count == 0 or status.get("counts", {}).get("trajectory_states") != count
            or status.get("first_estimate_timestamp_ns") != int(trajectory.timestamp_ns[0])
            or status.get("last_estimate_timestamp_ns") != int(trajectory.timestamp_ns[-1])):
        raise ValueError("native status does not match its trajectory artifact")
    timing = status.get("timing", {})
    for field in ("wall_seconds", "cpu_seconds", "peak_rss_kib"):
        value = timing.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError("native resource measurements are missing or invalid")


def _artifacts(output: Path, *, strict: bool = True) -> dict:
    artifacts = {}
    for path in sorted(output.iterdir()):
        if path.is_symlink() or not path.is_file():
            if strict:
                raise ValueError("runtime artifacts must be regular files without symlinks")
            continue
        if path.name != "result.json":
            artifacts[path.name] = {"sha256": _sha(path), "size_bytes": path.stat().st_size}
    return artifacts


def _load_specification(path: Path) -> dict:
    """Read an immutable specification without ambiguous duplicate JSON keys."""
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate specification key: {key}")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError(f"non-finite specification value: {value}")

    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"),
                           object_pairs_hook=unique_object, parse_constant=invalid_constant)
    except RecursionError as exc:
        raise ValueError("specification JSON is too deeply nested") from exc
    validate_specification(value)
    return value


def _validate_experiment_inputs(experiment: dict, prepared: Path, manifest: dict,
                                expected: list[int], image_id: str, version: dict,
                                provenance: dict, limits: dict) -> None:
    """Bind the validated profile to actual local inputs before creating output."""
    dataset, interval = experiment["dataset"], experiment["interval"]
    if (_sha(prepared / "manifest.json") != dataset["prepared_manifest_sha256"]
            or manifest["dataset_id"] != dataset["prepared_dataset_id"]
            or manifest["source"]["sha256"] != dataset["archive_sha256"]
            or manifest["files"]["reference/trajectory.csv"]["sha256"] != dataset["reference_sha256"]):
        raise ValueError("prepared dataset does not match the immutable specification")
    if (len(expected) != interval["camera_pairs"]
            or str(expected[0]) != interval["first_camera_timestamp_ns"]
            or str(expected[-1]) != interval["last_camera_timestamp_ns"]
            or manifest["streams"]["imu0"]["samples"] != interval["imu_samples"]):
        raise ValueError("camera/IMU timeline does not match the full-recording specification")
    configuration = {name.removeprefix("config/"): identity for name, identity in manifest["files"].items()
                     if name.startswith("config/")}
    if configuration != experiment["configuration"]["baseline_files"]:
        raise ValueError("baseline configuration does not match the immutable specification")
    if (image_id != experiment["runtime"]["image_id"]
            or _digest(version) != _digest(experiment["runtime"]["version"])):
        raise ValueError("runtime image or native adapter differs from the immutable specification")
    provenance_hashes = {name: hashlib.sha256(value.encode()).hexdigest() for name, value in provenance.items()}
    if provenance_hashes != experiment["runtime"]["provenance_sha256"]:
        raise ValueError("runtime binary/dependency provenance differs from the immutable specification")
    if _digest(limits) != _digest(experiment["resources"]):
        raise ValueError("resource overrides differ from the immutable specification")
    if (_sha(Path(__file__).with_name("vio_evaluation.py")) != experiment["evaluation"]["evaluator_sha256"]
            or _digest(GATES) != _digest(experiment["evaluation"]["qualification_criteria"])):
        raise ValueError("evaluator or qualification criteria differ from the immutable specification")


def _verify_configuration(directory: Path, identities: dict, *, prefix: str = "") -> None:
    for name, identity in identities.items():
        path = directory / (prefix + name)
        if (path.is_symlink() or not path.is_file() or path.stat().st_size != identity["size_bytes"]
                or _sha(path) != identity["sha256"]):
            raise ValueError("effective configuration differs from the immutable specification")


def _render_configuration(source: Path, destination: Path, experiment: dict) -> None:
    """Change only the exact num_pts line, with hashes checked before and after."""
    configuration = experiment["configuration"]
    _verify_configuration(source, configuration["baseline_files"])
    for name in configuration["baseline_files"]:
        content = (source / name).read_bytes()
        if name == "estimator_config.yaml":
            original = b"num_pts: 200\n"
            if content.count(original) != 1:
                raise ValueError("pinned baseline must contain exactly one num_pts line")
            content = content.replace(original, f'num_pts: {experiment["parameters"]["num_pts"]}\n'.encode("ascii"))
        (destination / name).write_bytes(content)
    _verify_configuration(destination, configuration["rendered_files"])


def run_reference(prepared: Path, image: str, output: Path, *, cpus: float = 2.0,
                  memory_mb: int = 4096, timeout_s: float = 900.0,
                  specification: dict | None = None) -> dict:
    """Run one bounded local qualification attempt and retain its actual outcome."""
    experiment = None
    if specification is not None:
        validate_specification(specification)
        experiment = deepcopy(specification)
    if Path(output).exists() or Path(output).is_symlink():
        raise FileExistsError(f"output directory already exists: {output}")
    prepared = Path(prepared).resolve(strict=True)
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError(f"output directory already exists: {output}")
    if output.is_relative_to(prepared):
        raise ValueError("execution output must be outside the prepared dataset")
    limits = _limits(cpus, memory_mb, timeout_s)
    if experiment is not None and _digest(limits) != _digest(experiment["resources"]):
        raise ValueError("resource overrides differ from the immutable specification")
    manifest = verify_prepared(prepared)
    expected = camera_timestamps_ns(prepared)
    if experiment is not None and _sha(prepared / "manifest.json") != experiment["dataset"]["prepared_manifest_sha256"]:
        raise ValueError("prepared dataset does not match the immutable specification")
    image_id = _image_identity(image)
    if experiment is not None and image_id != experiment["runtime"]["image_id"]:
        raise ValueError("runtime image differs from the immutable specification")
    version = _runtime_version(image_id, limits)
    provenance = _runtime_provenance(image_id, limits)
    if experiment is not None:
        _validate_experiment_inputs(experiment, prepared, manifest, expected,
                                    image_id, version, provenance, limits)
    spec = {"schema_version": 1, "dataset_manifest_sha256": _sha(prepared / "manifest.json"),
            "runtime_image_id": image_id, "runtime_version": version, "limits": limits,
            "runtime_provenance_sha256": {key: hashlib.sha256(value.encode()).hexdigest()
                                          for key, value in provenance.items()},
            "evaluation_protocol_version": 1, "qualification_criteria": GATES,
            "evaluator_sha256": _sha(Path(__file__).with_name("vio_evaluation.py")),
            "runner_sha256": _sha(Path(__file__)), "input_manifest": manifest}
    if experiment is not None:
        spec["experiment_specification_sha256"] = specification_sha256(experiment)
        spec["rendered_configuration_files"] = experiment["configuration"]["rendered_files"]
    with ExitStack() as cleanup:
        configuration = None
        if experiment is not None:
            configuration = Path(cleanup.enter_context(TemporaryDirectory(prefix="meridian-vio-config-")))
            _render_configuration(prepared / "config", configuration, experiment)
        return _execute_reference(prepared, output, image_id, limits, provenance, spec,
                                  expected, configuration=configuration, experiment=experiment)


def _execute_reference(prepared: Path, output: Path, image_id: str, limits: dict,
                       provenance: dict, spec: dict, expected: list[int], *,
                       configuration: Path | None = None, experiment: dict | None = None) -> dict:
    output.mkdir(parents=True)
    _json(output / "run-spec.json", spec)
    if experiment is not None:
        _json(output / "experiment-specification.json", experiment)
        for filename in experiment["configuration"]["rendered_files"]:
            (output / ("config-" + filename)).write_bytes((configuration / filename).read_bytes())
    for name, content in provenance.items():
        (output / name).write_text(content, encoding="utf-8")
    name = f"meridian-vio-{uuid.uuid4().hex}"
    command = container_command(prepared, output, image_id, name, limits, configuration=configuration)
    result = {"schema_version": 1, "state": "failed", "run_spec_sha256": _digest(spec),
              "host": _host(), "error": None}
    if experiment is not None:
        result["experiment_specification_sha256"] = spec["experiment_specification_sha256"]
    started = time.monotonic()
    try:
        with (output / "runner.log").open("wb") as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            try:
                returncode = process.wait(timeout=limits["timeout_s"])
            except BaseException:
                try:
                    stopped = subprocess.run(["docker", "kill", name], capture_output=True, timeout=30)
                    if stopped.returncode != 0:
                        result["kill_error"] = f"docker exit {stopped.returncode}"
                except (OSError, subprocess.SubprocessError) as kill_error:
                    result["kill_error"] = type(kill_error).__name__
                try:
                    process.wait(timeout=30)
                except (OSError, subprocess.SubprocessError) as reap_error:
                    result["reap_error"] = type(reap_error).__name__
                    try:
                        process.kill()
                        process.wait(timeout=5)
                    except (OSError, subprocess.SubprocessError):
                        pass
                raise
        result["container_wall_seconds"] = time.monotonic() - started
        result["returncode"] = returncode
        _artifacts(output)
        status = _parse_json((output / "status.json").read_text())
        result["native_status"] = status
        if returncode != 0:
            raise ValueError("native VIO execution did not complete successfully; inspect status.json and runner.log")
        _validate_native_status(status, read_trajectory(output / "trajectory.csv"))
        verify_prepared(prepared)
        if _sha(prepared / "manifest.json") != spec["dataset_manifest_sha256"]:
            raise ValueError("prepared inputs changed during execution")
        evaluation_options = {}
        if experiment is not None:
            recorded = _load_specification(output / "experiment-specification.json")
            if specification_sha256(recorded) != spec["experiment_specification_sha256"]:
                raise ValueError("experiment specification changed during execution")
            if _digest(_parse_json((output / "run-spec.json").read_text())) != result["run_spec_sha256"]:
                raise ValueError("run specification changed during execution")
            _verify_configuration(configuration, experiment["configuration"]["rendered_files"])
            _verify_configuration(output, experiment["configuration"]["rendered_files"], prefix="config-")
            evaluation_options = {
                "max_reference_gap_ns": experiment["evaluation"]["maximum_reference_bracket_ns"],
                "max_estimate_timestamp_error_ns": experiment["evaluation"]["maximum_estimate_timestamp_error_ns"],
            }
        evaluation = evaluate_trajectory(output / "trajectory.csv", prepared / "reference" / "trajectory.csv",
                                         expected_timestamps_ns=expected, **evaluation_options)
        _json(output / "evaluation.json", evaluation)
        result["qualification"] = qualification(evaluation)
        result["state"] = "complete"
    except BaseException as exc:
        result["container_wall_seconds"] = time.monotonic() - started
        result["error"] = {"type": type(exc).__name__, "message": str(exc)}
        raise
    finally:
        try:
            subprocess.run(["docker", "rm", "--force", name], capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as cleanup_error:
            result["cleanup_error"] = type(cleanup_error).__name__
        result["artifacts"] = _artifacts(output, strict=False)
        _json(output / "result.json", result)
    return result


def compare_repeats(directories: list[Path]) -> dict:
    """Compare actual raw states without refitting a different gauge per repeat."""
    if len(directories) < 2:
        raise ValueError("repeatability needs at least two completed runs")
    runs = [_parse_json((Path(p) / "result.json").read_text()) for p in directories]
    if any(r.get("state") != "complete" for r in runs):
        raise ValueError("all repeats must be completed runs")
    for directory, result in zip(directories, runs):
        directory = Path(directory)
        if _digest(_parse_json((directory / "run-spec.json").read_text())) != result["run_spec_sha256"]:
            raise ValueError("run specification no longer matches the completed result")
        if not {"trajectory.csv", "status.json", "evaluation.json", "run-spec.json"} <= set(result["artifacts"]):
            raise ValueError("completed run is missing required artifact identities")
        for name, identity in result["artifacts"].items():
            path = directory / name
            if (Path(name).name != name or path.is_symlink() or not path.is_file()
                    or path.stat().st_size != identity["size_bytes"] or _sha(path) != identity["sha256"]):
                raise ValueError("completed run artifact changed after publication")
    if any(r["run_spec_sha256"] != runs[0]["run_spec_sha256"] for r in runs):
        raise ValueError("repeats must have identical run specifications")
    paths = [Path(p) / "trajectory.csv" for p in directories]
    traces = [read_trajectory(p) for p in paths]
    baseline = traces[0]
    comparisons = []
    for index, trace in enumerate(traces[1:], start=1):
        identical_times = np.array_equal(baseline.timestamp_ns, trace.timestamp_ns)
        position = orientation = None
        if identical_times:
            position = float(np.max(np.linalg.norm(trace.position_m - baseline.position_m, axis=1)))
            dots = np.abs(np.sum(trace.quaternion_xyzw * baseline.quaternion_xyzw, axis=1))
            orientation = float(np.max(np.degrees(2 * np.arccos(np.clip(dots, 0, 1)))))
        comparisons.append({"repeat_index": index, "identical_timestamps": identical_times,
                            "trajectory_bytes_identical": _sha(paths[0]) == _sha(paths[index]),
                            "maximum_position_difference_m": position,
                            "maximum_orientation_difference_deg": orientation,
                            "passed": bool(identical_times and position <= REPEAT_POSITION_TOLERANCE_M
                                           and orientation <= REPEAT_ORIENTATION_TOLERANCE_DEG)})
    return {"schema_version": 1, "run_spec_sha256": runs[0]["run_spec_sha256"],
            "run_count": len(runs), "position_tolerance_m": REPEAT_POSITION_TOLERANCE_M,
            "orientation_tolerance_deg": REPEAT_ORIENTATION_TOLERANCE_DEG,
            "comparisons": comparisons, "passed": all(c["passed"] for c in comparisons)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    download = commands.add_parser("download", help="download the pinned room1 archive")
    download.add_argument("--output", type=Path, required=True)
    prepare = commands.add_parser("prepare", help="verify and separate sensor/reference inputs")
    prepare.add_argument("--archive", type=Path, required=True)
    prepare.add_argument("--config", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    run = commands.add_parser("run", help="execute and evaluate one isolated reference run")
    run.add_argument("--prepared", type=Path, required=True)
    run.add_argument("--image", default="meridian-openvins:reference")
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--cpus", type=float, default=2.0)
    run.add_argument("--memory-mb", type=int, default=4096)
    run.add_argument("--timeout-s", type=float, default=900.0)
    run.add_argument("--specification", type=Path,
                     help="consume an exact pinned experiment specification; conflicting resource/image overrides fail")
    repeats = commands.add_parser("compare-repeats", help="check completed runs for reproducibility")
    repeats.add_argument("--runs", type=Path, nargs="+", required=True)
    repeats.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "download":
            result = download_dataset(args.output)
        elif args.command == "prepare":
            result = prepare_dataset(args.archive, args.output, args.config)
        elif args.command == "run":
            result = run_reference(args.prepared, args.image, args.output, cpus=args.cpus,
                                   memory_mb=args.memory_mb, timeout_s=args.timeout_s,
                                   specification=_load_specification(args.specification) if args.specification else None)
        else:
            if args.output.exists():
                raise FileExistsError(f"output already exists: {args.output}")
            result = compare_repeats(args.runs)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            _json(args.output, result)
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
        return 0 if result.get("qualification", result).get("passed", True) else 1
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
