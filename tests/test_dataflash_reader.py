"""Exercise the real decoder with tiny, locally generated DataFlash binaries."""

import json
import math
import struct

import pytest

pytest.importorskip("pymavlink")

from meridian.dataflash import read_dataflash


_IMU = 130
_UNIT = 131
_MULT = 132
_FMTU = 133
_MSG = 134
_PARM = 135
_ARM = 136
_EV = 137
_ISBH = 138
_COLUMNS = "TimeUS,I,GyrX,GyrY,GyrZ,AccX,AccY,AccZ,EG,EA,T,GH,AH,GHz,AHz"
_FORMAT = "QBffffffIIfBBHH"


def _packet(type_id, format_string, *values):
    return b"\xa3\x95" + bytes([type_id]) + struct.pack("<" + format_string, *values)


def _fmt(type_id, name, format_string, struct_format, columns):
    return _packet(128, "BB4s16s64s", type_id, 3 + struct.calcsize("<" + struct_format),
                   name.encode(), format_string.encode(), columns.encode())


def _imu(time_us=1_000_000, instance=0, gyro=(0.1, -0.2, 0.3),
         accel=(0.0, 0.0, -9.81), errors=(0, 0), health=(1, 1)):
    return _packet(_IMU, _FORMAT, time_us, instance, *gyro, *accel, *errors,
                   25.0, *health, 1000, 1000)


def _binary(*messages, include_fmtu=True, unit_label=b"rad/s", multiplier=1.0,
            columns=_COLUMNS, unit_ids=b"s#EEEooo--O--zz", mult_ids=b"F-000000-----00"):
    definitions = b"".join([
        _fmt(_IMU, "IMU", _FORMAT, _FORMAT, columns),
        _fmt(_UNIT, "UNIT", "QbZ", "Qb64s", "TimeUS,Id,Label"),
        _fmt(_MULT, "MULT", "Qbd", "Qbd", "TimeUS,Id,Mult"),
        _fmt(_FMTU, "FMTU", "QBNN", "QB16s16s", "TimeUS,FmtType,UnitIds,MultIds"),
        _fmt(_MSG, "MSG", "QZ", "Q64s", "TimeUS,Message"),
        _fmt(_PARM, "PARM", "QNf", "Q16sf", "TimeUS,Name,Value"),
        _fmt(_ARM, "ARM", "QB", "QB", "TimeUS,ArmState"),
        _fmt(_EV, "EV", "QB", "QB", "TimeUS,Id"),
        _fmt(_ISBH, "ISBH", "QB", "QB", "TimeUS,type"),
    ])
    unit_definitions = b"".join([
        _packet(_UNIT, "Qb64s", 500_000, ord("s"), b"s"),
        _packet(_UNIT, "Qb64s", 500_000, ord("E"), unit_label),
        _packet(_UNIT, "Qb64s", 500_000, ord("o"), b"m/s/s"),
        _packet(_MULT, "Qbd", 500_000, ord("F"), 1e-6),
        _packet(_MULT, "Qbd", 500_000, ord("0"), multiplier),
    ])
    fmtu = _packet(_FMTU, "QB16s16s", 500_000, _IMU, unit_ids, mult_ids)
    return definitions + unit_definitions + (fmtu if include_fmtu else b"") + b"".join(messages)


def _write(tmp_path, content):
    path = tmp_path / "synthetic.bin"
    path.write_bytes(content)
    return path


def test_preserves_raw_scalars_instances_order_and_nonfinite_sensor_values(tmp_path):
    path = _write(tmp_path, _binary(
        _imu(1_000_000, 1), _imu(1_000_000, 0),
        _imu(900_000, 1, gyro=(float("nan"), float("inf"), -0.25)),
    ))
    before = path.read_bytes()
    data = read_dataflash(path)
    assert [item.time_us for item in data.records] == [1_000_000, 1_000_000, 900_000]
    assert [item.instance for item in data.records] == [1, 0, 1]
    assert data.records[0].gyro_rad_s == pytest.approx((0.1, -0.2, 0.3))
    assert data.records[0].accel_m_s2 == pytest.approx((0, 0, -9.81))
    assert math.isnan(data.records[2].gyro_rad_s[0])
    assert math.isinf(data.records[2].gyro_rad_s[1])
    assert data.metadata["imu_format"]["units"]["TimeUS"] == "us"
    assert data.metadata["imu_format"]["units"]["GyrX"] == "rad/s"
    assert data.metadata["imu_format"]["units"]["AccZ"] == "m/s^2"
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]
    json.dumps(data.metadata, allow_nan=False)


def test_metadata_is_allowlisted_and_preserves_parameter_and_arming_observations(tmp_path):
    data = read_dataflash(_write(tmp_path, _binary(
        _packet(_MSG, "Q64s", 500_000, b"ArduCopter-ARGOS V4.8.0-dev (8927564c)"),
        _packet(_MSG, "Q64s", 500_000, b"PRIVATE SERIAL OR LOCATION"),
        _packet(_PARM, "Q16sf", 500_000, b"LOG_DISARMED", 1.0),
        _packet(_PARM, "Q16sf", 600_000, b"LOG_DISARMED", 0.0),
        _packet(_PARM, "Q16sf", 500_000, b"PRIVATE_PARAM", 123.0),
        _packet(_ARM, "QB", 700_000, 1),
        _packet(_EV, "QB", 700_001, 10),
        _packet(_EV, "QB", 800_000, 11),
        _packet(_EV, "QB", 800_001, 99),
        _packet(_ISBH, "QB", 800_000, 0),
        _packet(_ISBH, "QB", 800_000, 1),
        _packet(_ISBH, "QB", 800_000, 1),
        _imu(),
    )))
    metadata = data.metadata
    assert metadata["firmware_banners"] == ["ArduCopter-ARGOS V4.8.0-dev (8927564c)"]
    assert [item["value"] for item in metadata["parameters"]] == [1.0, 0.0]
    assert [item["armed"] for item in metadata["arming_events"]] == [True, True, False]
    assert metadata["batch_headers"] == {"accelerometer": 1, "gyroscope": 2, "other": 0}
    assert "PRIVATE" not in json.dumps(metadata)
    assert str(tmp_path) not in json.dumps(metadata)


def test_health_and_error_counters_do_not_remove_unhealthy_records(tmp_path):
    data = read_dataflash(_write(tmp_path, _binary(
        _imu(errors=(3, 4)), _imu(1_040_000, errors=(7, 2), health=(0, 1)),
        _imu(1_080_000, 1, errors=(8, 9), health=(0, 0)),
    )))
    assert len(data.records) == 3
    health = data.metadata["imu_health"]
    assert health["0"]["GH"]["unhealthy_count"] == 1
    assert health["0"]["AH"]["unhealthy_count"] == 0
    assert health["1"]["AH"]["unhealthy_count"] == 1
    assert health["0"]["EG"] == {
        "observations": 2, "nonfinite_count": 0,
        "first": 3.0, "last": 7.0, "min": 3.0, "max": 7.0,
    }
    assert health["0"]["EA"]["min"] == 2.0
    assert data.metadata["arming_events"] == []
    assert "does not establish" in data.metadata["arming_evidence_limit"]


@pytest.mark.parametrize("kwargs", [
    {"include_fmtu": False}, {"unit_label": b"deg/s"}, {"multiplier": 0.01},
    {"unit_ids": b"s#???ooo--O--zz"}, {"mult_ids": b"?-000000-----00"},
    {"unit_ids": b"s#E"},
])
def test_refuses_unknown_missing_or_unsupported_units(tmp_path, kwargs):
    with pytest.raises(ValueError, match="IMU"):
        read_dataflash(_write(tmp_path, _binary(_imu(), **kwargs)))


@pytest.mark.parametrize("conflict", [
    _packet(_UNIT, "Qb64s", 900_000, ord("E"), b"deg/s"),
    _packet(_MULT, "Qbd", 900_000, ord("0"), 0.01),
    _packet(_FMTU, "QB16s16s", 900_000, _IMU, b"s#oooEEE--O--zz", b"F-000000-----00"),
])
def test_refuses_conflicting_definitions_even_after_sensor_records(tmp_path, conflict):
    with pytest.raises(ValueError, match="IMU"):
        read_dataflash(_write(tmp_path, _binary(_imu(), conflict)))


def test_refuses_missing_required_imu_column(tmp_path):
    with pytest.raises(ValueError, match="missing required fields: GyrX"):
        read_dataflash(_write(tmp_path, _binary(_imu(), columns=_COLUMNS.replace("GyrX", "NoX"))))


@pytest.mark.parametrize("content", [b"", b"not a dataflash log", _binary(), _binary(_imu()[:-10])])
def test_empty_malformed_or_no_complete_imu_input_fails(tmp_path, content):
    with pytest.raises(ValueError):
        read_dataflash(_write(tmp_path, content))


def test_missing_path_does_not_create_a_file(tmp_path):
    path = tmp_path / "missing.bin"
    with pytest.raises(ValueError, match="existing"):
        read_dataflash(path)
    assert not path.exists()


def test_truncated_tail_is_reported_without_discarding_complete_records(tmp_path):
    tail = _imu(1_040_000)[:-10]
    data = read_dataflash(_write(tmp_path, _binary(_imu(), tail)))
    assert len(data.records) == 1
    assert data.metadata["unparsed_tail_bytes"] == len(tail)


@pytest.mark.parametrize("tail", [b"", _imu()[:3], _imu()[:8], b"x" * 100])
def test_tail_count_includes_partial_header_and_skipped_garbage(tmp_path, tail):
    data = read_dataflash(_write(tmp_path, _binary(_imu(), tail)))
    assert len(data.records) == 1
    assert data.metadata["unparsed_tail_bytes"] == len(tail)


def test_parser_diagnostics_are_counted_without_printing_or_retaining_text(tmp_path, capfd):
    data = read_dataflash(_write(tmp_path, _binary(b"PRIVATE_GARBAGE" * 60, _imu())))
    assert len(data.records) == 1
    assert data.metadata["parser_diagnostic_count"] > 0
    assert capfd.readouterr() == ("", "")
    assert "PRIVATE_GARBAGE" not in json.dumps(data.metadata)


def test_decoder_resources_close_after_success_and_record_validation_failure(tmp_path, monkeypatch):
    from pymavlink import DFReader

    objects = []
    original_init = DFReader.DFReader_binary.__init__

    def observed_init(reader, *args, **kwargs):
        objects.append(reader)
        return original_init(reader, *args, **kwargs)

    monkeypatch.setattr(DFReader.DFReader_binary, "__init__", observed_init)
    read_dataflash(_write(tmp_path, _binary(_imu())))
    with pytest.raises(ValueError):
        read_dataflash(_write(tmp_path, _binary(_imu(), columns=_COLUMNS.replace("GyrX", "NoX"))))
    assert all(reader.filehandle.closed and reader.data_map.closed for reader in objects)


def test_decoder_resources_close_if_initialization_fails(tmp_path, monkeypatch):
    from pymavlink import DFReader

    objects = []
    original_init = DFReader.DFReader_binary.__init__

    def failed_init(reader, *args, **kwargs):
        objects.append(reader)
        original_init(reader, *args, **kwargs)
        raise struct.error("PRIVATE PARSER DETAILS")

    monkeypatch.setattr(DFReader.DFReader_binary, "__init__", failed_init)
    with pytest.raises(ValueError, match="Cannot decode supported DataFlash records: error") as error:
        read_dataflash(_write(tmp_path, _binary(_imu())))
    assert "PRIVATE" not in str(error.value)
    assert objects[0].filehandle.closed and objects[0].data_map.closed


@pytest.mark.parametrize("time_us,instance", [(-1, 0), (1_000_000, -1)])
def test_negative_integer_timestamp_or_instance_is_refused(tmp_path, time_us, instance):
    signed_format = "qbffffffIIfBBHH"
    content = _binary().replace(_FORMAT.encode(), signed_format.encode())
    content += _packet(_IMU, signed_format, time_us, instance, 0, 0, 0, 0, 0, -9.81,
                       0, 0, 25, 1, 1, 1000, 1000)
    with pytest.raises(ValueError, match="nonnegative integers"):
        read_dataflash(_write(tmp_path, content))


def test_absent_optional_health_fields_are_not_assumed_healthy(tmp_path):
    minimal_columns = "TimeUS,I,GyrX,GyrY,GyrZ,AccX,AccY,AccZ"
    content = _binary().replace(
        _fmt(_IMU, "IMU", _FORMAT, _FORMAT, _COLUMNS),
        _fmt(_IMU, "IMU", "QBffffff", "QBffffff", minimal_columns),
    )
    content += _packet(_IMU, "QBffffff", 1_000_000, 0, 0, 0, 0, 0, 0, -9.81)
    data = read_dataflash(_write(tmp_path, content))
    assert len(data.records) == 1
    assert data.metadata["imu_health"] == {"0": {}}


def test_missing_optional_dependency_reports_install_command(tmp_path, monkeypatch):
    import builtins

    original_import = builtins.__import__

    def without_pymavlink(name, *args, **kwargs):
        if name == "pymavlink":
            raise ModuleNotFoundError("pymavlink")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_pymavlink)
    with pytest.raises(ImportError, match=r"pip install -e '\.\[dataflash\]'"):
        read_dataflash(tmp_path / "not-opened.bin")
