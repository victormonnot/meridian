#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise the built native adapter without acquired data or reference poses."""

import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib


def mono16_png(path: Path) -> None:
    """Write an actual 512x512 uint16 grayscale PNG, using only the stdlib."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    pixels = b"".join(
        b"\x00" + b"".join(struct.pack(">H", 5000 if (x // 32 + y // 32) % 2 else 55000)
                            for x in range(512))
        for y in range(512)
    )
    path.write_bytes(b"\x89PNG\r\n\x1a\n" +
                     chunk(b"IHDR", struct.pack(">IIBBBBB", 512, 512, 16, 0, 0, 0, 0)) +
                     chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


def main() -> None:
    image = sys.argv[1] if len(sys.argv) > 1 else "meridian-openvins:reference"
    integration = Path(__file__).resolve().parent
    version = json.loads(subprocess.check_output(
        ["docker", "run", "--rm", "--network", "none", image, "--version"], text=True))
    assert version["protocol_version"] == 1
    subprocess.run([
        "docker", "run", "--rm", "--network", "none", "--entrypoint", "/bin/sh", image,
        "-c", "test ! -e /opt/openvins/ov_data && test ! -e /opt/openvins/.git "
              "&& test ! -e /opt/openvins/ov_eval",
    ], check=True)
    with tempfile.TemporaryDirectory(prefix="meridian-openvins-test-") as directory:
        root = Path(directory)
        sensors, config = root / "sensors", root / "config"
        shutil.copytree(integration / "config", config)
        origin = 1_000_000_000_000_000_000
        for camera in (0, 1):
            target = sensors / "mav0" / f"cam{camera}"
            (target / "data").mkdir(parents=True)
            mono16_png(target / "data" / "static.png")
            (target / "data.csv").write_text(
                "#timestamp [ns],filename\n" +
                "".join(f"{origin + 10_000_000 + i * 50_000_000},static.png\n" for i in range(40)))
        imu = sensors / "mav0" / "imu0"
        imu.mkdir()
        (imu / "data.csv").write_text(
            "#timestamp [ns],wx,wy,wz,ax,ay,az\n" +
            "".join(f"{origin + i * 5_000_000},0,0,0,0,0,9.80766\n" for i in range(401)))

        def execute(name: str) -> tuple[subprocess.CompletedProcess, dict, Path]:
            output = root / name
            output.mkdir()
            result = subprocess.run([
                "docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--user", f"{os.getuid()}:{os.getgid()}",
                "--cpus", "2", "--memory", "4g", "--pids-limit", "128", "--tmpfs", "/tmp",
                "--mount", f"type=bind,src={sensors},dst=/sensors,readonly",
                "--mount", f"type=bind,src={config},dst=/config,readonly",
                "--mount", f"type=bind,src={output},dst=/output", image,
                "--dataset", "/sensors", "--config", "/config/estimator_config.yaml", "--output", "/output",
            ], capture_output=True, text=True, timeout=60)
            status_file = output / "status.json"
            assert status_file.exists(), result.stdout + result.stderr
            return result, json.loads(status_file.read_text()), output

        result, status, output = execute("static")
        assert result.returncode == 3, result.stdout + result.stderr
        assert status["state"] == "uninitialized"
        assert status["counts"]["camera_pairs_processed"] == 40
        assert status["counts"]["trajectory_states"] == 0
        assert status["first_camera_timestamp_ns"] == origin + 10_000_000
        assert len((output / "trajectory.csv").read_text().splitlines()) == 1
        assert status["timing"]["peak_rss_kib"] > 0

        right = sensors / "mav0" / "cam1" / "data.csv"
        text = right.read_text()
        right.write_text(text.replace(str(origin + 10_000_000), str(origin + 10_000_001), 1))
        result, status, _ = execute("mismatched-stereo")
        assert result.returncode == 2 and status["state"] == "failed"
        assert "identical paired timestamps" in status["error"]
        right.write_text(text)

        imu_csv = imu / "data.csv"
        imu_csv.write_text(imu_csv.read_text().replace(",0,0,0,0,0,9.80766", ",nan,0,0,0,0,9.80766", 1))
        result, status, _ = execute("nonfinite-imu")
        assert result.returncode == 2 and status["state"] == "failed"
        assert "finite numbers" in status["error"]
    print("Native adapter smoke checks passed: uint16 static/no initialization, exact ns, stereo mismatch, nonfinite IMU.")


if __name__ == "__main__":
    main()
