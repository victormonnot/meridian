"""Bind the qualified OpenVINS configuration to Docker's two content ID forms.

The byte-exact OCI manifest was extracted from the transferred Docker archive,
not regenerated from a tag. Its digest commits to the original configuration
and all eleven layers. Saved numerical specifications continue to pin the
configuration digest; execution records retain the engine's actual content ID.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess


CONFIGURATION_ID = "sha256:73e1213d073ce52a9a509decb15628a7c043e0690609171c4b9382ed0c2730da"
OCI_MANIFEST_ID = "sha256:54863f14d9f92d2805f5fddcfb78d146c6aac2ad77dc348e304059e2252a400a"
SOURCE_ARCHIVE_SHA256 = "bebb6c05ad13ffbe1ebd383b291f9b84080e1cbc27a173eb2e443f3a9020dee8"
# Preserve these bytes exactly: whitespace changes alter the content identity.
QUALIFIED_OCI_MANIFEST = b'{"schemaVersion":2,"mediaType":"application/vnd.oci.image.manifest.v1+json","config":{"mediaType":"application/vnd.oci.image.config.v1+json","digest":"sha256:73e1213d073ce52a9a509decb15628a7c043e0690609171c4b9382ed0c2730da","size":4527},"layers":[{"mediaType":"application/vnd.oci.image.layer.v1.tar","digest":"sha256:32ab39660fd51c6dc3aae486b42d42bbe8fe4c1db78eaf57bb9c21198de48a16","size":80693248},{"mediaType":"application/vnd.oci.image.layer.v1.tar","digest":"sha256:b60fe524ce0e94c60e3f60f503feb80e99f9cad6f9870e1dfec31dad078445f2","size":1747820544},{"mediaType":"application/vnd.oci.image.layer.v1.tar","digest":"sha256:90db28aea8d32eee21e49ae27f479b055344eda964810a931839b9a54ea3348a","size":352768},{"mediaType":"application/vnd.oci.image.layer.v1.tar","digest":"sha256:40aab74328f9703fa2e8ac2f5cf6c320d03cb3ded5e3c9a196e13fe90f102305","size":2527232},{"mediaType":"application/vnd.oci.image.layer.v1.tar","digest":"sha256:3b9e9f449269668186f28cc6aeabb025ec4e2f8ade67737729c19c61dda532da","size":112640},{"mediaType":"application/vnd.oci.image.layer.v1.tar","digest":"sha256:9733bedf1e9b36d10b54db41b3bba0e9874de6476d81d8cc78e476fcd0e644cc","size":22528},{"mediaType":"application/vnd.oci.image.layer.v1.tar","digest":"sha256:2a0d1187695f4af9995537f9fac9c6ae542bbb15b8c0ec68be1d81f69bfd6179","size":567296},{"mediaType":"application/vnd.oci.image.layer.v1.tar","digest":"sha256:b8cd6f46049c3655a234e0c9c3c972e4cff1a418c678725ace2eeb22abbcdc17","size":297984},{"mediaType":"application/vnd.oci.image.layer.v1.tar","digest":"sha256:97c01c7420349fd6a6c06a551e1676c85d125530239ea35a9492f72b1b92aefc","size":653312},{"mediaType":"application/vnd.oci.image.layer.v1.tar","digest":"sha256:6a5c94e5f051278a1333c512b194506eeabec9cbd5d1bfd9ee4b342062488692","size":37888},{"mediaType":"application/vnd.oci.image.layer.v1.tar","digest":"sha256:5f70bf18a086007016e948b04aed3b82103a36bea41755b6cddfaf10ace3c6ef","size":1024}]}'


def verified_manifest_configuration(manifest: bytes = QUALIFIED_OCI_MANIFEST) -> str:
    """Accept only the reviewed manifest bytes and their original config binding."""
    if type(manifest) is not bytes or "sha256:" + hashlib.sha256(manifest).hexdigest() != OCI_MANIFEST_ID:
        raise ValueError("Qualified runtime manifest identity is invalid.")
    value = json.loads(manifest)
    if (type(value.get("schemaVersion")) is not int or value["schemaVersion"] != 2
            or value.get("mediaType") != "application/vnd.oci.image.manifest.v1+json"
            or value.get("config", {}).get("digest") != CONFIGURATION_ID):
        raise ValueError("Qualified runtime manifest does not bind the pinned configuration.")
    return value["config"]["digest"]


def resolve_runtime_identity(requested: str, expected_configuration: str, inspect) -> dict:
    """Resolve an immutable execution ID without changing a saved config pin.

    Only a missing request for the exact qualified config ID may fall back to
    its one reviewed immutable OCI manifest ID. Daemon errors, arbitrary tags,
    unqualified manifests and mismatched existing images never select another
    runtime. Inspection and execution still use content-addressed identities.
    """
    try:
        actual = inspect(requested)
    except subprocess.CalledProcessError as error:
        message = error.stderr or ""
        if isinstance(message, bytes):
            message = message.decode("utf-8", errors="replace")
        if (requested != CONFIGURATION_ID or expected_configuration != CONFIGURATION_ID
                or "no such image" not in message.lower()):
            raise
        verified_manifest_configuration()
        actual = inspect(OCI_MANIFEST_ID)
    if actual == expected_configuration:
        binding = "direct_configuration_id"
    elif actual == OCI_MANIFEST_ID and expected_configuration == verified_manifest_configuration():
        binding = "qualified_oci_manifest"
    else:
        raise ValueError("runtime image differs from the immutable specification")
    return {"schema_version": 1, "execution_image_id": actual,
            "configuration_image_id": expected_configuration, "binding": binding,
            "qualified_oci_manifest_sha256": OCI_MANIFEST_ID.removeprefix("sha256:") if binding == "qualified_oci_manifest" else None,
            "identity_resolver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
