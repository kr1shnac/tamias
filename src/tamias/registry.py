"""Model capability registry — the one place that answers "what can this model do".

Routing, model pickers and per-key allowlists must never guess whether a model
supports tool calls, reasoning, or a given effort level, nor how large a context
it holds. Today those facts exist nowhere in the package, so a router can pick a
model that cannot carry the step and a picker can advertise a model that no
longer exists.

Prices are deliberately **not** here: the price sheet (``pricing.py``) stays the
single source of cost truth, so the registry cannot drift from the number the
bill is computed with. This module owns capability only.

The catalogue is TOML so a deployment can pin its own without code changes:

    [[models]]
    id             = "openai/gpt-5-mini"
    provider       = "openai"
    context_window = 128000
    tools          = true
    reasoning      = true
    efforts        = ["low", "medium", "high"]
    modality       = "text"

The default path is ``$TAMIAS_REGISTRY`` when set, else
``~/.tamias/models.toml``. A missing default file yields an empty registry (not
an error); a file named explicitly that is missing does.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

REGISTRY_ENV_VAR = "TAMIAS_REGISTRY"

# The union of the provider effort vocabularies: OpenRouter's
# ``reasoning.effort`` (low/medium/high) and OpenAI's ``reasoning_effort``
# (xhigh/high/medium/low/minimal/none). A model advertises the subset it accepts.
KNOWN_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh")

# A cheap default when a caller asks for an effort the model does not list: walk
# down towards "none" (cheaper) rather than up (more expensive).
_EFFORT_ORDER = KNOWN_EFFORTS


class RegistryError(ValueError):
    """The registry file could not be read, parsed or validated."""


@dataclass(frozen=True)
class ModelInfo:
    """What one model can do. Prices live in the price sheet, not here."""

    id: str
    provider: str
    context_window: int
    tools: bool = False
    reasoning: bool = False
    efforts: tuple[str, ...] = ()
    modality: str = "text"


class Registry:
    """An immutable-by-convention lookup over known models."""

    def __init__(self, models: dict[str, ModelInfo] | None = None):
        self._models = dict(models or {})

    def __len__(self) -> int:
        return len(self._models)

    def get(self, model_id: str) -> ModelInfo | None:
        return self._models.get(model_id)

    def require(self, model_id: str) -> ModelInfo:
        """The model, or ``RegistryError`` — callers that must not guess use this."""
        info = self._models.get(model_id)
        if info is None:
            raise RegistryError(f"unknown model {model_id!r}: not in the registry")
        return info

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._models))

    def all(self) -> tuple[ModelInfo, ...]:
        return tuple(self._models[key] for key in sorted(self._models))

    def supports_tools(self, model_id: str) -> bool:
        info = self._models.get(model_id)
        return bool(info and info.tools)

    def supports_reasoning(self, model_id: str) -> bool:
        info = self._models.get(model_id)
        return bool(info and info.reasoning)

    def efforts(self, model_id: str) -> tuple[str, ...]:
        info = self._models.get(model_id)
        return info.efforts if info else ()

    def resolve_effort(self, model_id: str, desired: str) -> str | None:
        """The effort to send, or ``None`` when the model takes no effort field.

        Returns ``desired`` when supported. Otherwise walks **down** the effort
        ladder (towards the cheaper end) to the nearest supported level, so a
        request never carries an effort the model rejects. Returns ``None`` for
        an unknown model or one with no reasoning support.
        """
        info = self._models.get(model_id)
        if info is None or not info.reasoning or not info.efforts:
            return None
        if desired in info.efforts:
            return desired
        if desired not in _EFFORT_ORDER:
            return None
        start = _EFFORT_ORDER.index(desired)
        for candidate in reversed(_EFFORT_ORDER[: start + 1]):
            if candidate in info.efforts:
                return candidate
        return None


def empty_registry() -> Registry:
    return Registry({})


def default_registry_path(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    override = env.get(REGISTRY_ENV_VAR)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".tamias" / "models.toml"


_TOP_LEVEL_KEYS = ("models",)
_MODEL_KEYS = (
    "id",
    "provider",
    "context_window",
    "tools",
    "reasoning",
    "efforts",
    "modality",
)


def _require_str(entry: dict, key: str, where: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RegistryError(f"{where}: {key!r} must be a non-empty string")
    return value.strip()


def _optional_bool(entry: dict, key: str, where: str) -> bool:
    value = entry.get(key, False)
    if not isinstance(value, bool):
        raise RegistryError(f"{where}: {key!r} must be true or false")
    return value


def _parse_model(entry: object, index: int, seen: set[str]) -> ModelInfo:
    where = f"models[{index}]"

    if not isinstance(entry, dict):
        raise RegistryError(f"{where}: each model must be a table")

    for key in entry:
        if key not in _MODEL_KEYS:
            known = ", ".join(_MODEL_KEYS)
            raise RegistryError(f"{where}: unknown key {key!r} (known: {known})")

    model_id = _require_str(entry, "id", where)

    if model_id in seen:
        raise RegistryError(f"{where}: duplicate model id {model_id!r}")

    provider = _require_str(entry, "provider", where)

    window = entry.get("context_window")
    if isinstance(window, bool) or not isinstance(window, int) or window < 1:
        raise RegistryError(f"{where}: 'context_window' must be a positive integer")

    tools = _optional_bool(entry, "tools", where)
    reasoning = _optional_bool(entry, "reasoning", where)

    raw_efforts = entry.get("efforts", [])
    if not isinstance(raw_efforts, list) or not all(
        isinstance(item, str) for item in raw_efforts
    ):
        raise RegistryError(f"{where}: 'efforts' must be an array of strings")

    efforts: list[str] = []
    for effort in raw_efforts:
        if effort not in KNOWN_EFFORTS:
            known = ", ".join(KNOWN_EFFORTS)
            raise RegistryError(
                f"{where}: unknown effort {effort!r} (known: {known})"
            )
        if effort in efforts:
            raise RegistryError(f"{where}: duplicate effort {effort!r}")
        efforts.append(effort)

    if efforts and not reasoning:
        raise RegistryError(
            f"{where}: 'efforts' given but 'reasoning' is false"
        )

    modality = entry.get("modality", "text")
    if not isinstance(modality, str) or not modality.strip():
        raise RegistryError(f"{where}: 'modality' must be a non-empty string")

    seen.add(model_id)

    return ModelInfo(
        id=model_id,
        provider=provider,
        context_window=window,
        tools=tools,
        reasoning=reasoning,
        efforts=tuple(efforts),
        modality=modality.strip(),
    )


def parse_registry(data: object) -> Registry:
    """Validate an already-parsed TOML document into a ``Registry``."""
    if not isinstance(data, dict):
        raise RegistryError("registry must be a table")

    for key in data:
        if key not in _TOP_LEVEL_KEYS:
            raise RegistryError(f"unknown top-level key {key!r}")

    raw_models = data.get("models", [])
    if not isinstance(raw_models, list):
        raise RegistryError("'models' must be an array of tables")

    models: dict[str, ModelInfo] = {}
    seen: set[str] = set()

    for index, entry in enumerate(raw_models):
        info = _parse_model(entry, index, seen)
        models[info.id] = info

    return Registry(models)


def load_registry(path: str | Path, missing_ok: bool = False) -> Registry:
    """Read a registry TOML file into a ``Registry``.

    ``missing_ok`` is for the default path: a deployment that has not written a
    catalogue yet gets an empty registry rather than a crash. An explicitly
    named file that is missing always raises.
    """
    path = Path(path)

    if not path.exists():
        if missing_ok:
            return empty_registry()
        raise RegistryError(f"registry file not found: {path}")

    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as error:
        raise RegistryError(f"could not parse registry {path}: {error}") from error
    except OSError as error:
        raise RegistryError(f"could not read registry {path}: {error}") from error

    return parse_registry(data)
