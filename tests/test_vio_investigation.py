"""Independent fixtures for investigation support, gauge reuse and sensor timing."""

import copy
import csv
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pytest

from meridian import vio_evaluation as evaluator
from meridian.vio_investigation import build_image_index, build_investigation, _IMU_HEADER
from meridian.vio_investigation_metrics import summarize_interval, timestamp_ns


BASE = 1_520_530_308_199_447_626
POINTS = np.array([[0,0,0], [1,0,0], [0,1,0], [0,0,1], [1,2,0], [2,1,1]], dtype=float)
IDENTITY = np.array([0., 0., 0., 1.])


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _trajectory(path, times, positions, quaternions):
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(evaluator.TRAJECTORY_COLUMNS)
        writer.writerows([int(t), *p, *q] for t,p,q in zip(times, positions, quaternions, strict=True))


@pytest.fixture
def recording(tmp_path):
    times = [BASE + number*10_000_000 for number in range(6)]
    reference, estimate, imu = [tmp_path/name for name in ("reference.csv", "estimate.csv", "imu.csv")]
    quaternion = np.tile(IDENTITY, (6, 1))
    _trajectory(reference, times, POINTS, quaternion)
    _trajectory(estimate, times, POINTS, quaternion)
    with imu.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(_IMU_HEADER)
        writer.writerows([[BASE-2_000_000, 1,2,3,4,5,6], [BASE,2,3,4,5,6,7], [times[-1]+1_000_000,3,4,5,6,7,8]])
    request = {"schema_version": 1, "specification_sha256": "a"*64,
               "dataset_manifest_sha256": "b"*64, "dataset_id": "synthetic_fixture",
               "expected_timestamps_ns": times, "evaluator_sha256": _sha(Path(evaluator.__file__)),
               "reference_sha256": _sha(reference), "imu_sha256": _sha(imu),
               "license": {"spdx": "synthetic"}, "run_state": "complete"}
    return {"times": times, "reference": reference, "estimate": estimate, "imu": imu, "request": request}


def build(recording, *, native_status=None, evaluation=None):
    request = recording["request"]
    request["reference_sha256"], request["imu_sha256"] = _sha(recording["reference"]), _sha(recording["imu"])
    if evaluation is None:
        evaluation = evaluator.evaluate_trajectory(recording["estimate"], recording["reference"], expected_timestamps_ns=recording["times"])
    return build_investigation(recording["estimate"], recording["reference"], recording["imu"],
                               request=request, evaluation=evaluation, native_status=native_status)


@pytest.mark.parametrize("angle", [math.pi/2, math.pi, -math.pi/2])
def test_constant_world_gauge_is_removed_from_positions_and_attitude(recording, angle):
    c,s = math.cos(angle), math.sin(angle)
    rotation = np.array([[c,-s,0], [s,c,0], [0,0,1]])
    translation = np.array([10.,-20.,3.])
    estimate = (POINTS-translation) @ rotation
    q = np.tile([0,0,math.sin(-angle/2),math.cos(-angle/2)], (6,1))
    _trajectory(recording["estimate"], recording["times"], estimate, q)
    artifact = build(recording)
    for index, frame in enumerate(artifact["frames"]):
        np.testing.assert_allclose(frame["estimate"]["position_m"], POINTS[index], atol=1e-13)
        np.testing.assert_allclose(evaluator._rotation_matrices(np.array([frame["estimate"]["quaternion_xyzw"]]))[0], np.eye(3), atol=1e-13)
        assert frame["estimate"]["rpy_deg"] == pytest.approx([0,0,0], abs=1e-11)
        assert frame["position_error_m"] < 1e-13
    assert artifact["full_interval"]["metrics"]["ate_rmse_m"] < 1e-13


def test_absolute_ns_are_exact_strings_and_imu_uses_camera_origin_without_offset_fit(recording):
    artifact = build(recording, native_status={"initialized_timestamp_ns": BASE+1})
    assert artifact["frames"][0]["timestamp_ns"] == str(BASE)
    assert artifact["frames"][0]["estimate_timestamp_ns"] == str(BASE)
    assert artifact["summary"]["timing"]["expected_first_timestamp_ns"] == str(BASE)
    assert artifact["provenance"]["native_status"]["initialized_timestamp_ns"] == str(BASE+1)
    assert artifact["imu"]["samples"][0]["time_s"] == pytest.approx(-0.002)
    assert artifact["imu"]["samples"][-1]["time_s"] > artifact["time"]["duration_s"]
    assert artifact["imu"]["samples"][0]["gyro_rad_s"] == [1,2,3]
    assert artifact["imu"]["samples"][0]["accel_m_s2"] == [4,5,6]
    assert not artifact["imu"]["resampled"] and not artifact["imu"]["bias_corrected"]
    assert not artifact["time"]["offset_fitted"]
    assert all(not item["available"] for item in artifact["diagnostics"].values())
    json.dumps(artifact, allow_nan=False)


def test_interval_keeps_global_alignment_and_full_resolution_scale_defect(recording):
    _trajectory(recording["estimate"], recording["times"], POINTS*2, np.tile(IDENTITY, (6,1)))
    artifact = build(recording)
    selection = summarize_interval(artifact, str(BASE), str(BASE+20_000_000))
    rows = artifact["frames"][:3]
    errors = [np.linalg.norm(np.array(row["estimate"]["position_m"])-row["reference"]["position_m"]) for row in rows]
    assert selection["metrics"]["ate_rmse_m"] == pytest.approx(np.sqrt(np.mean(np.square(errors))))
    assert selection["metrics"]["ate_rmse_m"] > 0.5
    assert selection["alignment_scope"] == "full_recording_global_se3_unchanged"
    assert selection["interval"]["first_camera_index"] == 0
    assert selection["interval"]["last_camera_index"] == 2
    assert artifact["full_interval"]["metrics"] == pytest.approx(artifact["summary"]["metrics"])


def test_missing_initial_interior_trailing_output_is_not_interpolated(recording):
    keep = [1,2,4]
    _trajectory(recording["estimate"], [recording["times"][i] for i in keep], POINTS[keep], np.tile(IDENTITY, (3,1)))
    artifact = build(recording)
    assert [row["missing_estimate_reason"] for row in artifact["frames"]] == ["initialization",None,None,"interior",None,"trailing"]
    assert artifact["frames"][3]["estimate"] is None
    assert artifact["frames"][3]["position_error_m"] is None
    assert artifact["full_interval"]["coverage_fraction"] == 0.5
    assert artifact["full_interval"]["counts"]["expected_camera_samples"] == 6
    gaps = [event for event in artifact["events"] if event["kind"] == "missing_estimate"]
    assert [event["sample_count"] for event in gaps] == [1,1,1]
    assert gaps[0]["first_timestamp_ns"] == str(BASE)
    interval = summarize_interval(artifact, str(BASE), str(BASE))
    assert interval["status"] == "no_evaluated_samples" and interval["metrics"] is None
    assert interval["coverage_fraction"] == 0


def test_reference_gap_support_excludes_interpolation_across_long_bracket(recording):
    keep = [0,1,5]
    _trajectory(recording["reference"], [recording["times"][i] for i in keep], POINTS[keep], np.tile(IDENTITY,(3,1)))
    artifact = build(recording)
    assert [row["reference_supported"] for row in artifact["frames"]] == [True,True,False,False,False,True]
    assert artifact["frames"][2]["reference"] is None
    assert artifact["frames"][2]["estimate"] is not None
    assert artifact["frames"][2]["position_error_m"] is None
    assert artifact["full_interval"]["reference_coverage_fraction"] == 0.5
    assert artifact["full_interval"]["evaluated_fraction"] == 0.5


def test_orientation_error_is_geodesic_not_euler_component_subtraction(recording):
    angle = math.pi/4
    q = np.tile([math.sin(angle/2),0,0,math.cos(angle/2)],(6,1))
    _trajectory(recording["estimate"],recording["times"],POINTS,q)
    artifact = build(recording)
    assert artifact["full_interval"]["metrics"]["orientation_rmse_deg"] == pytest.approx(45)
    assert artifact["full_interval"]["metrics"]["orientation_rmse_rad"] == pytest.approx(angle)
    assert artifact["frames"][0]["estimate"]["rpy_deg"] == pytest.approx([45,0,0], abs=1e-12)


def test_empty_partial_run_retains_camera_imu_and_reference_without_inventing_scores(recording):
    _trajectory(recording["estimate"], [], [], [])
    recording["request"]["run_state"] = "partial"
    artifact = build(recording, native_status={"state":"uninitialized", "initialized_timestamp_ns":-1})
    assert artifact["run_state"] == "partial"
    assert artifact["summary"]["status"] == "insufficient_associations"
    assert all(row["missing_estimate_reason"] == "no_estimates" for row in artifact["frames"])
    assert artifact["full_interval"]["metrics"] is None
    assert artifact["full_interval"]["status"] == "global_alignment_unavailable"
    assert not any(event["kind"] == "native_initialization" for event in artifact["events"])
    assert len(artifact["imu"]["samples"]) == 3


def test_degenerate_alignment_retains_raw_pose_and_association_but_has_no_aligned_pose(recording):
    points = np.array([[i,0,0] for i in range(6)])
    _trajectory(recording["reference"], recording["times"], points, np.tile(IDENTITY,(6,1)))
    _trajectory(recording["estimate"], recording["times"], points, np.tile(IDENTITY,(6,1)))
    artifact = build(recording)
    assert artifact["summary"]["status"] == "degenerate_alignment"
    assert artifact["frames"][0]["estimate_raw"] is not None
    assert artifact["frames"][0]["estimate"] is None
    assert artifact["full_interval"]["counts"]["associated_camera_samples"] == 6
    assert artifact["full_interval"]["counts"]["evaluated_camera_samples"] == 0


@pytest.mark.parametrize("mutation", ["nan", "duplicate", "float_time", "wrong_header"])
def test_invalid_imu_is_rejected(recording, mutation):
    content = recording["imu"].read_text()
    if mutation == "nan":
        content = content.replace(",1,2,3,4,5,6", ",nan,2,3,4,5,6")
    elif mutation == "duplicate":
        content = content.replace(str(BASE-2_000_000), str(BASE))
    elif mutation == "float_time":
        content = content.replace(str(BASE-2_000_000), str(BASE-2_000_000)+".0")
    else:
        content = content.replace("rad s^-1", "degrees/s")
    recording["imu"].write_text(content)
    with pytest.raises(ValueError):
        build(recording)


def test_saved_evaluation_cannot_supply_fabricated_scores(recording):
    evaluation = evaluator.evaluate_trajectory(recording["estimate"], recording["reference"], expected_timestamps_ns=recording["times"])
    evaluation["per_sample"]["position_error_m"][0] = 99
    with pytest.raises(ValueError, match="Saved evaluation"):
        build(recording, evaluation=evaluation)


def test_source_identity_mismatch_rejected_before_build(recording):
    evaluation = evaluator.evaluate_trajectory(recording["estimate"], recording["reference"], expected_timestamps_ns=recording["times"])
    recording["request"]["reference_sha256"] = "c"*64
    with pytest.raises(ValueError, match="identity"):
        build_investigation(recording["estimate"],recording["reference"],recording["imu"],request=recording["request"],evaluation=evaluation)


def test_camera_index_is_pinned_and_describes_original_png(recording):
    files = {f"sensors/mav0/{camera}/data/{stamp}.png": {"sha256":"d"*64,"size_bytes":123}
             for camera in ("cam0","cam1") for stamp in recording["times"]}
    index = build_image_index({"files":files},recording["times"],specification_sha256="a"*64,dataset_manifest_sha256="b"*64)
    assert index["cameras"]["cam0"][0]["timestamp_ns"] == str(BASE)
    assert index["cameras"]["cam1"][-1]["index"] == 5
    assert index["image"]["served_encoding"] == "original_png"
    files[f"sensors/mav0/cam0/data/{BASE}.png"]["size_bytes"] = True
    with pytest.raises(ValueError):
        build_image_index({"files":files},recording["times"],specification_sha256="a"*64,dataset_manifest_sha256="b"*64)


@pytest.mark.parametrize("value", [BASE, float(BASE), True, "+12", "01", "1.0", " 1", str(2**63)])
def test_timestamp_contract_rejects_unsafe_or_ambiguous_values(value):
    with pytest.raises(ValueError):
        timestamp_ns(value)


def test_interval_boundaries_are_closed_and_empty_camera_support_is_explicit(recording):
    artifact = build(recording)
    empty = summarize_interval(artifact,str(BASE+1),str(BASE+2))
    assert empty["status"] == "no_camera_samples"
    assert empty["metrics"] is None and empty["coverage_fraction"] is None
    singleton = summarize_interval(artifact,str(BASE+10_000_000),str(BASE+10_000_000))
    assert singleton["counts"]["expected_camera_samples"] == 1
    assert singleton["interval"]["first_camera_index"] == 1
    for first,last in ((BASE-1,BASE), (BASE+1,BASE), (BASE,recording["times"][-1]+1)):
        with pytest.raises(ValueError):
            summarize_interval(artifact,str(first),str(last))


def test_interval_rejects_fabricated_support_and_nonfinite_errors(recording):
    artifact = build(recording)
    for key,value in (("evaluated",1), ("reference_supported",False), ("position_error_m",float("nan"))):
        broken = copy.deepcopy(artifact)
        broken["frames"][0][key] = value
        with pytest.raises(ValueError):
            summarize_interval(broken,str(BASE),str(recording["times"][-1]))
    artifact["alignment"] = None
    with pytest.raises(ValueError, match="support"):
        summarize_interval(artifact,str(BASE),str(recording["times"][-1]))


@pytest.mark.parametrize("pitch", [math.pi/2,-math.pi/2])
def test_gimbal_lock_has_explicit_finite_principal_display_convention(recording,pitch):
    q=np.tile([0,math.sin(pitch/2),0,math.cos(pitch/2)],(6,1))
    _trajectory(recording["estimate"],recording["times"],POINTS,q)
    artifact=build(recording)
    for frame in artifact["frames"]:
        assert frame["estimate"]["rpy_deg"] == pytest.approx([0,math.degrees(pitch),0],abs=1e-5)
        assert frame["orientation_error_deg"] == pytest.approx(90)


def test_native_failure_text_and_unknown_paths_do_not_enter_browser_provenance(recording):
    artifact=build(recording,native_status={"state":"failed","error":"cannot open /internal/private/path",
                                          "private_path":"/internal/secret","initialized_timestamp_ns":None})
    assert artifact["provenance"]["native_status"]["error_reported"]
    assert "/internal" not in json.dumps(artifact)
