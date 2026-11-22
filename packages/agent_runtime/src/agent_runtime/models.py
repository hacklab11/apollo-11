"""Model fallback chains."""

from dataclasses import replace

from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model, ModelRequestParameters, infer_model
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings


class _IdentifyRoute(WrapperModel):
    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        response = await super().request(
            messages, model_settings, model_request_parameters
        )
        # Keep the provider's model_name for tracing and retain the route we requested.
        return replace(response, model_id=self.model_id)


def serving_model(model: Model, response: ModelResponse) -> Model | None:
    if isinstance(model, FallbackModel):
        return next((m for m in model.models if m.model_id == response.model_id), None)
    return model


def model_chain(primary: Model | str, *fallbacks: Model | str) -> Model:
    """Primary model, then each fallback in order whenever a provider call fails."""
    if not fallbacks:
        return infer_model(primary)
    return FallbackModel(
        _IdentifyRoute(primary),
        *(_IdentifyRoute(model) for model in fallbacks),
        fallback_on=(ModelAPIError,),
    )
