"""Model fallback chains."""

from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.models import Model, infer_model
from pydantic_ai.models.fallback import FallbackModel


def model_chain(primary: Model | str, *fallbacks: Model | str) -> Model:
    """Primary model, then each fallback in order whenever a provider call fails."""
    if not fallbacks:
        return infer_model(primary)
    return FallbackModel(primary, *fallbacks, fallback_on=(ModelAPIError,))
