#!/usr/bin/env python3
"""Provision one finite import scratch filesystem without touching existing data.

Run as root with explicit image and mount paths. A new image is fully allocated,
formatted once, and mounted nosuid,nodev,noexec. Existing images are never
formatted or resized. --print-unit prints the matching persistent systemd mount
unit; install it under the name printed by systemd-escape --path --suffix=mount.
"""
import argparse
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess


SIZE = 8 * 1024**3


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def safe_path(value):
    path = Path(value)
    if not path.is_absolute() or path == Path("/") or re.fullmatch(r"/[A-Za-z0-9_./-]+", str(path)) is None:
        raise ValueError("Use an absolute simple operator-owned path.")
    if ".." in path.parts or any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("Scratch paths cannot contain symlinks or traversal.")
    return path


def unit(image, mount):
    return ("[Unit]\nDescription=Bounded Meridian sensor import scratch\nBefore=docker.service\n\n"
            f"[Mount]\nWhat={image}\nWhere={mount}\nType=ext4\n"
            "Options=loop,nosuid,nodev,noexec\nDirectoryMode=0700\n\n"
            "[Install]\nWantedBy=multi-user.target\n")


def provision(image, mount):
    if os.geteuid() != 0:
        raise ValueError("Scratch provisioning requires root.")
    if image == mount or image.is_relative_to(mount) or mount.is_relative_to(image):
        raise ValueError("Backing image and mount must be separate paths.")
    image.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    mount.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.path.ismount(mount):
        device = run("findmnt", "--noheadings", "--output", "SOURCE", "--mountpoint", str(mount))
        if run("losetup", "--noheadings", "--output", "BACK-FILE", device) != str(image):
            raise ValueError("Mount is occupied by another filesystem.")
    else:
        if any(mount.iterdir()):
            raise ValueError("Refusing to hide existing files below the mount.")
        if image.exists():
            metadata = image.lstat()
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                    or metadata.st_size != SIZE or metadata.st_uid != 0
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                    or run("blkid", "-p", "-o", "value", "-s", "TYPE", str(image)) != "ext4"):
                raise ValueError("Existing scratch image does not match the fixed profile.")
        else:
            if shutil.disk_usage(image.parent).free < SIZE + 10 * 1024**3:
                raise ValueError("Provisioning must retain at least 10 GiB free host storage.")
            descriptor = os.open(image, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                os.posix_fallocate(descriptor, 0, SIZE)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            # A failure leaves its exact new image for operator inspection;
            # a later invocation never reformats it automatically.
            run("mkfs.ext4", "-F", "-m", "0", "-N", "180000", str(image))
        run("mount", "-t", "ext4", "-o", "loop,nosuid,nodev,noexec", str(image), str(mount))
    capacity = os.statvfs(mount)
    if not 6 * 1024**3 < capacity.f_blocks * capacity.f_frsize <= SIZE:
        raise ValueError("Mounted scratch capacity is outside its fixed profile.")
    os.chown(mount, 10001, 10001)
    os.chmod(mount, 0o700)
    print(f"Import scratch ready: {capacity.f_blocks * capacity.f_frsize} filesystem bytes; 8 GiB backing image.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, type=safe_path)
    parser.add_argument("--mount", required=True, type=safe_path)
    parser.add_argument("--print-unit", action="store_true")
    args = parser.parse_args()
    if args.print_unit:
        print(unit(args.image, args.mount), end="")
    else:
        provision(args.image, args.mount)


if __name__ == "__main__":
    main()
