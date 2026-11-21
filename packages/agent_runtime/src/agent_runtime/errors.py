"""Errors the runtime raises to stop a run; the runner maps each to an end state."""

from pydantic_ai.exceptions import UsageLimitExceeded


class CostLimitExceeded(UsageLimitExceeded):
    pass


class UnpricedModel(RuntimeError):
    """A model answered that has no pinned price, so its cost cannot be capped."""


class LoopDetected(RuntimeError):
    pass


class RunKilled(RuntimeError):
    pass


class DelegationRefused(RuntimeError):
    pass


class CircuitOpenError(RuntimeError):
    pass
