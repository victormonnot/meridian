"""Independent exported-experiment verification and explicitly identified replay."""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

from meridian.vio_export import (
    TOLERANCES, _canonical_hash, _json_bytes, strict_json, verify_directory, verify_export,
)


def compare_results(original: Path, reproduced: Path) -> dict:
    """Check exact support and direct poses, without fitting a second gauge."""
    import numpy as np
    from meridian.vio_evaluation import read_trajectory

    original, reproduced = Path(original), Path(reproduced)
    before = read_trajectory(original / "trajectory.csv")
    after = read_trajectory(reproduced / "trajectory.csv")
    evaluation_before = strict_json((original / "evaluation.json").read_bytes())
    evaluation_after = strict_json((reproduced / "evaluation.json").read_bytes())
    same_timestamps = np.array_equal(before.timestamp_ns, after.timestamp_ns)
    position = orientation = None
    if same_timestamps:
        position = float(np.max(np.linalg.norm(after.position_m - before.position_m, axis=1)))
        # A relative quaternion angle remains well conditioned near zero. abs(w)
        # makes q and -q equivalent, unlike direct component comparison.
        qa, qb = before.quaternion_xyzw, after.quaternion_xyzw
        vector = (qa[:, 3, None] * qb[:, :3] - qb[:, 3, None] * qa[:, :3]
                  - np.cross(qa[:, :3], qb[:, :3]))
        scalar = np.sum(qa * qb, axis=1)
        orientation = float(np.max(np.degrees(2 * np.arctan2(
            np.linalg.norm(vector, axis=1), np.abs(scalar)))))
    support_keys = (
        "counts", "missing_estimate_intervals", "coverage_fraction", "evaluated_fraction",
        "reference_coverage_fraction", "evaluated_reference_fraction", "initialization_delay_s",
        "unsupported_reference_intervals",
    )
    support = all(key in evaluation_before and key in evaluation_after
                  and evaluation_before[key] == evaluation_after[key] for key in support_keys)
    evaluated_timestamps = (
        evaluation_before.get("per_sample", {}).get("timestamp_ns")
        == evaluation_after.get("per_sample", {}).get("timestamp_ns")
        and isinstance(evaluation_before.get("per_sample", {}).get("timestamp_ns"), list)
        and len(evaluation_before["per_sample"]["timestamp_ns"]) > 0
    )
    metrics = {}
    for key, tolerance in (
        ("ate_rmse_m", TOLERANCES["maximum_ate_rmse_difference_m"]),
        ("orientation_rmse_deg", TOLERANCES["maximum_orientation_rmse_difference_deg"]),
    ):
        first = evaluation_before.get("metrics", {}).get(key)
        second = evaluation_after.get("metrics", {}).get(key)
        valid = (type(first) in (float, int) and type(second) in (float, int)
                 and np.isfinite(first) and np.isfinite(second))
        difference = float(second - first) if valid else None
        metrics[key] = {"original": first, "reproduced": second, "difference": difference,
                        "absolute_tolerance": tolerance,
                        "passed": bool(valid and abs(difference) <= tolerance)}
    checks = {
        "identical_output_timestamps": bool(same_timestamps),
        "identical_evaluation_support": evaluated_timestamps,
        "identical_support_counts_and_missing_intervals": support,
        "direct_position_within_tolerance": position is not None and position <= TOLERANCES["maximum_direct_position_difference_m"],
        "direct_orientation_within_tolerance": orientation is not None and orientation <= TOLERANCES["maximum_direct_orientation_difference_deg"],
        "position_rmse_within_tolerance": metrics["ate_rmse_m"]["passed"],
        "orientation_rmse_within_tolerance": metrics["orientation_rmse_deg"]["passed"],
        "successful_evaluation": evaluation_before.get("status") == evaluation_after.get("status") == "ok",
        "identical_evaluation_protocol": (
            evaluation_before.get("protocol") == evaluation_after.get("protocol")
            and isinstance(evaluation_before.get("protocol"), dict)),
        "unit_scale_alignment": (
            evaluation_before.get("alignment", {}).get("scale")
            == evaluation_after.get("alignment", {}).get("scale") == 1.0),
    }
    return {"schema_version": 1, "passed": all(checks.values()), "checks": checks,
            "tolerances": deepcopy(TOLERANCES), "metrics": metrics,
            "maximum_direct_position_difference_m": position,
            "maximum_direct_orientation_difference_deg": orientation,
            "trajectory_bytes_identical": hashlib.sha256((original / "trajectory.csv").read_bytes()).digest()
                == hashlib.sha256((reproduced / "trajectory.csv").read_bytes()).digest(),
            "limitations": [
                "No new alignment is fitted for the direct-pose comparison.",
                "Each evaluated trajectory uses the unchanged full-recording SE3 protocol.",
                "One reproduction does not establish cross-platform bit identity or statistical confidence.",
                "Host resource times and memory are observations, not acceptance tolerances.",
            ]}


def reproduce(bundle: Path, work: Path, *, image: str, runtime_mode: str,
              dataset_archive: Path | None = None, prepared: Path | None = None) -> dict:
    """Replay an export without an account, API, database or hosted worker.

    A rebuilt image is a new runtime identity. Its source/version/dependency
    provenance is checked, but the immutable original specification is never
    changed or claimed to have executed under that different image.
    """
    manifest = verify_directory(bundle)
    if runtime_mode not in {"exact", "rebuilt"}:
        raise ValueError("Choose the explicit exact or rebuilt runtime mode.")
    if (dataset_archive is None) == (prepared is None):
        raise ValueError("Supply exactly one public dataset archive or prepared dataset.")
    work = Path(work)
    if work.exists() or work.is_symlink():
        raise FileExistsError("Reproduction work directory already exists.")
    bundle = Path(bundle).resolve(strict=True)
    work = work.resolve()
    if work.is_relative_to(bundle):
        raise ValueError("Reproduction output must be outside the immutable export.")
    from meridian.vio_dataset import prepare_dataset, verify_prepared, camera_timestamps_ns
    from meridian.vio_reference import (
        _execute_reference, _image_identity, _limits, _render_configuration,
        _runtime_provenance, _runtime_version, _sha, run_reference,
    )
    work.mkdir(parents=True)
    report = {"schema_version": 1, "state": "failed", "runtime_mode": runtime_mode,
              "export_manifest_sha256": _sha(bundle / "export-manifest.json"),
              "original_specification_sha256": manifest["specification_sha256"],
              "passed": False}
    try:
        spec = strict_json((bundle / "artifacts/experiment-specification.json").read_bytes())
        if dataset_archive is not None:
            prepared = work / "prepared"
            prepare_dataset(dataset_archive, prepared, bundle / "source/integrations/openvins/config")
        prepared = Path(prepared).resolve(strict=True)
        dataset = verify_prepared(prepared)
        if _sha(prepared / "manifest.json") != spec["dataset"]["prepared_manifest_sha256"]:
            raise ValueError("Prepared dataset differs from the original experiment.")
        report["prepared_manifest_sha256"] = _sha(prepared / "manifest.json")
        execution = work / "run"
        if runtime_mode == "exact":
            result = run_reference(prepared, image, execution, specification=spec)
            report["runtime_identity_claim"] = "original_qualified_runtime"
        else:
            limits = _limits(2.0, 4096, 900.0)
            actual_image = _image_identity(image)
            version = _runtime_version(actual_image, limits)
            provenance = _runtime_provenance(actual_image, limits)
            if version != spec["runtime"]["version"]:
                raise ValueError("Rebuilt native version differs from the original source/adapter contract.")
            if hashlib.sha256(provenance["dependency-versions.txt"].encode()).hexdigest() != spec["runtime"]["provenance_sha256"]["dependency-versions.txt"]:
                raise ValueError("Rebuilt installed dependencies differ from the recorded runtime.")
            # The image's distributed adapter sources must be the exported
            # sources, not just a runtime --version string.
            adapter_files = ["runner.cpp", "CMakeLists.txt", "Dockerfile", "LICENSE", "NOTICE.md"]
            for name in adapter_files:
                result = subprocess.run(
                    ["docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
                     "--security-opt", "no-new-privileges", "--user", "65534:65534",
                     "--cpus", "0.5", "--memory", "128m", "--pids-limit", "16",
                     "--entrypoint", "/usr/bin/sha256sum", actual_image,
                     f"/opt/meridian/adapter/{name}"],
                    check=True, capture_output=True, text=True, timeout=30)
                if result.stdout.split()[0] != _sha(bundle / "source/integrations/openvins" / name):
                    raise ValueError("Rebuilt image contains different adapter/build sources.")
            if _sha(Path(__file__).with_name("vio_evaluation.py")) != spec["evaluation"]["evaluator_sha256"]:
                raise ValueError("Reproduction evaluator differs from the original protocol.")
            runtime_provenance = {name: hashlib.sha256(content.encode()).hexdigest()
                                  for name, content in provenance.items()}
            run_spec = {
                "schema_version": 1, "dataset_manifest_sha256": spec["dataset"]["prepared_manifest_sha256"],
                "runtime_image_id": actual_image, "runtime_version": version, "limits": limits,
                "runtime_provenance_sha256": runtime_provenance,
                "evaluation_protocol_version": 1,
                "qualification_criteria": spec["evaluation"]["qualification_criteria"],
                "evaluator_sha256": spec["evaluation"]["evaluator_sha256"],
                "runner_sha256": _sha(Path(__file__).with_name("vio_reference.py")),
                "input_manifest": dataset,
                "source_reproduction": {
                    "original_specification_sha256": manifest["specification_sha256"],
                    "export_manifest_sha256": report["export_manifest_sha256"],
                    "original_runtime_image_id": spec["runtime"]["image_id"],
                    "claim": "rebuilt_runtime_with_checked_sources_and_dependencies",
                    "effective_configuration_files": spec["configuration"]["rendered_files"],
                },
            }
            with TemporaryDirectory(prefix="meridian-reproduction-config-") as configuration:
                configuration = Path(configuration)
                _render_configuration(prepared / "config", configuration, spec)
                result = _execute_reference(
                    prepared, execution, actual_image, limits, provenance, run_spec,
                    camera_timestamps_ns(prepared), configuration=configuration)
                for name in spec["configuration"]["rendered_files"]:
                    (work / ("effective-config-" + name)).write_bytes((configuration / name).read_bytes())
            report["runtime_identity_claim"] = "new_source_rebuilt_runtime_not_original_image"
        verify_prepared(prepared)
        if _sha(prepared / "manifest.json") != spec["dataset"]["prepared_manifest_sha256"]:
            raise ValueError("Prepared dataset changed during reproduction.")
        # Recheck source/evidence after execution as well as before it.
        verify_directory(bundle)
        observed_spec = strict_json((execution / "run-spec.json").read_bytes())
        report.update({
            "state": "complete", "actual_runtime_image_id": observed_spec["runtime_image_id"],
            "actual_runtime_provenance_sha256": observed_spec["runtime_provenance_sha256"],
            "original_runtime_image_id": spec["runtime"]["image_id"],
            "original_runtime_provenance_sha256": spec["runtime"]["provenance_sha256"],
            "runtime_bytes_identity_claimed": runtime_mode == "exact",
            "result_sha256": _sha(execution / "result.json"),
            "comparison": compare_results(bundle / "artifacts", execution),
            "qualification": result["qualification"],
        })
        report["passed"] = report["comparison"]["passed"]
        return report
    except BaseException as error:
        report["error_type"] = type(error).__name__
        # Detailed local exception text may identify local paths; it is emitted
        # to the caller, never copied into a reusable exported evidence report.
        raise
    finally:
        (work / "reproduction-report.json").write_bytes(_json_bytes(report))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify", help="check an archive or extracted bundle without numerical dependencies")
    verify.add_argument("--bundle", required=True, type=Path)
    replay = commands.add_parser("reproduce", help="execute and compare outside the hosted backend")
    replay.add_argument("--bundle", required=True, type=Path)
    replay.add_argument("--work", required=True, type=Path)
    replay.add_argument("--image", required=True)
    replay.add_argument("--runtime-mode", required=True, choices=("exact", "rebuilt"))
    inputs = replay.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--archive", type=Path)
    inputs.add_argument("--prepared", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            manifest = verify_directory(args.bundle) if args.bundle.is_dir() else verify_export(args.bundle)
            result = {"schema_version": 1, "passed": True,
                      "specification_sha256": manifest["specification_sha256"],
                      "file_count": len(manifest["files"])}
        else:
            result = reproduce(args.bundle, args.work, image=args.image,
                               runtime_mode=args.runtime_mode, dataset_archive=args.archive,
                               prepared=args.prepared)
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
        return 0 if result["passed"] else 1
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        parser.exit(2, f"error: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
