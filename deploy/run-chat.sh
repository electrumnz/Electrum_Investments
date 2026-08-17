#!/usr/bin/env bash
#
# Run ONE Hermes turn for the dashboard chat panel. Invoked as `hermes` by the
# `mudhorn` web process:
#
#     sudo -n -u hermes -- /opt/mudhorn/deploy/run-chat.sh
#
# The prompt arrives on STDIN. That is the whole point of this file.
#
# ## Why a wrapper rather than sudo straight to the binary
#
# The obvious sudoers rule is:
#
#     mudhorn ALL=(hermes) NOPASSWD: /home/hermes/.local/bin/hermes
#
# which permits ANY arguments — including `--yolo`, which disables the approval
# system the deployment leans on, and any future flag a Hermes release adds.
# The web process takes its input from whoever is signed in to a dashboard that
# now answers on the public internet, so "any arguments" is not a shape worth
# accepting.
#
# Here the flags are fixed in a root-owned file and the untrusted part travels
# on stdin, where it cannot be read as a flag no matter what it contains. The
# sudoers rule then names this script instead:
#
#     mudhorn ALL=(hermes) NOPASSWD: /opt/mudhorn/deploy/run-chat.sh
#
# Same pattern, same reasoning and the same ownership requirement as
# run-mcp.sh: this must stay root-owned and not writable by `mudhorn`, or the
# rule becomes a way to run arbitrary code as `hermes`. `bootstrap.sh` chowns
# deploy/ to root:root for exactly that reason.
#
# ## A second, quieter benefit
#
# It lives under /opt/mudhorn, which the web process can see. The Hermes binary
# lives under /home/hermes, which is 0700 and which the web unit also hides with
# ProtectHome — so the availability check could not stat it, and `Path.exists()`
# raises rather than returning False on that. That was a 500 on the Chat page.
# Checking for THIS file instead answers the same question honestly, and lets
# ProtectHome stay switched on.

set -euo pipefail

HERMES_HOME="${HERMES_HOME:-/home/hermes}"
HERMES_BIN="${HERMES_BIN:-$HERMES_HOME/.local/bin/hermes}"

# sudo does not reset HOME by default on every distribution, and Hermes keeps
# its config, its memory and its MCP registry under $HOME. Getting this wrong
# yields an agent that starts fine and has none of its tools — the same silent
# blindness run-mcp.sh exists to prevent.
export HOME="$HERMES_HOME"
cd "$HERMES_HOME"

if [[ ! -x "$HERMES_BIN" ]]; then
  echo "Hermes is not installed at $HERMES_BIN. See docs/HERMES_SETUP.md." >&2
  exit 127
fi

# ---------------------------------------------------- which endpoint answers
#
# Environment only. No Python changes, no rebuild, and rollback is blanking one
# line in one file — the next message picks it up, because there is no Hermes
# daemon and this wrapper execs a fresh process per turn.
#
# **The key is NOT read from /opt/mudhorn/.env.** That file is owned by
# `mudhorn` at mode 600 and this process runs as `hermes`, which cannot read
# it. That is the user split working, not a problem to route around: the souls'
# key is a SECOND credential belonging to the account that spends it, so the
# agent's environment still holds nothing that reaches the broker.
#
# One file per Hermes instance, which is what makes per-agent routing real:
#
#     /home/hermes/inference.env          yoda + the armorer   (this wrapper)
#     /home/hermes/dreamer/inference.env  grogu                (run-dream.sh)
#
# Owned by hermes, mode 600, plain KEY=value lines and no `export`:
#
#     DO_INFERENCE_KEY=...        a MODEL ACCESS KEY, never a DO access token
#     DO_INFERENCE_MODEL=...      a foundation-model slug, NEVER a router
#     DO_INFERENCE_BASE_URL=      optional; blank uses the documented endpoint
#
# Absent, or with an empty key, this turn goes to Anthropic exactly as before.
# Sourced without `set -a` on purpose: these stay shell variables, and the only
# things exported are the three the client actually reads.
INFERENCE_ENV="$HERMES_HOME/inference.env"
if [[ -f "$INFERENCE_ENV" ]]; then
  # shellcheck disable=SC1090
  . "$INFERENCE_ENV"
fi

DO_INFERENCE_KEY="${DO_INFERENCE_KEY:-}"
DO_INFERENCE_MODEL="${DO_INFERENCE_MODEL:-}"
DO_INFERENCE_BASE_URL="${DO_INFERENCE_BASE_URL:-https://inference.do-ai.run}"

if [[ -n "$DO_INFERENCE_KEY" ]]; then
  # Every branch below REFUSES rather than falling back. A half-configured
  # switch that quietly served the turn from Anthropic and reported success is
  # the one failure this must not have: the operator would believe the souls
  # had moved, and nothing afterwards would say otherwise.
  if [[ -z "$DO_INFERENCE_MODEL" ]]; then
    echo "DO_INFERENCE_KEY is set in $INFERENCE_ENV but DO_INFERENCE_MODEL is empty." >&2
    echo "The serving slug is NOT the Anthropic model id, and it is not guessed" >&2
    echo "here: a plausible wrong slug fails at the endpoint mid-conversation." >&2
    echo "Set it and prove it first -- deploy/README.md, 'Pointing the souls at" >&2
    echo "DigitalOcean'." >&2
    exit 78
  fi
  if [[ "$DO_INFERENCE_MODEL" == *router* ]]; then
    echo "DO_INFERENCE_MODEL names an inference router ($DO_INFERENCE_MODEL)." >&2
    echo "A router falls back to another model on rate limit, and which model" >&2
    echo "answered a Hermes turn is not visible from here -- 'hermes -z' returns" >&2
    echo "the response text and nothing else. A downgrade nobody can observe is" >&2
    echo "worse than a failed call. Pin a foundation-model slug instead." >&2
    exit 78
  fi
  if [[ "$DO_INFERENCE_BASE_URL" != https://* ]]; then
    echo "DO_INFERENCE_BASE_URL is not an https:// URL ($DO_INFERENCE_BASE_URL)." >&2
    echo "Fix it or blank DO_INFERENCE_KEY. This does not fall back to Anthropic." >&2
    exit 78
  fi

  # **`ANTHROPIC_MODEL` DOES NOT SET THE MODEL, and this banner used to claim it
  # did.** Observed live on 12 Aug 2026: `inference.env` named
  # `llama-4-maverick`, the wrapper printed `model llama-4-maverick`, and Hermes
  # asked DigitalOcean for `claude-sonnet-5` — the value in its OWN
  # `config.yaml`, under `model.default`. The request came back 403 because that
  # model is tier-gated, which is the lucky version: a slug the account COULD
  # serve would have answered normally, from a model nobody chose, under a
  # banner naming a different one.
  #
  # So the variable is checked against the config rather than exported and
  # hoped over. This is the file's existing rule — every branch REFUSES rather
  # than falling back — applied to the one claim it was making without
  # evidence. A wrapper that cannot set the model must not print one as though
  # it had.
  # **`$HERMES_HOME/.hermes/config.yaml`, not `$HERMES_HOME/config.yaml`.**
  # Hermes sets HOME to its own directory and reads `~/.hermes/config.yaml`
  # under it. The first version of this check looked one level too high,
  # found nothing, and SKIPPED — while the banner below said the model had
  # been checked. Observed live: `inference.env` was deliberately set to a
  # different model from the config and the turn ran anyway.
  #
  # So a config that cannot be read now REFUSES. Skipping was the whole
  # defect: this wrapper exists to stop a model being announced that is not
  # in force, and a check that quietly does not run is worse than no check,
  # because the banner keeps making the claim.
  HERMES_CONFIG="$HERMES_HOME/.hermes/config.yaml"
  if [[ ! -r "$HERMES_CONFIG" ]]; then
    echo "Cannot read $HERMES_CONFIG, so the model in force cannot be" >&2
    echo "established. Hermes takes its model from that file, not from" >&2
    echo "this wrapper. Refusing rather than announcing a slug from" >&2
    echo "$INFERENCE_ENV that may not be what answers." >&2
    exit 78
  fi
  # `model:` is a block with `default:` under it. Read the first `default:`
  # after the `model:` line and nothing else; a missing block is its own case
  # below rather than an empty string compared against a real slug.
  CONFIGURED_MODEL="$(
    awk '/^model:/{inblock=1; next}
         inblock && /^[^[:space:]]/{inblock=0}
         inblock && $1=="default:"{print $2; exit}' "$HERMES_CONFIG"
  )"
  if [[ -z "$CONFIGURED_MODEL" ]]; then
    echo "No model.default found in $HERMES_CONFIG." >&2
    echo "Hermes takes its model from that file, not from this wrapper, so" >&2
    echo "the model actually used here cannot be established. Refusing rather" >&2
    echo "than printing a slug from inference.env that may not be in force." >&2
    exit 78
  fi
  if [[ "$CONFIGURED_MODEL" != "$DO_INFERENCE_MODEL" ]]; then
    echo "Model mismatch, and the config wins:" >&2
    echo "  $INFERENCE_ENV says   DO_INFERENCE_MODEL=$DO_INFERENCE_MODEL" >&2
    echo "  $HERMES_CONFIG says   model.default=$CONFIGURED_MODEL" >&2
    echo "Hermes reads its own config; ANTHROPIC_MODEL does not override it." >&2
    echo "Set model.default to the DigitalOcean serving slug, or change" >&2
    echo "DO_INFERENCE_MODEL to match what is actually configured." >&2
    exit 78
  fi

  export ANTHROPIC_BASE_URL="$DO_INFERENCE_BASE_URL"
  # The DigitalOcean key REPLACES any Anthropic credential in this environment,
  # deliberately. **Hermes DOES honour ANTHROPIC_BASE_URL** — measured 12 Aug
  # 2026 and no longer an assumption: with the base URL set, a request for
  # `llama-4-maverick` succeeded, and Anthropic serves no model by that name, so
  # it can only have been answered by DigitalOcean. Leaving a working Anthropic
  # key beside a DigitalOcean base URL is still the arrangement that could serve
  # the turn from the old provider, so the replacement stays.
  #
  # `ANTHROPIC_MODEL` is exported for completeness and is NOT what selects the
  # model. See the check above.
  export ANTHROPIC_API_KEY="$DO_INFERENCE_KEY"
  export ANTHROPIC_MODEL="$DO_INFERENCE_MODEL"
  unset ANTHROPIC_AUTH_TOKEN
  echo "inference: DigitalOcean $DO_INFERENCE_BASE_URL, model $DO_INFERENCE_MODEL" >&2
  echo "inference: endpoint and model both checked; which model ANSWERED is still not visible here" >&2
else
  # Blanking the key is the documented rollback, so it has to be a COMPLETE
  # one. A base URL left exported by anything else in this environment would
  # otherwise send the turn to DigitalOcean with an Anthropic key -- a claim of
  # "Anthropic direct" while the opposite happened.
  unset ANTHROPIC_BASE_URL

  # ------------------------------------------------ is there a credential AT ALL
  #
  # The same check as `run-dream.sh`, and it is here because the hole was
  # identical rather than because this instance was observed failing. The
  # dreamer's was: it printed "inference: Anthropic direct" and hermes died on
  # "No inference provider configured" the next line, which reads as a broken
  # agent when it is an unfinished install. This branch asserted its own
  # precondition and checked nothing, exactly as that one did, so the wrapper
  # was strict about the configuration nobody uses and credulous about the
  # default.
  #
  # Leaving one of the two unfixed would mean the account agent still has the
  # trap, and it is the instance the operator would reach for to diagnose the
  # other one.
  HERMES_DOTENV="$HERMES_HOME/.hermes/.env"
  if [[ -z "${ANTHROPIC_API_KEY:-}" ]] \
     && [[ ! -s "$HERMES_DOTENV" ]]; then
    echo "No inference credential in this home, so there is nothing to answer with." >&2
    echo >&2
    echo "  $INFERENCE_ENV has no DO_INFERENCE_KEY," >&2
    echo "  ANTHROPIC_API_KEY is unset, and" >&2
    echo "  $HERMES_DOTENV is missing or empty." >&2
    echo >&2
    echo "Refusing rather than saying 'Anthropic direct' and letting hermes fail" >&2
    echo "with 'No inference provider configured' a line later." >&2
    echo >&2
    echo "Set DO_INFERENCE_KEY and DO_INFERENCE_MODEL in $INFERENCE_ENV -- see" >&2
    echo "deploy/README.md, 'Pointing the souls at DigitalOcean'." >&2
    exit 78
  fi

  echo "inference: Anthropic direct (no DO_INFERENCE_KEY in $INFERENCE_ENV)" >&2
fi

# The prompt is data, never argv. Read it whole; `-z` takes a single prompt and
# returns the final response on stdout with nothing else.
prompt="$(cat)"

if [[ -z "${prompt//[[:space:]]/}" ]]; then
  echo "empty prompt" >&2
  exit 2
fi

exec "$HERMES_BIN" -z "$prompt"
