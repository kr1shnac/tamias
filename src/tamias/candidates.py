"""Which model should run this request -- and why every other one was refused.

Tamias never guesses.  A request names a model; the router may want a cheaper
one; either way the model that actually runs has passed three independent gates:

1. **capability** -- the registry says it can carry the work: tools when the
   request carries tools, a context window large enough for the messages, the
   modality the request uses, and the reasoning effort the caller asked for.
2. **supply** -- a configured provider serves it, that provider's credential was
   read at start-up, and (when accounting is required) the price sheet knows what
   it costs.
3. **health** -- the limit ledger has not recorded a lockout for it.

Each gate runs on its own and records its own rejection, so a decision can
explain itself: ``3 of 9 models capable, 2 unpriced, 1 limited``.  A model that
fails any gate is never offered as a target, which is what makes "switch to the
cheapest relevant model" safe rather than optimistic.

Ranking is deliberately boring: **cheapest first, then highest quality, then
model id** for a stable order.  The caller supplies a quality floor for the step
it is routing, so cheapness can never buy a bad outcome -- it can only choose
among models already judged good enough.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass

from tamias.limits import LimitLedger
from tamias.pricing import ModelPrice, PriceSheet
from tamias.providers import ProviderCatalogue
from tamias.registry import ModelInfo, Registry

#: Capability score used when neither the registry nor the caller says otherwise.
DEFAULT_QUALITY = 50.0

#: The highest score a *derived* (undeclared) score may reach.  Provider metadata
#: says whether a model can take tools, reason and hold a window; it says nothing
#: about whether its answers are good.  Capping the derived score keeps an
#: unannotated model from outranking one the operator rated, so "this model is
#: good enough for a hard step" always comes from a human decision.
DERIVED_QUALITY_CAP = 70.0

#: How the blended per-million-token cost is weighted.  Agent traffic reads far
#: more than it writes, but output is priced higher; a 3:1 input:output mix is
#: the cheapest defensible single number for ranking.
INPUT_WEIGHT = 0.75
OUTPUT_WEIGHT = 0.25

CAPABILITY = "capability"
SUPPLY = "supply"
HEALTH = "health"


@dataclass(frozen=True)
class Needs:
    """What this request must have to be answerable at all."""

    tools: bool = False
    effort: str | None = None
    tokens: int = 0
    modality: str = "text"

    @property
    def summary(self) -> str:
        parts = [f"{self.tokens} tok", self.modality]
        if self.tools:
            parts.append("tools")
        if self.effort:
            parts.append(f"effort={self.effort}")
        return ", ".join(parts)


@dataclass(frozen=True)
class Candidate:
    """A model that passed all three gates, with what it would cost."""

    model: str
    provider: str
    quality: float
    cost: float | None
    """Blended USD per million tokens, or ``None`` when the sheet cannot price it."""
    context_window: int
    tools: bool
    reasoning: bool

    @property
    def free(self) -> bool:
        return self.cost == 0.0

    @property
    def priced(self) -> bool:
        return self.cost is not None

    def cost_label(self) -> str:
        if self.cost is None:
            return "unpriced"
        if self.cost == 0.0:
            return "free"
        return f"${self.cost:.3f}/Mtok"

    @property
    def order(self) -> tuple[float, float, str]:
        """Cheapest first; unpriced last; higher quality breaks a cost tie."""
        if self.cost is None:
            return (float("inf"), -self.quality, self.model)
        return (self.cost, -self.quality, self.model)


@dataclass(frozen=True)
class Rejection:
    model: str
    provider: str
    gate: str
    reason: str

    def describe(self) -> str:
        return f"{self.model} [{self.gate}: {self.reason}]"


@dataclass(frozen=True)
class CandidatePool:
    """The result of the three gates: what may run, ranked, and what may not."""

    candidates: tuple[Candidate, ...]
    rejected: tuple[Rejection, ...] = ()
    considered: int = 0

    def __bool__(self) -> bool:
        return bool(self.candidates)

    def __len__(self) -> int:
        return len(self.candidates)

    def ids(self) -> tuple[str, ...]:
        return tuple(candidate.model for candidate in self.candidates)

    def get(self, model: str) -> Candidate | None:
        for candidate in self.candidates:
            if candidate.model == model:
                return candidate
        return None

    def best(
        self,
        *,
        min_quality: float = 0.0,
        exclude: Collection[str] = (),
        require_priced: bool = False,
    ) -> Candidate | None:
        """The cheapest candidate good enough for the step, or ``None``.

        ``min_quality`` is the floor that keeps a cheap model from degrading a
        step that needs a strong one.  ``exclude`` lets a caller skip the model
        that just failed without rebuilding the pool.
        """
        blocked = set(exclude)
        for candidate in self.candidates:
            if candidate.model in blocked:
                continue
            if candidate.quality < min_quality:
                continue
            if require_priced and not candidate.priced:
                continue
            return candidate
        return None

    def rejection_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for rejection in self.rejected:
            counts[rejection.gate] = counts.get(rejection.gate, 0) + 1
        return counts

    def reason(self) -> str:
        """One short, loggable sentence explaining the pool."""
        counts = self.rejection_counts()
        if not counts:
            return f"{len(self.candidates)} capable"
        parts = ", ".join(
            f"{counts[gate]} {gate}" for gate in (CAPABILITY, SUPPLY, HEALTH) if gate in counts
        )
        return f"{len(self.candidates)} capable; refused {parts}"


def blended_cost(price: ModelPrice | None) -> float | None:
    """USD per million tokens at a 3:1 input:output mix, or ``None`` if unpriced."""
    if price is None:
        return None
    if price.input is None or price.output is None:
        return None
    return INPUT_WEIGHT * price.input + OUTPUT_WEIGHT * price.output


def quality_of(info: ModelInfo) -> float:
    """The capability score for a model.

    An explicit ``quality`` in the registry wins.  Otherwise the score is derived
    from facts the registry does hold -- reasoning support, tool support and a
    large window are the three that correlate with carrying a hard step -- and
    capped well below the top of the scale, because capability metadata is not a
    quality measurement.  Pin ``quality`` in ``models.toml`` for any model you
    want trusted on a step that must not degrade.
    """
    declared = getattr(info, "quality", None)
    if isinstance(declared, (int, float)) and not isinstance(declared, bool):
        return float(declared)
    score = DEFAULT_QUALITY
    if info.reasoning:
        score += 15
    if info.tools:
        score += 10
    if info.context_window >= 400_000:
        score += 10
    elif info.context_window >= 128_000:
        score += 5
    return min(DERIVED_QUALITY_CAP, score)


def _capability_rejection(
    info: ModelInfo, needs: Needs, catalogue: ProviderCatalogue | None
) -> str | None:
    if needs.tools and not info.tools:
        return "no tool calls"
    if needs.tokens and info.context_window < needs.tokens:
        return f"window {info.context_window} < {needs.tokens}"
    if needs.modality != "text" and info.modality != needs.modality:
        return f"modality {info.modality} != {needs.modality}"
    if needs.effort and info.reasoning and needs.effort not in info.efforts:
        # The registry can walk an effort down, so this is only fatal when the
        # model advertises reasoning but no level at or below what was asked.
        cheaper = [level for level in info.efforts if level in ("none", "minimal", "low")]
        if not cheaper and needs.effort in ("low", "minimal", "none"):
            return f"no effort at or below {needs.effort}"
    if needs.effort and not info.reasoning:
        # Asking for reasoning from a model that never reasons is a silent
        # downgrade; the router records it rather than pretending.
        return "no reasoning support"
    return None


def _supply_rejection(
    info: ModelInfo,
    catalogue: ProviderCatalogue | None,
    sheet: PriceSheet | None,
    require_priced: bool,
) -> str | None:
    if catalogue is not None and len(catalogue):
        provider = catalogue.provider_for(info.id) or catalogue.get(info.provider)
        if provider is None:
            return "no configured provider"
        if not provider.configured:
            return f"provider {provider.name} has no credential"
    if require_priced:
        price = sheet.get(info.id) if sheet is not None else None
        if blended_cost(price) is None:
            return "unpriced"
    return None


def _health_rejection(
    info: ModelInfo,
    catalogue: ProviderCatalogue | None,
    limits: LimitLedger | None,
    now: float | None,
) -> str | None:
    if limits is None:
        return None
    provider = info.provider
    if catalogue is not None and len(catalogue):
        resolved = catalogue.provider_for(info.id)
        if resolved is not None:
            provider = resolved.name
    provider_limit = limits.provider_limited(provider, now)
    if provider_limit is not None:
        return f"provider cooling down {int(provider_limit.remaining(now))}s"
    limit = limits.get(provider, info.id, now)
    if limit is not None:
        return f"cooling down {int(limit.remaining(now))}s ({limit.reason})"
    return None


def build_pool(
    *,
    registry: Registry,
    sheet: PriceSheet | None = None,
    catalogue: ProviderCatalogue | None = None,
    limits: LimitLedger | None = None,
    needs: Needs | None = None,
    require_priced: bool = False,
    extra_providers: Mapping[str, str] | None = None,
    now: float | None = None,
) -> CandidatePool:
    """Run the three gates over every known model and rank what survives.

    ``registry`` is the model universe.  ``extra_providers`` lets a caller map a
    model id to a provider name that the registry does not carry (useful for a
    sheet- or discovery-sourced model).
    """
    requirement = needs or Needs()
    candidates: list[Candidate] = []
    rejected: list[Rejection] = []
    considered = 0

    for info in registry.all():
        considered += 1
        provider_name = info.provider
        if catalogue is not None and len(catalogue):
            resolved = catalogue.provider_for(info.id)
            if resolved is not None:
                provider_name = resolved.name
        if extra_providers and info.id in extra_providers:
            provider_name = extra_providers[info.id]

        capability = _capability_rejection(info, requirement, catalogue)
        if capability is not None:
            rejected.append(Rejection(info.id, provider_name, CAPABILITY, capability))
            continue

        supply = _supply_rejection(info, catalogue, sheet, require_priced)
        if supply is not None:
            rejected.append(Rejection(info.id, provider_name, SUPPLY, supply))
            continue

        health = _health_rejection(info, catalogue, limits, now)
        if health is not None:
            rejected.append(Rejection(info.id, provider_name, HEALTH, health))
            continue

        price = sheet.get(info.id) if sheet is not None else None
        candidates.append(
            Candidate(
                model=info.id,
                provider=provider_name,
                quality=quality_of(info),
                cost=blended_cost(price),
                context_window=info.context_window,
                tools=info.tools,
                reasoning=info.reasoning,
            )
        )

    candidates.sort(key=lambda candidate: candidate.order)
    return CandidatePool(
        candidates=tuple(candidates),
        rejected=tuple(rejected),
        considered=considered,
    )


def needs_from_body(body: Mapping[str, object]) -> Needs:
    """Read the three gate inputs straight out of a chat/responses request body."""
    messages = body.get("messages")
    tools = body.get("tools")
    has_tools = isinstance(tools, list) and bool(tools)

    effort: str | None = None
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict) and isinstance(reasoning.get("effort"), str):
        effort = reasoning["effort"]
    elif isinstance(body.get("reasoning_effort"), str):
        effort = body["reasoning_effort"]

    modality = "text"
    chars = 0
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str):
                chars += len(content)
                continue
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                kind = part.get("type")
                if kind in ("image_url", "input_image", "image"):
                    modality = "image"
                elif kind in ("input_audio", "audio"):
                    modality = "audio"
                text = part.get("text")
                if isinstance(text, str):
                    chars += len(text)
    # A token is ~4 characters for English text; round up so the gate is a
    # guard rail rather than an invitation to overrun the window.
    tokens = (chars + 3) // 4
    return Needs(tools=has_tools, effort=effort, tokens=tokens, modality=modality)


def pool_for_models(
    models: Iterable[str],
    *,
    registry: Registry,
    sheet: PriceSheet | None = None,
    catalogue: ProviderCatalogue | None = None,
    limits: LimitLedger | None = None,
    needs: Needs | None = None,
    require_priced: bool = False,
    now: float | None = None,
) -> CandidatePool:
    """Restrict the pool to ``models`` while keeping the same gates and order.

    The router's configured pool is an allowlist: a deployment that names its
    cheap models must not be silently widened to everything the registry knows.
    """
    allowed = set(models)
    full = build_pool(
        registry=registry,
        sheet=sheet,
        catalogue=catalogue,
        limits=limits,
        needs=needs,
        require_priced=require_priced,
        now=now,
    )
    return CandidatePool(
        candidates=tuple(c for c in full.candidates if c.model in allowed),
        rejected=tuple(r for r in full.rejected if r.model in allowed),
        considered=full.considered,
    )
