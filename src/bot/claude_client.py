"""Wraps the Anthropic SDK for the trading bot's decision step.

Design:
- Frozen system prompt (no datetimes, IDs, or volatile content) so prompt caching works
- Volatile market state goes in the user message, after the cache breakpoint
- Structured outputs via `messages.parse()` against a Pydantic schema
- Tier-aware: Haiku has no thinking/effort; Sonnet/Opus default to adaptive thinking

Caching note: the system prompt is marked with a 1-hour TTL. At the default
15-minute decision cadence a 5-minute cache would expire between every call and
never pay for itself, whereas a 1-hour cache is read roughly four times per
write. See docs/COSTS.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import anthropic
from pydantic import BaseModel, Field

from .config import CLAUDE_MODEL_IDS, CLAUDE_PRICING_USD_PER_MTOK, ClaudeTier, Env, Rules
from .models import OrderProposal, PositionPlan, SymbolAssessment


class ClaudeDecision(BaseModel):
    """Top-level output Claude must produce on every call.

    `assessments` and `position_plans` cost output tokens on a cycle that
    proposes nothing, which is most cycles. That is the point. Without them a
    quiet cycle records only "no proposals", and "nothing met the conditions"
    is indistinguishable from "the loop never looked", which are very different
    states to be in and only one of them is fine.
    """

    market_assessment: str = Field(description="One-paragraph read of the current market.")
    proposals: list[OrderProposal] = Field(
        default_factory=list,
        description=(
            "Zero or more order proposals. An empty list means stand pat, "
            "which is a valid and often correct answer."
        ),
    )
    assessments: list[SymbolAssessment] = Field(
        default_factory=list,
        description=(
            "One entry for EVERY tradeable symbol you were given indicators for, "
            "including the ones you are not proposing. This is how the operator "
            "sees what you considered."
        ),
    )
    position_plans: list[PositionPlan] = Field(
        default_factory=list,
        description=(
            "One entry per position currently open, saying why it is still held "
            "and what would close it. Advisory only: nothing here is executed."
        ),
    )


@dataclass(frozen=True)
class CallUsage:
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    estimated_cost_usd: float


SYSTEM_PROMPT_TEMPLATE = """\
You are the decision engine inside an automated trading bot running against an
Alpaca **paper-trading** account (US equities and, when enabled, crypto).

You DO NOT execute trades. You PROPOSE orders. A separate, deterministic risk
module accepts or rejects each proposal against the rules below. That module is
code, not a prompt — you cannot negotiate with it, and arguing with a rejection
will not change the outcome. Your job is to make proposals it will approve.

## Hard rules (mirrored from config/rules.yaml)

{rules_summary}

## Output contract

Return JSON with:

- `market_assessment`: one paragraph on the current market read.
- `proposals`: a list of order proposals. An empty list is a valid answer and is
  the right one whenever no high-quality setup exists.
- `assessments`: **one entry for every symbol you were given indicators for**,
  including the ones you are passing on.
- `position_plans`: one entry for every position currently open.

Each proposal needs `symbol`, `direction` (buy or sell), `qty` (shares or coin
units, not lots), `limit_price`, `stop_loss_price`, `take_profit_price`, and a
`rationale` of at least one full sentence.

### assessments — say what you looked at, not only what you took

A cycle that proposes nothing must still record what you examined. Otherwise
"nothing met the conditions" and "the loop never looked at QQQ" are the same
entry afterwards, and only one of them is a working bot.

Each assessment needs `symbol`, `stance`, `reasoning`, and — when the stance is
`watch` — `waiting_for`.

- `take` — you are proposing it this cycle.
- `watch` — the setup is forming but a condition is not met yet. `waiting_for`
  must name **the observable thing that would trigger it**: a price level, a
  figure, a session. "SPY closing below 641.20, roughly 1 ATR under the 20-day"
  is useful. "More confirmation" is not, and will be shown to the operator as
  the empty statement it is.
- `pass` — you examined it and there is nothing there. Say briefly why.
- `blocked` — you would take it, but a rule forbids it or the data is missing.
  Name which.

Ground every `reasoning` in the figures supplied in the Indicators section. Do
not restate a number that is reported as unavailable, and do not compute one.

### position_plans — why you are still in, and what gets you out

For each open position: `symbol`, `action` (`hold`, `close` or `tighten_stop`),
`thesis_intact`, `reasoning`, `waiting_for`, `invalidation`.

**These are advisory and are not executed.** Closing a position and moving a
stop sit outside the proposal path deliberately. Write them for a person who
wants to know why the position is still on and what would end it — name the
level or the target, not a mood.

## How to behave

- **Prefer doing nothing.** In a controlled experiment where six frontier models
  traded real money for two weeks, the model that made 238 trades lost 57% of its
  stake while the most selective made 38. Fees and churn, not stock picking, did
  most of the damage. Trading less is a strategy, not a failure.
- **Size down, never up.** If a setup only fits within the risk cap at a smaller
  size, propose the smaller size.
- **Defer when volatility spikes or news is imminent.**
- **State the invalidation level.** Every rationale must say what signal you are
  acting on and what price would prove you wrong. Vague rationales get flagged in
  the audit log.
- **Never propose a market order.** Limit prices only, and keep them close to the
  current market.
"""


# A strategy is a label plus one line of guidance. Deliberately thin: the point
# is that every trade is tagged with which strategy produced it, so metrics.py
# can tell them apart. Deciding what these should actually be is the operator's
# job and is the genuinely hard part of the whole project.
def _strategy_block(name: str) -> str:
    """Indent a strategy's guidance so it sits under its instrument heading."""
    from .strategy import guidance_for

    return "\n".join(f"      {line}" for line in guidance_for(name).splitlines())


def build_system_prompt(rules: Rules) -> str:
    """Render the (static) system prompt from rules.

    Same rules in, same bytes out, so the prompt cache actually hits.
    """
    # One block per enabled class, since each carries its own session window,
    # symbol list and strategy. Disabled classes are omitted entirely rather
    # than listed as unavailable, which would only invite proposals for them.
    instrument_lines: list[str] = []
    for name, instrument in sorted(rules.enabled_instruments.items()):
        cap = (
            f", max {instrument.capital_cap_pct:.0f}% of equity"
            if instrument.capital_cap_pct is not None
            else ""
        )
        instrument_lines.append(
            f"- {name} — strategy '{instrument.strategy}'{cap}\n"
            f"    symbols: {', '.join(sorted(instrument.allowed_symbols))}\n"
            f"    sessions (UTC): {instrument.sessions_utc}\n"
            f"    approach:\n{_strategy_block(instrument.strategy)}"
        )

    rules_summary = "\n".join(
        [
            "## Instruments you may trade\n",
            *instrument_lines,
            "\n## Portfolio limits (apply across every instrument)\n",
            f"- Max risk per trade: {rules.account.max_risk_per_trade_pct:.2f}% of equity",
            f"- Max COMBINED risk across all open positions: "
            f"{rules.account.max_total_risk_pct:.2f}% of equity. This is the "
            f"binding constraint most of the time — size each trade so the total "
            f"stays under it.",
            f"- Max single position value: {rules.account.max_position_pct:.0f}% of "
            f"equity (a concentration check, rarely the limit that binds)",
            f"- Max concurrent positions: {rules.account.max_concurrent_positions}",
            f"- Max gross exposure: {rules.margin.max_gross_notional_pct:.0f}% of equity",
            f"- Daily loss kill-switch: {rules.account.daily_loss_kill_pct:.1f}%",
            f"- Stand-down: {rules.stand_down.consecutive_losses_trigger} consecutive "
            f"losses beyond {rules.stand_down.loss_threshold_r:.2f}R suspends live "
            f"trading for {rules.stand_down.stage_one_days} days "
            f"({rules.stand_down.stage_two_days} on a repeat within "
            f"{rules.stand_down.repeat_window_days} days). Paper trading continues.",
            f"- Max trades: {rules.frequency.max_trades_per_day}/day, "
            f"{rules.frequency.max_trades_per_week}/week",
            f"- Cooldown per symbol: {rules.frequency.min_seconds_between_trades_per_symbol}s",
            f"- Max buying power per order: "
            f"{rules.margin.max_buying_power_utilisation_pct:.0f}%",
            f"- News blackout: {rules.news_blackout_minutes_before} min before / "
            f"{rules.news_blackout_minutes_after} min after high-impact events",
        ]
    )
    return SYSTEM_PROMPT_TEMPLATE.format(rules_summary=rules_summary)


class ClaudeClient:
    def __init__(self, env: Env, system_prompt: str) -> None:
        self._client = anthropic.Anthropic(api_key=env.anthropic_api_key)
        self._tier = env.claude_tier
        self._model = CLAUDE_MODEL_IDS[self._tier]
        self._system_prompt = system_prompt

    def propose(self, market_context: str) -> tuple[ClaudeDecision, CallUsage]:
        """Send the market context, get back a structured decision plus token accounting."""
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": 4096,
            "system": [
                {
                    "type": "text",
                    "text": self._system_prompt,
                    "cache_control": {"type": "ephemeral", "ttl": "1h"},
                }
            ],
            "messages": [{"role": "user", "content": market_context}],
            "output_format": ClaudeDecision,
        }

        if self._tier in (ClaudeTier.SONNET, ClaudeTier.OPUS):
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["output_config"] = {"effort": "medium"}

        response = self._client.messages.parse(**kwargs)
        decision = response.parsed_output
        if decision is None:
            raise RuntimeError(
                "Claude returned no parsable decision; check the output schema "
                "and whether the response hit max_tokens."
            )
        return decision, self._usage_from(response)

    def _usage_from(self, response: anthropic.types.Message) -> CallUsage:
        u = response.usage
        in_tokens = u.input_tokens
        out_tokens = u.output_tokens
        cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0

        in_price, out_price, cache_read_price = CLAUDE_PRICING_USD_PER_MTOK[self._tier]
        # 1-hour cache writes bill at 2x base input.
        cost = (
            in_tokens * in_price
            + out_tokens * out_price
            + cache_read * cache_read_price
            + cache_write * in_price * 2.0
        ) / 1_000_000

        return CallUsage(
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            estimated_cost_usd=cost,
        )
