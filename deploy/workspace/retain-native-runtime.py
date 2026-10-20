#!/usr/bin/env python3
"""Retain a qualified native image with an inert, never-started container."""

import argparse
import json
import re
import subprocess

NAME = "meridian-native-runtime-retention"
LABEL = "meridian.component=runtime-retention"


def inspect_container():
    result = subprocess.run(
        ["docker", "container", "inspect", NAME], capture_output=True, text=True,
        check=False,
    )
    if result.returncode == 0:
        return json.loads(result.stdout)[0]
    if "No such container" not in result.stderr:
        raise RuntimeError("Unable to inspect the runtime retention container.")
    return None


def verify(container, image):
    config, host, state = (
        container["Config"], container["HostConfig"], container["State"]
    )
    labels = config.get("Labels") or {}
    if not (
        container["Image"] == image
        and config["Image"] == image
        and config["User"] == "10001:10001"
        and labels.get("meridian.component") == "runtime-retention"
        and labels.get("coolify.managed") != "true"
        and state["Status"] == "created"
        and not state["Running"]
        and state["StartedAt"] == "0001-01-01T00:00:00Z"
        and not container["Mounts"]
        and host["NetworkMode"] == "none"
        and host["ReadonlyRootfs"]
        and not host["Privileged"]
        and host["CapDrop"] == ["ALL"]
        and "no-new-privileges" in host["SecurityOpt"]
    ):
        raise RuntimeError(
            "Existing runtime retention container differs; refusing replacement."
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Qualified local sha256 image ID.")
    args = parser.parse_args()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", args.image):
        parser.error("--image must be an exact local sha256 image ID")
    observed = subprocess.check_output(
        ["docker", "image", "inspect", "--format", "{{.Id}}", args.image], text=True
    ).strip()
    if observed != args.image:
        raise RuntimeError("The qualified local image identity differs.")
    container = inspect_container()
    created = container is None
    if created:
        subprocess.run(
            [
                "docker", "create", "--pull", "never", "--name", NAME,
                "--label", LABEL, "--network", "none", "--read-only",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--user", "10001:10001", "--memory", "16m", "--memory-swap", "16m",
                "--cpus", "0.01", "--pids-limit", "4",
                "--entrypoint", "/bin/true", args.image,
            ],
            check=True, stdout=subprocess.DEVNULL,
        )
        container = inspect_container()
    verify(container, args.image)
    print(json.dumps({
        "name": NAME, "image_id": args.image, "created": created,
        "state": "created", "never_started": True,
        "mount_count": 0, "network": "none", "readonly_rootfs": True,
        "coolify_managed": False,
    }))


if __name__ == "__main__":
    main()
