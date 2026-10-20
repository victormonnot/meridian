"""Reproduce the room1 import regression using already-acquired pinned inputs."""

import argparse
import csv
import hashlib
import json
from pathlib import Path

from meridian.sensor_contract import contract_sha256
from meridian.sensor_import import prepare_package
from meridian.sensor_package import file_identity
from meridian.sensor_tumvi import adapt_tumvi
from meridian.vio_dataset import DATASET_ID, ROOM1, verify_prepared


def rows(path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.reader(handle))[1:]


def tree_identity(root):
    return {path.relative_to(root).as_posix(): file_identity(path)
            for path in sorted(root.rglob("*")) if path.is_file()}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def run(archive, legacy, output):
    archive_identity = file_identity(archive)
    require(archive_identity == {key: ROOM1[key] for key in ("sha256", "size_bytes")},
            "Source must be the frozen room1 archive.")
    frozen = verify_prepared(legacy)
    require(frozen["dataset_id"] == DATASET_ID, "Frozen preparation must be room1.")
    before = file_identity(legacy / "manifest.json")
    output.mkdir(parents=True, exist_ok=False)
    acquisitions, preparations, imported_trees, prepared_trees = [], [], [], []
    for name in ("a", "b"):
        print(f"Import {name}: verify original archive, CSV and all PNG payloads.", flush=True)
        acquisition = adapt_tumvi(archive, output / f"acquisition-{name}",
                                  acquisition_id="tumvi-room1-regression", sequence="room1")
        print(f"Preparation {name}: preserve PNG16 bytes and SI measurements.", flush=True)
        prepared = prepare_package(output / f"acquisition-{name}", output / f"prepared-{name}")
        acquisitions.append(acquisition)
        preparations.append(prepared)
        imported_trees.append(tree_identity(output / f"acquisition-{name}"))
        prepared_trees.append(tree_identity(output / f"prepared-{name}"))
    require(imported_trees[0] == imported_trees[1], "Repeated imports differ.")
    require(prepared_trees[0] == prepared_trees[1], "Repeated preparations differ.")
    require(preparations[0] == preparations[1], "Prepared semantic identities differ.")
    prepared_root, package_root = output / "prepared-a", output / "acquisition-a"
    comparisons = {}
    for camera in ("cam0", "cam1"):
        original = rows(legacy / f"sensors/mav0/{camera}/data.csv")
        imported = rows(package_root / f"sensors/{camera}/data.csv")
        prepared = rows(prepared_root / f"sensors/{camera}/data.csv")
        require(len(original) == len(imported) == len(prepared), "Camera counts differ.")
        for old, new, final in zip(original, imported, prepared, strict=True):
            require(old[0] == new[2] == final[2], "Camera timestamps differ.")
            expected = frozen["files"][f"sensors/mav0/{camera}/data/{old[1]}"]
            require(file_identity(package_root / f"sensors/{camera}" / new[3]) == expected,
                    "Imported PNG differs from frozen input.")
            require(file_identity(prepared_root / f"sensors/{camera}" / final[3]) == expected,
                    "Prepared PNG differs from frozen input.")
        comparisons[camera] = {"samples": len(original), "timestamps_identical": True,
                               "all_png_bytes_identical": True}
    imu = rows(legacy / "sensors/mav0/imu0/data.csv")
    for root in (package_root, prepared_root):
        require([row[2:] for row in rows(root / "sensors/imu0/data.csv")] == imu,
                "IMU timestamps or decimal observations differ.")
    reference = rows(legacy / "reference/trajectory.csv")
    for root in (package_root, prepared_root):
        require([row[2:] for row in rows(root / "references/mocap0/data.csv")] == reference,
                "Reference timestamp/position/quaternion decimal values differ.")
    require(file_identity(archive) == archive_identity, "Original archive changed.")
    require(file_identity(legacy / "manifest.json") == before, "Legacy manifest changed.")
    # Reverify all legacy payload hashes, not just the unchanged manifest.
    require(verify_prepared(legacy) == frozen, "Legacy preparation changed.")
    report = {
        "schema_version": 1, "status": "passed", "scope": "import_and_preparation_only",
        "source": archive_identity, "legacy_manifest": before,
        "acquisition_sha256": contract_sha256(acquisitions[0]),
        "prepared_input_sha256": contract_sha256(preparations[0]["prepared_input"]),
        "repeat_import_files": len(imported_trees[0]),
        "repeat_import_inventory_sha256": digest(imported_trees[0]),
        "repeat_preparation_files": len(prepared_trees[0]),
        "repeat_preparation_inventory_sha256": digest(prepared_trees[0]),
        "imports_byte_identical": True, "preparations_byte_identical": True,
        "frozen_comparisons": {**comparisons,
                               "imu": {"samples": len(imu), "all_decimal_values_identical": True},
                               "reference": {"samples": len(reference), "all_decimal_values_identical": True}},
        "source_and_legacy_unchanged": True, "estimator_executed": False,
        "execution_readiness": "not_assessed",
    }
    (output / "regression.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("legacy", type=Path)
    parser.add_argument("output", type=Path)
    options = parser.parse_args()
    run(options.archive, options.legacy, options.output)
