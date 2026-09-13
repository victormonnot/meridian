"""Read selected DataFlash observations without modifying or aligning samples.

In the inspected ArduCopter revision 8927564c, IMU records are calibrated,
filtered frontend snapshots at logger timestamps, not the simulator's
interval-average rates. Other firmware needs its own semantic verification.
Health/error counters and parser diagnostics cannot establish whole-recording
integrity.
"""

from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
import io
import math
from pathlib import Path
import re


@dataclass(frozen=True)
class ImuRecord:
    time_us: int
    instance: int
    gyro_rad_s: tuple[float, float, float]
    accel_m_s2: tuple[float, float, float]


@dataclass
class DataFlashData:
    records: list[ImuRecord]
    metadata: dict


_REQUIRED = ("TimeUS", "I", "GyrX", "GyrY", "GyrZ", "AccX", "AccY", "AccZ")
_PARAMETERS = frozenset({
    "LOG_BITMASK", "LOG_DISARMED", "LOG_BACKEND_TYPE", "LOG_REPLAY",
    "INS_LOG_BAT_MASK", "INS_LOG_BAT_OPT", "INS_LOG_BAT_CNT",
    "INS_LOG_BAT_LGCT", "INS_LOG_BAT_LGIN", "INS_GYRO_FILTER", "INS_ACCEL_FILTER",
    "INS_GYR_CAL", "INS_FAST_SAMPLE", "INS_GYRO_RATE", "AHRS_ORIENTATION",
    "AHRS_TRIM_X", "AHRS_TRIM_Y", "AHRS_TRIM_Z",
})
_BANNER = re.compile(
    r"^(?:ArduCopter|ArduPlane|ArduPilot)(?:-[A-Za-z0-9_-]+)? "
    r"V?\d+\.\d+(?:\.\d+)?(?:-[A-Za-z0-9.-]+)?(?: \([0-9a-fA-F]{7,40}\))?$"
)


class _UnsupportedDataFlash(ValueError):
    """A reader contract error whose message contains no source-file text."""


class _DiagnosticCounter(io.TextIOBase):
    """Count nonempty parser output lines without retaining their contents."""

    def __init__(self):
        self.count = 0
        self.pending = False

    def write(self, text):
        for part in text.splitlines(keepends=True):
            self.pending = self.pending or bool(part.strip())
            if part.endswith(("\n", "\r")):
                self.count += int(self.pending)
                self.pending = False
        return len(text)

    @property
    def line_count(self):
        return self.count + int(self.pending)


def _raw(message, field):
    """Access decoded scalars before pymavlink's format-code scaling."""
    return message._elements[message.fmt.colhash[field]]


def _text(value):
    if isinstance(value, bytes):
        return value.split(b"\0", 1)[0].decode("ascii", errors="replace")
    return str(value).split("\0", 1)[0]


def _validate_format(fmt):
    missing = set(_REQUIRED) - set(fmt.columns)
    if missing:
        raise _UnsupportedDataFlash("IMU is missing required fields: " + ", ".join(sorted(missing)))
    if len(set(fmt.columns)) != len(fmt.columns):
        raise _UnsupportedDataFlash("IMU has duplicate column names")
    for name in _REQUIRED:
        code = fmt.format[fmt.colhash[name]]
        supported = "bBhHiIqQ" if name in ("TimeUS", "I") else "fd"
        if code not in supported:
            raise _UnsupportedDataFlash(f"Unsupported raw IMU numeric format for {name}")


def _validate_units(descriptor, fmt_units, units, multipliers):
    type_id, format_string, columns = descriptor
    definitions = fmt_units.get(type_id, set())
    if len(definitions) != 1:
        raise ValueError("IMU requires one consistent logged FMTU definition")
    unit_ids, multiplier_ids = next(iter(definitions))
    resolved = {}
    for name in _REQUIRED:
        if name == "I":
            continue
        index = columns.index(name)
        if index >= len(unit_ids) or index >= len(multiplier_ids):
            raise ValueError(f"Missing logged IMU unit or multiplier for {name}")
        labels = units.get(unit_ids[index], set())
        factors = multipliers.get(multiplier_ids[index], set())
        if len(labels) != 1 or len(factors) != 1:
            raise ValueError(f"Missing or conflicting logged IMU units for {name}")
        label, factor = next(iter(labels)), next(iter(factors))
        expected = ({"s": 1e-6, "us": 1.0, "µs": 1.0} if name == "TimeUS"
                    else {"rad/s": 1.0} if name.startswith("Gyr")
                    else {"m/s/s": 1.0, "m/s^2": 1.0})
        if (label not in expected or not math.isfinite(factor)
                or not math.isclose(factor, expected[label], rel_tol=1e-7, abs_tol=0.0)):
            raise ValueError(f"Unsupported logged IMU unit or multiplier for {name}")
        resolved[name] = "us" if name == "TimeUS" else "rad/s" if name.startswith("Gyr") else "m/s^2"
    return {"type": type_id, "format": format_string, "columns": list(columns),
            "units": resolved, "unit_ids": unit_ids, "multiplier_ids": multiplier_ids}


def _update_health(health, message, instance):
    fields = health.setdefault(str(instance), {})
    for name in ("GH", "AH", "EG", "EA"):
        if name not in message.fmt.colhash:
            continue
        value = float(_raw(message, name))
        item = fields.setdefault(name, {"observations": 0, "nonfinite_count": 0})
        item["observations"] += 1
        if not math.isfinite(value):
            item["nonfinite_count"] += 1
            continue
        if name in ("GH", "AH"):
            item["unhealthy_count"] = item.get("unhealthy_count", 0) + int(value != 1)
        else:
            item.setdefault("first", value)
            item["last"] = value
            item["min"] = min(item.get("min", value), value)
            item["max"] = max(item.get("max", value), value)


def read_dataflash(path: Path) -> DataFlashData:
    """Decode IMU snapshots in file order and selected non-location metadata.

    Logged FMTU/UNIT/MULT definitions must establish microsecond timestamps,
    rad/s and m/s². Unknown/conflicting units are rejected, never guessed.
    Duplicate/regressing timestamps, instances and nonfinite sensor samples
    remain unchanged for the downstream audit. No source file is created.
    """
    try:
        import pymavlink
        from pymavlink import DFReader
    except ImportError as error:
        raise ImportError(
            "DataFlash reading requires the optional dependency; install with "
            "python -m pip install -e '.[dataflash]'"
        ) from error

    path = Path(path)
    if not path.is_file():
        raise ValueError("DataFlash input must be an existing regular file")
    if path.stat().st_size == 0:
        raise ValueError("DataFlash input is empty")

    records, descriptors = [], set()
    units, multipliers, fmt_units = {}, {}, {}
    banners, parameters, arming, health = [], [], [], {}
    batches = Counter({"accelerometer": 0, "gyroscope": 0, "other": 0})
    diagnostics = _DiagnosticCounter()
    last_decoded_end = 0
    # DFReader opens a file and mmap during initialization. Retaining the object
    # before __init__ lets us close both even if malformed input aborts setup.
    reader = DFReader.DFReader_binary.__new__(DFReader.DFReader_binary)
    # The optional C indexer writes directly to process stderr, bypassing the
    # Python redirects. Select the Python path on this reader only so parser
    # diagnostics remain counted and private, regardless of host defaults.
    reader.init_arrays_fast = reader.init_arrays
    try:
        with redirect_stdout(diagnostics), redirect_stderr(diagnostics):
            DFReader.DFReader_binary.__init__(reader, str(path), zero_time_base=True)
            while (message := reader.recv_msg()) is not None:
                last_decoded_end = reader.offset
                kind = message.get_type()
                if kind == "IMU":
                    _validate_format(message.fmt)
                    descriptors.add((message.fmt.type, message.fmt.format, tuple(message.fmt.columns)))
                    record = ImuRecord(
                        int(_raw(message, "TimeUS")), int(_raw(message, "I")),
                        tuple(float(_raw(message, name)) for name in ("GyrX", "GyrY", "GyrZ")),
                        tuple(float(_raw(message, name)) for name in ("AccX", "AccY", "AccZ")),
                    )
                    if record.time_us < 0 or record.instance < 0:
                        raise _UnsupportedDataFlash("IMU TimeUS and I must be nonnegative integers")
                    records.append(record)
                    _update_health(health, message, record.instance)
                elif kind == "UNIT":
                    units.setdefault(chr(int(_raw(message, "Id"))), set()).add(_text(_raw(message, "Label")))
                elif kind == "MULT":
                    multipliers.setdefault(chr(int(_raw(message, "Id"))), set()).add(float(_raw(message, "Mult")))
                elif kind == "FMTU":
                    fmt_units.setdefault(int(_raw(message, "FmtType")), set()).add(
                        (_text(_raw(message, "UnitIds")), _text(_raw(message, "MultIds"))))
                elif kind == "MSG":
                    banner = _text(_raw(message, "Message"))
                    if _BANNER.fullmatch(banner) and banner not in banners:
                        banners.append(banner)
                elif kind == "PARM":
                    name = _text(_raw(message, "Name"))
                    value = float(_raw(message, "Value"))
                    if name in _PARAMETERS and math.isfinite(value):
                        parameters.append({"name": name, "value": value,
                                           "time_us": int(_raw(message, "TimeUS"))})
                elif kind == "ARM":
                    arming.append({"source": "ARM", "time_us": int(_raw(message, "TimeUS")),
                                   "armed": bool(_raw(message, "ArmState"))})
                elif kind == "EV" and int(_raw(message, "Id")) in (10, 11):
                    arming.append({"source": "EV", "time_us": int(_raw(message, "TimeUS")),
                                   "armed": int(_raw(message, "Id")) == 10})
                elif kind == "ISBH":
                    sensor_type = int(_raw(message, "type"))
                    batches[{0: "accelerometer", 1: "gyroscope"}.get(sensor_type, "other")] += 1
            # Failed parsing can advance the cursor over a partial header or
            # garbage. Count from the last complete decoded message instead.
            trailing_bytes = reader.data_len - last_decoded_end
    except _UnsupportedDataFlash:
        raise
    except Exception as error:
        # Parser exceptions can contain arbitrary logged strings. Keep only
        # their class in the public-facing error; never echo raw diagnostics.
        raise ValueError("Cannot decode supported DataFlash records: " + type(error).__name__) from None
    finally:
        for name in ("data_map", "filehandle"):
            resource = getattr(reader, name, None)
            if resource is not None:
                resource.close()

    if not records:
        raise ValueError("DataFlash input contains no decodable IMU records")
    if len(descriptors) != 1:
        raise ValueError("Changing IMU format definitions are not supported")
    format_metadata = _validate_units(next(iter(descriptors)), fmt_units, units, multipliers)
    return DataFlashData(records, {
        "pymavlink_version": pymavlink.__version__, "imu_format": format_metadata,
        "firmware_banners": banners, "parameters": parameters, "arming_events": arming,
        "arming_evidence_limit": "No arming events does not establish a disarmed recording.",
        "batch_headers": dict(batches), "imu_health": health,
        "parser_diagnostic_count": diagnostics.line_count,
        "parser_indexer": "python",
        "unparsed_tail_bytes": trailing_bytes,
    })
