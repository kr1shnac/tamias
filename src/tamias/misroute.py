"""Escalation after a downgrade that went wrong: the router learning from the log.

``verdict`` reads the request log and can say that a downgrade was followed by a
failed turn.  This module is what acts on that, and it is the first part of
tamias whose behaviour changes because of what it has observed.

**Why the scope is one conversation.**  A misroute is one piece of evidence
about one model on one kind of step, and the counterfactual is never visible:
the proxy did not also run the strong model, so it cannot know the cheap one
failed where a better model would have succeeded.  One unlucky turn must
therefore not suppress a model for every conversation -- that is how a routing
gateway learns the wrong lesson from a single bad session.  So the policy here
is session-scoped: the conversation that showed a downgrade followed by a
failure stops shopping for cheap models for a while, and everyone else is
untouched.

**Why the state is in-memory.**  The durable record is the request log;
``tamias report --routing-verdict`` reads it, and the counters here are a
per-process reading of it.  A restart forgets them, exactly as the session table
does, and nothing learned here is ever hidden: an escalated request is logged as
a STAY whose reason names the escalation, so the report shows the policy's
effect.  When the log holds enough evidence to justify a *model-wide* decision,
that is a change to make on purpose with the data in front of you, not a
consequence this module reaches on its own.

**Off by default.**  Turning it on is a statement about how much you trust the
signal, and in ``shadow`` mode the policy is simulated rather than applied, on
the same terms as every other routing decision.
"""

from __future__ import annotations

from dataclasses import dataclass

from tamias.verdict import ERROR, classify_reason

__all__ = ["MisroutePolicy", "DEFAULT_POLICY", "failure_follows_downgrade", "escalation_reason"]


@dataclass(frozen=True)
class MisroutePolicy:
    """How a session escalates after a downgrade that went wrong.

    ``enabled`` is the operator's switch.  ``escalate_turns`` is how many
    further requests stay on the model the caller named once a session has
    shown a downgrade followed by a failure -- long enough to stop a conversation
    from being carried by a model that has already stumbled once, short enough
    that a mechanical run is not pinned to the expensive model for its whole life.
    """

    enabled: bool = False
    escalate_turns: int = 5


#: What ``create_app`` uses when the operator passes nothing: no adaptation.
DEFAULT_POLICY = MisroutePolicy()


def failure_follows_downgrade(prior_was_downgrade: bool, reason: object) -> bool:
    """Did this request look like a failure, and was the last one a downgrade?

    Both halves matter.  A failure on its own is exactly what the strong model
    is for anyway, and a downgrade on its own says nothing, so a strike is only
    recorded when a failure arrived on the turn *after* the session was moved
    off the model the caller asked for.
    """
    if not prior_was_downgrade:
        return False
    return classify_reason(reason) == ERROR


def escalation_reason(turns_left: int) -> str:
    """The reason a log row carries when the policy, not a rule, kept the model."""
    return f"misroute escalation: strong model for {turns_left} more request(s)"
