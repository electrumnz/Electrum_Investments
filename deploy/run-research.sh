#!/usr/bin/env bash
#
# Run ONE Hermes turn for the RESEARCHER. TODO.md item 26.
#
#     sudo -n -u hermes -- /opt/mudhorn/deploy/run-research.sh
#
# The prompt arrives on STDIN, exactly as in run-chat.sh and run-dream.sh and
# for exactly the same reason: the sudoers rule names this file with no
# arguments, so nothing a signed-in user types can be read as a flag.
#
# ## Why a THIRD instance, when there are already two
#
# Hermes loads one soul and one MCP registry per `HERMES_HOME`. run-dream.sh
# exists because the dreamer must not share an instance with a `place_order`
# tool. This one exists because a WEB-READING agent must not share an instance
# with either of the other two, and the argument runs in both directions:
#
#   - The account agent's instance registers this repo's MCP server. A page is
#     untrusted text written by anyone; putting a web reader in the process
#     that can place orders is the one arrangement to avoid.
#   - The dreamer's instance has no MCP server and is the one that would most
#     like the web. But the dreamer's product is a speculative causal chain,
#     and giving it a fetch tool puts a fetched page and that chain in one
#     context with nothing between them. The researcher hands over QUOTES; the
#     dreamer decides what they mean, in its own chain, in its own process.
#
# So this home has NO `mcp_servers` block. No broker, no dream store, no
# journal. The quarantine is a property of the process, not of a sentence in a
# soul file — which is the same distinction run-dream.sh was written for.
#
# ## What it may return
#
# Quotes and URLs. Never conclusions. A summary launders provenance: the
# distilled sentence has no author and no date, and a reader cannot tell which
# half was published and which half the model supplied. `src/bot/research.py`
# holds the rail structurally — `Citation` has nowhere to put a conclusion —
# and souls/kuiil.md holds it in the character. Both, because a prose rail
# alone is exactly the arrangement this repository replaces with a structure
# wherever it can.
#
# ## Setting it up
#
#   sudo -u hermes env HERMES_HOME=/home/hermes/researcher hermes   # first run
#   # then, as root:
#   #   - copy souls/kuiil.md to /home/hermes/researcher/SOUL.md
#   #   - merge deploy/hermes-research-config.yaml into
#   #     /home/hermes/researcher/.hermes/config.yaml with
#   #     deploy/merge-hermes-config.py (NEVER `cat >>` — see that file's header)
#   #   - CONFIRM there is no mcp_servers entry pointing at run-mcp.sh
#   #   - add to /etc/sudoers.d/mudhorn-chat:
#   #       mudhorn ALL=(hermes) NOPASSWD: /opt/mudhorn/deploy/run-research.sh
#   #   - write /home/hermes/researcher/inference.env, as for the dreamer
#
# Same ownership requirement as the other two wrappers: root-owned, not
# writable by `mudhorn`, or the sudoers rule becomes arbitrary code execution
# as `hermes`. `bootstrap.sh` chowns deploy/ to root:root for that reason.

set -euo pipefail

HERMES_HOME="${HERMES_RESEARCH_HOME:-/home/hermes/researcher}"
HERMES_BIN="${HERMES_BIN:-/home/hermes/.local/bin/hermes}"

# sudo does not reset HOME on every distribution, and Hermes keeps its config,
# memory and MCP registry under $HOME. Getting this wrong yields an agent that
# starts fine with none of its own settings — and here that specifically means
# an agent that may have picked up ANOTHER home's MCP registry, which is the
# one thing this file exists to prevent.
export HOME="$HERMES_HOME"
cd "$HERMES_HOME"

if [[ ! -x "$HERMES_BIN" ]]; then
  echo "Hermes is not installed at $HERMES_BIN. See docs/HERMES_SETUP.md." >&2
  exit 127
fi

# ------------------------------------------------- the quarantine, checked
#
# **The one check the other two wrappers do not have, and the reason this file
# is not a copy of run-dream.sh.** The isolation claim here is "this process
# cannot reach the broker", and on the other instances that claim rests on a
# deployment step somebody remembered. A deployment step nobody verifies is
# prose, and this repository's whole argument is that a guarantee has to be
# checked or it stops being one.
#
# It is a grep rather than a parse because it must not need Python, must not
# need the venv, and must fail CLOSED on anything it cannot read. An
# unreadable config refuses: "I could not establish that this instance is
# isolated" and "this instance is isolated" are different answers, and only
# one of them may run a turn.
HERMES_CONFIG="$HERMES_HOME/.hermes/config.yaml"
if [[ ! -r "$HERMES_CONFIG" ]]; then
  echo "Cannot read $HERMES_CONFIG, so it cannot be established that this" >&2
  echo "instance has no route to the broker. Refusing: an unverified" >&2
  echo "quarantine is not a quarantine. See deploy/hermes-research-config.yaml." >&2
  exit 78
fi
if grep -qE 'run-mcp\.sh|electrum-bot' "$HERMES_CONFIG"; then
  echo "$HERMES_CONFIG registers this repo's MCP server." >&2
  echo "The researcher reads the web, and a web-reading agent must not share a" >&2
  echo "process with place_order. Remove the mcp_servers entry from THIS home" >&2
  echo "-- the account agent keeps its own, under a different HERMES_HOME." >&2
  exit 78
fi

# ---------------------------------------------------- which endpoint answers
#
# The same arrangement as run-chat.sh and run-dream.sh, deliberately duplicated
# rather than factored out. Those two are already parallel copies for a stated
# reason: they are moved one at a time so a failure is attributable, and each
# is read end to end by a test to prove what that instance can reach. Logic
# that had moved into a shared file would be logic those tests no longer see.
INFERENCE_ENV="$HERMES_HOME/inference.env"
if [[ -f "$INFERENCE_ENV" ]]; then
  # shellcheck disable=SC1090
  . "$INFERENCE_ENV"
fi

DO_INFERENCE_KEY="${DO_INFERENCE_KEY:-}"
DO_INFERENCE_MODEL="${DO_INFERENCE_MODEL:-}"
DO_INFERENCE_BASE_URL="${DO_INFERENCE_BASE_URL:-https://inference.do-ai.run}"

if [[ -n "$DO_INFERENCE_KEY" ]]; then
  if [[ -z "$DO_INFERENCE_MODEL" ]]; then
    echo "DO_INFERENCE_KEY is set in $INFERENCE_ENV but DO_INFERENCE_MODEL is empty." >&2
    echo "The serving slug is NOT the Anthropic model id, and it is not guessed" >&2
    echo "here: a plausible wrong slug fails at the endpoint mid-conversation." >&2
    exit 78
  fi
  if [[ "$DO_INFERENCE_MODEL" == *router* ]]; then
    echo "DO_INFERENCE_MODEL names an inference router ($DO_INFERENCE_MODEL)." >&2
    echo "A router falls back to another model on rate limit, and which model" >&2
    echo "answered a turn is not visible from here. Pin a foundation-model slug." >&2
    exit 78
  fi
  if [[ "$DO_INFERENCE_BASE_URL" != https://* ]]; then
    echo "DO_INFERENCE_BASE_URL is not an https:// URL ($DO_INFERENCE_BASE_URL)." >&2
    echo "Fix it or blank DO_INFERENCE_KEY. This does not fall back to Anthropic." >&2
    exit 78
  fi

  # Hermes reads `model.default` from its own per-home config, NOT from the
  # environment — measured on the chat instance, where the banner named one
  # model while Hermes asked for another. A config that cannot be read refuses
  # rather than skipping, because a check that quietly does not run is worse
  # than no check: the banner below keeps making the claim.
  CONFIGURED_MODEL="$(
    awk '/^model:/{inblock=1; next}
         inblock && /^[^[:space:]]/{inblock=0}
         inblock && $1=="default:"{print $2; exit}' "$HERMES_CONFIG"
  )"
  if [[ -z "$CONFIGURED_MODEL" ]]; then
    echo "No model.default found in $HERMES_CONFIG." >&2
    echo "Hermes takes its model from that file, not from this wrapper, so the" >&2
    echo "model actually used cannot be established. Refusing." >&2
    exit 78
  fi
  if [[ "$CONFIGURED_MODEL" != "$DO_INFERENCE_MODEL" ]]; then
    echo "Model mismatch, and the config wins:" >&2
    echo "  $INFERENCE_ENV says   DO_INFERENCE_MODEL=$DO_INFERENCE_MODEL" >&2
    echo "  $HERMES_CONFIG says   model.default=$CONFIGURED_MODEL" >&2
    exit 78
  fi

  export ANTHROPIC_BASE_URL="$DO_INFERENCE_BASE_URL"
  export ANTHROPIC_API_KEY="$DO_INFERENCE_KEY"
  export ANTHROPIC_MODEL="$DO_INFERENCE_MODEL"
  unset ANTHROPIC_AUTH_TOKEN
  echo "inference: DigitalOcean $DO_INFERENCE_BASE_URL, model $DO_INFERENCE_MODEL" >&2
else
  # Blanking the key is the documented rollback, so it has to be complete: a
  # leftover base URL would send the turn to DigitalOcean with an Anthropic key
  # while this line claimed otherwise.
  unset ANTHROPIC_BASE_URL
  echo "inference: Anthropic direct (no DO_INFERENCE_KEY in $INFERENCE_ENV)" >&2
fi

prompt="$(cat)"

if [[ -z "${prompt//[[:space:]]/}" ]]; then
  echo "empty prompt" >&2
  exit 2
fi

exec "$HERMES_BIN" -z "$prompt"
