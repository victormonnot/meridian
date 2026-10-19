"""Scientific comparison fixtures: support loss must not become a hidden win.

The complete 2821-epoch schedule below is synthetic, with the qualified recording
endpoints used to exercise the immutable specification boundary. It is not a
measured TUM-VI result; error sequences have independent hand-computable values.
"""

import copy
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys

import pytest

from meridian import vio_comparison as comparison
from meridian import vio_investigation_metrics as scorer
from meridian.vio_specification import build_specification, specification_sha256, DATASET_ID


PROTOCOL = {
    "name": "meridian-vio-se3", "version": 1,
    "reference_association": "linear_position_shortest_arc_slerp_without_extrapolation",
    "max_reference_gap_ns": 20_000_000, "max_estimate_timestamp_error_ns": 1000,
    "estimate_interpolation": False, "alignment": "global_se3_least_squares_over_all_evaluated_positions",
    "removed_gauge": "constant_world_rotation_including_roll_pitch_yaw_and_translation",
    "scale_fitted": False, "time_offset_fitted": False,
    "weighting": "equal_weight_per_evaluated_camera_sample", "initialization_exclusion": "none",
    "initialization_delay_definition": "first_estimated_camera_timestamp_minus_first_expected_timestamp",
    "quaternion_convention": "Hamilton_xyzw_body_to_world",
}


def fixture_run(num_pts=200, number=1, errors=None, *, state="succeeded"):
    spec = build_specification(DATASET_ID, {"num_pts": num_pts})
    first, last = (int(spec["interval"][key]) for key in ("first_camera_timestamp_ns", "last_camera_timestamp_ns"))
    count = spec["interval"]["camera_pairs"]
    stamps = [first + (last-first)*index//(count-1) for index in range(count)]
    frames = []
    for index, stamp in enumerate(stamps):
        error = errors[index] if errors is not None and index < len(errors) else 1.0
        estimated = error is not None
        frames.append({"index": index, "timestamp_ns": str(stamp),
            "estimated": estimated, "reference_supported": True, "associated": estimated, "evaluated": estimated,
            "position_error_m": error, "orientation_error_deg": None if error is None else 2*error})
    artifact = {
        "schema_version": 1, "run_state": "complete" if state == "succeeded" else "partial",
        "specification_sha256": specification_sha256(spec),
        "dataset_manifest_sha256": spec["dataset"]["prepared_manifest_sha256"],
        "time": {"first_timestamp_ns": str(first), "last_timestamp_ns": str(last), "origin_timestamp_ns": str(first),
                 "basis": "recording_sensor_timestamps", "offset_fitted": False},
        "coordinate_frames": {"quaternion": "Hamilton_xyzw_body_to_world"},
        "units": {"position": "m", "orientation_error": "deg"}, "protocol": copy.deepcopy(PROTOCOL),
        "alignment": {"scale": 1.0, "translation_m": [10., -4., 2.]}, "frames": frames,
        "provenance": {"dataset_id": DATASET_ID, "reference_sha256": spec["dataset"]["reference_sha256"],
                       "evaluator_sha256": spec["evaluation"]["evaluator_sha256"],
                       "interval_scorer_sha256": hashlib.sha256(Path(scorer.__file__).read_bytes()).hexdigest(),
                       "display_downsampling": False, "builder_sha256": "a"*64,
                       "trajectory_sha256": f"{number:064x}", "imu_sha256": "b"*64, "evaluation_sha256": "c"*64},
    }
    recorded = scorer.summarize_interval(artifact, str(first), str(last))
    recorded["initialization_delay_s"] = 0.0
    return {"id": f"10000000-0000-4000-8000-{number:012d}", "state": state, "specification": spec,
            "specification_sha256": specification_sha256(spec), "investigation": artifact,
            "investigation_sha256": hashlib.sha256(json.dumps(artifact).encode()).hexdigest(),
            "failure": None if state == "succeeded" else {"code": "initialization_failure", "message": "Synthetic partial fixture."},
            "result": {"schema_version": 1, "evaluation": recorded,
                       "qualification": {"passed": True, "checks": {"ate": True}},
                       "execution": {"container_wall_seconds": 12.0, "native_wall_seconds": 10.0,
                                     "native_cpu_seconds": 9.0, "native_peak_rss_kib": 80000}},
            "experiment_id": "20000000-0000-4000-8000-000000000001", "experiment_name": "Synthetic support fixture",
            "revision_version": 1 if num_pts == 200 else 2,
            "started_at": "2026-10-19T09:00:00Z", "finished_at": "2026-10-19T09:00:12Z",
            "attempt_count": 1, "published_attempt": f"30000000-0000-4000-8000-{number:012d}"}


def selection(run, count=4):
    return {"start_ns": run["investigation"]["frames"][0]["timestamp_ns"],
            "end_ns": run["investigation"]["frames"][count-1]["timestamp_ns"]}


def test_dropped_difficult_samples_cannot_hide_worse_common_support():
    a = fixture_run(errors=[1., 1., 10., 10.])
    b = fixture_run(100, 2, [2., 2., None, None])
    result = comparison.compare_runs(a, b, **selection(a))
    assert result["baseline"]["own_support"]["metrics"]["ate_rmse_m"] == pytest.approx(math.sqrt(50.5))
    assert result["candidate"]["own_support"]["metrics"]["ate_rmse_m"] == 2
    common = result["common_support"]
    assert common["baseline_metrics"]["ate_rmse_m"] == 1
    assert common["candidate_metrics"]["ate_rmse_m"] == 2
    assert common["delta_candidate_minus_baseline"]["ate_rmse_m"] == 1
    assert common["delta_candidate_minus_baseline"]["orientation_rmse_deg"] == 2
    assert common["delta_candidate_minus_baseline"]["orientation_rmse_rad"] == pytest.approx(math.radians(2))
    assert common["evaluated_camera_samples"] == 2 and common["fraction_of_expected"] == 0.5
    assert result["observed_deltas"]["coverage_fraction"] == -0.5
    lost = result["support_difference"]["estimated"]["baseline_only"]
    assert lost["sample_count"] == 2 and len(lost["intervals"]) == 1
    assert lost["intervals"][0]["first_camera_index"] == 2
    assert lost["intervals"][0]["last_camera_index"] == 3
    assert "different_support" in [warning["code"] for warning in result["warnings"]]
    assert result["protocol"]["ranking"] is False
    assert "winner" not in result


def test_global_alignment_and_nonzero_errors_are_reused_without_refit():
    a = fixture_run(errors=[3., 4., 0., 0.])
    b = fixture_run(100, 2, [6., 8., 0., 0.])
    b["investigation"]["alignment"]["translation_m"] = [-40., 1., 80.]
    result = comparison.compare_runs(a, b, **selection(a, 2))
    assert result["common_support"]["baseline_metrics"]["ate_rmse_m"] == pytest.approx(math.sqrt(12.5))
    assert result["common_support"]["candidate_metrics"]["ate_rmse_m"] == pytest.approx(math.sqrt(50))
    assert result["protocol"]["alignment_scope"] == "original_per_run_full_recording_global_se3_unchanged"
    # A single selected sample still uses its globally aligned error; no interval fit can zero it.
    one = comparison.compare_runs(a, b, **selection(a, 1))
    assert one["common_support"]["baseline_metrics"]["ate_rmse_m"] == 3


def test_reference_gaps_remain_in_denominator_and_are_not_estimator_dropouts():
    a, b = fixture_run(), fixture_run(100, 2)
    for run in (a, b):
        row = run["investigation"]["frames"][1]
        row.update(reference_supported=False, associated=False, evaluated=False, position_error_m=None, orientation_error_deg=None)
    result = comparison.compare_runs(a, b, **selection(a))
    assert result["common_support"]["expected_camera_samples"] == 4
    assert result["common_support"]["evaluated_camera_samples"] == 3
    assert result["baseline"]["own_support"]["coverage_fraction"] == 1
    assert result["baseline"]["own_support"]["reference_coverage_fraction"] == 0.75
    assert result["support_difference"]["estimated"]["both"]["sample_count"] == 4
    assert result["support_difference"]["evaluated"]["neither"]["sample_count"] == 1


def test_support_regions_retain_isolated_loss_and_regain_without_bridging():
    a = fixture_run(errors=[None, 1., None, 1., None, 1.])
    b = fixture_run(100, 2, [1., None, None, 1., 1., 1.])
    result = comparison.compare_runs(a, b, **selection(a, 6))
    support = result["support_difference"]["estimated"]
    assert {key: value["sample_count"] for key, value in support.items()} == {"both": 2, "baseline_only": 1, "candidate_only": 2, "neither": 1}
    assert [item["first_camera_index"] for item in support["candidate_only"]["intervals"]] == [0, 4]
    assert all(item["sample_count"] == 1 for item in support["candidate_only"]["intervals"])
    delay = (int(a["investigation"]["frames"][1]["timestamp_ns"])-int(a["investigation"]["frames"][0]["timestamp_ns"]))*1e-9
    assert result["baseline"]["initialization_delay_s"] == delay
    assert result["candidate"]["initialization_delay_s"] == 0
    assert result["observed_deltas"]["initialization_delay_s"] == -delay


def test_empty_partial_failure_keeps_zero_coverage_null_metrics_and_failure():
    a, b = fixture_run(), fixture_run(100, 2, state="failed")
    b["result"] = None
    b["investigation"]["alignment"] = None
    for row in b["investigation"]["frames"]:
        row.update(estimated=False, associated=False, evaluated=False, position_error_m=None, orientation_error_deg=None)
    result = comparison.compare_runs(a, b)
    assert result["candidate"]["state"] == "failed"
    assert result["candidate"]["failure"]["code"] == "initialization_failure"
    assert result["candidate"]["own_support"]["status"] == "global_alignment_unavailable"
    assert result["candidate"]["own_support"]["coverage_fraction"] == 0
    assert result["candidate"]["own_support"]["metrics"] is None
    assert result["candidate"]["initialization_delay_s"] is None
    assert result["common_support"]["status"] == "no_common_evaluated_samples"
    assert result["common_support"]["delta_candidate_minus_baseline"] is None
    assert result["support_difference"]["estimated"]["baseline_only"]["sample_count"] == 2821


def test_partial_valid_errors_do_not_erase_failed_state():
    a, b = fixture_run(), fixture_run(100, 2, [0.1, 0.1, None, None], state="failed")
    b["result"] = None
    result = comparison.compare_runs(a, b, **selection(a))
    assert result["common_support"]["candidate_metrics"]["ate_rmse_m"] == pytest.approx(0.1)
    assert result["candidate"]["state"] == "failed" and result["candidate"]["investigation"]["run_state"] == "partial"
    assert "candidate_not_succeeded" in [warning["code"] for warning in result["warnings"]]


@pytest.mark.parametrize("unavailable", ["expired", "not_published", "cancelled"])
def test_unavailable_artifact_keeps_recorded_full_run_separate(unavailable):
    a, b = fixture_run(), fixture_run(100, 2)
    b.update(investigation=None, investigation_sha256=None, investigation_unavailable_reason=unavailable)
    if unavailable == "cancelled":
        b.update(state="cancelled", result=None, failure={"code": "cancelled", "message": "Cancelled."})
    result = comparison.compare_runs(a, b, **selection(a))
    assert result["candidate"]["own_support"] is None
    assert result["candidate"]["investigation"]["unavailable_reason"] == unavailable
    recorded = result["candidate"]["recorded_full_run"]
    if unavailable != "cancelled":
        assert recorded["counts"]["expected_camera_samples"] == 2821
        assert result["candidate"]["execution"]["native_cpu_seconds"] == 9
    else:
        assert recorded is None
    assert result["baseline"]["own_support"]["counts"]["expected_camera_samples"] == 4
    assert result["common_support"]["status"] == "investigation_unavailable"
    assert result["common_support"]["baseline_metrics"] is None
    assert result["support_difference"] is None


def test_both_artifacts_missing_preserves_full_denominator_but_not_interval_support():
    a, b = fixture_run(), fixture_run(100, 2)
    interval = selection(a)
    for run in (a, b):
        run.update(investigation=None, investigation_sha256=None)
    full = comparison.compare_runs(a, b)
    assert full["common_support"]["expected_camera_samples"] == 2821
    assert full["common_support"]["evaluated_camera_samples"] is None
    assert full["interval"]["first_camera_index"] == 0
    assert comparison.compare_runs(a, b, **interval)["common_support"]["expected_camera_samples"] is None


def test_repeat_has_distinct_ids_exact_spec_no_pooling_and_raw_deltas():
    a, b = fixture_run(), fixture_run(200, 2, [2., 2.])
    b["result"]["execution"]["native_wall_seconds"] = 12
    result = comparison.compare_runs(a, b, **selection(a, 2))
    assert result["kind"] == "repeated_execution" and result["configuration_changes"] == []
    assert result["common_support"]["delta_candidate_minus_baseline"]["ate_rmse_m"] == 1
    assert result["observed_deltas"]["native_wall_seconds"] == 2
    assert result["protocol"]["repeated_execution_aggregation"] == "none"
    with pytest.raises(comparison.ComparisonError, match="duplicate_run"):
        comparison.compare_runs(a, a)


def test_shared_no_camera_interval_has_no_scores_or_division_by_zero():
    a, b = fixture_run(), fixture_run(100, 2)
    stamp = int(a["investigation"]["frames"][0]["timestamp_ns"])+1
    result = comparison.compare_runs(a, b, start_ns=str(stamp), end_ns=str(stamp))
    assert result["common_support"]["status"] == "no_camera_samples"
    assert result["common_support"]["expected_camera_samples"] == 0
    assert result["common_support"]["fraction_of_expected"] is None
    assert result["interval"]["first_camera_index"] is None


@pytest.mark.parametrize("change", [
    lambda a: a["specification"]["runtime"].update(image_id="sha256:"+"0"*64),
    lambda a: a["specification"]["evaluation"].update(time_offset_fitting=True),
    lambda a: a["specification"]["conventions"].update(orientation="world_to_body"),
    lambda a: a["specification"]["configuration"]["baseline_files"]["kalibr_imu_chain.yaml"].update(sha256="0"*64),
    lambda a: a["specification"].update(schema_version=True),
    lambda a: a["specification"]["parameters"].update(num_pts=100.0),
    lambda a: a.update(specification_sha256="0"*64),
])
def test_incompatible_or_noncanonical_pinned_specifications_are_rejected(change):
    a, b = fixture_run(), fixture_run(100, 2)
    change(b)
    with pytest.raises(comparison.ComparisonError) as error:
        comparison.compare_runs(a, b)
    assert error.value.code == "invalid_specification"
    assert error.value.details["run"] == "candidate"


@pytest.mark.parametrize("change,field", [
    (lambda a: a["provenance"].update(builder_sha256="d"*64), "provenance.builder_sha256"),
    (lambda a: a["provenance"].update(imu_sha256="e"*64), "provenance.imu_sha256"),
    (lambda a: a["coordinate_frames"].update(quaternion="world_to_body"), "coordinate_frames"),
    (lambda a: a["units"].update(position="km"), "units"),
    (lambda a: a["frames"][1].update(timestamp_ns=str(int(a["frames"][1]["timestamp_ns"])+1)), "camera_timestamps"),
])
def test_direct_comparison_rejects_different_artifact_semantics(change, field):
    a, b = fixture_run(), fixture_run(100, 2)
    change(b["investigation"])
    with pytest.raises(comparison.ComparisonError) as error:
        comparison.compare_runs(a, b)
    assert error.value.code == "incompatible_runs" and error.value.details["field"] == field


def test_reference_support_mismatch_is_rejected_even_when_other_inputs_match():
    a, b = fixture_run(), fixture_run(100, 2)
    b["investigation"]["frames"][0].update(reference_supported=False, associated=False, evaluated=False, position_error_m=None, orientation_error_deg=None)
    with pytest.raises(comparison.ComparisonError) as error:
        comparison.compare_runs(a, b)
    assert error.value.details == {"field": "reference_support"}


@pytest.mark.parametrize("change", [
    lambda a: a["frames"][0].update(position_error_m=float("nan")),
    lambda a: a["frames"][0].update(position_error_m=float("inf")),
    lambda a: a["frames"][0].update(position_error_m=-1),
    lambda a: a["frames"][0].update(position_error_m=True),
    lambda a: a["frames"][0].update(estimated=1),
    lambda a: a["frames"][-1].update(position_error_m=float("inf")),
    lambda a: a.update(alignment=None),
    lambda a: a.update(schema_version=True),
    lambda a: a["protocol"].update(scale_fitted=True),
    lambda a: a["provenance"].update(display_downsampling=True),
    lambda a: a["provenance"].update(interval_scorer_sha256="f"*64),
    lambda a: a.update(run_state="partial"),
    lambda a: a["alignment"].update(scale=2),
])
def test_corrupt_artifacts_rejected_including_outside_selected_interval(change):
    a, b = fixture_run(), fixture_run(100, 2)
    change(b["investigation"])
    with pytest.raises(comparison.ComparisonError):
        comparison.compare_runs(a, b, **selection(a))


@pytest.mark.parametrize("bounds", [
    {"start_ns": "1"}, {"start_ns": True, "end_ns": "2"}, {"start_ns": "01", "end_ns": "2"},
    {"start_ns": "0", "end_ns": "2"}, {"start_ns": str(2**63), "end_ns": str(2**63)},
])
def test_invalid_interval_rejected(bounds):
    with pytest.raises(comparison.ComparisonError):
        comparison.compare_runs(fixture_run(), fixture_run(100, 2), **bounds)


def test_json_is_finite_compact_scalar_snapshot_and_does_not_mutate_inputs():
    a, b = fixture_run(), fixture_run(100, 2)
    a["investigation"]["imu"] = {"samples": [{"private_sensor_payload": True}]}
    a["result"]["execution"]["internal_path"] = "/private/worker/path"
    before = copy.deepcopy((a, b))
    result = comparison.compare_runs(a, b)
    encoded = json.dumps(result, allow_nan=False)
    assert (a, b) == before
    assert "private_sensor_payload" not in encoded and "/private/worker/path" not in encoded
    assert '"frames"' not in encoded and '"quaternion_xyzw"' not in encoded
    assert len(encoded) < 30_000
    assert result["provenance"]["comparison_source_sha256"] == hashlib.sha256(Path(comparison.__file__).read_bytes()).hexdigest()
    assert result["provenance"]["interval_scorer_sha256"] == hashlib.sha256(Path(scorer.__file__).read_bytes()).hexdigest()
    assert result["baseline"]["specification"] == a["specification"]
    assert result["baseline"]["evidence_provenance"]["trajectory_sha256"] == a["investigation"]["provenance"]["trajectory_sha256"]
    # The saved snapshot stays self-contained after the source objects change.
    a["specification"]["runtime"]["image_id"] = "deleted-source"
    assert result["baseline"]["specification"]["runtime"]["image_id"].startswith("sha256:")


@pytest.mark.parametrize("change", [
    lambda r: r["evaluation"]["metrics"].update(ate_rmse_m=-1),
    lambda r: r["evaluation"]["metrics"].update(ate_rmse_m=float("inf")),
    lambda r: r["evaluation"]["metrics"].update(ate_rmse_m=True),
    lambda r: r["evaluation"]["counts"].update(estimate_samples=-1),
    lambda r: r["evaluation"]["counts"].update(estimate_samples=2821.0),
    lambda r: r["evaluation"]["counts"].update(expected_camera_samples=100),
    lambda r: r["evaluation"].update(coverage_fraction=2),
    lambda r: r["evaluation"].update(coverage_fraction=0.5),
    lambda r: r["evaluation"].update(initialization_delay_s=-1),
    lambda r: r["execution"].update(native_cpu_seconds=float("nan")),
    lambda r: r["execution"].update(native_peak_rss_kib=-1),
    lambda r: r["qualification"]["checks"].update(ate="true"),
])
def test_recorded_full_run_rejects_invalid_scalars_even_after_artifact_expiry(change):
    a, b = fixture_run(), fixture_run(100, 2)
    b.update(investigation=None, investigation_sha256=None, investigation_unavailable_reason="expired")
    change(b["result"])
    with pytest.raises(comparison.ComparisonError) as error:
        comparison.compare_runs(a, b)
    assert error.value.code == "invalid_run"
    assert error.value.details["run"] == "candidate"


def test_runtime_config_identity_is_compared_independently_of_docker_storage_ids():
    a, b = fixture_run(), fixture_run(200, 2)
    # Host Docker storage descriptors are not scientific image config identities.
    a["investigation"]["provenance"]["actual_execution_image_id"] = a["specification"]["runtime"]["image_id"]
    b["investigation"]["provenance"]["actual_execution_image_id"] = "sha256:54863f14d9f92d2805f5fddcfb78d146c6aac2ad77dc348e304059e2252a400a"
    result = comparison.compare_runs(a, b)
    assert result["kind"] == "repeated_execution"
    assert result["baseline"]["specification"]["runtime"] == result["candidate"]["specification"]["runtime"]


def test_rejections_do_not_echo_artifact_values():
    a, b = fixture_run(), fixture_run(100, 2)
    b["investigation"]["frames"][0]["timestamp_ns"] = "/private/path/secret"
    with pytest.raises(comparison.ComparisonError) as error:
        comparison.compare_runs(a, b)
    assert "secret" not in str(error.value)
    assert "secret" not in json.dumps(error.value.details)


def test_import_and_comparison_need_no_numpy_or_django():
    script = '''
import sys
class RejectNumericalImports:
    def find_spec(self, name, path=None, target=None):
        if name.split('.')[0] in ('numpy', 'django'):
            raise AssertionError(name)
sys.meta_path.insert(0, RejectNumericalImports())
from meridian.vio_comparison import compare_runs, ComparisonError
assert issubclass(ComparisonError, ValueError)
'''
    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True)
