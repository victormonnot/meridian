"""Two retained VIO runs compared on explicit, unchanged camera support.

Only standard-library dependencies are used. The caller authenticates access and
verifies retained artifact bytes against the supplied SHA-256 identities. This
module checks scientific compatibility and aggregates existing sample errors; it
never fits an alignment, interpolates output, or ranks configurations.
"""

import copy
import hashlib
import json
import math
from pathlib import Path
import re
from uuid import UUID

from . import vio_investigation_metrics as scorer
from .vio_specification import validate_specification, specification_sha256


class ComparisonError(ValueError):
    """Safe machine-readable comparison rejection without artifact values."""

    def __init__(self, code, details=None):
        self.code = code
        self.details = details or {}
        super().__init__(code)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _digest(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _uuid(value):
    try:
        return type(value) is str and str(UUID(value)) == value
    except ValueError:
        return False


def _error(code, role=None, field=None):
    raise ComparisonError(code, {k: v for k, v in (("run", role), ("field", field)) if v is not None})


def _finite_nonnegative(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _protocol(spec):
    evaluation = spec["evaluation"]
    return {
        "name": "meridian-vio-se3", "version": evaluation["protocol_version"],
        "reference_association": "linear_position_shortest_arc_slerp_without_extrapolation",
        "max_reference_gap_ns": evaluation["maximum_reference_bracket_ns"],
        "max_estimate_timestamp_error_ns": evaluation["maximum_estimate_timestamp_error_ns"],
        "estimate_interpolation": False,
        "alignment": "global_se3_least_squares_over_all_evaluated_positions",
        "removed_gauge": "constant_world_rotation_including_roll_pitch_yaw_and_translation",
        "scale_fitted": False, "time_offset_fitted": False,
        "weighting": "equal_weight_per_evaluated_camera_sample", "initialization_exclusion": "none",
        "initialization_delay_definition": "first_estimated_camera_timestamp_minus_first_expected_timestamp",
        "quaternion_convention": "Hamilton_xyzw_body_to_world",
    }


def _recorded_result(run, role, protocol):
    """Keep the bounded public result fields, excluding paths or unknown keys."""
    result = run.get("result")
    if result is None:
        return None, None, None
    if type(result) is not dict or type(result.get("schema_version")) is not int or result["schema_version"] != 1:
        _error("invalid_run", role, "result")
    evaluation = result.get("evaluation")
    if type(evaluation) is not dict or _canonical(evaluation.get("protocol")) != _canonical(protocol):
        _error("invalid_run", role, "result.evaluation.protocol")
    evaluation = {k: copy.deepcopy(evaluation[k]) for k in (
        "status", "metrics", "counts", "coverage_fraction", "evaluated_fraction",
        "reference_coverage_fraction", "initialization_delay_s", "protocol") if k in evaluation}
    if evaluation.get("status") not in ("ok", "insufficient_associations", "degenerate_alignment"):
        _error("invalid_run", role, "result.evaluation.status")
    metrics = evaluation.get("metrics")
    if metrics is not None:
        names = {"ate_rmse_m", "position_error_mean_m", "position_error_max_m", "orientation_rmse_rad",
                 "orientation_rmse_deg", "orientation_error_mean_deg", "orientation_error_max_deg"}
        if type(metrics) is not dict or not set(metrics) <= names or not all(_finite_nonnegative(v) for v in metrics.values()):
            _error("invalid_run", role, "result.evaluation.metrics")
    counts = evaluation.get("counts", {})
    count_names = {"expected_camera_samples", "estimate_samples", "reference_samples", "reference_supported_camera_samples",
                   "associated_camera_samples", "evaluated_camera_samples", "missing_initial_camera_samples",
                   "missing_interior_camera_samples", "missing_trailing_camera_samples"}
    if type(counts) is not dict or not set(counts) <= count_names or not all(type(v) is int and v >= 0 for v in counts.values()):
        _error("invalid_run", role, "result.evaluation.counts")
    expected = counts.get("expected_camera_samples")
    if expected is not None and expected != run["specification"]["interval"]["camera_pairs"]:
        _error("invalid_run", role, "result.evaluation.counts.expected_camera_samples")
    for name in ("estimate_samples", "reference_supported_camera_samples", "associated_camera_samples", "evaluated_camera_samples"):
        if expected is not None and counts.get(name, 0) > expected:
            _error("invalid_run", role, "result.evaluation.counts")
    for key, numerator in (("coverage_fraction", "estimate_samples"),
                           ("reference_coverage_fraction", "reference_supported_camera_samples"),
                           ("evaluated_fraction", "evaluated_camera_samples")):
        value = evaluation.get(key)
        if value is not None and (not _finite_nonnegative(value) or value > 1):
            _error("invalid_run", role, "result.evaluation." + key)
        if value is not None and expected and numerator in counts and not math.isclose(value, counts[numerator]/expected, abs_tol=1e-12, rel_tol=1e-12):
            _error("invalid_run", role, "result.evaluation." + key)
    delay = evaluation.get("initialization_delay_s")
    if delay is not None and not _finite_nonnegative(delay):
        _error("invalid_run", role, "result.evaluation.initialization_delay_s")
    qualification = result.get("qualification")
    if qualification is not None:
        if (type(qualification) is not dict or type(qualification.get("passed")) is not bool
                or type(qualification.get("checks")) is not dict
                or not all(type(value) is bool for value in qualification["checks"].values())):
            _error("invalid_run", role, "result.qualification")
        qualification = copy.deepcopy({k: qualification[k] for k in ("passed", "checks")})
    execution = result.get("execution")
    if execution is not None:
        if type(execution) is not dict:
            _error("invalid_run", role, "result.execution")
        execution = {k: execution[k] for k in ("container_wall_seconds", "native_wall_seconds",
                     "native_cpu_seconds", "native_peak_rss_kib") if k in execution}
        for value in execution.values():
            if not _finite_nonnegative(value):
                _error("invalid_run", role, "result.execution")
    return evaluation, qualification, execution


def _validate_run(run, role, scorer_sha):
    if type(run) is not dict or not _uuid(run.get("id")):
        _error("invalid_run", role, "id")
    if run.get("state") not in ("succeeded", "failed", "cancelled"):
        _error("invalid_run", role, "state")
    spec = run.get("specification")
    try:
        validate_specification(spec)
        actual_sha = specification_sha256(spec)
    except (TypeError, ValueError, KeyError):
        _error("invalid_specification", role, "specification")
    if run.get("specification_sha256") != actual_sha:
        _error("invalid_specification", role, "specification_sha256")
    protocol = _protocol(spec)
    artifact = run.get("investigation")
    if artifact is None:
        if run.get("investigation_sha256") is not None:
            _error("invalid_run", role, "investigation_sha256")
        if run.get("investigation_unavailable_reason", "not_published") not in ("expired", "not_published", "cancelled"):
            _error("invalid_run", role, "investigation_unavailable_reason")
    else:
        if type(artifact) is not dict or not _digest(run.get("investigation_sha256")):
            _error("invalid_artifact", role, "investigation")
        expected_state = {"succeeded": "complete", "failed": "partial"}.get(run["state"])
        if expected_state is None or artifact.get("run_state") != expected_state:
            _error("invalid_artifact", role, "run_state")
        pins = {"specification_sha256": actual_sha,
                "dataset_manifest_sha256": spec["dataset"]["prepared_manifest_sha256"],
                "protocol": protocol}
        for key, value in pins.items():
            if _canonical(artifact.get(key)) != _canonical(value):
                _error("invalid_artifact", role, key)
        provenance = artifact.get("provenance", {})
        for key, value in {"dataset_id": spec["dataset"]["id"],
                           "reference_sha256": spec["dataset"]["reference_sha256"],
                           "evaluator_sha256": spec["evaluation"]["evaluator_sha256"],
                           "interval_scorer_sha256": scorer_sha,
                           "display_downsampling": False}.items():
            if _canonical(provenance.get(key)) != _canonical(value):
                _error("invalid_artifact", role, "provenance." + key)
        for key in ("builder_sha256", "trajectory_sha256", "imu_sha256", "evaluation_sha256"):
            if not _digest(provenance.get(key)):
                _error("invalid_artifact", role, "provenance." + key)
        timeline = artifact["time"]
        interval = spec["interval"]
        for key, value in {"first_timestamp_ns": interval["first_camera_timestamp_ns"],
                           "last_timestamp_ns": interval["last_camera_timestamp_ns"],
                           "origin_timestamp_ns": interval["first_camera_timestamp_ns"],
                           "basis": "recording_sensor_timestamps", "offset_fitted": False}.items():
            if _canonical(timeline.get(key)) != _canonical(value):
                _error("invalid_artifact", role, "time." + key)
        if len(artifact["frames"]) != interval["camera_pairs"]:
            _error("invalid_artifact", role, "frames")
        # Validate the complete support, including samples outside a requested interval.
        scorer.summarize_interval(artifact, timeline["first_timestamp_ns"], timeline["last_timestamp_ns"])
        alignment = artifact.get("alignment")
        if alignment is not None and (type(alignment) is not dict or type(alignment.get("scale")) not in (int, float) or alignment["scale"] != 1):
            _error("invalid_artifact", role, "alignment.scale")
    recorded, qualification, execution = _recorded_result(run, role, protocol)
    output = {key: copy.deepcopy(run.get(key)) for key in (
        "id", "experiment_id", "experiment_name", "revision_version", "started_at", "finished_at",
        "attempt_count", "published_attempt", "state", "failure", "specification_sha256")}
    output.update({"parameters": copy.deepcopy(spec["parameters"]), "specification": copy.deepcopy(spec),
                   "evidence_provenance": ({key: artifact["provenance"][key] for key in (
                       "builder_sha256", "trajectory_sha256", "reference_sha256", "imu_sha256",
                       "evaluator_sha256", "evaluation_sha256", "interval_scorer_sha256")} if artifact is not None else None),
                   "investigation": {"available": artifact is not None,
                       "sha256": run.get("investigation_sha256"),
                       "run_state": artifact["run_state"] if artifact is not None else None,
                       "unavailable_reason": None if artifact is not None else run.get("investigation_unavailable_reason", "not_published")},
                   "own_support": None, "recorded_full_run": recorded,
                   "qualification": qualification, "execution": execution,
                   "initialization_delay_s": None})
    if artifact is not None:
        first_estimate = next((f for f in artifact["frames"] if f["estimated"]), None)
        if first_estimate is not None:
            output["initialization_delay_s"] = (scorer.timestamp_ns(first_estimate["timestamp_ns"]) - scorer.timestamp_ns(artifact["time"]["first_timestamp_ns"]))*1e-9
    elif recorded is not None:
        output["initialization_delay_s"] = recorded.get("initialization_delay_s")
    return output


def _compatible(baseline, candidate):
    """Allow precisely the one supported parameter and its rendered file hash."""
    normalized = []
    for run in (baseline, candidate):
        spec = copy.deepcopy(run["specification"])
        spec.pop("parameters")
        spec["configuration"]["rendered_files"].pop("estimator_config.yaml")
        normalized.append(spec)
    if _canonical(normalized[0]) != _canonical(normalized[1]):
        _error("incompatible_runs", field="specification")
    left, right = baseline.get("investigation"), candidate.get("investigation")
    if left is None or right is None:
        return
    for key in ("protocol", "coordinate_frames", "units", "time"):
        if _canonical(left.get(key)) != _canonical(right.get(key)):
            _error("incompatible_runs", field=key)
    for key in ("builder_sha256", "interval_scorer_sha256", "evaluator_sha256", "reference_sha256", "imu_sha256"):
        if left["provenance"][key] != right["provenance"][key]:
            _error("incompatible_runs", field="provenance." + key)
    for a, b in zip(left["frames"], right["frames"], strict=True):
        if a["timestamp_ns"] != b["timestamp_ns"]:
            _error("incompatible_runs", field="camera_timestamps")
        if a["reference_supported"] != b["reference_supported"]:
            _error("incompatible_runs", field="reference_support")


def _metrics(rows):
    if not rows:
        return None
    position = [row["position_error_m"] for row in rows]
    orientation = [row["orientation_error_deg"] for row in rows]
    orientation_rmse = math.sqrt(math.fsum(x*x for x in orientation) / len(rows))
    return {"ate_rmse_m": math.sqrt(math.fsum(x*x for x in position) / len(rows)),
            "position_error_mean_m": math.fsum(position)/len(rows), "position_error_max_m": max(position),
            "orientation_rmse_rad": math.radians(orientation_rmse), "orientation_rmse_deg": orientation_rmse,
            "orientation_error_mean_deg": math.fsum(orientation)/len(rows), "orientation_error_max_deg": max(orientation)}


def _regions(rows):
    regions = []
    for row in rows:
        if not regions or regions[-1]["last_camera_index"] + 1 != row["index"]:
            regions.append({"first_camera_index": row["index"], "last_camera_index": row["index"],
                            "start_timestamp_ns": row["timestamp_ns"], "end_timestamp_ns": row["timestamp_ns"], "sample_count": 1})
        else:
            regions[-1].update(last_camera_index=row["index"], end_timestamp_ns=row["timestamp_ns"], sample_count=regions[-1]["sample_count"]+1)
    return {"sample_count": len(rows), "intervals": regions}


def _supports(left, right, flag):
    groups = {key: [] for key in ("both", "baseline_only", "candidate_only", "neither")}
    for a, b in zip(left, right, strict=True):
        key = "both" if a[flag] and b[flag] else "baseline_only" if a[flag] else "candidate_only" if b[flag] else "neither"
        groups[key].append(a)
    return {key: _regions(rows) for key, rows in groups.items()}


def _delta(left, right):
    if left is None or right is None:
        return None
    return right - left


def compare_runs(baseline, candidate, *, start_ns=None, end_ns=None):
    """Return a compact snapshot for two distinct terminal run identities.

    Bounds are inclusive canonical nanosecond strings and must both be supplied
    or both omitted. Missing retained evidence preserves recorded full-run scores
    but makes interval and common-support recomputation unavailable. Input SHA-256
    values identify bytes already verified by the caller, not canonicalized JSON.
    """
    try:
        return _compare(baseline, candidate, start_ns=start_ns, end_ns=end_ns)
    except ComparisonError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError, RecursionError):
        raise ComparisonError("invalid_comparison_input") from None


def _compare(baseline, candidate, *, start_ns, end_ns):
    scorer_sha = _sha(scorer.__file__)
    left = _validate_run(baseline, "baseline", scorer_sha)
    right = _validate_run(candidate, "candidate", scorer_sha)
    if left["id"] == right["id"]:
        _error("duplicate_run")
    _compatible(baseline, candidate)
    spec = baseline["specification"]
    first = spec["interval"]["first_camera_timestamp_ns"]
    last = spec["interval"]["last_camera_timestamp_ns"]
    if (start_ns is None) != (end_ns is None):
        _error("invalid_interval")
    start_ns, end_ns = (first, last) if start_ns is None else (start_ns, end_ns)
    start, end, origin = scorer.timestamp_ns(start_ns), scorer.timestamp_ns(end_ns), scorer.timestamp_ns(first)
    if not origin <= start <= end <= scorer.timestamp_ns(last):
        _error("invalid_interval")
    interval = {"start_timestamp_ns": start_ns, "end_timestamp_ns": end_ns,
                "start_time_s": (start-origin)*1e-9, "end_time_s": (end-origin)*1e-9,
                "first_camera_index": None, "last_camera_index": None, "boundary": "closed"}
    selected = []
    for run, output in ((baseline, left), (candidate, right)):
        artifact = run.get("investigation")
        rows = None
        if artifact is not None:
            output["own_support"] = scorer.summarize_interval(artifact, start_ns, end_ns)
            interval = copy.deepcopy(output["own_support"]["interval"])
            rows = [frame for frame in artifact["frames"] if start <= scorer.timestamp_ns(frame["timestamp_ns"]) <= end]
        selected.append(rows)
    expected = next((len(rows) for rows in selected if rows is not None), None)
    # Even without artifacts, the pinned complete recording has a known denominator.
    if expected is None and start_ns == first and end_ns == last:
        expected = spec["interval"]["camera_pairs"]
        interval.update(first_camera_index=0, last_camera_index=expected-1)
    common = {"status": "investigation_unavailable", "reason": None,
              "expected_camera_samples": expected, "evaluated_camera_samples": None,
              "fraction_of_expected": None, "baseline_metrics": None, "candidate_metrics": None,
              "delta_candidate_minus_baseline": None}
    support = None
    if all(rows is not None for rows in selected):
        a, b = selected
        support = {"estimated": _supports(a, b, "estimated"), "evaluated": _supports(a, b, "evaluated")}
        pairs = [(x, y) for x, y in zip(a, b, strict=True) if x["evaluated"] and y["evaluated"]]
        lm, rm = _metrics([x for x, _ in pairs]), _metrics([y for _, y in pairs])
        common.update(status="no_camera_samples" if not expected else "ok" if pairs else "no_common_evaluated_samples",
                      evaluated_camera_samples=len(pairs), fraction_of_expected=len(pairs)/expected if expected else None,
                      baseline_metrics=lm, candidate_metrics=rm,
                      delta_candidate_minus_baseline={key: rm[key]-lm[key] for key in lm} if lm else None)
    else:
        common["reason"] = "Both retained investigations are required for common-support scores."
    kind = "repeated_execution" if left["specification_sha256"] == right["specification_sha256"] else "configuration_variant"
    changes = [] if kind == "repeated_execution" else [{"parameter": "num_pts", "label": "Feature target",
        "baseline": left["parameters"]["num_pts"], "candidate": right["parameters"]["num_pts"],
        "unit": "features (total across two cameras after initialization)"}]
    warnings = [{"code": "global_alignment_retained", "message": "Each run keeps its own full-recording SE3 alignment; interval and common-support scores do not refit it."},
                {"code": "no_automatic_ranking", "message": "Error deltas are observations, not a ranking. Read them together with failures, qualification and support loss."},
                {"code": "host_cost_not_controlled", "message": "Execution costs are observed host measurements; load and hardware are not controlled by this comparison."}]
    if kind == "repeated_execution":
        warnings.append({"code": "two_run_repeat_only", "message": "These two run requests share one saved specification. Available observations remain separate, without pooling or statistical confidence."})
    if support is None:
        warnings.append({"code": "investigation_unavailable", "message": "Retained full-resolution evidence is unavailable for at least one run. Recorded full-run results cannot substitute for interval or common-support scores."})
    elif support["estimated"]["baseline_only"]["sample_count"] or support["estimated"]["candidate_only"]["sample_count"] or support["evaluated"]["baseline_only"]["sample_count"] or support["evaluated"]["candidate_only"]["sample_count"]:
        warnings.append({"code": "different_support", "message": "The runs cover different samples. Common-support errors exclude unshared samples and do not measure performance on dropped segments."})
    for role, run in (("baseline", left), ("candidate", right)):
        if run["state"] != "succeeded":
            warnings.append({"code": role + "_not_succeeded", "message": "The " + role + " run did not succeed; retained partial errors do not imply a successful execution."})
        if run["qualification"] is not None and not run["qualification"]["passed"]:
            warnings.append({"code": role + "_not_qualified", "message": "The " + role + " run did not pass the recorded integration qualification criteria."})
    deltas = {key: _delta((left["own_support"] or {}).get(key), (right["own_support"] or {}).get(key))
              for key in ("coverage_fraction", "evaluated_fraction")}
    deltas["initialization_delay_s"] = _delta(left["initialization_delay_s"], right["initialization_delay_s"])
    deltas.update({key: _delta((left["execution"] or {}).get(key), (right["execution"] or {}).get(key))
                   for key in ("native_wall_seconds", "native_cpu_seconds", "native_peak_rss_kib")})
    result = {"schema_version": 1, "kind": kind, "baseline": left, "candidate": right,
              "configuration_changes": changes, "interval": interval, "common_support": common,
              "support_difference": support, "observed_deltas": deltas, "warnings": warnings,
              "protocol": {"version": 1, "evaluation": _protocol(spec),
                  "alignment_scope": "original_per_run_full_recording_global_se3_unchanged",
                  "common_support": "intersection_of_evaluated_camera_samples",
                  "delta_convention": "candidate_minus_baseline", "ranking": False,
                  "repeated_execution_aggregation": "none"},
              "provenance": {"comparison_source_sha256": _sha(__file__), "interval_scorer_sha256": scorer_sha}}
    _canonical(result)
    return result
