"""Private execution contracts and optional-reference numerical evidence."""
from copy import deepcopy
import csv
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4

import numpy as np
import pytest

from meridian.sensor_contract import contract_sha256
from meridian.sensor_import import prepare_package
from meridian.vio_private_specification import (
    build_private_specification, validate_private_specification, specification_sha256,
    render_configuration, canonical, configuration_profile, validate_private_investigation,
    summarize_private_interval,
)
from meridian.vio_private_inputs import prepare_private_inputs, POSE_HEADER
from meridian.vio_private_evaluation import evaluate_private_request

EXAMPLES = Path(__file__).resolve().parents[1]/"examples/sensor-contracts"


def fixture(tmp_path, *, reference=True, transforms=None, apply_intrinsics=False):
    root = tmp_path/"source"; root.mkdir()
    source = json.loads((EXAMPLES/"stereo-partial-reference.json").read_text())
    source["clocks"] = [source["clocks"][0]]
    for stream in source["streams"]:
        stream["clock_id"] = source["clocks"][0]["id"]
    for f in source["files"]:
        path = root/f["path"]; path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EXAMPLES/f["path"], path)
    (root/"acquisition.json").write_text(json.dumps(source))
    calibration = json.loads((EXAMPLES/"calibration.json").read_text())
    calibration["acquisition"] = {"id":source["id"], "sha256":contract_sha256(source)}
    calibration["imu"] = {"sensor_id":"imu0", "frame_id":"imu", "gyro_bias_rad_s":[0,0,0],
        "accel_bias_m_s2":[0,0,0], "gyro_scale":[1,1,1], "accel_scale":[1,1,1],
        "noise":{"model":"continuous_white_noise_and_bias_diffusion", "gyro_density_rad_s_per_sqrt_hz":0.00016,
            "accel_density_m_s2_per_sqrt_hz":0.0028, "gyro_bias_diffusion_rad_s_per_sqrt_s":0.000022,
            "accel_bias_diffusion_m_s2_per_sqrt_s":0.00086, "assumptions":"Supplied prepared-domain model; not fitted."}}
    calibration["applicability"]["imu_intrinsics"] = "already_applied"
    if transforms: transforms(source, calibration)
    (root/"acquisition.json").write_text(json.dumps(source))
    calibration["acquisition"] = {"id":source["id"], "sha256":contract_sha256(source)}
    revision = tmp_path/"revision"
    prepared = prepare_package(root, revision, calibration=calibration, include_reference=reference, apply_imu_intrinsics=apply_intrinsics)
    prepared = json.loads((revision/"prepared-input.json").read_text())
    manifest = {"schema_version":1, "files":{}}
    for p in sorted(revision.rglob("*")):
        if p.is_file(): manifest["files"][p.relative_to(revision).as_posix()] = {"size_bytes":p.stat().st_size, "sha256":hashlib.sha256(p.read_bytes()).hexdigest()}
    marker = (canonical(manifest)+"\n").encode(); (revision/"publication-manifest.json").write_bytes(marker)
    binding = {"input_revision_id":str(uuid4()), "acquisition_id":str(uuid4()), "published_attempt":str(uuid4()),
        "acquisition_sha256":contract_sha256(source), "prepared_input_sha256":contract_sha256(prepared),
        "publication_manifest_sha256":hashlib.sha256(marker).hexdigest()}
    return binding, prepared, source, calibration, revision


def test_profile_is_fixed_and_preserves_legacy(tmp_path):
    binding, prepared, source, calibration, revision = fixture(tmp_path)
    spec = build_private_specification(binding, prepared, source, calibration)
    assert spec["interval"]["first_ns"] == "1520530308199447626"
    assert specification_sha256(spec) == specification_sha256(deepcopy(spec))
    config = render_configuration(spec)
    for option in ("calib_cam_extrinsics", "calib_cam_intrinsics", "calib_cam_timeoffset", "use_mask"):
        assert f"{option}: false" in config["estimator_config.yaml"]
    assert "mask0:" not in config["estimator_config.yaml"]
    assert spec["policy"]["gravity_m_s2"] == 9.80766
    assert "qualification_criteria" not in spec["evaluation"]
    assert configuration_profile()["id"] == spec["profile_id"]
    changed = deepcopy(spec); changed["policy"]["initial_camera_imu_offset_s"] = 0.001
    with pytest.raises(ValueError): validate_private_specification(changed)
    changed = deepcopy(spec); changed["calibration_parameters"]["cameras"][0]["intrinsics"][0] += 1
    with pytest.raises(ValueError): validate_private_specification(changed)


@pytest.mark.parametrize("change", [
    lambda s,c: s["streams"][0].update(timestamp_semantics="exposure_start"),
    lambda s,c: s["streams"][0]["camera"].update(shutter="rolling"),
    lambda s,c: c["imu"]["noise"].update(gyro_density_rad_s_per_sqrt_hz=0),
    lambda s,c: s["streams"][0]["processing"].update(calibration="applied"),
    lambda s,c: s["streams"][2]["processing"].update(calibration="unknown"),
])
def test_unsupported_metadata_fails_before_execution(tmp_path, change):
    binding, prepared, source, calibration, revision = fixture(tmp_path, transforms=change)
    with pytest.raises(ValueError): build_private_specification(binding, prepared, source, calibration)


def test_optional_reference_and_exact_sensor_only_staging(tmp_path):
    binding, prepared, source, calibration, revision = fixture(tmp_path, reference=False)
    spec = build_private_specification(binding, prepared, source, calibration)
    pulses=[]
    out = tmp_path/"staging"
    handoff = prepare_private_inputs(spec, revision, out, checkpoint=lambda:pulses.append(1))
    assert pulses
    assert handoff["evaluation"]["reference_available"] is False
    assert handoff["evaluation"]["reason"] == "reference_absent"
    assert len(handoff["camera_mounts"]) == 2
    assert all("/sensors/" in m["source_path"] and "/references/" not in m["source_path"] for m in handoff["camera_mounts"])
    assert not list((out/"sensors").rglob("*.png"))
    assert not list((out/"sensors").rglob("*.json"))
    for slot in ("cam0", "cam1"):
        mountpoint = out/f"sensors/mav0/{slot}/data"
        assert mountpoint.is_dir() and not list(mountpoint.iterdir())
    assert not (out/"sensors/references").exists()
    assert handoff["camera_timestamps_ns"] == ["1520530308199447626", "1520530308249447626"]
    assert (out/"sensors/mav0/cam0/data.csv").read_text().splitlines()[1].endswith(",0.png")
    assert (out/"sensors/mav0/imu0/data.csv").read_text().splitlines()[1].startswith("1520530308194447626,")
    assert len((out/"evaluation/reference.csv").read_text().splitlines()) == 1
    with pytest.raises(FileExistsError): prepare_private_inputs(spec, revision, out)


def test_source_change_and_nonexact_interval_rejected(tmp_path):
    binding, prepared, source, calibration, revision = fixture(tmp_path)
    spec = build_private_specification(binding, prepared, source, calibration)
    image = revision/"sensors/cam0/images/0.png"
    image.write_bytes(image.read_bytes()+b"x")
    with pytest.raises(ValueError, match="differs"): prepare_private_inputs(spec, revision, tmp_path/"staging")
    assert not (tmp_path/"staging").exists()


def test_nonexact_interval_metadata_is_not_payload_admission(tmp_path):
    binding, prepared, source, calibration, revision = fixture(tmp_path)
    interval = {"segment_id":"segment0", "first_ns":"1520530308199447627", "last_ns":"1520530308249447626"}
    spec = build_private_specification(binding, prepared, source, calibration, interval=interval)
    with pytest.raises(ValueError, match="exact camera sample"): prepare_private_inputs(spec, revision, tmp_path/"staging")
    assert not (tmp_path/"staging").exists()


def test_no_reference_keeps_local_pose_and_null_accuracy(tmp_path):
    binding, prepared, source, calibration, revision = fixture(tmp_path, reference=False)
    spec = build_private_specification(binding, prepared, source, calibration)
    handoff = prepare_private_inputs(spec, revision, tmp_path/"staging")
    trace = tmp_path/"trajectory.csv"
    with trace.open("w", newline="") as f:
        writer=csv.writer(f);writer.writerow(POSE_HEADER)
        writer.writerows([[stamp, n,0,0,0,0,0,1] for n,stamp in enumerate(handoff["camera_timestamps_ns"])])
    request={"schema_version":2,"specification":spec,"input_manifest":handoff["input_manifest"],"run_state":"complete"}
    result=evaluate_private_request(request,trace,handoff["evaluation"]["reference_path"],handoff["evaluation"]["imu_path"],None)
    evaluation, artifact=result["evaluation"],result["investigation"]
    assert evaluation["availability"] == {"status":"unavailable","reason":"reference_absent"}
    assert evaluation["metrics"] is None and evaluation["alignment"] is None
    assert evaluation["counts"]["estimate_samples"] == 2
    assert all(f["estimate_raw"] is not None and f["estimate"] is None and f["reference"] is None for f in artifact["frames"])
    assert artifact["provenance"]["reference_sha256"] is None
    assert artifact["imu"]["source"] == "prepared_input"
    assert artifact["full_interval"]["evaluation"]["status"] == "unavailable"
    assert artifact["cameras"][0]["resolution"] == [8,6]
    validate_private_investigation(artifact,specification=spec,input_manifest=handoff["input_manifest"],evaluation=evaluation,trajectory_sha256=hashlib.sha256(trace.read_bytes()).hexdigest())
    changed=deepcopy(artifact);changed["frames"][0]["estimate"] = changed["frames"][0]["estimate_raw"]
    with pytest.raises(ValueError):validate_private_investigation(changed,specification=spec,input_manifest=handoff["input_manifest"],evaluation=evaluation,trajectory_sha256=hashlib.sha256(trace.read_bytes()).hexdigest())


def test_api_modules_import_without_numerical_dependencies():
    script = 'import sys; from meridian import vio_private_specification, vio_private_inputs; assert "numpy" not in sys.modules and "PIL" not in sys.modules'
    subprocess.run([sys.executable,"-c",script],check=True)


@pytest.mark.parametrize("partial", [False, True])
def test_partial_reference_retains_full_denominator_and_fixed_alignment(tmp_path, partial):
    binding, prepared, source, calibration, revision = fixture(tmp_path)
    spec = build_private_specification(binding, prepared, source, calibration)
    handoff = prepare_private_inputs(spec, revision, tmp_path/"staging")
    first = int(spec["interval"]["first_ns"])
    expected = [str(first+n*10_000_000) for n in range(6)]
    points = [[0,0,0],[1,0,0],[0,1,0],[1,1,0],[1,0,1],[0,0,1]]
    trace, reference = tmp_path/"trace.csv", tmp_path/"reference.csv"
    for path, selection in ((trace,range(6)),(reference,[0,1,2,5] if partial else range(6))):
        with path.open("w",newline="") as f:
            writer=csv.writer(f);writer.writerow(POSE_HEADER)
            writer.writerows([[expected[n],*points[n],0,0,0,1] for n in selection])
    manifest=deepcopy(handoff["input_manifest"])
    manifest.update(camera_timestamps_ns=expected,camera_pairs=6,reference_sha256=hashlib.sha256(reference.read_bytes()).hexdigest())
    request={"schema_version":2,"specification":spec,"input_manifest":manifest,"run_state":"complete"}
    result=evaluate_private_request(request,trace,reference,handoff["evaluation"]["imu_path"],None)
    evaluation, artifact = result["evaluation"], result["investigation"]
    assert evaluation["availability"]["status"] == ("partial" if partial else "available")
    assert evaluation["counts"]["expected_camera_samples"] == 6
    assert evaluation["counts"]["reference_supported_camera_samples"] == (4 if partial else 6)
    assert evaluation["metrics"]["ate_rmse_m"] < 1e-12
    assert evaluation["alignment"]["scale"] == 1
    assert evaluation["protocol"]["time_offset_fitted"] is False
    assert evaluation["protocol"]["max_reference_gap_ns"] == 20_000_000
    if partial:
        assert artifact["frames"][3]["reference"] is None and artifact["frames"][4]["reference"] is None
        window = summarize_private_interval(artifact,expected[3],expected[4])
        assert window["evaluation"]["status"] == "unavailable" and window["metrics"] is None
        assert artifact["frames"][3]["estimate"] is not None


def test_reference_body_frame_mismatch_is_explicit_not_sensor_failure(tmp_path):
    binding, prepared, source, calibration, revision=fixture(tmp_path,transforms=lambda s,c:s["streams"][-1].update(frame_id="cam0"))
    spec=build_private_specification(binding,prepared,source,calibration)
    assert spec["reference"] == {"stream":None,"unavailable_reason":"reference_body_frame_mismatch"}
    handoff=prepare_private_inputs(spec,revision,tmp_path/"staging")
    assert handoff["evaluation"]["reference_available"] is False
    assert handoff["input_manifest"]["camera_pairs"] == 2


def test_timeline_assessment_does_not_read_pixels_and_checks_payload(tmp_path):
    from meridian.vio_private_inputs import inspect_private_timeline
    binding, prepared, source, calibration, revision=fixture(tmp_path)
    spec=build_private_specification(binding,prepared,source,calibration)
    (revision/"sensors/cam0/images/0.png").unlink()
    assert inspect_private_timeline(spec,revision) == {"camera_pairs":2,"imu_samples":3,
        "first_ns":"1520530308199447626","last_ns":"1520530308249447626"}
    (revision/"sensors/imu0/data.csv").write_text("changed")
    with pytest.raises(ValueError):inspect_private_timeline(spec,revision)


def test_staging_cancellation_preserves_input_and_removes_own_partial_output(tmp_path):
    binding, prepared, source, calibration, revision=fixture(tmp_path)
    spec=build_private_specification(binding,prepared,source,calibration)
    before={p.relative_to(revision).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in revision.rglob("*") if p.is_file()}
    output=tmp_path/"staging"
    def checkpoint():
        if output.exists(): raise InterruptedError("cancelled")
    with pytest.raises(InterruptedError):prepare_private_inputs(spec,revision,output,checkpoint=checkpoint)
    assert not output.exists()
    assert before == {p.relative_to(revision).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in revision.rglob("*") if p.is_file()}


def test_unexpected_reference_file_cannot_enter_native_camera_mount(tmp_path):
    binding, prepared, source, calibration, revision=fixture(tmp_path)
    spec=build_private_specification(binding,prepared,source,calibration)
    (revision/"sensors/cam0/images/reference.csv").write_text("private truth")
    with pytest.raises(ValueError,match="non-sensor"):prepare_private_inputs(spec,revision,tmp_path/"staging")
    assert not (tmp_path/"staging").exists()


def test_anisotropic_prepared_gain_is_not_silently_used_with_scalar_noise(tmp_path):
    def change(source, calibration):
        calibration["applicability"]["imu_intrinsics"]="candidate_not_applied"
        calibration["imu"]["gyro_scale"]=[2,1,1]
    binding,prepared,source,calibration,revision=fixture(tmp_path,transforms=change,apply_intrinsics=True)
    with pytest.raises(ValueError,match="nonidentity IMU gains"):
        build_private_specification(binding,prepared,source,calibration)


def test_bias_only_preparation_uses_identity_runtime_model(tmp_path):
    def change(source, calibration):
        calibration["applicability"]["imu_intrinsics"]="candidate_not_applied"
        calibration["imu"]["gyro_bias_rad_s"]=[.01,.02,.03]
    binding,prepared,source,calibration,revision=fixture(tmp_path,transforms=change,apply_intrinsics=True)
    spec=build_private_specification(binding,prepared,source,calibration)
    handoff=prepare_private_inputs(spec,revision,tmp_path/"staging")
    native=list(csv.reader(Path(handoff["evaluation"]["imu_path"]).open()))[1:]
    corrected=list(csv.reader((revision/spec["imu"]["index_path"]).open()))[1:]
    assert native == [row[2:] for row in corrected]
    assert "time_offset: 0.0" in render_configuration(spec)["kalibr_imu_chain.yaml"]
    assert spec["policy"]["imu_intrinsics"] == "prepared_measurements_identity_runtime_intrinsics"


def test_bad_specification_nested_shapes_fail_closed(tmp_path):
    binding,prepared,source,calibration,revision=fixture(tmp_path)
    spec=build_private_specification(binding,prepared,source,calibration)
    for path in (("calibration_parameters","cameras"),("input",),("interval",),("cameras",),("imu",)):
        bad=deepcopy(spec);target=bad
        for key in path[:-1]:target=target[key]
        target[path[-1]]=None
        with pytest.raises(ValueError):validate_private_specification(bad)


def test_legacy_qualified_evaluator_and_profile_remain_pinned():
    from meridian import vio_evaluation
    from meridian.vio_specification import build_specification
    legacy=build_specification("tumvi-room1-512-16-v1",{"num_pts":200})
    assert hashlib.sha256(Path(vio_evaluation.__file__).read_bytes()).hexdigest() == legacy["evaluation"]["evaluator_sha256"]
    assert legacy["profile_id"] == "openvins-tumvi-room1-v1"
    assert legacy["configuration"]["baseline_files"] == legacy["configuration"]["rendered_files"]
