"""Run the frozen, local room2 acceptance experiment without tuning controls."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess

from meridian import vio_dataset as dataset
from meridian import vio_reference as reference
from meridian.vio_runtime_identity import CONFIGURATION_ID, resolve_runtime_identity
from meridian.vio_specification import DATASET_ID, build_specification


RUN_COUNT = 3
PROTOCOL_SHA256 = "3a5e3dde8917c9e407f1517b86637f1b59c6aad5bb6a778791cbbeba4ff1462d"
FROZEN_RUNNER_SHA256 = "d840c4400675a16a6ed22873086a8267f3547b750a5d62fbbf1c03279bd51a09"
FROZEN_EVALUATOR_SHA256 = "539f07a0e3dbabe5fd3b4874b42218db1938e5f3733d8ac3bd9c714900a8cca6"


def _preflight(prepared: Path) -> tuple[dict, dict]:
    """Verify the unchanged numerical implementation, inputs and exact runtime."""
    baseline = build_specification(DATASET_ID, {"num_pts": 200})
    if (reference._sha(Path(reference.__file__)) != FROZEN_RUNNER_SHA256
            or reference._sha(Path(reference.__file__).with_name("vio_evaluation.py"))
            != FROZEN_EVALUATOR_SHA256
            or reference.GATES != baseline["evaluation"]["qualification_criteria"]):
        raise ValueError("Acceptance runner, evaluator or gates differ from the frozen protocol.")
    manifest = dataset.verify_prepared(prepared)
    if manifest["dataset_id"] != dataset.ROOM2_DATASET_ID:
        raise ValueError("Acceptance requires the reserved room2 recording.")
    configuration = {
        name.removeprefix("config/"): identity
        for name, identity in manifest["files"].items() if name.startswith("config/")
    }
    if configuration != baseline["configuration"]["baseline_files"]:
        raise ValueError("Acceptance configuration differs from the frozen 200-point baseline.")
    identity = resolve_runtime_identity(CONFIGURATION_ID, CONFIGURATION_ID, reference._image_identity)
    image_id = identity["execution_image_id"]
    limits = baseline["resources"]
    version = reference._runtime_version(image_id, limits)
    provenance = reference._runtime_provenance(image_id, limits)
    if version != baseline["runtime"]["version"]:
        raise ValueError("Acceptance native version differs from the frozen runtime.")
    if {name: hashlib.sha256(content.encode()).hexdigest()
            for name, content in provenance.items()} != baseline["runtime"]["provenance_sha256"]:
        raise ValueError("Acceptance binary/dependency inventory differs from the frozen runtime.")
    return manifest, identity


def _attempt_record(directory: Path, index: int, error: BaseException | None) -> dict:
    """Summarize actual evidence without copying local exception paths."""
    record = {"index": index, "directory": directory.name, "state": "failed",
              "passed": False, "error_type": type(error).__name__ if error else None}
    result_path = directory / "result.json"
    if not result_path.is_file():
        return record
    result = reference._parse_json(result_path.read_text(encoding="utf-8"))
    record.update({
        "state": result["state"],
        "passed": error is None and result["state"] == "complete"
            and result.get("qualification", {}).get("passed") is True,
        "result_sha256": reference._sha(result_path),
        "run_spec_sha256": result["run_spec_sha256"],
        "qualification": result.get("qualification"),
        "container_wall_seconds": result.get("container_wall_seconds"),
        "native_status": result.get("native_status"),
        "host": result.get("host"),
        "output_bytes": sum(path.stat().st_size for path in directory.iterdir() if path.is_file()),
    })
    if "trajectory.csv" in result.get("artifacts", {}):
        record["trajectory"] = result["artifacts"]["trajectory.csv"]
    evaluation_path = directory / "evaluation.json"
    if evaluation_path.is_file():
        evaluation = reference._parse_json(evaluation_path.read_text(encoding="utf-8"))
        record["evaluation"] = {
            key: evaluation.get(key) for key in (
                "status", "counts", "coverage_fraction", "evaluated_fraction",
                "reference_coverage_fraction", "initialization_delay_s", "metrics",
            )
        }
    return record


def run_acceptance(prepared: Path, output: Path) -> dict:
    """Retain all three baseline attempts; failures are never replaced or tuned."""
    if Path(output).exists() or Path(output).is_symlink():
        raise FileExistsError("Acceptance output already exists.")
    prepared, output = Path(prepared).resolve(strict=True), Path(output).resolve()
    if output.is_relative_to(prepared):
        raise ValueError("Acceptance output must be outside the prepared recording.")
    manifest, runtime_identity = _preflight(prepared)
    output.mkdir(parents=True)
    report = {
        "schema_version": 1,
        "protocol": "room2-acceptance-v1",
        "protocol_source": {"path": "docs/room2-acceptance.md", "sha256": PROTOCOL_SHA256},
        "state": "incomplete",
        "passed": False,
        "planned_run_count": RUN_COUNT,
        "parameters": {"num_pts": 200},
        "dataset_id": manifest["dataset_id"],
        "source": manifest["source"],
        "prepared_manifest_sha256": reference._sha(prepared / "manifest.json"),
        "prepared_bytes": sum(item["size_bytes"] for item in manifest["files"].values()),
        "streams": manifest["streams"],
        "reference_sha256": manifest["files"]["reference/trajectory.csv"]["sha256"],
        "runtime_identity": runtime_identity,
        "runner_sha256": FROZEN_RUNNER_SHA256,
        "evaluator_sha256": FROZEN_EVALUATOR_SHA256,
        "acceptance_runner_sha256": reference._sha(Path(__file__)),
        "qualification_criteria": dict(reference.GATES),
        "resources": reference._limits(2.0, 4096, 900.0),
        "attempts": [],
        "repeatability": None,
        "limitations": [
            "Local reserved-recording evaluation; room2 is not a hosted experiment profile.",
            "Three fixed baseline attempts, with no tuning or result-dependent interval selection.",
            "One host and two handheld indoor sequences do not establish general drone robustness.",
            "No flight, embedded, real-time, covariance-consistency or market-value validation.",
        ],
    }
    try:
        for index in range(1, RUN_COUNT + 1):
            directory = output / f"run-{index}"
            error = None
            try:
                if reference._sha(prepared / "manifest.json") != report["prepared_manifest_sha256"]:
                    raise ValueError("Acceptance prepared identity changed between attempts.")
                reference.run_reference(prepared, runtime_identity["execution_image_id"], directory)
                if reference._sha(prepared / "manifest.json") != report["prepared_manifest_sha256"]:
                    raise ValueError("Acceptance prepared identity changed during an attempt.")
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as failure:
                error = failure
            except BaseException as interruption:
                report["attempts"].append(_attempt_record(directory, index, interruption))
                raise
            report["attempts"].append(_attempt_record(directory, index, error))
            reference._json(output / "acceptance-report.json", report)
        if all(attempt["state"] == "complete" for attempt in report["attempts"]):
            try:
                report["repeatability"] = reference.compare_repeats(
                    [output / f"run-{index}" for index in range(1, RUN_COUNT + 1)])
            except (OSError, ValueError, KeyError) as failure:
                report["repeatability"] = {"passed": False, "error_type": type(failure).__name__}
        report["state"] = "complete"
        report["passed"] = (
            all(attempt["passed"] for attempt in report["attempts"])
            and report["repeatability"] is not None
            and report["repeatability"]["passed"]
        )
        return report
    finally:
        reference._json(output / "acceptance-report.json", report)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    download = commands.add_parser("download", help="download only the pinned reserved room2 archive")
    download.add_argument("--output", required=True, type=Path)
    prepare = commands.add_parser("prepare", help="verify room2 and separate sensor/reference files")
    prepare.add_argument("--archive", required=True, type=Path)
    prepare.add_argument("--config", required=True, type=Path)
    prepare.add_argument("--output", required=True, type=Path)
    run = commands.add_parser("run", help="execute exactly three unchanged baseline attempts")
    run.add_argument("--prepared", required=True, type=Path)
    run.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "download":
            result = dataset.download_dataset(args.output, dataset_id=dataset.ROOM2_DATASET_ID)
        elif args.command == "prepare":
            result = dataset.prepare_dataset(
                args.archive, args.output, args.config, dataset_id=dataset.ROOM2_DATASET_ID)
        else:
            result = run_acceptance(args.prepared, args.output)
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
        return 0 if result.get("passed", True) else 1
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        parser.exit(2, f"error: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
