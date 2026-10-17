"""Independent, fixed-scale trajectory evaluation for the first VIO reference.

Poses use metres and Hamilton ``xyzw`` unit quaternions rotating body vectors
into the trajectory's world frame. Integer nanoseconds are never converted to
floating-point absolute times. The evaluator fits one global SE(3) gauge over
all evaluated samples; it does not fit scale, time offset, or separate segments.
Consequently constant world-frame rotation (including roll/pitch) and translation
are removed, while body-frame mistakes and time-varying errors remain measurable.

Reference positions are linearly interpolated and orientations use shortest-arc
SLERP. Interpolation requires a reference bracket no wider than the declared gap;
there is no extrapolation. Estimates are never interpolated or gap-filled. Each
must match a distinct expected camera timestamp within the declared tolerance.
Coverage always uses the complete supplied camera schedule, including startup.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np


TRAJECTORY_COLUMNS = (
    "timestamp_ns", "px_m", "py_m", "pz_m", "qx", "qy", "qz", "qw",
)
QUATERNION_NORM_TOLERANCE = 1e-6


def _timestamps(values: Sequence[int] | np.ndarray, name: str) -> np.ndarray:
    array = np.asarray(values)
    if array.ndim != 1:
        raise ValueError(f"{name} must be a one-dimensional integer sequence")
    # Inspect original values: casting a float/bool array to int64 is not allowed.
    items = list(values)
    bounds = np.iinfo(np.int64)
    for value in items:
        if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)):
            raise ValueError(f"{name} must contain integer nanoseconds, not floats or booleans")
        if not bounds.min <= int(value) <= bounds.max:
            raise ValueError(f"{name} is outside the int64 nanosecond range")
    result = np.array(items, dtype=np.int64)
    if np.any(result[1:] <= result[:-1]):
        raise ValueError(f"{name} must be strictly increasing without duplicates")
    return result


def _real_array(values, shape: tuple[int, int], name: str) -> np.ndarray:
    array = np.asarray(values)
    if array.shape != shape or array.dtype.kind not in "iuf":
        raise ValueError(f"{name} must be a real numeric array with shape {shape}")
    result = np.array(array, dtype=np.float64, copy=True)
    if not np.all(np.isfinite(result)):
        raise ValueError(f"{name} must be finite")
    return result


@dataclass(frozen=True)
class Trajectory:
    """Validated pose arrays; empty trajectories represent absent estimator output."""

    timestamp_ns: np.ndarray
    position_m: np.ndarray
    quaternion_xyzw: np.ndarray

    def __post_init__(self) -> None:
        times = _timestamps(self.timestamp_ns, "trajectory timestamps")
        position = _real_array(self.position_m, (len(times), 3), "positions")
        quaternion = _real_array(self.quaternion_xyzw, (len(times), 4), "quaternions")
        norms = np.linalg.norm(quaternion, axis=1)
        if not np.all(np.isfinite(norms)) or np.any(np.abs(norms - 1) > QUATERNION_NORM_TOLERANCE):
            raise ValueError("quaternions must be unit Hamilton xyzw quaternions")
        # Correct serialization roundoff only, after enforcing the unit-norm contract.
        quaternion /= norms[:, None]
        for name, array in (("timestamp_ns", times), ("position_m", position),
                            ("quaternion_xyzw", quaternion)):
            array.flags.writeable = False
            object.__setattr__(self, name, array)


def read_trajectory(path: str | Path) -> Trajectory:
    """Read the exact normalized CSV schema, without silently dropping bad rows."""
    times, positions, quaternions = [], [], []
    with Path(path).open(newline="", encoding="utf-8") as stream:
        reader = csv.reader(stream)
        if next(reader, None) != list(TRAJECTORY_COLUMNS):
            raise ValueError("trajectory CSV header must match the normalized pose schema")
        for line, row in enumerate(reader, start=2):
            if len(row) != len(TRAJECTORY_COLUMNS):
                raise ValueError(f"trajectory CSV row {line} has the wrong column count")
            try:
                if re.fullmatch(r"[+-]?[0-9]+", row[0]) is None:
                    raise ValueError("timestamp is not a decimal integer")
                timestamp = int(row[0])
                values = [float(value) for value in row[1:]]
            except ValueError as error:
                raise ValueError(f"invalid numeric value in trajectory CSV row {line}") from error
            times.append(timestamp)
            positions.append(values[:3])
            quaternions.append(values[3:])
    return Trajectory(
        times, np.asarray(positions, dtype=float).reshape(-1, 3),
        np.asarray(quaternions, dtype=float).reshape(-1, 4),
    )


def _integer_option(value: int, name: str, minimum: int) -> int:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)):
        raise ValueError(f"{name} must be an integer")
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return int(value)


def _slerp(first: np.ndarray, second: np.ndarray, fraction: float) -> np.ndarray:
    dot = float(first @ second)
    if dot < 0:
        second, dot = -second, -dot
    dot = np.clip(dot, -1.0, 1.0)
    if dot > 0.9995:
        result = first + fraction * (second - first)
    else:
        angle = np.arccos(dot)
        result = (np.sin((1 - fraction) * angle) * first
                  + np.sin(fraction * angle) * second) / np.sin(angle)
    return result / np.linalg.norm(result)


def _reference_at(reference: Trajectory, expected: np.ndarray, maximum_gap: int):
    supported = np.zeros(len(expected), dtype=bool)
    positions = np.zeros((len(expected), 3))
    quaternions = np.zeros((len(expected), 4))
    for index, stamp in enumerate(expected):
        upper = int(np.searchsorted(reference.timestamp_ns, stamp))
        if upper < len(reference.timestamp_ns) and reference.timestamp_ns[upper] == stamp:
            supported[index] = True
            positions[index] = reference.position_m[upper]
            quaternions[index] = reference.quaternion_xyzw[upper]
        elif 0 < upper < len(reference.timestamp_ns):
            lower_time = int(reference.timestamp_ns[upper - 1])
            width = int(reference.timestamp_ns[upper]) - lower_time
            if width <= maximum_gap:
                fraction = (int(stamp) - lower_time) / width
                supported[index] = True
                positions[index] = ((1 - fraction) * reference.position_m[upper - 1]
                                    + fraction * reference.position_m[upper])
                quaternions[index] = _slerp(
                    reference.quaternion_xyzw[upper - 1],
                    reference.quaternion_xyzw[upper], fraction,
                )
    return supported, positions, quaternions


def _estimate_indices(estimate: Trajectory, expected: np.ndarray, tolerance: int):
    indices, errors = [], []
    for stamp in estimate.timestamp_ns:
        upper = int(np.searchsorted(expected, stamp))
        candidates = [index for index in (upper - 1, upper) if 0 <= index < len(expected)]
        differences = [abs(int(stamp) - int(expected[index])) for index in candidates]
        smallest = min(differences)
        if smallest > tolerance:
            raise ValueError("estimate timestamp does not match the expected camera schedule")
        if differences.count(smallest) != 1:
            raise ValueError("estimate timestamp has an ambiguous camera association")
        indices.append(candidates[differences.index(smallest)])
        errors.append(smallest)
    if len(set(indices)) != len(indices):
        raise ValueError("multiple estimates match the same expected camera timestamp")
    return np.asarray(indices, dtype=int), errors


def _missing_intervals(expected: np.ndarray, missing: np.ndarray) -> list[dict]:
    """Inclusive missing sample runs; span is not a duration-weighted coverage metric."""
    indices = np.flatnonzero(missing)
    if not len(indices):
        return []
    groups = np.split(indices, np.flatnonzero(np.diff(indices) > 1) + 1)
    return [{
        "first_timestamp_ns": int(expected[group[0]]),
        "last_timestamp_ns": int(expected[group[-1]]),
        "sample_count": int(len(group)),
        "span_s": (int(expected[group[-1]]) - int(expected[group[0]])) * 1e-9,
    } for group in groups]


def _rotation_matrices(quaternions: np.ndarray) -> np.ndarray:
    x, y, z, w = quaternions.T
    result = np.empty((len(quaternions), 3, 3))
    result[:, 0, 0], result[:, 0, 1], result[:, 0, 2] = 1 - 2 * (y*y + z*z), 2 * (x*y - z*w), 2 * (x*z + y*w)
    result[:, 1, 0], result[:, 1, 1], result[:, 1, 2] = 2 * (x*y + z*w), 1 - 2 * (x*x + z*z), 2 * (y*z - x*w)
    result[:, 2, 0], result[:, 2, 1], result[:, 2, 2] = 2 * (x*z - y*w), 2 * (y*z + x*w), 1 - 2 * (x*x + y*y)
    return result


def _orientation_errors(aligned: np.ndarray, reference: np.ndarray) -> np.ndarray:
    relative = np.einsum("nji,njk->nik", reference, aligned)
    skew = np.stack((relative[:, 2, 1] - relative[:, 1, 2],
                     relative[:, 0, 2] - relative[:, 2, 0],
                     relative[:, 1, 0] - relative[:, 0, 1]), axis=1)
    sine = 0.5 * np.linalg.norm(skew, axis=1)
    cosine = np.clip((np.trace(relative, axis1=1, axis2=2) - 1) / 2, -1, 1)
    return np.arctan2(sine, cosine)


def evaluate_trajectories(
    estimate: Trajectory,
    reference: Trajectory,
    *,
    expected_timestamps_ns: Sequence[int] | np.ndarray,
    max_reference_gap_ns: int = 20_000_000,
    max_estimate_timestamp_error_ns: int = 1_000,
) -> dict:
    """Return JSON-compatible metrics, coverage and per-sample errors.

    The camera schedule is mandatory and must cover the complete selected input
    interval, not just timestamps with successful estimator output. At least
    three associated, non-collinear positions are needed to identify a global
    SE(3) alignment. Insufficient support returns an explicit status and null
    metrics, rather than a perfect zero score. Malformed inputs raise ValueError.
    This function computes evidence; it does not assign qualification thresholds.
    """
    if not isinstance(estimate, Trajectory) or not isinstance(reference, Trajectory):
        raise ValueError("estimate and reference must be validated Trajectory objects")
    expected = _timestamps(expected_timestamps_ns, "expected camera timestamps")
    if not len(expected):
        raise ValueError("expected camera timestamps must not be empty")
    gap = _integer_option(max_reference_gap_ns, "max_reference_gap_ns", 1)
    tolerance = _integer_option(max_estimate_timestamp_error_ns, "max_estimate_timestamp_error_ns", 0)
    estimate_indices, timestamp_errors = _estimate_indices(estimate, expected, tolerance)
    estimated = np.zeros(len(expected), dtype=bool)
    estimated[estimate_indices] = True
    reference_supported, reference_position, reference_quaternion = _reference_at(reference, expected, gap)
    evaluated = estimated & reference_supported
    count = int(evaluated.sum())
    reference_count = int(reference_supported.sum())
    first = int(estimate_indices[0]) if len(estimate_indices) else None
    last = int(estimate_indices[-1]) if len(estimate_indices) else None
    result = {
        "schema_version": 1,
        "protocol": {
            "name": "meridian-vio-se3", "version": 1,
            "reference_association": "linear_position_shortest_arc_slerp_without_extrapolation",
            "max_reference_gap_ns": gap,
            "max_estimate_timestamp_error_ns": tolerance,
            "estimate_interpolation": False,
            "alignment": "global_se3_least_squares_over_all_evaluated_positions",
            "removed_gauge": "constant_world_rotation_including_roll_pitch_yaw_and_translation",
            "scale_fitted": False, "time_offset_fitted": False,
            "weighting": "equal_weight_per_evaluated_camera_sample",
            "initialization_exclusion": "none",
            "initialization_delay_definition": "first_estimated_camera_timestamp_minus_first_expected_timestamp",
            "quaternion_convention": "Hamilton_xyzw_body_to_world",
        },
        "status": "insufficient_associations",
        "counts": {
            "expected_camera_samples": int(len(expected)),
            "estimate_samples": int(len(estimate.timestamp_ns)),
            "reference_samples": int(len(reference.timestamp_ns)),
            "reference_supported_camera_samples": reference_count,
            "evaluated_camera_samples": count,
            "missing_initial_camera_samples": first if first is not None else int(len(expected)),
            "missing_interior_camera_samples": int((~estimated[first:last + 1]).sum()) if first is not None else 0,
            "missing_trailing_camera_samples": len(expected) - last - 1 if last is not None else 0,
        },
        "coverage_fraction": float(estimated.mean()),
        "reference_coverage_fraction": float(reference_supported.mean()),
        "evaluated_fraction": float(evaluated.mean()),
        "evaluated_reference_fraction": count / reference_count if reference_count else None,
        "initialization_delay_s": (int(expected[first]) - int(expected[0])) * 1e-9 if first is not None else None,
        "timing": {
            "expected_first_timestamp_ns": int(expected[0]),
            "expected_last_timestamp_ns": int(expected[-1]),
            "first_estimate_timestamp_ns": int(estimate.timestamp_ns[0]) if first is not None else None,
            "last_estimate_timestamp_ns": int(estimate.timestamp_ns[-1]) if first is not None else None,
            "maximum_estimate_association_error_ns": max(timestamp_errors) if timestamp_errors else None,
        },
        "missing_estimate_intervals": _missing_intervals(expected, ~estimated),
        "unsupported_reference_intervals": _missing_intervals(expected, ~reference_supported),
        "alignment": None, "metrics": None, "per_sample": None,
    }
    if count < 3:
        return result

    selected_estimate = reference_supported[estimate_indices]
    positions = estimate.position_m[selected_estimate]
    target = reference_position[evaluated]
    centered = positions - positions.mean(axis=0)
    target_centered = target - target.mean(axis=0)
    covariance = centered.T @ target_centered
    if not np.all(np.isfinite(covariance)):
        raise ValueError("positions exceed the numerical range for SE3 alignment")
    left, singular, right_transpose = np.linalg.svd(covariance)
    if singular[0] <= 0 or singular[1] <= singular[0] * 1e-10:
        result["status"] = "degenerate_alignment"
        return result
    correction = np.eye(3)
    correction[2, 2] = -1.0 if np.linalg.det(right_transpose.T @ left.T) < 0 else 1.0
    rotation = right_transpose.T @ correction @ left.T
    translation = target.mean(axis=0) - rotation @ positions.mean(axis=0)
    position_error = np.linalg.norm(positions @ rotation.T + translation - target, axis=1)
    rotations = _rotation_matrices(estimate.quaternion_xyzw[selected_estimate])
    aligned_rotations = np.einsum("ij,njk->nik", rotation, rotations)
    orientation_error = _orientation_errors(aligned_rotations, _rotation_matrices(reference_quaternion[evaluated]))
    metrics = {
        "ate_rmse_m": float(np.sqrt(np.mean(position_error**2))),
        "position_error_mean_m": float(position_error.mean()),
        "position_error_max_m": float(position_error.max()),
        "orientation_rmse_rad": float(np.sqrt(np.mean(orientation_error**2))),
        "orientation_rmse_deg": float(np.rad2deg(np.sqrt(np.mean(orientation_error**2)))),
        "orientation_error_mean_deg": float(np.rad2deg(orientation_error.mean())),
        "orientation_error_max_deg": float(np.rad2deg(orientation_error.max())),
    }
    if not all(np.isfinite(value) for value in metrics.values()):
        raise ValueError("evaluation produced non-finite metrics")
    result.update({
        "status": "ok",
        "alignment": {
            "rotation_estimate_world_to_reference_world": rotation.tolist(),
            "translation_m": translation.tolist(), "scale": 1.0,
            "position_cross_covariance_singular_values": singular.tolist(),
        },
        "metrics": metrics,
        "per_sample": {
            "timestamp_ns": [int(stamp) for stamp in expected[evaluated]],
            "position_error_m": position_error.tolist(),
            "orientation_error_deg": np.rad2deg(orientation_error).tolist(),
        },
    })
    return result


def evaluate_trajectory(
    estimate_path: str | Path,
    reference_path: str | Path,
    *,
    expected_timestamps_ns: Sequence[int] | np.ndarray,
    max_reference_gap_ns: int = 20_000_000,
    max_estimate_timestamp_error_ns: int = 1_000,
) -> dict:
    """CSV entry point; see :func:`evaluate_trajectories` for the fixed protocol."""
    return evaluate_trajectories(
        read_trajectory(estimate_path), read_trajectory(reference_path),
        expected_timestamps_ns=expected_timestamps_ns,
        max_reference_gap_ns=max_reference_gap_ns,
        max_estimate_timestamp_error_ns=max_estimate_timestamp_error_ns,
    )
