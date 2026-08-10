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

  export ANTHROPIC_BASE_URL="$DO_INFERENCE_BASE_URL"
  # The DigitalOcean key REPLACES any Anthropic credential in this environment,
  # deliberately. Whether Hermes honours ANTHROPIC_BASE_URL is unverified — the
  # authoritative check is behavioural, so ask the agent rather than reading a
  # config file. If it turns out not to, this key reaches Anthropic and is
  # refused with a 401, which is loud. Leaving a working Anthropic key beside a
  # DigitalOcean base URL is the arrangement that could serve the turn from the
  # old provider while the operator believed it had moved.
  export ANTHROPIC_API_KEY="$DO_INFERENCE_KEY"
  export ANTHROPIC_MODEL="$DO_INFERENCE_MODEL"
  unset ANTHROPIC_AUTH_TOKEN
  echo "inference: requesting DigitalOcean $DO_INFERENCE_BASE_URL, model $DO_INFERENCE_MODEL" >&2
  echo "inference: requesting, not confirming -- this wrapper cannot see which model answered" >&2
else
  # Blanking the key is the documented rollback, so it has to be a COMPLETE
  # one. A base URL left exported by anything else in this environment would
  # otherwise send the turn to DigitalOcean with an Anthropic key -- a claim of
  # "Anthropic direct" while the opposite happened.
  unset ANTHROPIC_BASE_URL
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
