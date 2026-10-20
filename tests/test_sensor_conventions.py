"""Independent convention examples for the sensor contracts; no data pipeline."""

import math
from pathlib import Path
import subprocess
import sys

import pytest

from meridian.sensor_conventions import (
    INT64_MAX,
    INT64_MIN,
    accel_to_m_s2,
    gyro_to_rad_s,
    invert_rigid_transform,
    map_constant_clock,
    quaternion_to_rotation,
    timestamp_ns,
    timestamp_to_ns,
    transform_point,
    validate_quaternion_xyzw,
    validate_rigid_transform,
    validate_rotation,
)


IDENTITY = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
QUARTER_TURN_Z = [[0, -1, 0], [1, 0, 0], [0, 0, 1]]
EPOCH = 1_520_530_308_199_447_626


@pytest.mark.parametrize("value", ["0", "-1", "1", str(INT64_MIN), str(INT64_MAX)])
def test_canonical_signed_timestamp_boundaries(value):
    assert timestamp_ns(value) == int(value)


def test_epoch_nanoseconds_keep_a_single_nanosecond():
    assert timestamp_ns(str(EPOCH + 1)) - timestamp_ns(str(EPOCH)) == 1
    assert timestamp_to_ns(str(EPOCH + 1), "ns") == EPOCH + 1


@pytest.mark.parametrize("value", [
    True, False, 1, 1.0, None, "", "+1", "01", "-01", "-0", " 1", "1 ",
    "1\n", "1.0", "1e9", "1_000", "１２", str(INT64_MIN - 1), str(INT64_MAX + 1),
    "9" * 5000,
])
def test_timestamp_rejects_noncanonical_or_out_of_range_input(value):
    with pytest.raises(ValueError):
        timestamp_ns(value)


@pytest.mark.parametrize("value,unit,expected", [
    ("1520530308", "s", 1_520_530_308_000_000_000),
    ("1520530308199", "ms", 1_520_530_308_199_000_000),
    ("1520530308199447", "us", 1_520_530_308_199_447_000),
    ("-2", "s", -2_000_000_000),
    ("0", "ns", 0),
])
def test_time_units_scale_integer_ticks_exactly(value, unit, expected):
    assert timestamp_to_ns(value, unit) == expected


@pytest.mark.parametrize("unit", ["seconds", "µs", "", None, [], True])
def test_time_conversion_requires_an_explicit_supported_unit(unit):
    with pytest.raises(ValueError):
        timestamp_to_ns("1", unit)


@pytest.mark.parametrize("value,unit", [
    ("9223372037", "s"), ("-9223372037", "s"),
    (str(INT64_MAX), "ms"), (str(INT64_MIN), "us"), (1.5, "s"),
])
def test_time_conversion_never_rounds_or_wraps(value, unit):
    with pytest.raises(ValueError):
        timestamp_to_ns(value, unit)


def test_explicit_sensor_units_have_known_physical_values():
    assert gyro_to_rad_s(180, "deg/s") == pytest.approx(math.pi)
    assert gyro_to_rad_s(-90, "deg/s") == pytest.approx(-math.pi / 2)
    assert gyro_to_rad_s(0.125, "rad/s") == 0.125
    assert accel_to_m_s2(-2, "g", local_gravity_m_s2=9.81) == -19.62
    assert accel_to_m_s2(1, "g", local_gravity_m_s2=9.7) == 9.7
    assert accel_to_m_s2(-3, "m/s^2") == -3


@pytest.mark.parametrize("gravity", [None, 0, -1, True, "9.81", math.nan, math.inf])
def test_gravity_unit_requires_explicit_finite_positive_local_gravity(gravity):
    with pytest.raises(ValueError):
        accel_to_m_s2(1, "g", local_gravity_m_s2=gravity)


@pytest.mark.parametrize("value", [True, "2", None, math.inf, math.nan, 10**1000])
def test_sensor_conversion_rejects_invalid_real_values(value):
    with pytest.raises(ValueError):
        gyro_to_rad_s(value, "rad/s")
    with pytest.raises(ValueError):
        accel_to_m_s2(value, "m/s^2")


@pytest.mark.parametrize("unit", ["degrees/s", "m/s/s", [], None])
def test_sensor_conversion_rejects_unrecognized_units(unit):
    with pytest.raises(ValueError):
        gyro_to_rad_s(1, unit)
    with pytest.raises(ValueError):
        accel_to_m_s2(1, unit)


def test_acceleration_conversion_rejects_numerical_overflow():
    with pytest.raises(ValueError, match="finite numerical range"):
        accel_to_m_s2(1e308, "g", local_gravity_m_s2=10)


def test_point_transform_direction_translation_and_analytic_inverse():
    # Rotation alone maps +x to +y. Translation is expressed in target axes.
    assert transform_point([1, 0, 0], QUARTER_TURN_Z, [0, 0, 0]) == (0, 1, 0)
    assert transform_point([2, 3, 4], QUARTER_TURN_Z, [10, 20, 30]) == (7, 22, 34)
    rotation, translation = invert_rigid_transform(QUARTER_TURN_Z, [10, 20, 30])
    assert rotation == ((0, 1, 0), (-1, 0, 0), (0, 0, 1))
    assert translation == (-20, 10, -30)
    assert transform_point([7, 22, 34], rotation, translation) == (2, 3, 4)


def test_geometry_validation_returns_copies_and_never_repairs_inputs():
    rotation = [row[:] for row in IDENTITY]
    translation = [1, 2, 3]
    saved_rotation, saved_translation = validate_rigid_transform(rotation, translation)
    rotation[0][0] = 2
    translation[0] = 9
    assert saved_rotation == ((1, 0, 0), (0, 1, 0), (0, 0, 1))
    assert saved_translation == (1, 2, 3)
    near_identity = [[1 + 1e-10, 0, 0], [0, 1, 0], [0, 0, 1]]
    assert validate_rotation(near_identity)[0][0] == near_identity[0][0]
    with pytest.raises(ValueError):
        validate_rotation([[1 + 1e-8, 0, 0], [0, 1, 0], [0, 0, 1]])


def test_rotation_tolerance_also_constrains_the_inverse_direction():
    # Row-Gram deviations are 9.8e-10 but column-Gram deviations are 1.39e-9.
    # Admitting this matrix would make its transpose fail the same validator.
    near_rotation = [
        [0.9238795331515026, -0.38268343209990363, 0],
        [0.3826834326302758, 0.9238795318710709, 0],
        [0, 0, 1],
    ]
    with pytest.raises(ValueError, match="orthonormal"):
        validate_rotation(near_rotation)


@pytest.mark.parametrize("rotation", [
    [[-1, 0, 0], [0, 1, 0], [0, 0, 1]],  # Reflection is orthonormal but improper.
    [[1, 0.1, 0], [0, 1, 0], [0, 0, 1]],
    [[2, 0, 0], [0, 2, 0], [0, 0, 2]],
    [[True, 0, 0], [0, 1, 0], [0, 0, 1]],
    [[math.nan, 0, 0], [0, 1, 0], [0, 0, 1]],
    [[math.inf, 0, 0], [0, 1, 0], [0, 0, 1]],
    [[1e308, 0, 0], [0, 1, 0], [0, 0, 1]],
    [[1, 0], [0, 1]], None, "identity",
])
def test_rotation_rejects_reflection_shear_scale_nonfinite_and_bad_shapes(rotation):
    with pytest.raises(ValueError):
        validate_rotation(rotation)


@pytest.mark.parametrize("vector", [[1, 2], [0, 0, math.nan], [0, True, 0], None])
def test_point_and_translation_require_three_finite_reals(vector):
    with pytest.raises(ValueError):
        transform_point(vector, IDENTITY, [0, 0, 0])
    with pytest.raises(ValueError):
        validate_rigid_transform(IDENTITY, vector)


def test_point_arithmetic_overflow_is_an_explicit_failure():
    with pytest.raises(ValueError, match="finite numerical range"):
        transform_point([1e308, 0, 0], IDENTITY, [1e308, 0, 0])


def test_hamilton_xyzw_positive_quarter_turn_and_quaternion_sign():
    quaternion = [0, 0, math.sqrt(0.5), math.sqrt(0.5)]
    rotation = quaternion_to_rotation(quaternion)
    for actual, expected in zip(rotation, QUARTER_TURN_Z):
        assert actual == pytest.approx(expected, abs=1e-15)
    assert transform_point([1, 0, 0], rotation, [0, 0, 0]) == pytest.approx([0, 1, 0], abs=1e-15)
    opposite = quaternion_to_rotation([-value for value in quaternion])
    assert opposite == rotation
    assert validate_quaternion_xyzw([0, 0, 0, -1]) == (0, 0, 0, -1)


def test_half_turn_does_not_depend_on_a_nonzero_scalar_component():
    assert quaternion_to_rotation([1, 0, 0, 0]) == ((1, 0, 0), (0, -1, 0), (0, 0, -1))


@pytest.mark.parametrize("quaternion", [
    [0, 0, 0, 0], [0, 0, 0, 2], [0, 0, 0, 1 + 1e-8],
    [0, 0, 0, math.nan], [0, 0, 0, math.inf], [0, 0, 0, True],
    [1, 0, 0], None,
])
def test_quaternion_validation_does_not_normalize_invalid_inputs(quaternion):
    with pytest.raises(ValueError):
        validate_quaternion_xyzw(quaternion)
    with pytest.raises(ValueError):
        quaternion_to_rotation(quaternion)


def test_quaternion_conversion_admits_only_declared_norm_roundoff():
    quaternion = [0, 0, 0, 1 + 1e-10]
    assert validate_quaternion_xyzw(quaternion)[3] == quaternion[3]
    assert quaternion_to_rotation(quaternion) == ((1, 0, 0), (0, 1, 0), (0, 0, 1))


def clock(stamp, **changes):
    options = dict(source_origin_ns=EPOCH, target_origin_ns=EPOCH + 1000,
                   offset_ns=7, segment_start_ns=EPOCH, segment_end_ns=EPOCH + 10)
    options.update(changes)
    return map_constant_clock(stamp, **options)


def test_clock_mapping_known_offset_sign_and_closed_segment_endpoints():
    assert clock(EPOCH) == EPOCH + 1007
    assert clock(EPOCH + 1) == EPOCH + 1008
    assert clock(EPOCH + 10) == EPOCH + 1017
    assert clock(EPOCH + 1, offset_ns=-7) == EPOCH + 994
    assert clock(EPOCH, target_origin_ns=-20, offset_ns=-7) == -27


def test_clock_mapping_keeps_integer_precision_in_large_intermediate_difference():
    assert map_constant_clock(
        INT64_MAX, source_origin_ns=INT64_MIN, target_origin_ns=INT64_MIN,
        offset_ns=0, segment_start_ns=INT64_MIN, segment_end_ns=INT64_MAX,
    ) == INT64_MAX


@pytest.mark.parametrize("stamp", [EPOCH - 1, EPOCH + 11])
def test_clock_mapping_rejects_samples_outside_its_source_segment(stamp):
    with pytest.raises(ValueError, match="outside"):
        clock(stamp)


@pytest.mark.parametrize("changes", [
    {"target_origin_ns": INT64_MAX, "offset_ns": 1},
    {"target_origin_ns": INT64_MIN, "offset_ns": -1},
    {"segment_start_ns": EPOCH + 1, "segment_end_ns": EPOCH},
])
def test_clock_mapping_rejects_output_overflow_and_reversed_segments(changes):
    with pytest.raises(ValueError):
        clock(EPOCH, **changes)


@pytest.mark.parametrize("field", [
    "source_origin_ns", "target_origin_ns", "offset_ns",
    "segment_start_ns", "segment_end_ns",
])
@pytest.mark.parametrize("value", [True, 0.0, "0", INT64_MAX + 1, INT64_MIN - 1])
def test_every_clock_field_requires_exact_signed_int64(field, value):
    with pytest.raises(ValueError):
        clock(EPOCH, **{field: value})


@pytest.mark.parametrize("stamp", [True, 1.0, "1", INT64_MIN - 1, INT64_MAX + 1])
def test_clock_source_timestamp_requires_exact_signed_int64(stamp):
    with pytest.raises(ValueError):
        clock(stamp)


def test_invalid_contract_errors_do_not_echo_supplied_content():
    marker = "private-input-path-or-secret"
    for operation in (
        lambda: timestamp_ns(marker),
        lambda: timestamp_to_ns("1", marker),
        lambda: gyro_to_rad_s(marker, "rad/s"),
        lambda: accel_to_m_s2(1, marker),
        lambda: validate_rotation(marker),
        lambda: validate_quaternion_xyzw(marker),
        lambda: clock(EPOCH, offset_ns=marker),
    ):
        with pytest.raises(ValueError) as caught:
            operation()
        assert marker not in str(caught.value)


def test_conventions_import_and_run_without_third_party_packages():
    source = Path(__file__).parents[1] / "src"
    script = """
import sys
sys.path.insert(0, sys.argv[1])
from meridian.sensor_conventions import timestamp_ns, quaternion_to_rotation
assert timestamp_ns('1520530308199447627') == 1520530308199447627
assert quaternion_to_rotation([0, 0, 0, 1])[0] == (1, 0, 0)
assert 'numpy' not in sys.modules
"""
    subprocess.run([sys.executable, "-I", "-S", "-c", script, str(source)],
                   check=True, capture_output=True, text=True, timeout=10)
