"""Load runtime configuration without reading secrets."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
)

_TIMESTAMP = TypeAdapter(AwareDatetime)
_HTTP_URL = TypeAdapter(HttpUrl)


def _utc_timestamp(value: object) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return _TIMESTAMP.validate_python(value, strict=True).astimezone(UTC)


def _cache_path(value: object) -> Path:
    if isinstance(value, Path):
        return value
    if not isinstance(value, str) or not value.strip():
        raise ValueError("expected a non-empty file path")
    return Path(value)


def _http_url(value: str) -> str:
    _HTTP_URL.validate_python(value)
    return value


Timestamp = Annotated[datetime, BeforeValidator(_utc_timestamp)]
CachePath = Annotated[Path, BeforeValidator(_cache_path)]
AgentUrl = Annotated[str, AfterValidator(_http_url)]
ModelId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Effort = Literal["low", "medium", "high", "xhigh", "max"]
Port = Annotated[int, Field(ge=1024, le=65535)]

_DEFAULT_MODEL = "claude-opus-5-5"
_DEFAULT_EFFORT: dict[str, Effort] = {
    "sentinel": "low",
    "detective": "high",
    "skeptic": "medium",
    "reporter": "medium",
    "cartographer": "high",
    "specialists": "medium",
    "chat": "medium",
}


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class DataSettings(_Section):
    source: Literal["sim", "duckdb"] = "sim"
    path: CachePath = Field(
        Path("data/world.duckdb"),
        description="Read-only warehouse path for the duckdb source only",
    )


class SimSettings(_Section):
    seed: int = Field(42, ge=0)
    history_days: int = Field(28, ge=21, le=60)
    start_ts: Timestamp = datetime(2026, 11, 27, tzinfo=UTC)
    seconds_per_sim_hour: float = Field(5.0, ge=0.5, le=60)
    autostart: bool = True


class OrchestratorSettings(_Section):
    max_concurrent_investigations: int = Field(2, ge=1, le=4)
    pause_sim_during_investigation: bool = True


class SentinelSettings(_Section):
    top_k: int = Field(10, ge=3, le=30)
    z_threshold: float = Field(3.0, ge=2.0, le=6.0)
    divergence_pp: float = Field(3.0, ge=1.0, le=20.0)
    min_completeness: float = Field(0.9, ge=0.5, le=1.0)


class DetectiveSettings(_Section):
    max_steps: int = Field(15, ge=5, le=30)
    confirm_share: float = Field(0.7, ge=0.5, le=0.95)
    min_independent_evidence: int = Field(2, ge=1, le=3)


class ReporterSettings(_Section):
    resolve_after_ticks: int = Field(3, ge=1, le=24)
    resolve_z: float = Field(2.0, ge=0.5, le=3.0)


class LlmSettings(_Section):
    provider: Literal["anthropic", "openai_compatible"] = "anthropic"
    base_url: AgentUrl = "https://openrouter.ai/api/v1"
    api_key_env: Annotated[
        str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    ] = "OPENROUTER_API_KEY"
    mode: Literal["live", "record", "replay"] = "live"
    cache_path: CachePath = Path("data/llm_cache.jsonl")
    models: dict[str, ModelId] = Field(
        default_factory=lambda: {"default": _DEFAULT_MODEL}
    )
    effort: dict[str, Effort] = Field(default_factory=_DEFAULT_EFFORT.copy)
    max_tokens: int = Field(16000, ge=1024, le=64000)
    max_calls_per_hour: int = Field(600, ge=50, le=5000)

    @field_validator("models")
    @classmethod
    def _with_default_model(cls, models: dict[str, str]) -> dict[str, str]:
        return {"default": _DEFAULT_MODEL, **models}

    @field_validator("effort")
    @classmethod
    def _with_default_effort(cls, effort: dict[str, Effort]) -> dict[str, Effort]:
        return {**_DEFAULT_EFFORT, **effort}

    def model_for(self, agent: str) -> str:
        return self.models.get(agent, self.models["default"])


class DemoSettings(_Section):
    show_truth: bool = False


class A2aSettings(_Section):
    growth_url: AgentUrl = "http://localhost:8200/growth"
    marketplace_url: AgentUrl = "http://localhost:8200/marketplace"
    discovery_url: AgentUrl = "http://localhost:8200/discovery"
    selleros_url: AgentUrl = "http://localhost:8300"
    timeout_s: float = Field(60.0, ge=5, le=120)


class PortsSettings(_Section):
    core: Port = 8000
    mcp: Port = 8100
    specialists: Port = 8200
    selleros: Port = 8300


class Settings(_Section):
    data: DataSettings = Field(default_factory=DataSettings)
    sim: SimSettings = Field(default_factory=SimSettings)
    orchestrator: OrchestratorSettings = Field(default_factory=OrchestratorSettings)
    sentinel: SentinelSettings = Field(default_factory=SentinelSettings)
    detective: DetectiveSettings = Field(default_factory=DetectiveSettings)
    reporter: ReporterSettings = Field(default_factory=ReporterSettings)
    llm: LlmSettings = Field(default_factory=LlmSettings)
    demo: DemoSettings = Field(default_factory=DemoSettings)
    a2a: A2aSettings = Field(default_factory=A2aSettings)
    ports: PortsSettings = Field(default_factory=PortsSettings)


def _validation_message(errors: ValidationError) -> str:
    schema = Settings.model_json_schema()
    messages = []
    for error in errors.errors(include_input=False, include_url=False):
        field: dict[str, Any] = schema
        for part in error["loc"]:
            if "$ref" in field:
                field = schema["$defs"][field["$ref"].rsplit("/", 1)[-1]]
            field = field.get("properties", {}).get(part, {})

        key = ".".join(map(str, error["loc"])) or "settings"
        lower, upper = field.get("minimum"), field.get("maximum")
        if lower is not None and upper is not None:
            messages.append(
                f"{key}: {error['msg']} (valid range: {lower:g} to {upper:g})"
            )
        else:
            messages.append(f"{key}: {error['msg']}")
    return "; ".join(messages)


def load_settings(path: str | Path = "config/settings.yaml") -> Settings:
    """Load YAML, applying defaults to missing sections and keys."""
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f":{mark.line + 1}:{mark.column + 1}" if mark else ""
        raise ValueError(f"invalid settings YAML in {path}{location}") from None

    try:
        return Settings.model_validate({} if raw is None else raw)
    except ValidationError as exc:
        raise ValueError(_validation_message(exc)) from None
