#!/usr/bin/env python
"""Run the real agent-to-agent conference and measure every cap it claims.

The LIVE half of `tests/test_confer.py`, in the same shape and for the same
reason as `agent_behaviour_live.py` and `dream_cycle_live.py`: `tests/` may not
touch the network, so the caps are pinned offline with a stubbed `TurnCaller`
and the two agents have never actually been watched talking to each other.

`bot.confer.run_conference` and `bot.confer.Conference` are used unmodified.
Nothing here re-implements a prompt, a turn, a cap or a store write.

## What it drives, and in what order

The order matters, because each phase leaves the shelf in the state the next one
needs:

1. **A real run.** `run_conference` over whatever is in `Vault.VAULT`. This is
   the one that costs model calls and produces the transcripts.
2. **The change gate.** The same call again, seconds later. Nothing has changed
   about any dream, so every candidate should be skipped for free.
3. **The epoch.** Exchanges driven directly through `confer_once` — which is
   documented as reachable from a command and bypasses `_consider` — until
   `exchanges_in_epoch` reaches `MAX_EXCHANGES_PER_EPOCH`, recording the epoch
   arithmetic after each one. Then `run()` again, to watch it refuse.
4. **The hand-back.** `TraderPowers.adopt` then `TraderPowers.hand_back`, which
   are the trading agent's only two powers, then `run()` again: a fresh exchange
   is allowed and the LIFETIME count carries.
5. **The lifetime ceiling**, if `--lifetime` is passed. Twelve exchanges on one
   dream is expensive, so it is opt-in rather than default.

## What is observed rather than read

Two structural claims, and reading the imports at the top of `confer.py` is not
evidence for either:

- **The trading agent's side reaches no broker.** Every hostname resolved and
  every socket peer is recorded through an audit hook, and so is the set of
  `bot.*` modules that ended up loaded. `AlpacaBroker` and `MockBroker` both
  live in `bot.broker`; a process that never imports it holds no broker at all.
- **An exchange that decided nothing is still stored.** The transcript is read
  back out of the store after each exchange and written to the record verbatim.

Usage:

    ANTHROPIC_API_KEY_ELECTRUM=... python scripts/confer_live.py \
        --dreams /path/to/dreams.db --out tests/fixtures/confer_live.json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO = Path(os.environ.get("MUDHORN_REPO", "/home/user/Electrum_Investments"))
sys.path.insert(0, str(REPO / "src"))

from bot.confer import (  # noqa: E402
    MAX_DREAMS_PER_RUN,
    MAX_EXCHANGES_LIFETIME,
    MAX_EXCHANGES_PER_EPOCH,
    MAX_TURNS_PER_EXCHANGE,
    TEXT_MAX_CHARS,
    Conference,
    ConferenceReport,
    ExchangeResult,
    TraderPowers,
    epoch_started_at,
    exchanges_in_epoch,
    exchanges_so_far,
    has_something_changed,
    last_agent_turn_at,
    run_conference,
)
from bot.config import CLAUDE_MODEL_IDS, Env, Rules  # noqa: E402
from bot.dreaming import DREAMER, OPERATOR, Dream, DreamStore, Vault  # noqa: E402


class Tripwire:
    """What the process reached for, watched rather than read off the source.

    Identical in intent to the one in `dream_cycle_live.py`: a structural claim
    is checked by watching the process. An audit hook records every hostname
    resolved and every socket peer, and `sys.modules` says which `bot.*` modules
    were ever loaded — a run that never imports `bot.broker` is a run in which no
    broker object exists.
    """

    ORDER_PATH = ("bot.broker", "bot.risk", "bot.mcp_server", "bot.reconcile", "bot.main")

    def __init__(self) -> None:
        self.hosts: set[str] = set()
        self.peers: set[str] = set()

    def install(self) -> None:
        def hook(event: str, args: tuple[Any, ...]) -> None:
            if event == "socket.getaddrinfo" and args:
                self.hosts.add(str(args[0]))
            elif event == "socket.connect" and len(args) > 1:
                self.peers.add(str(args[1]))

        sys.addaudithook(hook)

    def report(self) -> dict[str, Any]:
        return {
            "hostnames_resolved": sorted(self.hosts),
            "socket_peers": sorted(self.peers),
            "bot_modules_loaded": sorted(
                m for m in sys.modules if m == "bot" or m.startswith("bot.")
            ),
            "order_path_modules_loaded": [m for m in self.ORDER_PATH if m in sys.modules],
        }


class RecordingCaller:
    """Wraps one side's `ClaudeClient` so the turn is seen BEFORE the store trims it.

    `DreamStore.add_message` trims to `TEXT_MAX_CHARS`, so reading the stored
    text can only ever show a message at or under the cap — which proves nothing
    about whether the cap ever bit. The pre-trim length is the number worth
    having, and it can only be taken here.
    """

    def __init__(self, inner: Any, who: str) -> None:
        self.inner = inner
        self.who = who
        self.calls: list[dict[str, Any]] = []

    def confer(self, prompt: str, output_format: Any) -> tuple[Any, Any]:
        turn, usage = self.inner.confer(prompt, output_format)
        text = str(getattr(turn, "text", ""))
        self.calls.append(
            {
                "who": self.who,
                "prompt_chars": len(prompt),
                "raw_text_chars": len(text),
                "over_text_max_chars": len(text) > TEXT_MAX_CHARS,
                "verdict": str(getattr(turn, "verdict", "") or "") or None,
                "cost_usd": round(float(getattr(usage, "estimated_cost_usd", 0.0)), 4),
            }
        )
        return turn, usage


# ---------------------------------------------------------------- rendering


def transcript_of(store: DreamStore, dream_id: int) -> list[dict[str, Any]]:
    return [
        {
            "at": m.at.isoformat(),
            "speaker": m.speaker,
            "kind": m.kind,
            "chars": len(m.text),
            "at_or_under_text_max": len(m.text) <= TEXT_MAX_CHARS,
            "text": m.text,
        }
        for m in store.messages(dream_id)
    ]


def counters(store: DreamStore, dream_id: int) -> dict[str, Any]:
    """The three numbers every cap in this module is computed from.

    Recomputed from the transcript on every call rather than cached, which is
    what `confer.py` itself does — there is no counter column, deliberately.
    """
    dream = store.get(dream_id)
    messages = store.messages(dream_id)
    marker = last_agent_turn_at(messages)
    change = (
        has_something_changed(dream, marker, messages=messages)
        if dream is not None
        else None
    )
    return {
        "lifetime_exchanges": exchanges_so_far(messages),
        "exchanges_in_epoch": exchanges_in_epoch(dream, messages) if dream else None,
        "epoch_started_at": epoch_started_at(dream, messages).isoformat() if dream else None,
        "last_agent_turn_at": marker.isoformat() if marker else None,
        "vault": str(dream.vault) if dream else None,
        "change_gate_open": bool(change) if change is not None else None,
        "change_gate_summary": change.summary if change is not None else None,
    }


def exchange_row(store: DreamStore, result: ExchangeResult) -> dict[str, Any]:
    return {
        "dream_id": result.dream_id,
        "outcome": str(result.outcome),
        "turns": result.turns,
        "detail": result.detail,
        "calls": len(result.usage),
        "cost_usd": round(result.cost_usd, 4),
        "cost_a_model_call": result.conferred,
        "counters_after": counters(store, result.dream_id),
    }


def report_row(store: DreamStore, report: ConferenceReport) -> dict[str, Any]:
    return {
        "considered": report.considered,
        "conferred": report.conferred,
        "skipped": report.skipped,
        "adopted": report.adopted,
        "calls": report.calls,
        "cost_usd": round(report.cost_usd, 4),
        "within_max_dreams_per_run": report.conferred <= MAX_DREAMS_PER_RUN,
        "exchanges": [exchange_row(store, e) for e in report.exchanges],
    }


def dream_row(dream: Dream) -> dict[str, Any]:
    return {
        "id": dream.id,
        "title": dream.title,
        "vault": str(dream.vault),
        "verdict": str(dream.verdict) if dream.verdict else None,
        "verification": str(dream.verification),
        "weakest_hop": dream.weakest_hop,
        "symbols": list(dream.symbols),
        "asset_class_key": dream.asset_class_key,
        "hops": len(dream.chain),
        "hops_checked": sum(1 for h in dream.chain if h.checked),
        "conditions": len(dream.conditions),
        "conditions_met": dream.conditions_met,
    }


# --------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dreams", required=True, help="A dreams.db to work from.")
    parser.add_argument("--work", default="")
    parser.add_argument("--out", default="")
    parser.add_argument(
        "--lifetime",
        action="store_true",
        help="Also drive one dream to MAX_EXCHANGES_LIFETIME. Expensive.",
    )
    args = parser.parse_args()

    key = os.environ.get("ANTHROPIC_API_KEY_ELECTRUM") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise SystemExit("No Anthropic key: set ANTHROPIC_API_KEY_ELECTRUM.")
    os.environ["ANTHROPIC_API_KEY"] = key

    tripwire = Tripwire()
    tripwire.install()

    work = Path(args.work or "/tmp/mudhorn-confer-live").resolve()
    work.mkdir(parents=True, exist_ok=True)
    os.chdir(work)

    db = work / "dreams.db"
    shutil.copy2(Path(args.dreams), db)

    env = Env()
    rules = Rules.load(REPO / "config" / "rules.yaml")
    store = DreamStore(db)
    caps = rules.dreaming.vault_caps()
    powers = TraderPowers(store)

    import bot

    record: dict[str, Any] = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "repo": str(REPO),
        "bot_package": bot.__file__,
        "head": (REPO / "HEAD.txt").read_text().strip()
        if (REPO / "HEAD.txt").exists()
        else "unknown",
        "work_dir": str(work),
        "dreams_db_from": str(Path(args.dreams)),
        "model": CLAUDE_MODEL_IDS[env.dream_tier],
        "caps_as_shipped": {
            "MAX_TURNS_PER_EXCHANGE": MAX_TURNS_PER_EXCHANGE,
            "MAX_DREAMS_PER_RUN": MAX_DREAMS_PER_RUN,
            "MAX_EXCHANGES_PER_EPOCH": MAX_EXCHANGES_PER_EPOCH,
            "MAX_EXCHANGES_LIFETIME": MAX_EXCHANGES_LIFETIME,
            "TEXT_MAX_CHARS": TEXT_MAX_CHARS,
            "vault_caps": {
                "vault": caps.vault,
                "adopted": caps.adopted,
            },
        },
        "trader_powers_public_methods": sorted(
            n for n in dir(TraderPowers) if not n.startswith("_")
        ),
        "phases": [],
    }

    # ------------------------------------------------------ phase 0: setup
    #
    # Whatever the dream cycle actually promoted, plus — only if that is fewer
    # than three — real dreams moved onto the shelf by the DREAMER, which is one
    # of its own powers. Recorded as harness setup rather than as a promotion,
    # because the promotion rule held these back for reasons the dream-cycle
    # transcript quotes.

    staged: list[dict[str, Any]] = []
    in_vault = store.in_vault(Vault.VAULT)
    if len(in_vault) < 3:
        for dream in store.recent(limit=50):
            if len(store.in_vault(Vault.VAULT)) >= 3:
                break
            if dream.vault is Vault.VAULT or dream.id is None:
                continue
            moved = store.move(
                int(dream.id),
                Vault.VAULT,
                by=DREAMER,
                reason="Staged onto the vault by the audit harness so the conference "
                "has candidates. The promotion rule's own verdict is in the dream-cycle "
                "transcript.",
                caps=caps,
            )
            staged.append(
                {
                    "dream_id": dream.id,
                    "from": str(dream.vault),
                    "ok": moved.ok,
                    "detail": moved.detail,
                    "refusals": [str(r) for r in moved.refusals],
                }
            )
    record["phases"].append(
        {
            "phase": "0-setup",
            "vault_before": [dream_row(d) for d in in_vault],
            "staged_by_the_harness": staged,
            "vault_after": [dream_row(d) for d in store.in_vault(Vault.VAULT)],
        }
    )

    conference = Conference(env, rules, store)
    dreamer_calls = RecordingCaller(conference._dreamer, "dreamer")
    trader_calls = RecordingCaller(conference._trader, "trader")
    conference._dreamer = dreamer_calls
    conference._trader = trader_calls

    # -------------------------------------------- phase 1: one real run
    print("[phase 1] run_conference ...", flush=True)
    now = datetime.now(UTC)
    report = run_conference(
        env,
        rules,
        store,
        now=now,
        caps=caps,
        dreamer_client=dreamer_calls,
        trader_client=trader_calls,
    )
    phase1 = report_row(store, report)
    phase1["phase"] = "1-real-run"
    phase1["transcripts"] = {
        str(e.dream_id): transcript_of(store, e.dream_id) for e in report.exchanges
    }
    phase1["turn_cap_respected"] = all(
        e.turns <= MAX_TURNS_PER_EXCHANGE for e in report.exchanges
    )
    phase1["exchanges_that_decided_nothing_are_stored"] = [
        {
            "dream_id": e.dream_id,
            "outcome": str(e.outcome),
            "messages_on_file": len(store.messages(e.dream_id)),
        }
        for e in report.exchanges
        if str(e.outcome) in {"declined", "parked", "turns_exhausted", "adoption_refused"}
    ]
    record["phases"].append(phase1)
    print(f"    -> {[str(e.outcome) for e in report.exchanges]}", flush=True)

    # ------------------------------ phase 2: the change gate, seconds later
    print("[phase 2] the change gate ...", flush=True)
    again = run_conference(
        env,
        rules,
        store,
        now=now + timedelta(seconds=30),
        caps=caps,
        dreamer_client=dreamer_calls,
        trader_client=trader_calls,
    )
    phase2 = report_row(store, again)
    phase2["phase"] = "2-change-gate"
    phase2["note"] = (
        "Same shelf, thirty seconds later. Anything conferred in phase 1 should "
        "be skipped for free; anything NOT conferred in phase 1 was never "
        "conferred, so its gate is still open and it costs a real exchange."
    )
    record["phases"].append(phase2)
    print(f"    -> {[str(e.outcome) for e in again.exchanges]}", flush=True)

    # --------------------------------------------------- phase 3: the epoch
    #
    # `confer_once` bypasses `_consider`, which is what makes this reachable at
    # all: every event that opens the change gate also moves `epoch_started_at`,
    # so three exchanges inside one epoch is a state `run()` may not be able to
    # produce on its own. That is recorded as a finding rather than worked
    # around — the arithmetic after every exchange is in `counters`.

    target = next(
        (d for d in store.in_vault(Vault.VAULT) if d.id is not None), None
    )
    if target is None:
        record["phases"].append({"phase": "3-epoch", "skipped": "nothing left in the vault"})
    else:
        dream_id = int(target.id or 0)
        steps: list[dict[str, Any]] = [{"before": counters(store, dream_id)}]
        stamp = now + timedelta(minutes=1)
        while True:
            current = store.get(dream_id)
            if current is None:
                break
            if current.vault is not Vault.VAULT:
                # An accept takes the dream off the shelf. Hand it back so the
                # epoch walk can continue, and record that it happened rather
                # than quietly re-running.
                back = powers.hand_back(
                    dream_id,
                    reason="Audit harness: returned so the epoch walk continues.",
                    at=stamp,
                )
                steps.append({"handed_back_mid_walk": back.ok, "detail": back.detail})
                stamp += timedelta(minutes=1)
                current = store.get(dream_id)
                if current is None:
                    break
            held = exchanges_in_epoch(current, store.messages(dream_id))
            if held >= MAX_EXCHANGES_PER_EPOCH:
                break
            print(f"[phase 3] confer_once (epoch holds {held}) ...", flush=True)
            result = conference.confer_once(current, now=stamp, caps=caps)
            steps.append(
                {
                    "driven_by": "Conference.confer_once",
                    **exchange_row(store, result),
                }
            )
            stamp += timedelta(minutes=1)
            if len(steps) > MAX_EXCHANGES_PER_EPOCH + 2:
                break

        print("[phase 3] run() again, expecting a refusal ...", flush=True)
        refused = run_conference(
            env,
            rules,
            store,
            now=stamp,
            caps=caps,
            dreamer_client=dreamer_calls,
            trader_client=trader_calls,
        )
        record["phases"].append(
            {
                "phase": "3-epoch",
                "dream_id": dream_id,
                "exchanges": steps,
                "run_after_spending_the_epoch": report_row(store, refused),
                "refusal_for_this_dream": next(
                    (
                        exchange_row(store, e)
                        for e in refused.exchanges
                        if e.dream_id == dream_id
                    ),
                    None,
                ),
                "transcript": transcript_of(store, dream_id),
            }
        )

        # ------------------------------------------- phase 4: the hand-back
        print("[phase 4] adopt, hand back, confer again ...", flush=True)
        before = counters(store, dream_id)
        adopted = powers.adopt(
            dream_id,
            at=stamp + timedelta(minutes=1),
            caps=caps,
            ttl_days=rules.dreaming.ttl_days.adopted,
        )
        handed_back = None
        if adopted.ok:
            handed_back = powers.hand_back(
                dream_id,
                reason="Audit harness: taken and handed straight back, to show that a "
                "hand-back opens a fresh epoch and that the lifetime count does not "
                "reset with it.",
                at=stamp + timedelta(minutes=2),
            )
        after_handback = counters(store, dream_id)
        fresh = run_conference(
            env,
            rules,
            store,
            now=stamp + timedelta(minutes=3),
            caps=caps,
            dreamer_client=dreamer_calls,
            trader_client=trader_calls,
        )
        record["phases"].append(
            {
                "phase": "4-hand-back-resets-the-epoch",
                "dream_id": dream_id,
                "counters_before_adoption": before,
                "adopt": {
                    "ok": adopted.ok,
                    "detail": adopted.detail,
                    "refusals": [str(r) for r in adopted.refusals],
                },
                "hand_back": (
                    {
                        "ok": handed_back.ok,
                        "detail": handed_back.detail,
                        "refusals": [str(r) for r in handed_back.refusals],
                    }
                    if handed_back
                    else None
                ),
                "counters_after_hand_back": after_handback,
                "run_after_hand_back": report_row(store, fresh),
                "lifetime_did_not_reset": (
                    after_handback["lifetime_exchanges"] >= before["lifetime_exchanges"]
                ),
            }
        )

        # ------------------------------------ phase 5: the lifetime ceiling
        if args.lifetime:
            print("[phase 5] driving to the lifetime ceiling ...", flush=True)
            stamp += timedelta(minutes=10)
            walk: list[dict[str, Any]] = []
            while True:
                current = store.get(dream_id)
                if current is None:
                    break
                lifetime = exchanges_so_far(store.messages(dream_id))
                if lifetime >= MAX_EXCHANGES_LIFETIME:
                    break
                print(f"    exchange {lifetime + 1}/{MAX_EXCHANGES_LIFETIME} ...", flush=True)
                result = conference.confer_once(current, now=stamp, caps=caps)
                walk.append(exchange_row(store, result))
                stamp += timedelta(minutes=1)
                if current.vault is not Vault.VAULT:
                    # An adoption takes the dream off the shelf; hand it back so
                    # the walk can continue. Recorded rather than silent.
                    powers.hand_back(
                        dream_id,
                        reason="Audit harness: returned so the lifetime walk continues.",
                        at=stamp,
                    )
                    stamp += timedelta(minutes=1)
            ceiling = run_conference(
                env,
                rules,
                store,
                now=stamp,
                caps=caps,
                dreamer_client=dreamer_calls,
                trader_client=trader_calls,
            )
            record["phases"].append(
                {
                    "phase": "5-lifetime-ceiling",
                    "dream_id": dream_id,
                    "exchanges": walk,
                    "counters": counters(store, dream_id),
                    "run_at_the_ceiling": report_row(store, ceiling),
                }
            )

    record["model_calls"] = dreamer_calls.calls + trader_calls.calls
    record["text_max_chars"] = {
        "cap": TEXT_MAX_CHARS,
        "calls_measured": len(dreamer_calls.calls) + len(trader_calls.calls),
        "turns_over_the_cap_before_trimming": sum(
            1 for c in dreamer_calls.calls + trader_calls.calls if c["over_text_max_chars"]
        ),
        "longest_raw_turn_chars": max(
            (c["raw_text_chars"] for c in dreamer_calls.calls + trader_calls.calls),
            default=0,
        ),
    }
    record["vault_counts"] = {str(v): c for v, c in store.counts_by_vault().items()}
    record["adoptions"] = [
        {
            "dream_id": a.dream_id,
            "symbols": list(a.symbols_granted),
            "asset_class": a.asset_class,
            "adopted_at": a.adopted_at.isoformat(),
            "returned_at": a.returned_at.isoformat() if a.returned_at else None,
            "return_reason": a.return_reason,
            "live_now": a.is_live(datetime.now(UTC)),
        }
        for a in store.adoptions()
    ]
    record["tripwire"] = tripwire.report()
    # `OPERATOR` is imported so the record can name the speaker the change gate
    # treats as a new voice, without a second spelling of the string.
    record["change_gate_new_voice_speaker"] = OPERATOR

    out = Path(args.out) if args.out else work / "confer_live.json"
    if not out.is_absolute():
        out = REPO / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
