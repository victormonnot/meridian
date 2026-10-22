"""Immutable calibration revisions retain provenance inside a bounded sandbox."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from meridian import sensor_hosted_revision as hosted
from meridian.sensor_contract import contract_sha256
from meridian.sensor_package import validate_package


EXAMPLES = Path(__file__).parents[1] / "examples/sensor-contracts"


def fixture(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    acquisition = json.loads((EXAMPLES / "replay-acquisition.json").read_bytes())
    for entry in acquisition["files"]:
        target = source / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EXAMPLES / entry["path"], target)
    (source / "acquisition.json").write_text(json.dumps(acquisition))
    request = {
        "prepared_id": "prepared-hosted-revision",
        "source_identity": {"id": acquisition["id"], "sha256": contract_sha256(acquisition)},
        "calibration": (EXAMPLES / "calibration.json").read_text(),
        "clock_mappings": [],
        "dependency_acquisitions": [(EXAMPLES / "stereo-no-reference.json").read_text()],
        "apply_imu_intrinsics": False, "imu_target_frame": None,
        "imu_stream_id": None, "include_reference": True,
    }
    return source, request


def test_immutable_supplied_revision_is_reproducible_and_fully_inventoried(tmp_path):
    source, request = fixture(tmp_path)
    original = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    a = hosted.run_revision(source, tmp_path / "a", request)
    b = hosted.run_revision(source, tmp_path / "b", request)
    assert a == b and a["state"] == "ready"
    assert a["prepared_input"]["calibration"] is not None
    assert a["result"]["execution_readiness"] == "not_assessed"
    assert {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()} == original
    actual = {p.relative_to(tmp_path / "a/result").as_posix()
              for p in (tmp_path / "a/result").rglob("*") if p.is_file()}
    assert actual == set(a["manifest"]["files"])
    for name, expected in a["manifest"]["files"].items():
        content = (tmp_path / "a/result" / name).read_bytes()
        assert content == (tmp_path / "b/result" / name).read_bytes()
        assert expected == {"size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    with pytest.raises(FileExistsError):
        hosted.run_revision(source, tmp_path / "a", request)
    validate_package(source)


@pytest.mark.parametrize("change", ["duplicate_key", "wrong_source", "unsupported_option", "large_dependency"])
def test_rejected_request_never_publishes_output(tmp_path, change):
    source, request = fixture(tmp_path)
    if change == "duplicate_key":
        request["calibration"] = request["calibration"].replace('{', '{"id":"duplicate",', 1)
    elif change == "wrong_source":
        request["source_identity"]["sha256"] = "0" * 64
    elif change == "unsupported_option":
        request["apply_imu_intrinsics"] = 1
    else:
        request["dependency_acquisitions"] = [" " * hosted.MAX_REQUEST_BYTES]
    with pytest.raises(ValueError):
        hosted.run_revision(source, tmp_path / "rejected", request)
    assert not (tmp_path / "rejected").exists()
    validate_package(source)


def test_source_payload_corruption_cannot_publish_a_revision(tmp_path):
    source, request = fixture(tmp_path)
    payload = next(p for p in source.rglob("*.csv"))
    payload.write_bytes(payload.read_bytes() + b"corrupt\n")
    with pytest.raises(ValueError):
        hosted.run_revision(source, tmp_path / "rejected", request)
    assert not (tmp_path / "rejected/completion.json").exists()


def test_revision_budget_failure_retains_original_and_no_completion(tmp_path, monkeypatch):
    source, request = fixture(tmp_path)
    monkeypatch.setattr(hosted, "LIMITS", replace(hosted.LIMITS, max_total_bytes=100))
    with pytest.raises(ValueError):
        hosted.run_revision(source, tmp_path / "rejected", request)
    assert not (tmp_path / "rejected/completion.json").exists()
    validate_package(source)


def test_inventory_rejects_links_and_accounts_publication_metadata(tmp_path, monkeypatch):
    root = tmp_path / "output"
    root.mkdir()
    (root / "data.csv").write_bytes(b"0,1,2\n")
    monkeypatch.setattr(hosted, "MAX_RESULT_BYTES", 6)
    with pytest.raises(ValueError, match="storage budget"):
        hosted.result_inventory(root)
    monkeypatch.setattr(hosted, "MAX_RESULT_BYTES", 2 * 1024**3)
    (root / "link.csv").symlink_to(root / "data.csv")
    with pytest.raises(ValueError, match="link"):
        hosted.result_inventory(root)


def test_cli_rejection_does_not_disclose_private_source(tmp_path, capsys):
    request = tmp_path / "request.json"
    request.write_text('{}')
    assert hosted.main([str(tmp_path / "confidential-source"), str(tmp_path / "output"), str(request)]) == 2
    output = capsys.readouterr().out
    assert "confidential" not in output and str(tmp_path) not in output
    assert json.loads(output)["code"] == "invalid_package"
