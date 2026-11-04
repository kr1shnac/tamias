"""Discover which models each provider currently offers, and remember them.

A provider's ``GET /v1/models`` is the only live answer to "what can I route
to?".  Tamias reads it once at start-up, merges it into the capability registry,
and caches the result so a provider that is unreachable at boot does not empty
the routing pool.

Merging is additive and hand-written facts always win: a ``models.toml`` entry
keeps its ``quality``, ``context_window`` and ``efforts``; discovery only adds
models the file does not mention.  That way discovery never silently downgrades a
model the operator has tuned.

What a provider publishes varies.  OpenRouter returns ``context_length``,
``supported_parameters`` and ``architecture.input_modalities``; a plain
OpenAI-compatible server often returns only ``id``.  For an id-only model the
capabilities are **assumed** (tools on, 128k window) because assuming otherwise
would leave no routable candidate at all for agent traffic.  ``tamias models
discover`` prints which entries were assumed so they can be pinned by hand.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from tamias.providers import Credential, Provider, ProviderCatalogue
from tamias.registry import ModelInfo, Registry

DISCOVERY_TIMEOUT_SECONDS = 10.0

#: Window assumed for a model whose provider publishes no context length.  Wide
#: enough for normal agent traffic, small enough that a one-million-token
#: request is not blindly sent to a model that cannot take it.
ASSUMED_CONTEXT_WINDOW = 128_000

#: ``/models`` paths to try, in order.  Providers that publish the OpenAI-compatible
#: surface answer the first; the rest answer the bare one.
_MODEL_PATHS = ("/v1/models", "/models")


def _assumed(
    context_window: int | None, tools: bool | None, reasoning: bool | None
) -> tuple[str, ...]:
    """Which capabilities the provider left unstated and tamias had to fill in."""
    missing = []
    if context_window is None:
        missing.append("context_window")
    if tools is None:
        missing.append("tools")
    if reasoning is None:
        missing.append("reasoning")
    return tuple(missing)


@dataclass(frozen=True)
class DiscoveredModel:
    """One model as a provider described it, before the registry merges it."""

    id: str
    provider: str
    context_window: int | None = None
    tools: bool | None = None
    reasoning: bool | None = None
    modality: str = "text"
    assumed: tuple[str, ...] = ()

    def as_model_info(self) -> ModelInfo:
        """Convert to a registry entry, filling assumptions for what was missing."""
        assumed = list(self.assumed)
        window = self.context_window
        if window is None:
            window = ASSUMED_CONTEXT_WINDOW
            if "context_window" not in assumed:
                assumed.append("context_window")
        tools = self.tools
        if tools is None:
            tools = True
            if "tools" not in assumed:
                assumed.append("tools")
        reasoning = self.reasoning
        if reasoning is None:
            # Tools are the capability agents need; reasoning is optional, so the
            # conservative assumption is "it cannot reason" rather than pretending
            # an effort level was honoured.
            reasoning = False
            if "reasoning" not in assumed:
                assumed.append("reasoning")
        return ModelInfo(
            id=self.id,
            provider=self.provider,
            context_window=window,
            tools=tools,
            reasoning=reasoning,
            efforts=(),
            modality=self.modality,
            quality=None,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "context_window": self.context_window,
            "tools": self.tools,
            "reasoning": self.reasoning,
            "modality": self.modality,
            "assumed": list(self.assumed),
        }

    @classmethod
    def from_dict(cls, provider: str, data: Any) -> DiscoveredModel | None:
        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            return None
        window = data.get("context_window")
        if isinstance(window, bool) or not isinstance(window, int) or window < 1:
            window = None
        tools = data.get("tools")
        reasoning = data.get("reasoning")
        modality = data.get("modality")
        assumed = data.get("assumed")
        return cls(
            id=data["id"],
            provider=provider,
            context_window=window,
            tools=tools if isinstance(tools, bool) else None,
            reasoning=reasoning if isinstance(reasoning, bool) else None,
            modality=modality if isinstance(modality, str) and modality else "text",
            assumed=(
                tuple(item for item in assumed if isinstance(item, str))
                if isinstance(assumed, list)
                else ()
            ),
        )


def _supports(entry: dict[str, Any], capability: str) -> bool | None:
    """Read a capability from a ``/models`` entry, or ``None`` when unstated."""
    parameters = entry.get("supported_parameters")
    if isinstance(parameters, list):
        names = {str(item) for item in parameters}
        if capability == "tools":
            return "tools" in names or "tool_choice" in names
        if capability == "reasoning":
            return bool(names & {"reasoning", "include_reasoning", "reasoning_effort"})
    for key in ("tools", "function_calling"):
        if capability == "tools" and isinstance(entry.get(key), bool):
            return entry[key]
    if capability == "reasoning" and isinstance(entry.get("reasoning"), bool):
        return entry["reasoning"]
    return None


def _modality(entry: dict[str, Any]) -> str:
    architecture = entry.get("architecture")
    if isinstance(architecture, dict):
        inputs = architecture.get("input_modalities")
        if isinstance(inputs, list) and inputs:
            names = {str(item) for item in inputs}
            if "image" in names:
                return "image"
            if "audio" in names:
                return "audio"
    return "text"


def parse_models(provider: str, payload: Any) -> list[DiscoveredModel]:
    """Read a provider's ``/models`` body into discovered entries."""
    if isinstance(payload, dict):
        entries = payload.get("data")
        if entries is None:
            entries = payload.get("models")
    else:
        entries = payload
    if not isinstance(entries, list):
        return []

    found: list[DiscoveredModel] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        if not model_id and isinstance(entry.get("name"), str):
            model_id = entry["name"]
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        if model_id in seen:
            continue
        seen.add(model_id)

        window = entry.get("context_length", entry.get("context_window"))
        if isinstance(window, bool) or not isinstance(window, int) or window < 1:
            window = None

        found.append(
            DiscoveredModel(
                id=model_id.strip(),
                provider=provider,
                context_window=window,
                tools=_supports(entry, "tools"),
                reasoning=_supports(entry, "reasoning"),
                modality=_modality(entry),
                assumed=_assumed(window, _supports(entry, "tools"), _supports(entry, "reasoning")),
            )
        )
    return found


def fetch_models(
    provider: Provider,
    *,
    credential: Credential | None = None,
    transport: httpx.BaseTransport | None = None,
    timeout: float = DISCOVERY_TIMEOUT_SECONDS,
) -> list[DiscoveredModel]:
    """Ask one provider what it offers.  Never raises: an unreachable provider
    yields an empty list so discovery cannot take the gateway down with it.
    """
    headers = {"accept": "application/json"}
    if credential is not None:
        headers["authorization"] = credential.header()
    with httpx.Client(timeout=timeout, transport=transport) as client:
        for path in _MODEL_PATHS:
            try:
                response = client.get(f"{provider.root}{path}", headers=headers)
            except httpx.HTTPError:
                continue
            if response.status_code != 200:
                continue
            try:
                payload = response.json()
            except ValueError:
                continue
            models = parse_models(provider.name, payload)
            if models:
                return models
    return []


@dataclass
class DiscoveryReport:
    """What discovery found, per provider, and what it had to assume."""

    found: dict[str, list[DiscoveredModel]] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    cached: bool = False

    @property
    def assumed(self) -> tuple[str, ...]:
        return tuple(
            model.id
            for models in self.found.values()
            for model in models
            if model.assumed
        )

    def summary(self) -> str:
        total = sum(len(models) for models in self.found.values())
        parts = [f"{total} model(s) from {len(self.found)} provider(s)"]
        if self.cached:
            parts.append("cache")
        if self.failed:
            parts.append("unreachable: " + ", ".join(sorted(self.failed)))
        return ", ".join(parts)


def discovery_cache_path(env: dict[str, str] | None = None) -> Path:
    import os

    environ = os.environ if env is None else env
    override = environ.get("TAMIAS_MODELS_CACHE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".tamias" / "models.cache.json"


def read_cache(path: Path) -> dict[str, list[DiscoveredModel]]:
    """The last successful discovery per provider, or ``{}`` when unreadable."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    providers = raw.get("providers")
    if not isinstance(providers, dict):
        return {}
    result: dict[str, list[DiscoveredModel]] = {}
    for name, entries in providers.items():
        if not isinstance(entries, list):
            continue
        models = [DiscoveredModel.from_dict(str(name), entry) for entry in entries]
        result[str(name)] = [model for model in models if model is not None]
    return result


def write_cache(path: Path, found: dict[str, list[DiscoveredModel]]) -> None:
    """Persist the discovery result.  Best effort: a read-only home is not fatal."""
    document = {
        "fetched_at": int(time.time()),
        "providers": {
            name: [model.as_dict() for model in models] for name, models in found.items()
        },
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
    except OSError:
        return


def _listed(provider: Provider, models: list[DiscoveredModel]) -> list[DiscoveredModel]:
    """Apply the operator's allowlist when the provider is pinned to it.

    The list wins over the catalogue in both directions: an id the provider no
    longer advertises is still routable, because the operator is the one who
    knows it works, and an id it does advertise but the operator left out is not
    offered at all.  Metadata comes from the catalogue when it is there.
    """
    if provider.discover != "listed":
        return models
    listed = set(provider.models)
    found = {model.id: model for model in models if model.id in listed}
    for model_id in provider.models:
        found.setdefault(model_id, DiscoveredModel(id=model_id, provider=provider.name))
    return [found[model_id] for model_id in provider.models]


def discover(
    catalogue: ProviderCatalogue,
    *,
    cache_path: Path | None = None,
    transport: httpx.BaseTransport | None = None,
) -> DiscoveryReport:
    """Discover every configured provider, falling back to the cache per provider."""
    cache_file = discovery_cache_path() if cache_path is None else cache_path
    cached = read_cache(cache_file)
    report = DiscoveryReport()

    for provider in catalogue.all():
        if not provider.configured:
            report.failed[provider.name] = "no credential"
            continue
        models = _listed(
            provider,
            fetch_models(
                provider,
                credential=catalogue.credential(provider.name),
                transport=transport,
            ),
        )
        if models:
            report.found[provider.name] = models
            continue
        fallback = _listed(provider, cached.get(provider.name, []))
        if fallback:
            report.found[provider.name] = fallback
            report.cached = True
        else:
            report.failed[provider.name] = "unreachable"

    if report.found:
        merged = dict(cached)
        merged.update(report.found)
        write_cache(cache_file, merged)
    return report


def merge_into(registry: Registry, report: DiscoveryReport) -> Registry:
    """Add discovered models the registry does not already describe.

    Hand-written entries win outright, including a deliberately lower
    ``context_window`` or a ``quality`` score; discovery only fills gaps.
    """
    merged: dict[str, ModelInfo] = {info.id: info for info in registry.all()}
    for models in report.found.values():
        for model in models:
            if model.id in merged:
                continue
            merged[model.id] = model.as_model_info()
    return Registry(merged)
