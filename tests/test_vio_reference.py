"""Exercise isolation, failed execution, qualification, and artifact integrity."""

import csv
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import numpy as np
import pytest

from meridian import vio_reference as reference
from meridian.vio_evaluation import TRAJECTORY_COLUMNS
from meridian.vio_specification import DATASET_ID, build_specification, specification_sha256


def _poses(path, *, offset=0.0):
    times = [1_500_000_000_000_000_000 + i * 50_000_000 for i in range(101)]
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(TRAJECTORY_COLUMNS)
        for i, stamp in enumerate(times):
            a = i / 20
            writer.writerow([stamp, np.cos(a) + offset, np.sin(a), a / 5, 0, 0, 0, 1])
    return times


def _status(times):
    return {"schema_version": 1, "state": "complete", "error": None,
            "counts": {"trajectory_states": len(times)},
            "first_estimate_timestamp_ns": times[0], "last_estimate_timestamp_ns": times[-1],
            "timing": {"wall_seconds": 0.2, "cpu_seconds": 0.3, "peak_rss_kib": 100}}


def _runtime(tmp_path, monkeypatch, *, exitcode=0, timeout=False, corrupt_status=False):
    prepared = tmp_path / "prepared"
    for name in ("sensors", "config", "reference"):
        (prepared / name).mkdir(parents=True)
    times = _poses(prepared / "reference" / "trajectory.csv")
    reference._json(prepared / "manifest.json", {"schema_version": 1})
    monkeypatch.setattr(reference, "verify_prepared", lambda _: {"schema_version": 1})
    monkeypatch.setattr(reference, "camera_timestamps_ns", lambda _: times)
    monkeypatch.setattr(reference, "_image_identity", lambda _: "sha256:" + "a" * 64)
    monkeypatch.setattr(reference, "_runtime_version", lambda *_: {"protocol_version": 1})
    monkeypatch.setattr(reference, "_runtime_provenance", lambda *_: {"dependency-versions.txt": "fixture\t1\n"})
    invocations = []

    class Process:
        def __init__(self, command, **kwargs):
            invocations.append(command)
            mount = next(part for part in command if ",dst=/output" in part)
            directory = Path(mount.removeprefix("type=bind,src=").split(",dst=")[0])
            _poses(directory / "trajectory.csv")
            status = _status(times)
            if corrupt_status:
                status["counts"]["trajectory_states"] -= 1
            reference._json(directory / "status.json", status)
            self.calls = 0

        def wait(self, timeout=None):
            self.calls += 1
            if timeout_flag and self.calls == 1:
                raise subprocess.TimeoutExpired("docker", timeout)
            return exitcode

    timeout_flag = timeout
    monkeypatch.setattr(reference.subprocess, "Popen", Process)
    monkeypatch.setattr(reference.subprocess, "run", lambda command, **kwargs:
                        invocations.append(command) or subprocess.CompletedProcess(command, 0))
    return prepared, invocations


def test_container_mounts_only_sensors_config_and_output(tmp_path):
    prepared, output = tmp_path / "private acquisition", tmp_path / "output"
    limits = reference._limits(2, 4096, 900)
    command = reference.container_command(prepared, output, "sha256:fixture", "run-fixture", limits)
    mounts = [command[i + 1] for i, part in enumerate(command) if part == "--mount"]
    assert mounts == [f"type=bind,src={prepared}/sensors,dst=/sensors,readonly",
                      f"type=bind,src={prepared}/config,dst=/config,readonly",
                      f"type=bind,src={output},dst=/output"]
    assert not any("reference" in mount for mount in mounts)
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--memory") + 1] == "4096m"
    assert "--read-only" in command and "--cap-drop" in command


def test_completed_run_evaluates_independently_and_records_artifacts(tmp_path, monkeypatch):
    prepared, commands = _runtime(tmp_path, monkeypatch)
    output = tmp_path / "run"
    result = reference.run_reference(prepared, "fixture", output)
    assert result["state"] == "complete" and result["qualification"]["passed"]
    assert result["qualification"]["checks"]["evaluated_samples"]
    assert set(result["artifacts"]) == {"trajectory.csv", "status.json", "evaluation.json", "run-spec.json", "runner.log", "dependency-versions.txt"}
    assert json.loads((output / "evaluation.json").read_text())["metrics"]["ate_rmse_m"] < 1e-12
    assert commands[-1][:3] == ["docker", "rm", "--force"]


@pytest.mark.parametrize("options,error", [({"exitcode": 1}, ValueError),
                                        ({"corrupt_status": True}, ValueError),
                                        ({"timeout": True}, subprocess.TimeoutExpired)])
def test_failures_remain_failed_and_cleanup_container(tmp_path, monkeypatch, options, error):
    prepared, commands = _runtime(tmp_path, monkeypatch, **options)
    output = tmp_path / "failed-run"
    with pytest.raises(error):
        reference.run_reference(prepared, "fixture", output)
    result = json.loads((output / "result.json").read_text())
    assert result["state"] == "failed" and result["error"]["type"] == error.__name__
    assert not (output / "evaluation.json").exists()
    assert commands[-1][:3] == ["docker", "rm", "--force"]
    if options.get("timeout"):
        assert any(command[:2] == ["docker", "kill"] for command in commands)


def test_invalid_or_existing_destinations_do_not_start_runtime(tmp_path, monkeypatch):
    prepared, commands = _runtime(tmp_path, monkeypatch)
    with pytest.raises(FileExistsError):
        reference.run_reference(prepared, "fixture", prepared)
    with pytest.raises(ValueError, match="outside"):
        reference.run_reference(prepared, "fixture", prepared / "new-result")
    with pytest.raises(ValueError, match="cpus"):
        reference.run_reference(prepared, "fixture", tmp_path / "new-result", cpus=float("nan"))
    assert commands == []


def test_empty_or_degenerate_evaluation_fails_qualification_without_fabricated_metrics():
    result = reference.qualification({"status": "insufficient_associations", "metrics": None,
                                      "counts": {"evaluated_camera_samples": 0},
                                      "initialization_delay_s": None})
    assert not result["passed"]
    assert not any(result["checks"].values())


def test_low_error_cannot_hide_missing_camera_coverage():
    result = reference.qualification({"status": "ok", "counts": {"evaluated_camera_samples": 100},
                                      "coverage_fraction": 0.2, "evaluated_fraction": 0.2,
                                      "reference_coverage_fraction": 1.0,
                                      "initialization_delay_s": 0,
                                      "metrics": {"ate_rmse_m": 0, "orientation_rmse_deg": 0}})
    assert result["checks"]["position_error"] and not result["passed"]


def test_repeatability_checks_identity_and_detects_artifact_tampering(tmp_path, monkeypatch):
    prepared, _ = _runtime(tmp_path, monkeypatch)
    directories = [tmp_path / "first", tmp_path / "second"]
    for directory in directories:
        reference.run_reference(prepared, "fixture", directory)
    report = reference.compare_repeats(directories)
    assert report["passed"] and report["comparisons"][0]["trajectory_bytes_identical"]
    with (directories[1] / "trajectory.csv").open("a") as stream:
        stream.write("modified after execution\n")
    with pytest.raises(ValueError, match="artifact changed"):
        reference.compare_repeats(directories)


def test_repeatability_rejects_different_resource_protocols(tmp_path, monkeypatch):
    prepared, _ = _runtime(tmp_path, monkeypatch)
    directories = [tmp_path / "first", tmp_path / "second"]
    reference.run_reference(prepared, "fixture", directories[0], cpus=1)
    reference.run_reference(prepared, "fixture", directories[1], cpus=2)
    with pytest.raises(ValueError, match="identical run specifications"):
        reference.compare_repeats(directories)


def test_dangling_output_symlink_is_not_followed(tmp_path, monkeypatch):
    prepared, commands = _runtime(tmp_path, monkeypatch)
    target, link = tmp_path / "unrelated", tmp_path / "requested"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(FileExistsError):
        reference.run_reference(prepared, "fixture", link)
    assert not target.exists() and commands == []


def test_runtime_symlinks_are_not_read_or_followed_when_publishing_failure(tmp_path, monkeypatch):
    prepared, _ = _runtime(tmp_path, monkeypatch)
    output, unrelated = tmp_path / "run", tmp_path / "unrelated.json"
    unrelated.write_text('"unrelated content"')
    original = reference.subprocess.Popen

    def process_with_links(*args, **kwargs):
        process = original(*args, **kwargs)
        (output / "status.json").unlink()
        (output / "status.json").symlink_to(unrelated)
        (output / "result.json").symlink_to(unrelated)
        return process

    monkeypatch.setattr(reference.subprocess, "Popen", process_with_links)
    with pytest.raises(ValueError, match="regular files"):
        reference.run_reference(prepared, "fixture", output)
    result = json.loads((output / "result.json").read_text())
    assert result["state"] == "failed" and "status.json" not in result["artifacts"]
    assert not (output / "result.json").is_symlink()
    assert unrelated.read_text() == '"unrelated content"'


def test_input_manifest_change_during_execution_prevents_evaluation(tmp_path, monkeypatch):
    prepared, _ = _runtime(tmp_path, monkeypatch)
    original = reference.subprocess.Popen

    def process_with_changed_input(*args, **kwargs):
        process = original(*args, **kwargs)
        reference._json(prepared / "manifest.json", {"schema_version": 1, "changed": True})
        return process

    monkeypatch.setattr(reference.subprocess, "Popen", process_with_changed_input)
    output = tmp_path / "run"
    with pytest.raises(ValueError, match="inputs changed"):
        reference.run_reference(prepared, "fixture", output)
    assert not (output / "evaluation.json").exists()
    assert json.loads((output / "result.json").read_text())["state"] == "failed"


@pytest.mark.parametrize("protocol", [True, 1.0, 2, None])
def test_runtime_version_rejects_unsupported_protocol(tmp_path, monkeypatch, protocol):
    payload = {"schema_version": 1, "protocol_version": protocol,
               "upstream_revision": reference.UPSTREAM_REVISION, "adapter_sha256": "a" * 64}
    monkeypatch.setattr(reference.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload)))
    with pytest.raises(ValueError, match="protocol"):
        reference._runtime_version("fixture", reference._limits(2, 4096, 900))


def test_failed_kill_preserves_original_timeout_and_failure_record(tmp_path, monkeypatch):
    prepared, _ = _runtime(tmp_path, monkeypatch, timeout=True)
    original = reference.subprocess.run

    def failed_kill(command, **kwargs):
        if command[:2] == ["docker", "kill"]:
            raise OSError("fixture daemon unavailable")
        return original(command, **kwargs)

    monkeypatch.setattr(reference.subprocess, "run", failed_kill)
    output = tmp_path / "run"
    with pytest.raises(subprocess.TimeoutExpired):
        reference.run_reference(prepared, "fixture", output)
    result = json.loads((output / "result.json").read_text())
    assert result["error"]["type"] == "TimeoutExpired" and result["kill_error"] == "OSError"


@pytest.mark.parametrize("cleanup_failure", [
    subprocess.TimeoutExpired(["docker", "rm", "--force"], 30),
    OSError("Docker cleanup is unavailable"),
])
def test_cleanup_failure_preserves_original_timeout_and_failure_record(tmp_path, monkeypatch, cleanup_failure):
    prepared, commands = _runtime(tmp_path, monkeypatch, timeout=True)
    original_run = reference.subprocess.run

    def failed_cleanup(command, **kwargs):
        if command[:3] == ["docker", "rm", "--force"]:
            raise cleanup_failure
        return original_run(command, **kwargs)

    monkeypatch.setattr(reference.subprocess, "run", failed_cleanup)
    output = tmp_path / "failed-with-cleanup-error"
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        reference.run_reference(prepared, "fixture", output)
    assert caught.value.cmd == "docker"
    assert caught.value.timeout == 900
    result = json.loads((output / "result.json").read_text())
    assert result["state"] == "failed"
    assert result["error"] == {"type": "TimeoutExpired", "message": str(caught.value)}
    assert result["cleanup_error"] == type(cleanup_failure).__name__
    assert {"run-spec.json", "runner.log", "trajectory.csv", "status.json"} <= set(result["artifacts"])
    assert not (output / "evaluation.json").exists()
    assert any(command[:2] == ["docker", "kill"] for command in commands)


@pytest.mark.parametrize("points", [100, 200, 400])
def test_rendered_configuration_changes_only_allowlisted_line_and_preserves_baseline(tmp_path, points):
    source = Path(__file__).resolve().parents[1] / "integrations/openvins/config"
    before = {path.name: path.read_bytes() for path in source.iterdir()}
    rendered = tmp_path / "rendered"
    rendered.mkdir()
    spec = build_specification(DATASET_ID, {"num_pts": points})
    reference._render_configuration(source, rendered, spec)
    assert {path.name: path.read_bytes() for path in source.iterdir()} == before
    for name, original in before.items():
        content = (rendered / name).read_bytes()
        expected = original.replace(b"num_pts: 200\n", f"num_pts: {points}\n".encode()) if name == "estimator_config.yaml" else original
        assert content == expected
        assert hashlib.sha256(content).hexdigest() == spec["configuration"]["rendered_files"][name]["sha256"]


def test_renderer_rejects_modified_baseline_instead_of_inheriting_unsupported_settings(tmp_path):
    source = tmp_path / "baseline"
    shutil.copytree(Path(__file__).resolve().parents[1] / "integrations/openvins/config", source)
    config = source / "estimator_config.yaml"
    config.write_bytes(config.read_bytes().replace(b"fast_threshold: 20", b"fast_threshold: 99"))
    rendered = tmp_path / "rendered"
    rendered.mkdir()
    with pytest.raises(ValueError, match="configuration"):
        reference._render_configuration(source, rendered, build_specification(DATASET_ID, {}))
    assert not list(rendered.iterdir())


@pytest.mark.parametrize("raw", ['{"schema_version":1,"schema_version":1}',
                                  '{"schema_version":NaN}', '[]'])
def test_specification_file_rejects_ambiguous_or_invalid_json(tmp_path, raw):
    path = tmp_path / "specification.json"
    path.write_text(raw)
    with pytest.raises(ValueError):
        reference._load_specification(path)


def test_invalid_specification_fails_before_input_or_runtime_access(tmp_path, monkeypatch):
    def forbidden(*_args):
        pytest.fail("invalid specification must not inspect inputs or Docker")
    monkeypatch.setattr(reference, "verify_prepared", forbidden)
    monkeypatch.setattr(reference, "_image_identity", forbidden)
    spec = build_specification(DATASET_ID, {})
    spec["parameters"]["num_pts"] = True
    with pytest.raises(ValueError):
        reference.run_reference(tmp_path / "missing", "fixture", tmp_path / "out", specification=spec)
    assert not (tmp_path / "out").exists()


def test_conflicting_resource_override_fails_before_dataset_or_runtime_access(tmp_path, monkeypatch):
    prepared = tmp_path / "prepared"
    prepared.mkdir()
    def forbidden(*_args):
        pytest.fail("conflicting resources must not inspect inputs or Docker")
    monkeypatch.setattr(reference, "verify_prepared", forbidden)
    monkeypatch.setattr(reference, "_image_identity", forbidden)
    with pytest.raises(ValueError, match="resource overrides"):
        reference.run_reference(prepared, "fixture", tmp_path / "out", cpus=1.0,
                                specification=build_specification(DATASET_ID, {}))
    assert not (tmp_path / "out").exists()


def test_wrong_dataset_fails_before_docker_inspection_or_execution(tmp_path, monkeypatch):
    prepared, _ = _runtime(tmp_path, monkeypatch)
    def forbidden(*_args):
        pytest.fail("a different dataset must not inspect or execute Docker")
    monkeypatch.setattr(reference, "_image_identity", forbidden)
    with pytest.raises(ValueError, match="prepared dataset"):
        reference.run_reference(prepared, "fixture", tmp_path / "out",
                                specification=build_specification(DATASET_ID, {}))
    assert not (tmp_path / "out").exists()


def test_wrong_image_fails_before_runtime_version_probe_or_job(tmp_path, monkeypatch):
    prepared, _ = _runtime(tmp_path, monkeypatch)
    spec = build_specification(DATASET_ID, {})
    original_sha = reference._sha
    monkeypatch.setattr(reference, "_sha", lambda p: spec["dataset"]["prepared_manifest_sha256"]
                        if p == prepared / "manifest.json" else original_sha(p))
    def forbidden(*_args):
        pytest.fail("an unpinned image must not run even a version probe")
    monkeypatch.setattr(reference, "_runtime_version", forbidden)
    with pytest.raises(ValueError, match="runtime image"):
        reference.run_reference(prepared, "wrong-image", tmp_path / "out", specification=spec)
    assert not (tmp_path / "out").exists()


def _pinned_execution_fixture(tmp_path, monkeypatch, *, timeout=False):
    """Isolate execution/materialization; separate tests exercise preflight pins."""
    prepared, commands = _runtime(tmp_path, monkeypatch, timeout=timeout)
    source = Path(__file__).resolve().parents[1] / "integrations/openvins/config"
    shutil.copytree(source, prepared / "config", dirs_exist_ok=True)
    spec = build_specification(DATASET_ID, {"num_pts": 100})
    original_sha = reference._sha
    monkeypatch.setattr(reference, "_sha", lambda p: spec["dataset"]["prepared_manifest_sha256"]
                        if p == prepared / "manifest.json" else original_sha(p))
    monkeypatch.setattr(reference, "_image_identity", lambda _: spec["runtime"]["image_id"])
    monkeypatch.setattr(reference, "_validate_experiment_inputs", lambda *_: None)
    return prepared, commands, spec


def test_explicit_specification_executes_with_separate_configuration_and_exact_identity(tmp_path, monkeypatch):
    prepared, commands, spec = _pinned_execution_fixture(tmp_path, monkeypatch)
    before = json.dumps(spec, sort_keys=True)
    output = tmp_path / "run"
    result = reference.run_reference(prepared, "fixture", output, specification=spec)
    assert result["state"] == "complete"
    assert json.dumps(spec, sort_keys=True) == before
    assert json.loads((output / "experiment-specification.json").read_text()) == spec
    assert result["experiment_specification_sha256"] == specification_sha256(spec)
    recorded = json.loads((output / "run-spec.json").read_text())
    assert recorded["experiment_specification_sha256"] == specification_sha256(spec)
    assert recorded["rendered_configuration_files"] == spec["configuration"]["rendered_files"]
    for name, identity in spec["configuration"]["rendered_files"].items():
        assert result["artifacts"]["config-" + name] == identity
    command = commands[0]
    config_mount = next(part for part in command if ",dst=/config" in part)
    temporary = Path(config_mount.removeprefix("type=bind,src=").split(",dst=")[0])
    assert temporary != prepared / "config" and not temporary.is_relative_to(output)
    assert config_mount.endswith(",readonly")
    assert not temporary.exists()
    assert b"num_pts: 200\n" in (prepared / "config/estimator_config.yaml").read_bytes()
    assert b"num_pts: 100\n" in (output / "config-estimator_config.yaml").read_bytes()


def test_explicit_specification_timeout_retains_identity_and_removes_temporary_configuration(tmp_path, monkeypatch):
    prepared, commands, spec = _pinned_execution_fixture(tmp_path, monkeypatch, timeout=True)
    output = tmp_path / "run"
    with pytest.raises(subprocess.TimeoutExpired):
        reference.run_reference(prepared, "fixture", output, specification=spec)
    result = json.loads((output / "result.json").read_text())
    assert result["state"] == "failed"
    assert result["experiment_specification_sha256"] == specification_sha256(spec)
    assert json.loads((output / "experiment-specification.json").read_text()) == spec
    config_mount = next(part for part in commands[0] if ",dst=/config" in part)
    assert not Path(config_mount.removeprefix("type=bind,src=").split(",dst=")[0]).exists()


@pytest.mark.parametrize("target", ["experiment-specification.json", "config-estimator_config.yaml", "run-spec.json"])
def test_runtime_cannot_silently_change_specification_or_archived_configuration(tmp_path, monkeypatch, target):
    prepared, _, spec = _pinned_execution_fixture(tmp_path, monkeypatch)
    original_process = reference.subprocess.Popen
    output = tmp_path / "run"
    def corrupt(command, **kwargs):
        process = original_process(command, **kwargs)
        (output / target).write_text("{}\n")
        return process
    monkeypatch.setattr(reference.subprocess, "Popen", corrupt)
    with pytest.raises(ValueError):
        reference.run_reference(prepared, "fixture", output, specification=spec)
    result = json.loads((output / "result.json").read_text())
    assert result["state"] == "failed" and result["error"]["type"] == "ValueError"


def test_cli_passes_the_exact_specification_to_the_reference_runner(tmp_path, monkeypatch, capsys):
    path = tmp_path / "specification.json"
    spec = build_specification(DATASET_ID, {"num_pts": 400})
    path.write_text(json.dumps(spec))
    received = []
    monkeypatch.setattr(reference, "run_reference", lambda *args, **kwargs:
                        received.append(kwargs["specification"]) or {"qualification": {"passed": True}})
    assert reference.main(["run", "--prepared", str(tmp_path / "prepared"), "--output", str(tmp_path / "out"),
                           "--specification", str(path)]) == 0
    assert received == [spec]
    assert json.loads(capsys.readouterr().out)["qualification"]["passed"]


@pytest.mark.parametrize("changed", [None, "dataset_id", "archive", "reference", "calibration",
                                     "timeline", "imu_count", "image", "adapter", "binary",
                                     "evaluator", "criteria", "resources"])
def test_preflight_rejects_each_changed_input_or_runtime_identity(tmp_path, monkeypatch, changed):
    spec = build_specification(DATASET_ID, {})
    prepared = tmp_path / "prepared"
    prepared.mkdir()
    # This isolated preflight fixture substitutes tiny provenance contents.
    # The complete published pins are exercised by actual native qualifications.
    provenance = {"dependency-versions.txt": "fixture dependencies", "binary-sha256.txt": "fixture binaries"}
    spec["runtime"]["provenance_sha256"] = {
        name: hashlib.sha256(content.encode()).hexdigest() for name, content in provenance.items()
    }
    manifest = {
        "dataset_id": spec["dataset"]["prepared_dataset_id"],
        "source": {"sha256": spec["dataset"]["archive_sha256"]},
        "files": {"config/" + name: dict(identity) for name, identity in spec["configuration"]["baseline_files"].items()},
        "streams": {"imu0": {"samples": spec["interval"]["imu_samples"]}},
    }
    manifest["files"]["reference/trajectory.csv"] = {"sha256": spec["dataset"]["reference_sha256"]}
    start = int(spec["interval"]["first_camera_timestamp_ns"])
    expected = [start + i * 50_000_000 for i in range(spec["interval"]["camera_pairs"])]
    expected[-1] = int(spec["interval"]["last_camera_timestamp_ns"])
    image, version = spec["runtime"]["image_id"], dict(spec["runtime"]["version"])
    limits = dict(spec["resources"])
    original_sha = reference._sha
    def identity(path):
        if path == prepared / "manifest.json":
            return spec["dataset"]["prepared_manifest_sha256"]
        if changed == "evaluator" and path.name == "vio_evaluation.py":
            return "0" * 64
        return original_sha(path)
    monkeypatch.setattr(reference, "_sha", identity)
    if changed == "dataset_id":
        manifest["dataset_id"] = "different-recording"
    elif changed == "archive":
        manifest["source"]["sha256"] = "0" * 64
    elif changed == "reference":
        manifest["files"]["reference/trajectory.csv"]["sha256"] = "0" * 64
    elif changed == "calibration":
        manifest["files"]["config/kalibr_imucam_chain.yaml"]["sha256"] = "0" * 64
    elif changed == "timeline":
        expected = expected[1:]
    elif changed == "imu_count":
        manifest["streams"]["imu0"]["samples"] -= 1
    elif changed == "image":
        image = "sha256:" + "0" * 64
    elif changed == "adapter":
        version["adapter_sha256"] = "0" * 64
    elif changed == "binary":
        provenance["binary-sha256.txt"] = "different binaries"
    elif changed == "criteria":
        monkeypatch.setattr(reference, "GATES", {**reference.GATES, "minimum_coverage_fraction": 0.1})
    elif changed == "resources":
        limits["cpus"] = 100.0
    if changed is None:
        reference._validate_experiment_inputs(spec, prepared, manifest, expected, image, version, provenance, limits)
    else:
        with pytest.raises(ValueError):
            reference._validate_experiment_inputs(spec, prepared, manifest, expected, image, version, provenance, limits)
