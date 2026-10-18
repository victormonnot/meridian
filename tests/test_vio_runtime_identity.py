"""Preserve qualified image content across classic and containerd Docker stores."""

import hashlib
import json
import subprocess

import pytest

from meridian import vio_runtime_identity as identity


def test_exact_archive_manifest_commits_to_the_original_configuration_and_layers():
    raw = identity.QUALIFIED_OCI_MANIFEST
    assert len(raw) == 1923
    assert "sha256:" + hashlib.sha256(raw).hexdigest() == identity.OCI_MANIFEST_ID
    assert identity.verified_manifest_configuration(raw) == identity.CONFIGURATION_ID
    manifest = json.loads(raw)
    assert len(manifest["layers"]) == 11
    assert manifest["config"]["size"] == 4527


def test_classic_engine_executes_the_original_configuration_identity_without_fallback():
    inspected = []
    result = identity.resolve_runtime_identity(identity.CONFIGURATION_ID, identity.CONFIGURATION_ID,
                                               lambda value: inspected.append(value) or value)
    assert inspected == [identity.CONFIGURATION_ID]
    assert result["execution_image_id"] == result["configuration_image_id"] == identity.CONFIGURATION_ID
    assert result["binding"] == "direct_configuration_id"
    assert result["qualified_oci_manifest_sha256"] is None


def test_containerd_uses_only_the_exact_qualified_manifest_when_config_id_is_absent():
    inspected = []
    def inspect(value):
        inspected.append(value)
        if value == identity.CONFIGURATION_ID:
            raise subprocess.CalledProcessError(1, ["docker", "inspect", value],
                                                stderr="Error response from daemon: No such image: " + value)
        return value
    result = identity.resolve_runtime_identity(identity.CONFIGURATION_ID, identity.CONFIGURATION_ID, inspect)
    assert inspected == [identity.CONFIGURATION_ID, identity.OCI_MANIFEST_ID]
    assert result["execution_image_id"] == identity.OCI_MANIFEST_ID
    assert result["configuration_image_id"] == identity.CONFIGURATION_ID
    assert result["binding"] == "qualified_oci_manifest"
    assert result["qualified_oci_manifest_sha256"] == identity.OCI_MANIFEST_ID[7:]


def test_explicit_qualified_oci_reference_keeps_the_scientific_config_identity():
    result = identity.resolve_runtime_identity(identity.OCI_MANIFEST_ID, identity.CONFIGURATION_ID, lambda value: value)
    assert result["execution_image_id"] == identity.OCI_MANIFEST_ID
    assert result["configuration_image_id"] == identity.CONFIGURATION_ID


@pytest.mark.parametrize("requested,message", [
    ("unqualified:latest", "No such image: unqualified:latest"),
    (identity.CONFIGURATION_ID, "Cannot connect to the Docker daemon"),
    (identity.CONFIGURATION_ID, "permission denied while connecting to the Docker daemon socket"),
    (identity.OCI_MANIFEST_ID, "No such image: missing qualified manifest"),
])
def test_missing_mutable_tags_and_infrastructure_errors_never_choose_another_image(requested, message):
    inspected = []
    error = subprocess.CalledProcessError(1, "docker", stderr=message)
    def inspect(value):
        inspected.append(value)
        raise error
    with pytest.raises(subprocess.CalledProcessError) as caught:
        identity.resolve_runtime_identity(requested, identity.CONFIGURATION_ID, inspect)
    assert caught.value is error and inspected == [requested]


@pytest.mark.parametrize("actual", ["sha256:" + "0" * 64, "not-an-image", "latest"])
def test_existing_unqualified_images_cannot_be_replaced_by_a_silent_fallback(actual):
    inspected = []
    with pytest.raises(ValueError, match="runtime image differs"):
        identity.resolve_runtime_identity(identity.CONFIGURATION_ID, identity.CONFIGURATION_ID,
                                           lambda value: inspected.append(value) or actual)
    assert inspected == [identity.CONFIGURATION_ID]


@pytest.mark.parametrize("change", ["whitespace", "configuration", "layer", "truncated"])
def test_manifest_tampering_cannot_rebind_the_frozen_specification(change):
    original = identity.QUALIFIED_OCI_MANIFEST
    if change == "whitespace":
        changed = original + b"\n"
    elif change == "configuration":
        changed = original.replace(identity.CONFIGURATION_ID.encode(), ("sha256:" + "0" * 64).encode())
    elif change == "layer":
        changed = original.replace(b"80693248", b"80693249")
    else:
        changed = original[:-1]
    with pytest.raises(ValueError, match="manifest identity"):
        identity.verified_manifest_configuration(changed)


def test_a_recomputed_manifest_digest_still_cannot_change_the_qualified_configuration(monkeypatch):
    changed = identity.QUALIFIED_OCI_MANIFEST.replace(identity.CONFIGURATION_ID.encode(), ("sha256:" + "0" * 64).encode())
    monkeypatch.setattr(identity, "OCI_MANIFEST_ID", "sha256:" + hashlib.sha256(changed).hexdigest())
    with pytest.raises(ValueError, match="pinned configuration"):
        identity.verified_manifest_configuration(changed)
