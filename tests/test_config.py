from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from bot.config import (
    CLAUDE_MODEL_IDS,
    CLAUDE_MODEL_SPECS,
    CLAUDE_PRICING_USD_PER_MTOK,
    MODEL_SPECS,
    AccountRules,
    ClaudeTier,
    Env,
    InstrumentRules,
    LiveTradingRefused,
    ModelSpec,
    Rules,
    resolve_model_spec,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_rules_load_from_yaml():
    rules = Rules.load(REPO_ROOT / "config" / "rules.yaml")
    assert rules.account.max_concurrent_positions > 0
    assert rules.allowed_symbols, "allowed_symbols must not be empty"
    assert rules.instruments["crypto"].enabled is False, "crypto must default OFF"
    assert rules.frequency.max_trades_per_day > 0
    assert rules.margin.max_gross_notional_pct > 0, "margin guards must ship configured"


def test_claude_model_ids_and_pricing_complete():
    for tier in ClaudeTier:
        assert tier in CLAUDE_MODEL_IDS
        assert CLAUDE_MODEL_IDS[tier].startswith("claude-")
        assert tier in CLAUDE_PRICING_USD_PER_MTOK
        base_in, out, cache_read = CLAUDE_PRICING_USD_PER_MTOK[tier]
        assert 0 < base_in < out
        # Cache reads bill at 10% of base input.
        assert cache_read == pytest.approx(base_in * 0.1)


# ------------------------------------------------------- naming a model at all
#
# "Which model" used to be welded to a three-value Claude enum: `CLAUDE_MODEL_IDS`
# for the id, `CLAUDE_PRICING_USD_PER_MTOK` for the price, and an
# `if tier in (SONNET, OPUS)` in the transport for the optional request fields.
# `ModelSpec` carries all three, and the tiers are instances of it rather than
# the only possibility.


def test_the_old_tables_are_derived_from_the_specs_rather_than_written_twice():
    """Two dicts naming the same three models is two places to disagree.

    They are kept because `scripts/` reads them to label a live run, so they
    could not simply go — but nothing writes them out by hand any more, and this
    is what says so. A spec whose price changed while a table did not would be a
    figure nobody could read off one file.
    """
    for tier, spec in CLAUDE_MODEL_SPECS.items():
        assert CLAUDE_MODEL_IDS[tier] == spec.model_id
        assert spec.pricing is not None
        assert CLAUDE_PRICING_USD_PER_MTOK[tier] == spec.pricing.as_tuple()
        # And the registry keyed by wire id agrees with the one keyed by tier.
        assert MODEL_SPECS[spec.model_id] is spec


def test_the_claude_tiers_keep_exactly_the_request_fields_they_always_had():
    """The decoupling must not change a single byte of an existing request.

    `thinking` and `output_config.effort` went out for Sonnet and Opus and not
    for Haiku, because Haiku has no extended thinking at all. That test lived
    inline in the transport as `if self._tier in (SONNET, OPUS)`; it lives on
    the spec now, and this is the pin that the move was a move and not a change.
    """
    haiku = CLAUDE_MODEL_SPECS[ClaudeTier.HAIKU]
    assert haiku.sends_anthropic_thinking is False
    assert haiku.sends_anthropic_effort is False

    for tier in (ClaudeTier.SONNET, ClaudeTier.OPUS):
        spec = CLAUDE_MODEL_SPECS[tier]
        assert spec.sends_anthropic_thinking is True
        assert spec.sends_anthropic_effort is True


def test_an_unnamed_model_still_resolves_to_the_tier():
    """Empty means the tier, which is today's behaviour exactly.

    The same shape as `DO_INFERENCE_KEY`: the empty value is the supported
    default, so rollback is unsetting a variable rather than editing code. An
    existing deployment on `CLAUDE_TIER=sonnet` must be untouched by all of
    this.
    """
    for tier in ClaudeTier:
        assert resolve_model_spec("", tier) is CLAUDE_MODEL_SPECS[tier]
        # Whitespace is not a name either.
        assert resolve_model_spec("   ", tier) is CLAUDE_MODEL_SPECS[tier]


def test_a_model_nobody_has_priced_is_nameable_and_reports_no_price():
    """The whole point, and the trap inside it.

    A model this repository has never seen is a fully supported thing to name —
    that is what the work was for. What it must NOT come with is a price, and
    `ModelPricing` has no default anywhere for exactly that reason: an invented
    cost is the same class of error as an invented indicator, and it would reach
    the Settings page looking like a measurement.

    It must also be sent NEITHER Anthropic-only request field. `thinking` and
    `output_config` have an Anthropic shape; DigitalOcean's schema lists a flat
    `reasoning_effort` and no `output_config` at all, so sending them would be
    wrong in two ways at once. Both False is a plain request rather than a wrong
    one.
    """
    spec = resolve_model_spec("glm-5.2", ClaudeTier.SONNET)

    assert spec.model_id == "glm-5.2"
    assert spec.pricing is None
    assert spec.price_is_known is False
    assert spec.sends_anthropic_thinking is False
    assert spec.sends_anthropic_effort is False


def test_naming_a_known_model_brings_its_prices_with_it():
    """A model id that IS in the registry is not a stranger.

    Naming `claude-sonnet-5` outright has to be the same thing as reaching it
    through the tier — otherwise the honest-pricing rule would punish the
    operator for spelling out what they already had.
    """
    spec = resolve_model_spec("claude-sonnet-5", ClaudeTier.HAIKU)

    assert spec is CLAUDE_MODEL_SPECS[ClaudeTier.SONNET]
    assert spec.price_is_known is True


def test_the_environment_carries_no_price_for_a_model():
    """Prices reach `MODEL_SPECS` in a commit, never through `.env`.

    Same rule as a limit reaching `config/rules.yaml` rather than being typed
    somewhere at runtime: a cost with no review and no history behind it is
    indistinguishable, on the page that renders it, from one somebody measured.
    An unpriced model is fully usable and simply reports an unknown cost.
    """
    priced = [
        name
        for name in Env.model_fields
        if any(word in name for word in ("price", "usd_per", "cost"))
    ]
    assert priced == [], (
        f"{priced} would let a cost be typed into the environment. An invented "
        "price renders exactly like a measured one."
    )


def test_the_two_model_ids_are_separately_settable_and_default_to_the_tiers():
    """The loop wakes 96 times a day and the dreamer once.

    The model that is right for one is not automatically right for the other,
    which is why `DREAM_CLAUDE_TIER` already existed. The id overrides follow
    the same split, and neither one set is the shipped default.
    """
    env = Env(_env_file=None)  # type: ignore[call-arg]

    assert env.decision_spec is CLAUDE_MODEL_SPECS[ClaudeTier.HAIKU]
    assert env.dream_spec is CLAUDE_MODEL_SPECS[ClaudeTier.SONNET]

    env.decision_model_id = "some-open-model"
    assert env.decision_spec == ModelSpec(model_id="some-open-model")
    # And the dreamer is untouched by it, exactly as its tier is.
    assert env.dream_spec is CLAUDE_MODEL_SPECS[ClaudeTier.SONNET]

    env.dream_model_id = "another-open-model"
    assert env.dream_spec == ModelSpec(model_id="another-open-model")


def _account_rules(**overrides: Any) -> AccountRules:
    base: dict[str, Any] = {
        "min_equity_floor_usd": 90_000,
        "max_risk_per_trade_pct": 1.0,
        "max_position_pct": 50.0,
        "max_total_risk_pct": 2.0,
        "max_concurrent_positions": 1,
        "daily_loss_kill_pct": 1,
    }
    base.update(overrides)
    return AccountRules(**base)


def test_invalid_max_risk_pct_rejected():
    with pytest.raises(ValueError):
        _account_rules(max_risk_per_trade_pct=0)


def test_total_risk_below_per_trade_risk_rejected():
    """A total cap under the per-trade cap would block every possible trade."""
    with pytest.raises(ValueError, match="max_risk_per_trade_pct"):
        _account_rules(max_risk_per_trade_pct=2.0, max_total_risk_pct=1.0)


def test_stand_down_stage_two_must_exceed_stage_one():
    from bot.config import StandDownRules

    with pytest.raises(ValueError, match="stage_one_days"):
        StandDownRules(stage_one_days=10, stage_two_days=3)


def test_execution_mode_follows_paper_flag():
    from bot.models import ExecutionMode

    assert _env().execution_mode == ExecutionMode.PAPER
    assert _env(ALPACA_PAPER_TRADE=False).execution_mode == ExecutionMode.LIVE


# --------------------------------------------------------- paper-only guard


def _env(**overrides: Any) -> Env:
    """Build an Env ignoring any developer .env file, so these tests are hermetic."""
    return Env(_env_file=None, **overrides)  # type: ignore[call-arg]


# ------------------------------------------------------ which endpoint answers


@pytest.fixture
def no_do_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_env_file=None` silences the file, not the process environment.

    A developer with DO_INFERENCE_KEY exported would otherwise get a different
    answer from these tests than CI does, which is the one thing a test about
    "what is configured" must not do.

    `ANTHROPIC_BASE_URL` is cleared for the same reason and is the one most
    likely to be set: it is the Anthropic SDK's own switch, `scripts/` export it
    to run a probe, and it now takes part in this answer rather than being
    invisible to it.
    """
    monkeypatch.delenv("DO_INFERENCE_KEY", raising=False)
    monkeypatch.delenv("DO_INFERENCE_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)


def test_the_inference_fields_default_to_empty_and_empty_means_anthropic(no_do_key):
    """The switch is one variable and its empty value is today's behaviour.

    Same shape as DASHBOARD_CHAT_TOKEN and X_BEARER_TOKEN: a deployment that
    never heard of this feature is a supported configuration, and rollback is
    unsetting a variable rather than editing code.
    """
    from bot.config import ANTHROPIC

    env = _env()

    assert env.do_inference_key == ""
    assert env.do_inference_base_url == ""

    provider = env.inference_provider
    assert provider.name == ANTHROPIC
    assert provider.usable is True
    assert not provider.is_digitalocean
    assert "Anthropic" in provider.detail


def test_a_key_alone_selects_digitalocean_at_the_documented_endpoint(no_do_key):
    """One variable, not two. The base URL exists in exactly one place."""
    from bot.config import DO_INFERENCE_DEFAULT_BASE_URL

    provider = _env(DO_INFERENCE_KEY="do-model-access-key").inference_provider

    assert provider.is_digitalocean
    assert provider.usable is True
    assert provider.base_url == DO_INFERENCE_DEFAULT_BASE_URL


def test_a_base_url_with_no_key_is_reported_as_moving_nothing(no_do_key):
    """Half a configuration must not read as a working one.

    The key is the switch. An operator who set the endpoint and forgot the key
    would otherwise believe the swap had happened, with nothing afterwards to
    say it had not — which is the failure this whole arrangement exists to
    refuse, arriving through a typo instead of through a model.
    """
    from bot.config import ANTHROPIC

    provider = _env(DO_INFERENCE_BASE_URL="https://inference.do-ai.run").inference_provider

    assert provider.name == ANTHROPIC
    assert provider.usable is False
    assert "nothing has moved" in provider.detail


def test_an_unusable_endpoint_is_not_a_quiet_fall_back_to_anthropic(no_do_key):
    """A key that cannot be used must not report as "Anthropic, all fine".

    Three states rather than two. The third one is what lets a caller refuse
    loudly instead of proceeding on a provider the operator did not choose, and
    the sentence says so in as many words so nobody has to infer it.
    """
    provider = _env(
        DO_INFERENCE_KEY="do-model-access-key",
        DO_INFERENCE_BASE_URL="http://inference.do-ai.run",
    ).inference_provider

    assert provider.is_digitalocean
    assert provider.usable is False
    assert "NOT a fall back to Anthropic" in provider.detail


def test_the_banner_never_claims_a_switch_that_has_not_been_thrown(no_do_key):
    """Phase 1 moves the souls on Hermes and NO Python model call.

    So a key in `.env` today is a declaration, not a route, and the line a
    startup banner prints has to say that. This test is the thing that makes
    `PYTHON_MODEL_PATH_USES_DO` a checkable fact rather than a remembered one:
    flipping it without moving a call site — or moving a call site without
    flipping it — fails here.
    """
    from bot.config import PYTHON_MODEL_PATH_USES_DO

    detail = _env(DO_INFERENCE_KEY="do-model-access-key").inference_provider.detail

    if PYTHON_MODEL_PATH_USES_DO:
        assert "NOT IN FORCE" not in detail
    else:
        assert "NOT IN FORCE" in detail
        assert "Hermes" in detail


def test_the_provider_sentence_never_contains_the_key(no_do_key):
    """Credentials are reported as configured or not configured, never rendered.

    `detail` is printed at startup, into a journal somebody may paste, so it
    inherits the Settings page's rule: no value, no prefix, no length.
    """
    secret = "do-v1-super-secret-model-access-key"

    for env in (
        _env(DO_INFERENCE_KEY=secret),
        _env(DO_INFERENCE_KEY=secret, DO_INFERENCE_BASE_URL="http://nope"),
    ):
        assert secret not in env.inference_provider.detail


def test_a_misconfigured_endpoint_does_not_stop_the_process_starting(no_do_key):
    """It reports; it does not raise.

    Refusing to boot over a variable no Python path reads yet would be a denial
    at the least useful moment, and the thing denied would be the trading loop.
    The friction belongs where the setting is USED — the Hermes wrappers, which
    refuse the turn and cost a chat message instead of a session.
    """
    env = _env(DO_INFERENCE_KEY="k", DO_INFERENCE_BASE_URL="not-a-url")

    assert env.inference_provider.usable is False  # no exception on the way here


# ------------------------------------------ the wrappers that actually switch
#
# The souls' endpoint is chosen in `deploy/run-chat.sh` and `deploy/run-dream.sh`
# rather than in Python, so these RUN the scripts against a stub Hermes. Reading
# them for a substring would pin the words and not the behaviour, which is the
# `str()`-on-an-enum trap in a different costume: the shape can look right while
# every branch misses.


def _stub_hermes(tmp_path: Path) -> Path:
    """A Hermes that reports the environment it was handed and nothing else."""
    binary = tmp_path / "hermes"
    binary.write_text(
        "#!/usr/bin/env bash\n"
        'echo "base=${ANTHROPIC_BASE_URL:-none} '
        'model=${ANTHROPIC_MODEL:-none} key=${ANTHROPIC_API_KEY:-none}"\n',
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary


def _run_wrapper(script: str, home: Path, tmp_path: Path) -> Any:
    import subprocess

    home.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HERMES_BIN": str(_stub_hermes(tmp_path)),
        # Each wrapper names its own home differently, so both are supplied and
        # each script reads the one it cares about.
        "HERMES_HOME": str(home),
        "HERMES_DREAM_HOME": str(home),
    }
    return subprocess.run(
        ["bash", str(REPO_ROOT / "deploy" / script)],
        input="hello",
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )


WRAPPERS = ["run-chat.sh", "run-dream.sh"]


def _hermes_config(home: Path) -> Path:
    """Where Hermes ACTUALLY keeps its config: `$HERMES_HOME/.hermes/config.yaml`.

    **These tests wrote `$HERMES_HOME/config.yaml` and passed, while the wrapper
    read the same wrong path and silently skipped its check on the real box.**
    The test was written from the code rather than from the deployment, so both
    agreed with each other and neither agreed with Hermes. Observed live:
    `inference.env` was deliberately set to a different model from the config,
    and the turn ran anyway under a banner claiming the model had been checked.

    A helper rather than a literal in four places, so the path is stated once
    and a future move is one edit rather than four that can disagree.
    """
    directory = home / ".hermes"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "config.yaml"



@pytest.mark.parametrize("script", WRAPPERS)
def test_no_inference_file_means_anthropic_and_a_working_turn(script, tmp_path):
    """The default path, and the one every existing deployment is on."""
    done = _run_wrapper(script, tmp_path / "home", tmp_path)

    assert done.returncode == 0, done.stderr
    assert "base=none" in done.stdout, "an endpoint leaked into a turn nobody configured"
    assert "Anthropic direct" in done.stderr


@pytest.mark.parametrize("script", WRAPPERS)
def test_a_key_with_no_model_refuses_the_turn_rather_than_using_anthropic(script, tmp_path):
    """The failure this arrangement exists to prevent, in its likeliest form.

    The serving slug is not the Anthropic model id and is deliberately not
    guessed, so a key on its own is half a switch. Falling through to Anthropic
    here would answer the operator normally while they believed the souls had
    moved — a confident partial answer arriving through the plumbing.
    """
    home = tmp_path / "home"
    home.mkdir()
    (home / "inference.env").write_text("DO_INFERENCE_KEY=fake\n", encoding="utf-8")

    done = _run_wrapper(script, home, tmp_path)

    assert done.returncode != 0
    assert "DO_INFERENCE_MODEL" in done.stderr
    assert done.stdout.strip() == "", "the turn ran anyway"


@pytest.mark.parametrize("script", WRAPPERS)
def test_a_router_is_refused_because_nothing_here_could_see_the_downgrade(script, tmp_path):
    """A router falls back to another model on rate limit, and `hermes -z`
    returns the response text and nothing else — so which model answered is not
    observable from this box at all. Removing the mechanism is the only honest
    answer when the observation is impossible."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "inference.env").write_text(
        "DO_INFERENCE_KEY=fake\nDO_INFERENCE_MODEL=my-router-fast\n", encoding="utf-8"
    )

    done = _run_wrapper(script, home, tmp_path)

    assert done.returncode != 0
    assert "router" in done.stderr
    assert done.stdout.strip() == ""


@pytest.mark.parametrize("script", WRAPPERS)
def test_a_configured_switch_replaces_the_anthropic_credential(script, tmp_path):
    """Not merely adds an endpoint beside it.

    Whether Hermes honours ANTHROPIC_BASE_URL is unverified. If it does not, the
    DigitalOcean key reaches Anthropic and is refused with a 401 — loud, and the
    right direction. A working Anthropic key left beside a DigitalOcean base URL
    is the arrangement that could serve the turn from the old provider while the
    operator believed it had moved.
    """
    home = tmp_path / "home"
    home.mkdir()
    (home / "inference.env").write_text(
        "DO_INFERENCE_KEY=fake-do-key\nDO_INFERENCE_MODEL=some-slug\n", encoding="utf-8"
    )
    # The config has to agree, or the mismatch check below refuses first and
    # this test would pass for the wrong reason.
    _hermes_config(home).write_text("model:\n  default: some-slug\n", encoding="utf-8")

    done = _run_wrapper(script, home, tmp_path)

    assert done.returncode == 0, done.stderr
    assert "base=https://inference.do-ai.run" in done.stdout
    assert "model=some-slug" in done.stdout
    assert "key=fake-do-key" in done.stdout
    # It never claims the swap HAPPENED. The wrapper cannot see which model
    # answered, and a line saying otherwise would be the overclaim this
    # repository keeps catching. The endpoint and the configured model ARE both
    # checked now, so the banner says that much and stops exactly there.
    assert "which model ANSWERED is still not visible" in done.stderr


@pytest.mark.parametrize("script", WRAPPERS)
def test_a_model_the_wrapper_cannot_set_is_refused_rather_than_announced(script, tmp_path):
    """**The rule that rejects, and it is here because it happened.**

    `ANTHROPIC_MODEL` does not select the model. Hermes reads `model.default`
    from its own `config.yaml`. Observed live on 12 Aug 2026: `inference.env`
    named `llama-4-maverick`, the wrapper printed `model llama-4-maverick`, and
    Hermes asked DigitalOcean for `claude-sonnet-5`.

    That returned 403 only because the other model happened to be tier-gated.
    A slug the account COULD serve would have answered normally, from a model
    nobody chose, under a banner naming a different one — which is the
    confident wrong figure this repository exists to refuse, arriving through
    the deployment rather than through a model.
    """
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "inference.env").write_text(
        "DO_INFERENCE_KEY=fake-do-key\nDO_INFERENCE_MODEL=llama-4-maverick\n",
        encoding="utf-8",
    )
    # The shape the real file has: a `model:` block, and a DIFFERENT nested
    # `model:` further down that the reader must not pick up instead.
    _hermes_config(home).write_text(
        "model:\n  default: claude-sonnet-5\n  provider: anthropic\n"
        "speech:\n  model: whisper-1\n",
        encoding="utf-8",
    )

    done = _run_wrapper(script, home, tmp_path)

    assert done.returncode == 78, done.stdout
    assert "Model mismatch" in done.stderr
    assert "claude-sonnet-5" in done.stderr
    assert "llama-4-maverick" in done.stderr
    # Refused, so no turn was taken at all — not taken against the wrong model.
    assert "base=" not in done.stdout


@pytest.mark.parametrize("script", WRAPPERS)
def test_a_matching_model_is_allowed_through(script, tmp_path):
    """The other half, or the check above would pass by refusing everything."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "inference.env").write_text(
        "DO_INFERENCE_KEY=fake-do-key\nDO_INFERENCE_MODEL=llama-4-maverick\n",
        encoding="utf-8",
    )
    _hermes_config(home).write_text(
        "model:\n  default: llama-4-maverick\n  provider: anthropic\n"
        "speech:\n  model: whisper-1\n",
        encoding="utf-8",
    )

    done = _run_wrapper(script, home, tmp_path)

    assert done.returncode == 0, done.stderr
    assert "model=llama-4-maverick" in done.stdout


@pytest.mark.parametrize("script", WRAPPERS)
def test_a_config_with_no_model_default_refuses(script, tmp_path):
    """Unknown is not the same as matching, so it must not read as agreement.

    A config carrying no `model.default` means the model in force cannot be
    established from here. Proceeding would print a slug out of `inference.env`
    with nothing behind it — the same overclaim, one step quieter.
    """
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "inference.env").write_text(
        "DO_INFERENCE_KEY=fake-do-key\nDO_INFERENCE_MODEL=llama-4-maverick\n",
        encoding="utf-8",
    )
    _hermes_config(home).write_text("speech:\n  model: whisper-1\n", encoding="utf-8")

    done = _run_wrapper(script, home, tmp_path)

    assert done.returncode == 78, done.stdout
    assert "No model.default" in done.stderr


@pytest.mark.parametrize("script", WRAPPERS)
def test_a_config_that_cannot_be_read_refuses_rather_than_skipping(script, tmp_path):
    """**The bug the check itself had, and the reason it is pinned.**

    The first version guarded the whole comparison with `[[ -r $config ]]`, so a
    config it could not find was SKIPPED — and the banner two lines below still
    said the model had been checked. It was looking one level too high
    (`$HERMES_HOME/config.yaml` rather than `$HERMES_HOME/.hermes/config.yaml`),
    so on the real box it skipped every time. Observed live: `inference.env` was
    deliberately pointed at a different model from the config and the turn ran
    anyway, announcing a slug that was not in force.

    That is the disease this whole block exists to cure, caught inside the cure.
    A check that quietly does not run is worse than no check at all, because the
    claim it licenses keeps being printed.
    """
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "inference.env").write_text(
        "DO_INFERENCE_KEY=fake-do-key\nDO_INFERENCE_MODEL=llama-4-maverick\n",
        encoding="utf-8",
    )
    # No config written at all — the state the real box was in.

    done = _run_wrapper(script, home, tmp_path)

    assert done.returncode == 78, done.stdout
    assert ".hermes/config.yaml" in done.stderr, "the path it could not read is not named"
    assert "base=" not in done.stdout, "the turn ran with the model unestablished"
    assert "both checked" not in done.stderr, "it claimed a check it had skipped"


@pytest.mark.parametrize("script", WRAPPERS)
def test_blanking_the_key_leaves_no_endpoint_behind(script, tmp_path):
    """Rollback is one blank line, so it has to be a COMPLETE rollback.

    A base URL surviving the key would send the turn to DigitalOcean with an
    Anthropic credential while the wrapper reported "Anthropic direct".
    """
    home = tmp_path / "home"
    home.mkdir()
    (home / "inference.env").write_text(
        "DO_INFERENCE_KEY=\nDO_INFERENCE_BASE_URL=https://inference.do-ai.run\n",
        encoding="utf-8",
    )

    done = _run_wrapper(script, home, tmp_path)

    assert done.returncode == 0, done.stderr
    assert "base=none" in done.stdout
    assert "Anthropic direct" in done.stderr


@pytest.mark.parametrize("script", WRAPPERS)
def test_the_souls_key_never_comes_from_the_bots_env_file(script, tmp_path):
    """`hermes` cannot read /opt/mudhorn/.env, and that is the user split
    working rather than a problem to route around. Two credentials in two
    accounts is the answer here, not a duplication to tidy away — it is what
    keeps the agent's environment free of anything that reaches the broker.

    Comments are stripped first: both files TALK about that file at length,
    because the reasoning is the thing worth writing down. What must not appear
    is a line that READS it.
    """
    text = (REPO_ROOT / "deploy" / script).read_text(encoding="utf-8")
    code = "\n".join(
        line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
    )

    assert "/opt/mudhorn/.env" not in code
    assert "inference.env" in code, "the wrapper reads no inference settings at all"


def test_paper_mode_is_the_default():
    env = _env()
    assert env.alpaca_paper_trade is True
    env.assert_paper_only()  # must not raise


def test_live_mode_is_refused():
    with pytest.raises(LiveTradingRefused, match="paper-only"):
        _env(ALPACA_PAPER_TRADE=False).assert_paper_only()


def test_base_url_follows_paper_flag():
    assert "paper-api" in _env().alpaca_base_url
    assert "paper-api" not in _env(ALPACA_PAPER_TRADE=False).alpaca_base_url


def test_alpaca_broker_refuses_live_env():
    """Second line of defence: the broker re-checks rather than trusting startup."""
    from bot.broker import AlpacaBroker

    env = _env(ALPACA_PAPER_TRADE=False, ALPACA_API_KEY="k", ALPACA_SECRET_KEY="s")
    with pytest.raises(LiveTradingRefused):
        AlpacaBroker(env)


def test_allowed_symbols_is_derived_from_enabled_classes():
    """Disabled classes contribute nothing, so their symbols are not tradeable."""
    rules = Rules.load(REPO_ROOT / "config" / "rules.yaml")
    assert "SPY" in rules.allowed_symbols
    assert rules.is_symbol_allowed("SPY")
    assert not rules.is_symbol_allowed("BTC/USD")   # crypto class is disabled

    enabled = rules.model_copy(deep=True)
    enabled.instruments["crypto"].enabled = True
    enabled.instruments["crypto"].allowed_symbols = ["BTC/USD"]
    assert enabled.is_symbol_allowed("BTC/USD")
    assert enabled.class_name_for("BTC/USD") == "crypto"
    assert enabled.strategy_for("BTC/USD") == "momentum"


def test_a_per_class_total_risk_cap_is_optional_and_bounded():
    """`None` is "no opinion", not zero — absent means the portfolio total alone.

    The bounds are the same shape as the other per-class limits: a percentage
    that has to be a real one. Zero would be a class that could never trade,
    written as though it were a setting.
    """
    assert InstrumentRules().max_class_total_risk_pct is None

    with pytest.raises(ValueError):
        InstrumentRules(max_class_total_risk_pct=0)
    with pytest.raises(ValueError):
        InstrumentRules(max_class_total_risk_pct=-1.0)
    with pytest.raises(ValueError):
        InstrumentRules(max_class_total_risk_pct=11.0)

    assert InstrumentRules(max_class_total_risk_pct=0.5).max_class_total_risk_pct == 0.5


def test_a_looser_class_total_is_accepted_rather_than_refused_at_load():
    """Same doctrine as `max_risk_per_trade_pct`: it overrides in EITHER direction.

    A validator that refused a class total above the portfolio's 2% would be a
    denial at the least useful moment — boot, with no explanation and no way for
    the operator to say "yes, I mean it". That friction belongs in the settings
    agent. What matters here is that the file and the gate agree, so the value
    is kept exactly as written and is never floored back with a `min`.

    It buys nothing on its own, because `max_total_risk_pct` stays
    portfolio-wide and refuses the trade that would breach it first — but that
    is an interaction to know about, not a reason to reject the config.
    """
    loose = InstrumentRules(max_class_total_risk_pct=5.0)

    assert loose.max_class_total_risk_pct == 5.0


def test_the_shipped_crypto_class_caps_its_total_risk():
    """Guards the operator's rule in the file, not only in the gate."""
    from .conftest import RULES_PATH

    crypto = Rules.load(RULES_PATH).instruments["crypto"]

    assert crypto.max_class_total_risk_pct == 0.5
    # The same figure as the per-trade cap, which is what makes crypto one
    # full-size position at a time. Deliberate rather than a coincidence: it
    # agrees with max_concurrent_positions: 1 instead of overriding it.
    assert crypto.max_risk_per_trade_pct == 0.5
    assert crypto.max_concurrent_positions == 1


def test_the_equity_class_declares_no_total_risk_cap():
    """Absence is the setting. Equities are bounded by the portfolio total."""
    from .conftest import RULES_PATH

    assert Rules.load(RULES_PATH).instruments["us_equity"].max_class_total_risk_pct is None


def test_enabled_instrument_must_have_symbols_and_sessions():
    """An enabled class with no session window could never trade."""
    from bot.config import InstrumentRules

    with pytest.raises(ValueError, match="sessions_utc"):
        InstrumentRules(enabled=True, allowed_symbols=["SPY"], sessions_utc=[])

    with pytest.raises(ValueError, match="allowed_symbols"):
        InstrumentRules(enabled=True, allowed_symbols=[], sessions_utc=[(14, 21)])


def test_enabled_instrument_must_declare_its_trading_days():
    """Required rather than defaulted, because both defaults are wrong somewhere.

    Monday-to-Friday would silently shut crypto at weekends; all seven would
    leave equities tradeable on a Saturday, where Alpaca queues the order to
    Monday's open rather than refusing it. Failing at startup beats either.
    """
    from bot.config import InstrumentRules

    with pytest.raises(ValueError, match="session_days_utc"):
        InstrumentRules(
            enabled=True,
            allowed_symbols=["SPY"],
            sessions_utc=[(14, 21)],
            session_days_utc=[],
        )

    with pytest.raises(ValueError, match="0-6"):
        InstrumentRules(
            enabled=True,
            allowed_symbols=["SPY"],
            sessions_utc=[(14, 21)],
            session_days_utc=[1, 7],
        )


def test_the_open_window_is_derived_from_the_instruments_not_copied():
    """The loop's skip and the gate's rejection must never disagree.

    Both read the same `is_in_session`, so enabling a 24/7 class widens the
    loop's working hours without a second setting being touched. A separate
    copy of the equity hours would drift, and the symptom would be the loop
    skipping a session the gate would have allowed, with nothing to say why.
    """
    from datetime import UTC, datetime

    from bot.config import Rules

    from .conftest import RULES_PATH

    rules = Rules.load(RULES_PATH)
    monday_open = datetime(2026, 5, 4, 15, 0, tzinfo=UTC)
    saturday = datetime(2026, 5, 9, 15, 0, tzinfo=UTC)
    monday_night = datetime(2026, 5, 4, 3, 0, tzinfo=UTC)

    assert rules.classes_in_session(monday_open) == ["us_equity"]
    assert rules.any_class_in_session(monday_open)
    assert not rules.any_class_in_session(saturday)
    assert not rules.any_class_in_session(monday_night)

    # Enabling the 24/7 class makes the account a seven-day operation, and the
    # skip becomes a no-op, without any second setting being edited.
    rules.instruments["crypto"].enabled = True
    rules.instruments["crypto"].allowed_symbols = ["BTC/USD"]

    assert rules.any_class_in_session(saturday)
    assert rules.classes_in_session(saturday) == ["crypto"]


# ------------------------------------------------- per-day session windows


def _utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# CME Globex under US daylight time: Sunday evening open, a daily maintenance
# break, an early Friday close, and Saturday dark. This is the schedule the flat
# form cannot express, and the reason the mapping form exists.
GLOBEX = {
    0: [(0, 21), (22, 24)],
    1: [(0, 21), (22, 24)],
    2: [(0, 21), (22, 24)],
    3: [(0, 21), (22, 24)],
    4: [(0, 21)],
    6: [(22, 24)],
}


def _globex() -> InstrumentRules:
    return InstrumentRules(enabled=True, allowed_symbols=["CL"], sessions_utc=GLOBEX)


@pytest.mark.parametrize(
    ("moment", "open_", "why"),
    [
        (_utc(2026, 5, 10, 21), False, "Sunday afternoon, before the evening open"),
        (_utc(2026, 5, 10, 22, 30), True, "Sunday evening, after the open"),
        (_utc(2026, 5, 4, 10), True, "Monday mid-session"),
        (_utc(2026, 5, 4, 21, 30), False, "inside the daily maintenance break"),
        (_utc(2026, 5, 4, 22, 30), True, "after the break reopens"),
        (_utc(2026, 5, 8, 20), True, "Friday, before the 16:00 CT close"),
        (_utc(2026, 5, 8, 22, 30), False, "Friday night, no reopen"),
        (_utc(2026, 5, 9, 12), False, "Saturday, dark all day"),
    ],
)
def test_a_globex_schedule_is_expressible_and_correct(moment, open_, why):
    """The whole point of the mapping form.

    Sunday is the case that proves it: under the flat form Sunday would inherit
    Monday's hours, and 12:00 on a Sunday would read as open. The gate would
    approve, and the broker would queue the fill into the next session.
    """
    assert _globex().is_in_session(moment) is open_, why


def test_the_trading_days_are_derived_from_the_mapping_keys():
    """Two places naming the trading days is two places to disagree."""
    assert _globex().session_days_utc == [0, 1, 2, 3, 4, 6]


def test_a_day_list_that_contradicts_the_mapping_is_refused():
    """A trading day with no window trades nothing; a window on a shut day never fires.

    Either reads as a bug in the bot rather than a typo in the config.
    """
    from bot.config import InstrumentRules

    with pytest.raises(ValueError, match="does not match"):
        InstrumentRules(
            enabled=True,
            allowed_symbols=["CL"],
            sessions_utc=GLOBEX,
            session_days_utc=[0, 1, 2, 3, 4, 5, 6],  # claims Saturday
        )


def test_a_weekday_listed_with_no_windows_is_refused():
    from bot.config import InstrumentRules

    with pytest.raises(ValueError, match="no windows"):
        InstrumentRules(
            enabled=True,
            allowed_symbols=["CL"],
            sessions_utc={0: [(0, 24)], 5: []},
        )


@pytest.mark.parametrize("window", [(21, 21), (22, 21), (0, 25), (-1, 4)])
def test_an_impossible_window_is_refused(window):
    from bot.config import InstrumentRules

    with pytest.raises(ValueError):
        InstrumentRules(
            enabled=True,
            allowed_symbols=["CL"],
            sessions_utc={0: [window]},
        )


def test_the_flat_form_still_works_and_is_what_the_shipped_config_uses():
    """The mapping must not have made the simple case harder to write."""
    from bot.config import Rules

    from .conftest import RULES_PATH

    equities = Rules.load(RULES_PATH).instruments["us_equity"]

    # 08:00 UTC is 04:00 New York in summer — the operator widened this to
    # cover pre-market and after-hours. The flat form is what is under test
    # here; the hours themselves are a config decision.
    assert equities.windows_by_day[0] == [(8, 24)]
    assert set(equities.windows_by_day) == {0, 1, 2, 3, 4}
    assert equities.render_sessions() == "08:00-24:00"


def test_a_per_day_schedule_renders_per_day():
    """Settings and the system prompt both show this, so it must read sensibly."""
    rendered = _globex().render_sessions()

    assert "Sun 22:00-24:00" in rendered
    assert "Fri 00:00-21:00" in rendered
    assert "Sat" not in rendered


def test_the_skip_defaults_to_on_and_is_not_a_risk_rule():
    """It can stop the model being asked. It can never widen what it may answer."""
    from bot.config import Rules

    from .conftest import RULES_PATH

    rules = Rules.load(RULES_PATH)

    assert rules.loop.skip_model_call_when_all_markets_closed is True
    assert not hasattr(rules.loop, "max_risk_per_trade_pct")


def test_the_shipped_rules_close_the_weekend_for_equities():
    """Guards the config, not just the gate. The rule is only as good as the file."""
    from bot.config import Rules

    from .conftest import RULES_PATH

    equities = Rules.load(RULES_PATH).instruments["us_equity"]

    assert equities.session_days_utc == [0, 1, 2, 3, 4]
    assert 5 not in equities.session_days_utc  # Saturday
    assert 6 not in equities.session_days_utc  # Sunday


# ------------------------------------------------------------- the dreaming block


def test_symbol_grants_default_to_off_in_the_model():
    """The half of the asymmetry that protects a caller who never heard of this.

    A `Rules` built with no `dreaming:` block — an older deployment, a test
    fixture, a config nobody has updated — grants nothing at all. Fails closed,
    the same rule as `FinnhubCalendar.is_degraded` and `stops_unchecked`: an
    unknown is never treated as a permission.
    """
    from bot.config import DreamingRules

    assert DreamingRules().allow_symbol_grants is False


def test_the_shipped_config_turns_symbol_grants_on():
    """The other half. A decision somebody made belongs in a file that can be
    read and reverted in one line, not in a default nobody sees."""
    rules = Rules.load(REPO_ROOT / "config" / "rules.yaml")

    assert rules.dreaming.allow_symbol_grants is True
    assert rules.dreaming.max_granted_symbols > 0


def test_the_configured_vault_numbers_match_the_value_objects():
    """The numbers live in the file and the dataclasses keep their own defaults
    for callers that pass nothing. This is what stops the two drifting into two
    different answers to the same question.

    **It loads the FILE**, which is the point and was the bug. It used to
    compare `DreamingRules()` with `VaultCaps()` — two sets of code defaults,
    which agree with each other by construction and say nothing whatever about
    `config/rules.yaml`. A file setting `caps.adopted: 5` kept this green while
    the enforced number stayed 3, so the anti-drift test could not see the
    drift it exists for.
    """
    from bot.config import DreamingRules
    from bot.dreaming import VaultCaps, VaultTTLs

    shipped = Rules.load(REPO_ROOT / "config" / "rules.yaml").dreaming

    assert shipped.vault_caps() == VaultCaps()
    assert shipped.vault_ttls() == VaultTTLs()
    # And the code defaults still agree, so a caller that passes nothing gets
    # the same answer as one that read the file.
    assert DreamingRules().vault_caps() == VaultCaps()
    assert DreamingRules().vault_ttls() == VaultTTLs()


def test_the_shipped_vault_caps_match_the_position_cap():
    """An adopted dream the account has no slot to trade is a promise it cannot
    keep. The two numbers are related by intent rather than by code, so the
    relationship is asserted rather than left to a comment."""
    rules = Rules.load(REPO_ROOT / "config" / "rules.yaml")

    assert rules.dreaming.caps.adopted == rules.account.max_concurrent_positions
    # The archive is uncapped on purpose: the only alternative to archiving a
    # dream when the archive is full is deleting it.
    assert rules.dreaming.caps.archive is None
    assert rules.dreaming.ttl_days.archive is None


# ------------------------------------------------- which class a symbol is in


def test_true_class_key_reads_a_disabled_blocks_symbol_list_too():
    """`for_symbol` scans ENABLED classes only, which made "is this symbol from
    a class the operator switched off?" unanswerable — at exactly the moment an
    adopted dream claimed one under a different class's key."""
    rules = Rules.load(REPO_ROOT / "config" / "rules.yaml")
    rules.instruments["crypto"].allowed_symbols = ["ETH/USD"]
    assert rules.instruments["crypto"].enabled is False

    assert rules.for_symbol("ETH/USD") is None
    assert rules.true_class_key("ETH/USD") == "crypto"


def test_true_class_key_agrees_with_the_rule_the_broker_routes_on():
    """`AlpacaBroker.place_order` branches on `"/" in symbol`, so a slashed
    symbol is a crypto order — unbracketed, no broker-side stop — whatever any
    config says about it."""
    rules = Rules.load(REPO_ROOT / "config" / "rules.yaml")

    assert rules.true_class_key("BTC/USD") == "crypto"
    assert rules.true_class_key("MP") == "us_equity"
    assert rules.true_class_key("SPY") == "us_equity"


def test_true_class_key_is_empty_when_two_blocks_claim_one_symbol():
    """Where the file disagrees with itself there is no single answer, and the
    callers read `""` as "drop this" rather than as a default."""
    rules = Rules.load(REPO_ROOT / "config" / "rules.yaml")
    rules.instruments["crypto"].allowed_symbols = ["WIDGET"]
    rules.instruments["shelved"] = rules.instruments["crypto"].model_copy(
        update={"allowed_symbols": ["WIDGET"], "enabled": False}
    )

    assert rules.true_class_key("WIDGET") == ""


def test_true_class_key_is_empty_when_the_listing_contradicts_the_routing():
    """A config that filed `BTC/USD` under the equity book would be a class the
    gate applied and a class the broker used, and they would not be the same
    one. Neither answer is safe, so there is no answer."""
    rules = Rules.load(REPO_ROOT / "config" / "rules.yaml")
    rules.instruments["us_equity"].allowed_symbols = [
        *rules.instruments["us_equity"].allowed_symbols,
        "BTC/USD",
    ]

    assert rules.true_class_key("BTC/USD") == ""


def test_true_class_key_is_empty_for_a_symbol_that_is_not_one():
    rules = Rules.load(REPO_ROOT / "config" / "rules.yaml")

    assert rules.true_class_key("   ") == ""


def test_a_bare_ticker_filed_under_crypto_has_no_establishable_class():
    """The disagreement runs both ways, and the consequence is the same.

    A symbol with no slash placed in the crypto block would be gated as crypto
    and ROUTED as an equity — bracketed, with a broker-side stop, under crypto's
    caps. Neither reading is the truth, so there is no answer and the grant is
    dropped. Worth knowing for the day a second broker arrives: a `futures:`
    block listing `ES` lands here too, and the routing rule is what has to learn
    about it rather than this method.
    """
    rules = Rules.load(REPO_ROOT / "config" / "rules.yaml")
    rules.instruments["crypto"].allowed_symbols = ["ES"]

    assert rules.true_class_key("ES") == ""


# ------------------------------------------------- the position-actions block


def test_the_loop_may_not_act_on_position_plans_by_default():
    """A `Rules` built with no `position_actions:` block executes nothing.

    Fails closed, like every other switch here. An older deployment or a test
    fixture that never heard of this feature does not suddenly gain an
    unattended execution path because a new field landed.
    """
    from bot.config import PositionActionRules

    assert PositionActionRules().enabled is False


def test_the_shipped_config_leaves_unattended_execution_off():
    """Unlike `dreaming.allow_symbol_grants`, the model default and the file
    AGREE here, because nobody has asked for this one to be on.

    The operator asked for the trading agent to be able to action its own
    position, and that is satisfied by the MCP tools with a person in the
    conversation. This flag is the other thing: the fifteen-minute loop moving
    a stop at 3am with nobody watching. Building the record is not the same
    decision as arming the trigger.
    """
    rules = Rules.load(REPO_ROOT / "config" / "rules.yaml")

    assert rules.position_actions.enabled is False


# ------------------------------------------ the out-of-hours fill switch


def test_no_instrument_surrenders_its_stop_by_default():
    """An `InstrumentRules` that never heard of this field gives up nothing.

    The same fail-closed asymmetry as `PositionActionRules.enabled` and
    `DreamingRules.allow_symbol_grants`, and it matters more here than for
    either of them: this is the only setting in the file that trades away one
    of the operator's four rules. A deployment predating the feature, and every
    test fixture that builds `Rules` by hand, keeps its broker-side stop.
    """
    from bot.config import InstrumentRules

    assert InstrumentRules().allow_extended_hours_fills is False


def test_the_shipped_config_surrenders_nothing():
    """Off in the model AND off in the file, which is the pattern every switch
    that spends or acts follows here — `--execute`, `position_actions.enabled`,
    the dream timer, `enable-forge.sh`.

    `extended_hours_fill_classes` is the readable form of the question, and it
    exists so that "is anything trading without a stop at the broker" is a fact
    something can answer rather than a flag buried in one block of one file.
    """
    rules = Rules.load(REPO_ROOT / "config" / "rules.yaml")

    assert rules.instruments["us_equity"].allow_extended_hours_fills is False
    assert rules.extended_hours_fill_classes == []


def test_a_class_that_has_surrendered_its_stop_is_named():
    """The switch must not be invisible once thrown. A surrender nobody can see
    is how a feature ships inert in one direction and unnoticed in the other."""
    rules = Rules.load(REPO_ROOT / "config" / "rules.yaml")
    rules.instruments["us_equity"].allow_extended_hours_fills = True

    assert rules.extended_hours_fill_classes == ["us_equity"]


def test_a_disabled_class_is_never_named_as_having_surrendered_anything():
    """It cannot trade at all, so reporting it would name a cost nobody is
    paying — the same reason `allowed_symbols` unions enabled classes only."""
    rules = Rules.load(REPO_ROOT / "config" / "rules.yaml")
    rules.instruments["crypto"].allow_extended_hours_fills = True

    assert rules.instruments["crypto"].enabled is False
    assert rules.extended_hours_fill_classes == []


# ---------------------------------------------------------------------------
# The widened allowlist
#
# `instruments.us_equity.allowed_symbols` went from six names to twenty on the
# operator's decision. It is a PERMISSION, so every test here proves what the
# gate still REFUSES rather than what it now admits — a widened allowlist that
# only ever gets tested for the names it added is a widened allowlist nobody has
# checked the edges of.


def _shipped() -> Rules:
    return Rules.load(REPO_ROOT / "config" / "rules.yaml")


def _gate_for(rules: Rules):
    from bot.risk import RiskGate

    return RiskGate(
        rules,
        equity_at_session_start=100_000.0,
        now=datetime(2026, 5, 4, 15, 0, tzinfo=UTC),  # a Monday, inside session
    )


def _snapshot(**overrides: Any):
    from bot.models import AccountSnapshot

    return AccountSnapshot(
        equity_usd=100_000.0,
        cash_usd=100_000.0,
        buying_power_usd=100_000.0,
        open_positions=[],
        **overrides,
    )


def _proposal(symbol: str, **overrides: Any):
    from bot.models import Direction, OrderProposal

    fields: dict[str, Any] = {
        "symbol": symbol,
        "direction": Direction.BUY,
        "qty": 3,
        "limit_price": 580.00,
        "stop_loss_price": 575.00,
        "take_profit_price": 590.00,
        "rationale": "Shape only; every figure here exists to exercise a gate.",
    }
    fields.update(overrides)
    return OrderProposal(**fields)


def _tick(symbol: str):
    from bot.models import Tick

    return Tick(
        symbol=symbol, bid=579.98, ask=580.02,
        timestamp=datetime(2026, 5, 4, 15, 0, tzinfo=UTC),
    )


def _reasons(verdict) -> str:
    return " | ".join(verdict.reasons).lower()


def test_every_listed_symbol_agrees_with_the_block_that_lists_it():
    """The audited `grants.py` bypass, arriving through the config file instead.

    A symbol filed under a class it cannot belong to is a live permission under
    the WRONG class's limits: `BTC/USD` under `us_equity` would be routed by
    `AlpacaBroker` as crypto — unbracketed, so with no broker-side stop at all —
    while facing the equity book's caps. `true_class_key` derives the class from
    the same rule the broker routes on, and this fails the build if the file
    disagrees with it.

    A test rather than a load-time validator, deliberately: refusing to start is
    a denial at the least useful moment, and the commented-out `futures:`
    template resolves to `""` on purpose while no second broker exists.
    """
    rules = _shipped()
    for key, instrument in rules.instruments.items():
        for symbol in instrument.allowed_symbols:
            assert rules.true_class_key(symbol) == key, (
                f"{symbol} is listed under {key} but its true class is "
                f"{rules.true_class_key(symbol)!r}"
            )


def test_the_widened_list_still_refuses_everything_outside_it():
    """Twenty names is not "most large caps". The gate refuses by list, not by
    resemblance, and it says so in the rejection."""
    rules = _shipped()
    verdict = _gate_for(rules).evaluate(
        _proposal("GME"), account=_snapshot(), tick=_tick("GME")
    )
    assert not verdict.approved
    assert "not in the allowed list" in _reasons(verdict)


def test_a_disabled_class_is_still_never_crossed():
    """Crypto is configured-but-off on purpose, and widening the equity book
    does not reach it. Enabling it is still a one-word edit and nothing else."""
    rules = _shipped()
    assert rules.instruments["crypto"].enabled is False
    assert "BTC/USD" not in rules.allowed_symbols

    verdict = _gate_for(rules).evaluate(
        _proposal("BTC/USD", limit_price=60_000.0, stop_loss_price=59_000.0,
                  take_profit_price=61_000.0, qty=0.01),
        account=_snapshot(),
        tick=_tick("BTC/USD"),
    )
    assert not verdict.approved
    assert "not in the allowed list" in _reasons(verdict)


def test_a_dream_grant_cannot_reach_a_class_the_operator_shut():
    """The other door into the same room, checked against the SHIPPED config.

    An adopted dream widens `allowed_symbols` at runtime, so widening the static
    list must not have made that path inconsistent. The enabled-class hard block
    still refuses crypto whatever key the adoption row claims — and the same
    resolution still grants an equity outside the twenty, which is what keeps
    adoption the interesting route rather than a dead one.
    """
    from bot.grants import resolve_granted_symbols

    class _Store:
        def __init__(self, granted: dict[str, str]) -> None:
            self._granted = granted

        def granted_symbols(self, now: datetime) -> dict[str, str]:
            return dict(self._granted)

    rules = _shipped()
    moment = datetime(2026, 5, 4, 15, 0, tzinfo=UTC)

    # Honestly filed, and still refused: the class is switched off.
    assert resolve_granted_symbols(_Store({"BTC/USD": "crypto"}), rules, now=moment) == {}
    # Mislabelled to dodge that, and refused by the class the SYMBOL is, which
    # is the rule `AlpacaBroker.place_order` routes on.
    assert resolve_granted_symbols(_Store({"BTC/USD": "us_equity"}), rules, now=moment) == {}
    # An equity outside the twenty is still granted, so the block above is doing
    # work rather than refusing everything.
    assert resolve_granted_symbols(_Store({"TSLA": "us_equity"}), rules, now=moment) == {
        "TSLA": "us_equity"
    }
    assert "TSLA" not in rules.allowed_symbols


def test_the_tape_and_the_allowlist_are_different_lists_in_BOTH_directions():
    """`watchlist:` is DISPLAY ONLY and must never be merged into this.

    Pinned as a shape rather than as two fixed lists: the tape carries names the
    bot may not trade AND the allowlist carries names the tape does not show, so
    a later "tidy-up" that made one derive from the other fails here. The
    selection criteria genuinely differ — VIXY and USO are informative to watch
    and structurally decaying to mean-revert.
    """
    rules = _shipped()
    tape = set(rules.watchlist.symbols)
    allowed = set(rules.allowed_symbols)

    assert tape - allowed, "the tape must carry at least one watch-only symbol"
    assert allowed - tape, "the allowlist must not be a subset of the tape"
    for watch_only in ("VIXY", "USO", "BTC/USD", "ETH/USD"):
        assert watch_only in tape
        assert not rules.is_symbol_allowed(watch_only)


def test_widening_the_list_widened_no_limit():
    """A permission is not a limit. A newly admitted symbol is sized by exactly
    the same arithmetic as SPY was, and a proposal over the per-trade cap is
    refused with the cap named."""
    rules = _shipped()
    assert rules.account.max_risk_per_trade_pct == 1.0
    assert rules.instruments["us_equity"].max_concurrent_positions == 3
    assert rules.frequency.max_trades_per_day == 5

    # 250 shares risking $5 each is $1,250 — 1.25% of a $100k account.
    verdict = _gate_for(rules).evaluate(
        _proposal("AMZN", qty=250), account=_snapshot(), tick=_tick("AMZN")
    )
    assert not verdict.approved
    assert "risk" in _reasons(verdict)


def test_the_widened_list_is_still_DERIVED_and_not_a_second_copy():
    """Disabling the class removes all twenty at once, which is only true while
    `Rules.allowed_symbols` is derived from the enabled blocks. A hand-maintained
    list somewhere else would survive this."""
    rules = _shipped()
    assert len(rules.allowed_symbols) == 20

    off = rules.model_copy(deep=True)
    off.instruments["us_equity"].enabled = False
    assert off.allowed_symbols == []
    for symbol in rules.allowed_symbols:
        assert not off.is_symbol_allowed(symbol)


def test_the_credential_follows_the_provider_and_never_falls_back(no_do_key):
    """One question, one answer, and the wrong answer was the dangerous one.

    Three call sites tested `anthropic_api_key` directly and reported
    `ANTHROPIC_API_KEY is not set`. That was the right question while there was
    one endpoint. With two it is wrong in both directions, and the second is the
    shape that hurts: on a box configured for DigitalOcean the check PASSES with
    an Anthropic key present that nothing will use, and FAILS with a perfectly
    good model access key set.

    The absence of a fallback is the property under test. If this reached for
    the Anthropic key when the DigitalOcean one were missing, an operator who
    mistyped `DO_INFERENCE_KEY` would get a working bot billed to the wrong
    account, running orders on a model nobody chose, with nothing on any surface
    saying which.
    """
    on_anthropic = _env(ANTHROPIC_API_KEY="anthropic-key")
    assert on_anthropic.model_api_key == "anthropic-key"
    assert on_anthropic.inference_provider.credential_name == "ANTHROPIC_API_KEY"

    on_digitalocean = _env(
        ANTHROPIC_API_KEY="anthropic-key-that-must-not-be-used",
        DO_INFERENCE_KEY="do-model-access-key",
    )
    assert on_digitalocean.model_api_key == "do-model-access-key"
    assert on_digitalocean.inference_provider.credential_name == "DO_INFERENCE_KEY"


def test_a_claude_tier_default_is_recognised_from_the_spec_table(no_do_key):
    """Derived, so a fourth tier cannot arrive without this following it.

    These three ids reach a DigitalOcean endpoint as a guaranteed failure — that
    endpoint names Anthropic models differently and 403s the ones it lists on
    this account's tier — so `ModelClient` refuses at construction rather than
    letting the loop discover it 96 times a day. A hand-written list of three
    strings would go stale silently the first time the table changed.
    """
    from bot.config import CLAUDE_MODEL_IDS, is_claude_tier_default

    for model_id in CLAUDE_MODEL_IDS.values():
        assert is_claude_tier_default(model_id), model_id

    assert not is_claude_tier_default("deepseek-v4-pro")
    assert not is_claude_tier_default("")


# ------------------------------------------ the SDK's own switch, made visible
#
# Found by attacking the provider selection rather than by reading it. The
# Anthropic SDK resolves its endpoint as *kwarg > `ANTHROPIC_BASE_URL` > a
# credentials profile on disk*, so for as long as `ModelClient` passed no
# `base_url` on the Anthropic branch, the endpoint was chosen by something this
# configuration could not see and no surface could report.


def test_the_default_anthropic_endpoint_is_named_rather_than_left_to_the_sdk(no_do_key):
    """`base_url` was an empty string on this branch, which is not an endpoint.

    It has to be the real one, because `ModelClient` passes it: a provider that
    describes a configuration and a client that talks to a different endpoint
    are two facts about one thing that can disagree, which is the arrangement
    this repository refuses everywhere else.
    """
    from bot.config import ANTHROPIC, ANTHROPIC_DEFAULT_BASE_URL

    provider = _env().inference_provider

    assert provider.name == ANTHROPIC
    assert provider.usable is True
    assert provider.base_url == ANTHROPIC_DEFAULT_BASE_URL


def test_an_sdk_base_url_is_reported_rather_than_honoured_in_silence(no_do_key):
    """A deliberate proxy is supported and is NOT refused — it is named.

    This process cannot know whether an arbitrary endpoint enforces
    `output_config`; the transport assumes it does. Reporting the weaker fact
    rather than implying the stronger one is the same rule as
    `FinnhubCalendar.is_degraded` and the tailnet status.
    """
    from bot.config import ANTHROPIC

    provider = _env(ANTHROPIC_BASE_URL="https://gateway.example/v1").inference_provider

    assert provider.name == ANTHROPIC
    assert provider.usable is True
    assert provider.base_url == "https://gateway.example/v1"
    assert "gateway.example" in provider.detail
    assert "cannot check" in provider.detail


def test_the_sdk_switch_aimed_at_digitalocean_without_the_key_is_unusable(no_do_key):
    """The arrangement that is wrong in every part at once, and announced itself
    in none of them.

    `ANTHROPIC_BASE_URL=https://inference.do-ai.run` with no `DO_INFERENCE_KEY`
    sent `output_config` — which that endpoint accepts with a 200 and IGNORES —
    plus the Anthropic credential, on the transport that assumes the schema was
    enforced server-side. Reported as DigitalOcean-and-unusable rather than as a
    new state, so `ModelClient`, `provider_is_unusable` and
    `model_calls_are_impossible` all refuse it with no new code.
    """
    provider = _env(ANTHROPIC_BASE_URL="https://inference.do-ai.run").inference_provider

    assert provider.is_digitalocean
    assert provider.usable is False
    assert "DO_INFERENCE_KEY is the switch" in provider.detail


def test_the_digitalocean_match_is_on_the_HOST_and_not_the_whole_string(no_do_key):
    """One endpoint written three ways is one endpoint.

    A trailing slash or a path would slip past an equality test, and the whole
    value of the check is that it fires on the arrangement an operator actually
    types.
    """
    for written in (
        "https://inference.do-ai.run/",
        "https://inference.do-ai.run/v1",
        "HTTPS://INFERENCE.DO-AI.RUN",
    ):
        provider = _env(ANTHROPIC_BASE_URL=written).inference_provider
        assert provider.usable is False, written


def test_a_custom_digitalocean_endpoint_is_matched_too(no_do_key):
    """The host to compare against is whichever one is configured, not only the
    documented default — otherwise an operator on a private endpoint gets the
    silent version of the same fault."""
    provider = _env(
        DO_INFERENCE_BASE_URL="https://private.example",
        ANTHROPIC_BASE_URL="https://private.example/v1",
    ).inference_provider

    assert provider.is_digitalocean
    assert provider.usable is False


def test_the_digitalocean_key_still_wins_over_the_sdk_switch(no_do_key):
    """When the repo's switch is thrown, `ModelClient` passes that endpoint
    explicitly, so the SDK's variable cannot reach past it. Pinned so that a
    later tidy-up cannot make the two fight."""
    from bot.config import DO_INFERENCE_DEFAULT_BASE_URL

    provider = _env(
        DO_INFERENCE_KEY="do-model-access-key",
        ANTHROPIC_BASE_URL="https://somewhere.else.example",
    ).inference_provider

    assert provider.is_digitalocean
    assert provider.usable is True
    assert provider.base_url == DO_INFERENCE_DEFAULT_BASE_URL


def test_anthropics_own_url_written_out_is_not_reported_as_an_override(monkeypatch):
    """Setting `ANTHROPIC_BASE_URL` to Anthropic is not a swap.

    Found the way these things are found: the container this was written in
    exports exactly that value, and the first version of the check announced it
    as a proxy — a banner saying calls go somewhere other than Anthropic when
    they go to Anthropic. The good outcome must not read as the unusual one.
    """
    from bot.config import ANTHROPIC_DEFAULT_BASE_URL

    monkeypatch.delenv("DO_INFERENCE_KEY", raising=False)
    monkeypatch.delenv("DO_INFERENCE_BASE_URL", raising=False)

    for written in (
        ANTHROPIC_DEFAULT_BASE_URL,
        f"{ANTHROPIC_DEFAULT_BASE_URL}/",
        "  https://api.anthropic.com  ",
    ):
        provider = _env(ANTHROPIC_BASE_URL=written).inference_provider
        assert provider.base_url == ANTHROPIC_DEFAULT_BASE_URL, written
        assert "Anthropic direct" in provider.detail, written

    # A PATH on the same host is a different endpoint and is still named.
    proxied = _env(ANTHROPIC_BASE_URL="https://api.anthropic.com/proxy").inference_provider
    assert proxied.base_url == "https://api.anthropic.com/proxy"
    assert "cannot check" in proxied.detail
