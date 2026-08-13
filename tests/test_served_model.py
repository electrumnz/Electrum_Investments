"""Which model actually answered, as distinct from which one was asked for.

Two endpoints now serve this repository's model calls, and `CLAUDE.md` records
what that already cost once: `Env.inference_provider` described a
CONFIGURATION and was read as though it described the ENDPOINT, so a box with
`ANTHROPIC_BASE_URL` aimed at a proxy got a banner saying "Anthropic direct"
and the schema-enforced transport pointed at something that may ignore it.

This is the same distinction one level further in. The request names a model;
the response says what answered. A catalogue that aliases a retired id, a proxy
that routes elsewhere, an endpoint serving a familiar name from unfamiliar
weights — every one of those succeeds, validates against the schema, and is
priced against the model that was asked for. Nothing anywhere said so.

**It REPORTS and never refuses**, which is the property most likely to be
"improved" later. An alias is an ordinary thing for a catalogue to do, and
throwing away a validated decision — proposals, assessments and the cost
already spent — over a naming difference would cost far more than it protects.
"""

from __future__ import annotations

from datetime import UTC, datetime

from bot.model_client import CallUsage
from bot.models import Decision


def _usage(served: str | None, requested: str | None) -> CallUsage:
    return CallUsage(
        input_tokens=10,
        output_tokens=5,
        cache_read_tokens=0,
        cache_write_tokens=0,
        estimated_cost_usd=0.001,
        served_model=served,
        requested_model=requested,
    )


def test_the_same_model_back_reports_agreement() -> None:
    assert _usage("claude-sonnet-5", "claude-sonnet-5").served_as_requested is True


def test_a_different_model_back_is_a_positive_finding() -> None:
    usage = _usage("anthropic-claude-5-haiku", "claude-sonnet-5")
    assert usage.served_as_requested is False


def test_no_model_id_in_the_response_is_UNKNOWN_and_never_a_mismatch() -> None:
    """The third value is the whole point, and it is the easy one to lose.

    `None` here means the response carried no model id — "could not ask". A
    `bool` would have to pick between calling that agreement, which invents
    reassurance, and calling it a mismatch, which invents a finding. Same rule
    as `has_cycles`, `can_grade_anything` and first-visit being kept apart from
    empty.
    """
    assert _usage(None, "claude-sonnet-5").served_as_requested is None
    assert _usage("claude-sonnet-5", None).served_as_requested is None
    assert _usage(None, None).served_as_requested is None


def test_a_non_string_model_field_reads_as_unknown_rather_than_being_coerced() -> None:
    """The SDK builds responses with unchecked construction, measured not assumed.

    `CLAUDE.md` records the same trap on the token counts: a `usage` of
    `{"input_tokens": "10"}` reaches the reader carrying a string. `str()` on
    an object that is not a name would produce something plausible-looking,
    which would then be compared against the requested id and reported as a
    substitution nobody performed — a plausible wrong figure, arriving through
    the accounting.
    """
    from bot.model_client import ModelClient

    class _Usage:
        input_tokens = 10
        output_tokens = 5

    class _Response:
        usage = _Usage()
        model = object()

    client = ModelClient.__new__(ModelClient)
    client._model = "claude-sonnet-5"

    class _Spec:
        pricing = None

    client._spec = _Spec()  # type: ignore[assignment]
    usage = ModelClient._usage_from(client, _Response())  # type: ignore[arg-type]
    assert usage.served_model is None
    assert usage.served_as_requested is None


def test_the_response_model_is_carried_through_when_it_is_a_string() -> None:
    from bot.model_client import ModelClient

    class _Usage:
        input_tokens = 10
        output_tokens = 5

    class _Response:
        usage = _Usage()
        model = "anthropic-claude-5-sonnet"

    client = ModelClient.__new__(ModelClient)
    client._model = "claude-sonnet-5"

    class _Spec:
        pricing = None

    client._spec = _Spec()  # type: ignore[assignment]
    usage = ModelClient._usage_from(client, _Response())  # type: ignore[arg-type]
    assert usage.served_model == "anthropic-claude-5-sonnet"
    assert usage.requested_model == "claude-sonnet-5"
    assert usage.served_as_requested is False


def test_the_audit_record_carries_the_served_model_and_defaults_to_unknown() -> None:
    """The heartbeat says it once and scrolls; the audit log has to keep it.

    A decision three months old still has to be able to answer "which weights
    sized this position". The default is `None` rather than the requested id,
    because every record written before this field existed genuinely does not
    know — and the audit log is append-only and never migrated, so those
    records will be read back for as long as the log exists.
    """
    old = Decision(timestamp=datetime(2026, 5, 4, 15, 0, tzinfo=UTC))
    assert old.served_model_id is None

    recorded = Decision(
        timestamp=datetime(2026, 5, 4, 15, 0, tzinfo=UTC),
        served_model_id="anthropic-claude-5-sonnet",
    )
    assert recorded.served_model_id == "anthropic-claude-5-sonnet"
    # Round-trips through the append-only format, which is the only thing that
    # makes it recoverable later.
    assert (
        Decision.model_validate_json(recorded.model_dump_json()).served_model_id
        == "anthropic-claude-5-sonnet"
    )
