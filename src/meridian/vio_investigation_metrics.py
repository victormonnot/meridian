"""Interval scores from full-resolution, globally aligned investigation samples.

This module intentionally uses only the standard library so the private HTTP API
can score a retained artifact without loading a numerical estimator or NumPy.
The selected interval never changes the recording's gauge alignment.
"""

import math
import re


def timestamp_ns(value: str) -> int:
    """Accept canonical decimal strings without losing absolute-time precision."""
    if type(value) is not str or re.fullmatch(r"0|-?[1-9][0-9]*", value) is None:
        raise ValueError("Timestamps must be canonical decimal nanosecond strings.")
    result = int(value)
    if not -(2**63) <= result < 2**63:
        raise ValueError("Timestamp is outside the signed int64 range.")
    return result


def _nonnegative(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("Sample errors must be finite nonnegative numbers.")
    return value


def summarize_interval(investigation: dict, start_ns: str, end_ns: str) -> dict:
    """Score inclusive camera support; never interpolate, downsample, or refit.

    Empty camera support and unavailable global alignment have explicit states
    and null metrics. Coverage includes every expected camera epoch in the
    selection, including initialization and missing output/reference samples.
    """
    if (type(investigation) is not dict
            or type(investigation.get("schema_version")) is not int
            or investigation["schema_version"] != 1):
        raise ValueError("Unsupported investigation schema.")
    start, end = timestamp_ns(start_ns), timestamp_ns(end_ns)
    timeline = investigation["time"]
    first, last = timestamp_ns(timeline["first_timestamp_ns"]), timestamp_ns(timeline["last_timestamp_ns"])
    origin = timestamp_ns(timeline["origin_timestamp_ns"])
    if not first <= start <= end <= last:
        raise ValueError("The closed interval must lie within the camera recording.")
    frames = investigation["frames"]
    if type(frames) is not list or not frames:
        raise ValueError("Investigation must retain the complete camera schedule.")
    selected, previous = [], None
    for index, frame in enumerate(frames):
        stamp = timestamp_ns(frame["timestamp_ns"])
        if (type(frame.get("index")) is not int or frame["index"] != index
                or (previous is not None and stamp <= previous)):
            raise ValueError("Camera samples must have sequential indices and increasing timestamps.")
        previous = stamp
        if start <= stamp <= end:
            for key in ("estimated", "reference_supported", "associated", "evaluated"):
                if type(frame.get(key)) is not bool:
                    raise ValueError("Sample support flags must be booleans.")
            if (frame["associated"] != (frame["estimated"] and frame["reference_supported"])
                    or (frame["evaluated"] and (not frame["associated"] or investigation.get("alignment") is None))):
                raise ValueError("Inconsistent sample support.")
            selected.append(frame)
    if timestamp_ns(frames[0]["timestamp_ns"]) != first or timestamp_ns(frames[-1]["timestamp_ns"]) != last:
        raise ValueError("Camera schedule does not cover the declared recording.")
    count = len(selected)
    counts = {"expected_camera_samples": count}
    for name, flag in (("estimate_samples", "estimated"),
                       ("reference_supported_camera_samples", "reference_supported"),
                       ("associated_camera_samples", "associated"),
                       ("evaluated_camera_samples", "evaluated")):
        counts[name] = sum(frame[flag] for frame in selected)
    position, orientation = [], []
    for frame in selected:
        if frame["evaluated"]:
            position.append(_nonnegative(frame["position_error_m"]))
            orientation.append(_nonnegative(frame["orientation_error_deg"]))
        elif frame.get("position_error_m") is not None or frame.get("orientation_error_deg") is not None:
            raise ValueError("Unsupported samples must not contain error scores.")
    metrics = None
    if position:
        orientation_rmse = math.sqrt(math.fsum(x*x for x in orientation) / len(orientation))
        metrics = {
            "ate_rmse_m": math.sqrt(math.fsum(x*x for x in position) / len(position)),
            "position_error_mean_m": math.fsum(position) / len(position),
            "position_error_max_m": max(position),
            "orientation_rmse_rad": math.radians(orientation_rmse),
            "orientation_rmse_deg": orientation_rmse,
            "orientation_error_mean_deg": math.fsum(orientation) / len(orientation),
            "orientation_error_max_deg": max(orientation),
        }
        if not all(math.isfinite(value) for value in metrics.values()):
            raise ValueError("Interval errors exceed the numerical range.")
    status = ("no_camera_samples" if not count else
              "global_alignment_unavailable" if investigation.get("alignment") is None else
              "no_evaluated_samples" if metrics is None else "ok")
    return {
        "schema_version": 1, "status": status,
        "interval": {"start_timestamp_ns": start_ns, "end_timestamp_ns": end_ns,
                     "start_time_s": (start-origin)*1e-9, "end_time_s": (end-origin)*1e-9,
                     "first_camera_index": selected[0]["index"] if selected else None,
                     "last_camera_index": selected[-1]["index"] if selected else None,
                     "boundary": "closed"},
        "alignment_scope": "full_recording_global_se3_unchanged",
        "counts": counts,
        "coverage_fraction": counts["estimate_samples"] / count if count else None,
        "reference_coverage_fraction": counts["reference_supported_camera_samples"] / count if count else None,
        "evaluated_fraction": counts["evaluated_camera_samples"] / count if count else None,
        "evaluated_reference_fraction": (counts["evaluated_camera_samples"] / counts["reference_supported_camera_samples"]
                                         if counts["reference_supported_camera_samples"] else None),
        "metrics": metrics, "protocol": investigation["protocol"],
    }
