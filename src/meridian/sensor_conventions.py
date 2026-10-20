"""Standard-library primitives for explicit sensor-contract conventions.

These helpers check declared scalar units, rigid geometry and constant clock
mappings. They do not import recordings, infer calibration, estimate clock
relationships, resample observations or execute a preparation pipeline.
"""

import math
import re


INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1
GEOMETRY_TOLERANCE = 1e-9
_TIME_FACTORS_NS = {"s": 1_000_000_000, "ms": 1_000_000, "us": 1_000, "ns": 1}


def _int64(value, message):
    if type(value) is not int or not INT64_MIN <= value <= INT64_MAX:
        raise ValueError(message)
    return value


def timestamp_ns(value: str) -> int:
    """Parse a canonical signed int64 decimal string, without float conversion."""
    if (type(value) is not str or len(value) > 20
            or re.fullmatch(r"0|-?[1-9][0-9]*", value) is None):
        raise ValueError("Timestamp must be a canonical decimal integer string.")
    return _int64(int(value), "Timestamp is outside the signed int64 range.")


def timestamp_to_ns(value: str, unit: str) -> int:
    """Convert integer s/ms/us/ns ticks to int64 ns with exact integer scaling.

    Fractional or floating-point absolute timestamps are not accepted. Scaling
    preserves the supplied clock origin; it does not synchronize clocks.
    """
    if type(unit) is not str or unit not in _TIME_FACTORS_NS:
        raise ValueError("Timestamp unit must be s, ms, us or ns.")
    result = timestamp_ns(value) * _TIME_FACTORS_NS[unit]
    return _int64(result, "Converted timestamp is outside the signed int64 range.")


def _finite_number(value):
    if type(value) not in (int, float):
        raise ValueError("Expected a finite real number.")
    try:
        result = float(value)
    except OverflowError as error:
        raise ValueError("Real number exceeds the finite numerical range.") from error
    if not math.isfinite(result):
        raise ValueError("Expected a finite real number.")
    return result


def gyro_to_rad_s(value, unit: str) -> float:
    """Convert a scalar rate declared in rad/s or deg/s to rad/s."""
    if type(unit) is not str or unit not in ("rad/s", "deg/s"):
        raise ValueError("Gyroscope unit must be rad/s or deg/s.")
    number = _finite_number(value)
    return number if unit == "rad/s" else number * (math.pi / 180.0)


def accel_to_m_s2(value, unit: str, *, local_gravity_m_s2=None) -> float:
    """Convert m/s^2 or explicitly defined local-gravity units to m/s^2.

    Here ``g`` means the supplied positive local gravity, not an implicit
    standard-gravity constant. The source must support that interpretation.
    This scalar conversion does not remove gravity or calibrate an IMU.
    """
    if type(unit) is not str or unit not in ("m/s^2", "g"):
        raise ValueError("Accelerometer unit must be m/s^2 or g.")
    number = _finite_number(value)
    gravity = None
    if local_gravity_m_s2 is not None:
        gravity = _finite_number(local_gravity_m_s2)
        if gravity <= 0:
            raise ValueError("Local gravity must be positive.")
    if unit == "m/s^2":
        return number
    if gravity is None:
        raise ValueError("Conversion from g requires explicit positive local gravity.")
    result = number * gravity
    if not math.isfinite(result):
        raise ValueError("Converted acceleration exceeds the finite numerical range.")
    return result


def _vector(value, length):
    if type(value) not in (list, tuple) or len(value) != length:
        raise ValueError("Vector has an unsupported shape.")
    return tuple(_finite_number(item) for item in value)


def validate_rotation(rotation) -> tuple[tuple[float, ...], ...]:
    """Copy a finite proper 3x3 rotation without projecting it onto SO(3).

    Every entry of R*R^T-I, R^T*R-I and det(R)-1 must have absolute error at most
    1e-9. Checking both Gram matrices preserves the tolerance on inversion.
    Reflections, scale and shear outside that tolerance are rejected.
    """
    if type(rotation) not in (list, tuple) or len(rotation) != 3:
        raise ValueError("Rotation must be a 3 by 3 matrix.")
    rows = tuple(_vector(row, 3) for row in rotation)
    if any(abs(entry) > 1 + GEOMETRY_TOLERANCE for row in rows for entry in row):
        raise ValueError("Rotation must be orthonormal.")
    for i in range(3):
        for j in range(3):
            row_dot = math.fsum(rows[i][k] * rows[j][k] for k in range(3))
            column_dot = math.fsum(rows[k][i] * rows[k][j] for k in range(3))
            identity = 1 if i == j else 0
            if (abs(row_dot - identity) > GEOMETRY_TOLERANCE
                    or abs(column_dot - identity) > GEOMETRY_TOLERANCE):
                raise ValueError("Rotation must be orthonormal.")
    a, b, c = rows
    determinant = (
        a[0] * (b[1] * c[2] - b[2] * c[1])
        - a[1] * (b[0] * c[2] - b[2] * c[0])
        + a[2] * (b[0] * c[1] - b[1] * c[0])
    )
    if abs(determinant - 1) > GEOMETRY_TOLERANCE:
        raise ValueError("Rotation must be proper with determinant positive one.")
    return rows


def validate_rigid_transform(rotation, translation):
    """Copy R_target_source and t_target_source, whose translation is in metres."""
    return validate_rotation(rotation), _vector(translation, 3)


def _rotated_vector(rotation, vector):
    try:
        result = tuple(math.fsum(row[i] * vector[i] for i in range(3))
                       for row in rotation)
    except (OverflowError, ValueError) as error:
        raise ValueError("Transformed vector exceeds the finite numerical range.") from error
    if not all(math.isfinite(value) for value in result):
        raise ValueError("Transformed vector exceeds the finite numerical range.")
    return result


def transform_point(point, rotation, translation) -> tuple[float, ...]:
    """Apply p_target = R_target_source * p_source + t_target_source in metres.

    This point transform is not an acceleration transform: a change of IMU
    location requires lever-arm dynamics, not addition of a position offset.
    """
    matrix, offset = validate_rigid_transform(rotation, translation)
    rotated = _rotated_vector(matrix, _vector(point, 3))
    result = tuple(rotated[i] + offset[i] for i in range(3))
    if not all(math.isfinite(value) for value in result):
        raise ValueError("Transformed point exceeds the finite numerical range.")
    return result


def invert_rigid_transform(rotation, translation):
    """Return the inverse (R^T, -R^T*t) in the same metre convention."""
    matrix, offset = validate_rigid_transform(rotation, translation)
    inverse = tuple(tuple(matrix[j][i] for j in range(3)) for i in range(3))
    rotated = _rotated_vector(inverse, offset)
    return inverse, tuple(-value for value in rotated)


def validate_quaternion_xyzw(quaternion) -> tuple[float, ...]:
    """Copy a finite unit Hamilton quaternion, scalar last, without sign changes.

    Both q and -q represent the same rotation. Absolute norm error may be at
    most 1e-9; a non-unit quaternion is not silently repaired.
    """
    values = _vector(quaternion, 4)
    norm = math.hypot(*values)
    if abs(norm - 1) > GEOMETRY_TOLERANCE:
        raise ValueError("Quaternion must have unit norm.")
    return values


def quaternion_to_rotation(quaternion) -> tuple[tuple[float, ...], ...]:
    """Convert Hamilton xyzw to R_target_source for an active source-to-target map.

    The homogeneous formula accounts only for norm roundoff already admitted by
    validation. It does not accept arbitrary non-unit inputs or alter the stored
    quaternion. A positive z-axis quarter-turn maps source +x onto target +y.
    """
    x, y, z, w = validate_quaternion_xyzw(quaternion)
    scale = 2 / math.fsum(value * value for value in (x, y, z, w))
    return (
        (1 - scale * (y*y + z*z), scale * (x*y - z*w), scale * (x*z + y*w)),
        (scale * (x*y + z*w), 1 - scale * (x*x + z*z), scale * (y*z - x*w)),
        (scale * (x*z - y*w), scale * (y*z + x*w), 1 - scale * (x*x + y*y)),
    )


def map_constant_clock(stamp_ns: int, *, source_origin_ns: int,
                       target_origin_ns: int, offset_ns: int,
                       segment_start_ns: int, segment_end_ns: int) -> int:
    """Map one timestamp with unit clock rate within a closed source segment.

    t_target = target_origin + (t_source - source_origin) + offset

    All supplied values and the result must be signed int64 integers. Python
    integer intermediates remain exact even when subtracting distant origins.
    Positive offset advances the target timestamp. This helper neither infers
    the offset nor establishes clock stability, drift or sensor simultaneity.
    """
    for value in (stamp_ns, source_origin_ns, target_origin_ns, offset_ns,
                  segment_start_ns, segment_end_ns):
        _int64(value, "Clock mapping requires signed int64 integer values.")
    if segment_start_ns > segment_end_ns:
        raise ValueError("Clock segment bounds must be ordered.")
    if not segment_start_ns <= stamp_ns <= segment_end_ns:
        raise ValueError("Timestamp lies outside the closed source clock segment.")
    result = target_origin_ns + (stamp_ns - source_origin_ns) + offset_ns
    return _int64(result, "Mapped timestamp is outside the signed int64 range.")
