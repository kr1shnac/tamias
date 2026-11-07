"""Remembered limits: a 429 must change what the *next* request tries.

The claim these tests defend: one provider refusal is learned once, applied to
every later decision until the cooldown expires, and survives a restart.  A
client-error status must never cool a model down, and a ``Retry-After`` the
provider asks for must be honoured when it is longer than the ladder.
"""

from __future__ import annotations

import time

import pytest

from tamias.limits import (
    BACKOFF_SECONDS,
    QUOTA_TTL_SECONDS,
    RETRYABLE_STATUSES,
    UNAVAILABLE_SECONDS,
    LimitLedger,
    Quota,
    parse_retry_after,
    quota_from_headers,
    retryable,
)

NOW = 1_800_000_000.0


# ------------------------------------------------------------------- the quota ---


def test_quota_headers_are_read_in_either_house_style():
    openai_style = quota_from_headers(
        "openrouter",
        {"x-ratelimit-remaining-requests": "42", "x-ratelimit-reset-requests": "120"},
    )
    draft_style = quota_from_headers("openrouter", {"ratelimit-remaining": "42"})

    assert openai_style is not None
    assert openai_style.remaining == 42
    assert openai_style.resets_in == 120
    assert draft_style is not None and draft_style.remaining == 42
    assert draft_style.resets_in is None, "no reset header means no reset to report"


def test_no_quota_headers_means_no_quota_to_show():
    """An invented remaining count is a number no provider stands behind."""
    assert quota_from_headers("nvidia", {"content-type": "application/json"}) is None
    assert quota_from_headers("nvidia", {"x-ratelimit-remaining": ""}) is None


def test_a_duration_string_reset_is_understood():
    quota = quota_from_headers("openrouter", {"x-ratelimit-reset-requests": "1m30s"})
    assert quota is not None and quota.resets_in == 90


def test_a_recorded_quota_round_trips_and_expires():
    ledger = LimitLedger(":memory:")
    ledger.record_quota(
        Quota(provider="openrouter", remaining=17, resets_in=90, seen=NOW), now=NOW
    )

    (quota,) = ledger.quotas(now=NOW + 5)
    assert quota.provider == "openrouter" and quota.remaining == 17
    assert quota.resets_in == pytest.approx(85, abs=1)

    assert ledger.quotas(now=NOW + QUOTA_TTL_SECONDS + 1) == ()


def test_a_stale_quota_is_pruned_with_the_cooldowns():
    ledger = LimitLedger(":memory:")
    ledger.record_quota(
        Quota(provider="openrouter", remaining=5, resets_in=None, seen=NOW), now=NOW
    )
    ledger.record("nvidia", "some/model", 429, now=NOW)

    assert ledger.prune(now=NOW + QUOTA_TTL_SECONDS + 1) == 2
    assert ledger.quotas(now=NOW + QUOTA_TTL_SECONDS + 1) == ()
    assert ledger.get("nvidia", "some/model", now=NOW + QUOTA_TTL_SECONDS + 1) is None


def test_a_429_locks_the_model_out_and_the_lock_expires():
    ledger = LimitLedger(":memory:")
    limit = ledger.record("openrouter", "cheap/model", 429, now=NOW)

    assert limit is not None and limit.strikes == 1
    assert ledger.get("openrouter", "cheap/model", now=NOW + 1).until == NOW + BACKOFF_SECONDS[0]
    assert ledger.is_limited("openrouter", "cheap/model", now=NOW + 1)
    assert not ledger.is_limited("openrouter", "cheap/model", now=NOW + BACKOFF_SECONDS[0] + 1)


def test_the_cooldown_escalates_with_repeated_strikes():
    ledger = LimitLedger(":memory:")
    waits = []
    for step in range(len(BACKOFF_SECONDS) + 1):
        moment = NOW + step * 10_000
        limit = ledger.record("openrouter", "cheap/model", 429, now=moment)
        waits.append(limit.until - moment)

    assert waits[: len(BACKOFF_SECONDS)] == [float(value) for value in BACKOFF_SECONDS]
    assert waits[-1] == BACKOFF_SECONDS[-1], "the ladder caps instead of growing forever"


def test_retry_after_wins_only_when_it_asks_for_longer():
    ledger = LimitLedger(":memory:")

    longer = ledger.record("nvidia", "m", 429, retry_after=900, now=NOW)
    shorter = ledger.record("openai", "m", 429, retry_after=1, now=NOW)

    assert longer.until - NOW == 900
    assert shorter.until - NOW == BACKOFF_SECONDS[0]


def test_a_request_error_never_cools_a_model_down():
    ledger = LimitLedger(":memory:")

    assert ledger.record("openrouter", "m", 400, now=NOW) is None
    assert ledger.record("openrouter", "m", 422, now=NOW) is None
    assert ledger.active(now=NOW) == ()


def test_a_404_is_remembered_for_hours_not_minutes():
    """A model the account cannot call is not a rate limit and must not be retried."""
    ledger = LimitLedger(":memory:")

    limit = ledger.record("nvidia", "retired/model", 404, now=NOW)

    assert limit is not None
    assert limit.until - NOW == UNAVAILABLE_SECONDS
    assert "not offered" in limit.reason
    assert ledger.is_limited("nvidia", "retired/model", now=NOW + 60)
    assert not ledger.is_limited("nvidia", "retired/model", now=NOW + UNAVAILABLE_SECONDS + 1)


def test_a_repeated_404_does_not_escalate_beyond_one_expiry():
    ledger = LimitLedger(":memory:")

    first = ledger.record("nvidia", "m", 404, now=NOW)
    again = ledger.record("nvidia", "m", 404, retry_after=1, now=NOW + 10)

    assert again.until - (NOW + 10) == first.until - NOW


def test_retryable_covers_both_a_limit_and_a_model_this_provider_does_not_serve():
    assert retryable(429)
    assert retryable(503)
    assert retryable(404), "a 404 means another model may still answer"
    assert not retryable(400), "a malformed request would fail everywhere"
    assert 404 not in RETRYABLE_STATUSES


def test_a_rejected_credential_locks_the_whole_provider():
    ledger = LimitLedger(":memory:")

    ledger.record("openrouter", "some/model", 401, now=NOW)

    assert ledger.provider_limited("openrouter", now=NOW) is not None
    # The model row stays clean: the fault is the key, not this model.
    assert ledger.get("openrouter", "some/model", now=NOW) is None


def test_a_gated_model_cools_down_like_a_rate_limit():
    ledger = LimitLedger(":memory:")

    limit = ledger.record("openrouter", "gated/model", 403, now=NOW)

    assert limit is not None and ledger.is_limited("openrouter", "gated/model", now=NOW)


def test_a_success_clears_the_lock():
    ledger = LimitLedger(":memory:")
    ledger.record("openrouter", "m", 429, now=NOW)

    ledger.clear("openrouter", "m")

    assert not ledger.is_limited("openrouter", "m", now=NOW)


def test_the_lock_survives_a_restart(tmp_path):
    path = tmp_path / "limits.db"
    ledger = LimitLedger(path)
    ledger.record("nvidia", "m", 429, now=NOW)
    ledger.close()

    reopened = LimitLedger(path)

    assert reopened.is_limited("nvidia", "m", now=NOW + 1)


def test_prune_drops_only_expired_rows():
    ledger = LimitLedger(":memory:")
    ledger.record("a", "m", 429, now=NOW)
    ledger.record("b", "m", 429, now=NOW + 10_000)

    removed = ledger.prune(now=NOW + BACKOFF_SECONDS[0] + 1)

    assert removed == 1
    assert [limit.provider for limit in ledger.active(now=NOW + 10_001)] == ["b"]


def test_active_lists_the_soonest_expiry_first():
    ledger = LimitLedger(":memory:")
    ledger.record("late", "m", 429, retry_after=1000, now=NOW)
    ledger.record("soon", "m", 429, retry_after=100, now=NOW)

    assert [limit.provider for limit in ledger.active(now=NOW)] == ["soon", "late"]


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("120", 120.0),
        ("0", 0.0),
        (" 30 ", 30.0),
        ("-5", None),
        ("soon", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_retry_after_accepts_seconds_and_rejects_nonsense(header, expected):
    assert parse_retry_after(header, now=NOW) == expected


def test_parse_retry_after_accepts_an_http_date():
    now = time.time()

    seconds = parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT", now=now)

    assert seconds == 0.0, "a date in the past means retry now, not a negative wait"
