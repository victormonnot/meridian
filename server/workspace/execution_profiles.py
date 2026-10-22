"""Dispatch immutable execution contracts without changing the historical profile."""
from meridian import vio_specification as historical
from meridian import vio_private_specification as private


def is_private(specification):
    return isinstance(specification, dict) and specification.get("profile_id") == private.PROFILE_ID


def validate_specification(specification):
    if is_private(specification):
        private.validate_private_specification(specification)
    else:
        historical.validate_specification(specification)


def specification_sha256(specification):
    return (private.specification_sha256(specification) if is_private(specification)
            else historical.specification_sha256(specification))


def input_selection(specification):
    return {"input_revision_id": specification["input"]["input_revision_id"],
            "interval": specification["interval"]} if is_private(specification) else None
