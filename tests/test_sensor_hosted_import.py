"""Hosted profile checks using synthetic transfer archives only."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

import pytest

from meridian import sensor_hosted_import as hosted
from meridian.sensor_contract import validate_bundle
from meridian.sensor_import import prepare_package
from meridian.sensor_package import DEFAULT_LIMITS, validate_package


EXAMPLES = Path(__file__).parents[1] / "examples/sensor-contracts"
REQUEST = {"prepared_id": "prepared-fixture", "imu_stream_id": None, "include_reference": True}


def package(tmp_path, name="stereo-no-reference"):
    root = tmp_path / "source"
    root.mkdir()
    descriptor = json.loads((EXAMPLES / (name + ".json")).read_bytes())
    for entry in descriptor["files"]:
        target = root / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EXAMPLES / entry["path"], target)
    (root / "acquisition.json").write_text(json.dumps(descriptor))
    archive = tmp_path / "upload.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                handle.write(path, path.relative_to(root).as_posix())
    return root, archive, descriptor


@pytest.mark.parametrize("name", ["stereo-no-reference", "stereo-partial-reference", "imu-snapshots"])
def test_hosted_import_preserves_original_and_links_every_published_file(tmp_path, name):
    source, archive, descriptor = package(tmp_path, name)
    original = archive.read_bytes()
    a = hosted.run_import(archive, tmp_path / "a", REQUEST)
    b = hosted.run_import(archive, tmp_path / "b", REQUEST)
    assert a == b and archive.read_bytes() == original
    assert a["sha256"] == hashlib.sha256(original).hexdigest()
    assert a["descriptor"] == descriptor and a["state"] == "ready"
    assert a["result"]["execution_readiness"] == "not_assessed"
    prepared = json.loads((tmp_path / "a/result/prepared/prepared-input.json").read_bytes())
    validate_bundle([descriptor, prepared])
    validate_package(tmp_path / "a/result/acquisition", limits=hosted.HOSTED_LIMITS)
    actual = {path.relative_to(tmp_path / "a/result").as_posix()
              for path in (tmp_path / "a/result").rglob("*") if path.is_file()}
    assert actual == set(a["manifest"]["files"])
    assert not any("upload.zip" in name or name.startswith("original/") for name in actual)
    for name, identity in a["manifest"]["files"].items():
        content = (tmp_path / "a/result" / name).read_bytes()
        assert identity == {"sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)}
        assert content == (tmp_path / "b/result" / name).read_bytes()


def test_preparation_output_budget_is_independent_from_valid_input_budget(tmp_path):
    source, _, _ = package(tmp_path)
    validate_package(source)
    output = tmp_path / "prepared"
    small = replace(DEFAULT_LIMITS, max_total_bytes=100)
    with pytest.raises(ValueError, match="byte limits"):
        prepare_package(source, output, limits=DEFAULT_LIMITS, output_limits=small)
    assert not output.exists()
    # The same acquisition is valid and remains intact after derived output refusal.
    validate_package(source)


def test_hosted_profile_checks_row_and_prepared_storage_limits(tmp_path, monkeypatch):
    _, archive, _ = package(tmp_path)
    monkeypatch.setattr(hosted, "HOSTED_LIMITS", replace(hosted.HOSTED_LIMITS, max_csv_rows=1))
    with pytest.raises(ValueError):
        hosted.run_import(archive, tmp_path / "limited", REQUEST)
    assert not (tmp_path / "limited/completion.json").exists()


@pytest.mark.parametrize("change", ["prepared_id", "imu_stream_id", "include_reference", "extra"])
def test_bad_request_never_opens_archive(tmp_path, change):
    request = dict(REQUEST)
    request[change] = "../private" if change != "include_reference" else 1
    with pytest.raises(ValueError, match="request"):
        hosted.run_import(tmp_path / "absent", tmp_path / "out", request)
    assert not (tmp_path / "out").exists()


def test_cli_failure_is_bounded_without_source_paths(tmp_path, capsys):
    source = tmp_path / "confidential-recording.zip"
    source.write_bytes(b"corrupt")
    request = tmp_path / "request.json"
    request.write_text(json.dumps(REQUEST))
    assert hosted.main([str(source), str(tmp_path / "output"), str(request)]) == 2
    content = capsys.readouterr().out
    assert "confidential" not in content and str(tmp_path) not in content
    assert json.loads(content)["code"] == "invalid_package"


def test_inventory_rejects_long_names_links_and_total_expansion(tmp_path, monkeypatch):
    result = tmp_path / "result"
    path = result / "acquisition" / ("a" * 240) / "data.csv"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"data")
    with pytest.raises(ValueError, match="256"):
        hosted.result_inventory(result)
    shutil.rmtree(result)
    result.mkdir()
    (result / "report.json").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError, match="link"):
        hosted.result_inventory(result)
    (result / "report.json").unlink()
    (result / "report.json").write_bytes(b"data")
    monkeypatch.setattr(hosted, "MAX_RESULT_BYTES", 3)
    with pytest.raises(ValueError, match="storage"):
        hosted.result_inventory(result)


def test_hosted_inventory_bounds_escaped_json_path_bytes(tmp_path):
    result = tmp_path / "result"
    path = result / "acquisition" / "originals" / ('"' * 130)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"opaque")
    assert len(path.relative_to(result).as_posix().encode("utf-8")) < 256
    with pytest.raises(ValueError, match="JSON-serialized"):
        hosted.result_inventory(result)
