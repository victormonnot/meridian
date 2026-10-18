"""Standard-library control signals shared by the worker and compute adapter."""


class AttemptCancelled(Exception):
    """The owner requested cancellation; stop child processes before returning."""


class AttemptLeaseLost(Exception):
    """This attempt no longer owns its execution lease and cannot publish."""
