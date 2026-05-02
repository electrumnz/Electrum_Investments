"""Wraps the Anthropic SDK for the trading bot's decision step.

Design:
- Frozen system prompt (no datetimes, IDs, or volatile content) so prompt caching works
- Volatile market state goes in the user message, after the cache breakpoint
- Structured outputs via `messages.parse()` against a Pydantic schema
- Tier-aware: Haiku has no thinking/effort; Sonnet/Opus default to adaptive thinking
"""

from __future__ import annotations

from dataclasses import dataclass

import anthropic
from pydantic import BaseModel, Field

from .config import CLAUDE_MODEL_IDS, ClaudeTier, Env, Rules
from .models import OrderProposal


class ClaudeDecision(BaseModel):
    """Top-level output Claude must produce on every call."""

    market_assessment: str = Field(description="One-paragraph read of the current market.")
    proposals: list[OrderProposal] = Field(
        description=(
            "Zero or more order proposals. Empty list means stand pat — that's a valid output."
        )
    )


@dataclass(frozen=True)
class CallUsage:
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    estimated_cost_usd: float


PRICE_PER_M_TOKENS: dict[ClaudeTier, tuple[float, float]] = {
    ClaudeTier.HAIKU: (1.0, 5.0),
    ClaudeTier.SONNET: (3.0, 15.0),
    ClaudeTier.OPUS: (5.0, 25.0),
}


SYSTEM_PROMPT_TEMPLATE = """\
You are the decision engine inside an automated trading bot for BlackBull Markets.
You DO NOT execute trades. You PROPOSE orders. A separate, deterministic risk module
will accept or reject each proposal against the rules below — your job is to make
proposals that the risk module will approve.

## Hard rules (mirrored from config/rules.yaml — always honour these)

{rules_summary}

## Output contract

Return JSON matching this shape exactly:

- `market_assessment`: one paragraph on the current market read.
- `proposals`: list of order proposals; an empty list is a valid answer when
  no high-quality setup exists. Do not propose marginal trades to look productive.

Each proposal must include `symbol`, `direction` (buy or sell), `size_lots`,
`stop_loss_price`, `take_profit_price`, and a `rationale` of at least one full sentence.

## Style

- Prefer doing nothing over forcing a trade.
- Risk per trade must fit within the per-trade cap; you can size down but never up.
- If volatility is unusually high or news is imminent, defer.
- Be explicit in your rationale about WHAT signal you're acting on and WHERE the
  invalidation level is. Vague rationales will be flagged in audit.
"""


def build_system_prompt(rules: Rules) -> str:
    """Render the (static) system prompt from rules. Same input → same bytes → cacheable."""
    rules_summary = "\n".join(
        [
            f"- Allowed symbols: {', '.join(sorted(rules.allowed_symbols))}",
            f"- Crypto sleeve: {'enabled' if rules.crypto_sleeve.enabled else 'disabled'}"
            + (
                f" (cap {rules.crypto_sleeve.capital_cap_pct}% of equity, "
                f"symbols: {', '.join(sorted(rules.crypto_sleeve.allowed_symbols))})"
                if rules.crypto_sleeve.enabled
                else ""
            ),
            f"- Max risk per trade: {rules.account.max_risk_per_trade_pct:.2f}% of equity",
            f"- Max concurrent positions: {rules.account.max_concurrent_positions}",
            f"- Max gross leverage: {rules.account.max_gross_leverage:.1f}x",
            f"- Daily loss kill-switch: {rules.account.daily_loss_kill_pct:.1f}%",
            f"- Allowed UTC sessions: {rules.sessions_utc}",
            f"- News blackout: {rules.news_blackout_minutes_before} min before / "
            f"{rules.news_blackout_minutes_after} min after high-impact events",
            f"- Min hold time after entry: {rules.min_hold_seconds}s",
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
        """Send the market context, get back a structured decision + token accounting."""
        kwargs: dict[str, object] = {
            "model": self._model,
            "max_tokens": 4096,
            "system": [
                {
                    "type": "text",
                    "text": self._system_prompt,
                    "cache_control": {"type": "ephemeral"},
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
        usage = self._usage_from(response)
        return decision, usage

    def _usage_from(self, response: anthropic.types.Message) -> CallUsage:
        u = response.usage
        in_tokens = u.input_tokens
        out_tokens = u.output_tokens
        cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
        in_price, out_price = PRICE_PER_M_TOKENS[self._tier]
        cost = (
            in_tokens * in_price
            + out_tokens * out_price
            + cache_read * in_price * 0.1
            + cache_write * in_price * 1.25
        ) / 1_000_000
        return CallUsage(
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            estimated_cost_usd=cost,
        )
