"""Check repeated-trial evidence without regenerating experiment trajectories."""

import json
from pathlib import Path

import pytest

from meridian.web_trials import build_repeated_trials


ROOT = Path(__file__).resolve().parents[1]
SOURCE_FOLDERS = {"paired": "ekf-comparison", "controlled": "controlled-scenarios"}
PAIRED_FIELDS = {"gyro": "gyro_angle_rmse_deg", "complementary": "complementary_angle_rmse_deg",
                 "kalman": "angle_kalman_angle_rmse_deg", "ekf": "ekf_angle_rmse_deg"}
CASES = [("paired", "nominal"), ("paired", "translation_pulse")]
CASES += [("controlled", name) for name in ("initial_offset", "accel_dropout", "bias_ramp",
           "initial_overconfident", "accel_noise_mismatch", "timing_jitter", "accel_delay")]


def load_summary(source):
    return json.loads((ROOT/"results"/SOURCE_FOLDERS[source]/"summary.json").read_text())


def metric_container(summary, source, method="ekf"):
    trial = summary["validation"]["trials"][0]
    if source == "paired":
        return trial["nominal"], PAIRED_FIELDS[method]
    return trial["scenarios"]["nominal"]["metrics"][method], "angle_rmse_deg"


def aggregate_container(summary, source, method="ekf"):
    aggregates = summary["validation"]["aggregates"]["nominal"]
    return (aggregates[PAIRED_FIELDS[method]] if source == "paired" else
            aggregates[method]["angle_rmse_deg"])


@pytest.mark.parametrize("source,name", CASES)
def test_all_cases_preserve_recorded_seed_order_and_unrounded_values(source, name):
    summary = load_summary(source)
    before = json.dumps(summary)
    result = build_repeated_trials(summary, name, source=source)
    assert set(result) == {"seeds", "rmse_deg"}
    assert result["seeds"] == list(range(20))
    assert summary["seed"] == 42 and summary["seed"] not in result["seeds"]
    assert set(result["rmse_deg"]) == set(PAIRED_FIELDS)
    for method, values in result["rmse_deg"].items():
        if source == "paired":
            expected = [trial[name][PAIRED_FIELDS[method]] for trial in summary["validation"]["trials"]]
        else:
            expected = [trial["scenarios"][name]["metrics"][method]["angle_rmse_deg"]
                        for trial in summary["validation"]["trials"]]
        assert values == expected
    assert json.dumps(summary) == before


@pytest.mark.parametrize("source", SOURCE_FOLDERS)
def test_one_trial_is_valid_and_does_not_share_mutable_storage(source):
    summary = load_summary(source)
    summary["validation"]["seeds"] = [0]
    summary["validation"]["trials"] = summary["validation"]["trials"][:1]
    for method in PAIRED_FIELDS:
        metrics, key = metric_container(summary, source, method)
        aggregate_container(summary, source, method).update(mean=metrics[key], worst=metrics[key])
    result = build_repeated_trials(summary, "nominal", source=source)
    assert result["seeds"] == [0]
    assert all(len(values) == 1 for values in result["rmse_deg"].values())
    result["seeds"][0] = 8
    result["rmse_deg"]["ekf"][0] = -1
    assert summary["validation"]["seeds"] == [0]
    metrics, key = metric_container(summary, source)
    assert metrics[key] > 0


@pytest.mark.parametrize("source", SOURCE_FOLDERS)
@pytest.mark.parametrize("mutation", ["empty", "not_list", "duplicate", "boolean", "fractional", "negative",
                                      "unsafe", "string", "order", "missing_trial", "extra_trial", "boolean_trial_seed"])
def test_reject_ambiguous_or_misaligned_seed_evidence(source, mutation):
    summary = load_summary(source)
    validation = summary["validation"]
    seeds = validation["seeds"]
    if mutation == "empty": validation["seeds"] = []
    elif mutation == "not_list": validation["seeds"] = "0..19"
    elif mutation == "duplicate": seeds[1] = seeds[0]
    elif mutation == "boolean": seeds[0] = False
    elif mutation == "fractional": seeds[0] = .5
    elif mutation == "negative": seeds[0] = -1
    elif mutation == "unsafe": seeds[0] = 2**53
    elif mutation == "string": seeds[0] = "0"
    elif mutation == "order": validation["trials"].reverse()
    elif mutation == "missing_trial": validation["trials"].pop()
    elif mutation == "extra_trial": validation["trials"].append(validation["trials"][0])
    else: validation["trials"][0]["seed"] = False
    with pytest.raises(ValueError, match="repeated-trial"):
        build_repeated_trials(summary, "nominal", source=source)


@pytest.mark.parametrize("source", SOURCE_FOLDERS)
@pytest.mark.parametrize("value", [None, True, "0.2", -1, float("nan"), float("inf"), 10**400])
def test_reject_invalid_endpoint_rmse(source, value):
    summary = load_summary(source)
    metrics, key = metric_container(summary, source)
    metrics[key] = value
    with pytest.raises(ValueError, match="RMSE must be a finite nonnegative number"):
        build_repeated_trials(summary, "nominal", source=source)


@pytest.mark.parametrize("source", SOURCE_FOLDERS)
def test_reject_missing_method(source):
    summary = load_summary(source)
    metrics, key = metric_container(summary, source, "kalman")
    del metrics[key]
    with pytest.raises(ValueError, match="kalman.*RMSE"):
        build_repeated_trials(summary, "nominal", source=source)


@pytest.mark.parametrize("source", SOURCE_FOLDERS)
@pytest.mark.parametrize("field", ["mean", "worst"])
@pytest.mark.parametrize("value", [None, False, float("nan"), -1, 1000.])
def test_reject_invalid_or_inconsistent_source_aggregate(source, field, value):
    summary = load_summary(source)
    aggregate_container(summary, source)[field] = value
    with pytest.raises(ValueError, match="aggregate"):
        build_repeated_trials(summary, "nominal", source=source)


@pytest.mark.parametrize("source", SOURCE_FOLDERS)
def test_changed_trial_requires_coherent_source_aggregate(source):
    summary = load_summary(source)
    metrics, key = metric_container(summary, source)
    metrics[key] += .1
    with pytest.raises(ValueError, match="inconsistent.*aggregate mean"):
        build_repeated_trials(summary, "nominal", source=source)


@pytest.mark.parametrize("source", SOURCE_FOLDERS)
@pytest.mark.parametrize("field", ["schema_version", "data_source", "experiment"])
def test_reject_unsupported_summary(source, field):
    summary = load_summary(source)
    summary[field] = "unknown"
    with pytest.raises(ValueError, match="unsupported repeated-trial experiment"):
        build_repeated_trials(summary, "nominal", source=source)


def test_reject_unknown_source_and_unavailable_case():
    summary = load_summary("paired")
    with pytest.raises(ValueError, match="unsupported repeated-trial source"):
        build_repeated_trials(summary, "nominal", source="live")
    with pytest.raises(ValueError, match="case accel_delay"):
        build_repeated_trials(summary, "accel_delay", source="paired")


def test_selected_run_outside_repeated_range_is_not_inserted_or_clipped():
    summary = load_summary("paired")
    result = build_repeated_trials(summary, "nominal", source="paired")
    selected = summary["scenarios"]["nominal"]["vector_ekf"]["rmse_deg"]
    assert max(result["rmse_deg"]["ekf"]) < selected
    assert len(result["rmse_deg"]["ekf"]) == 20
    assert 42 not in result["seeds"]
