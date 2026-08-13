#!/usr/bin/env bash
#
# Run ONE Hermes turn for the DREAMER panel on /dreaming. Invoked as `hermes`
# by the `mudhorn` web process:
#
#     sudo -n -u hermes -- /opt/mudhorn/deploy/run-dream.sh
#
# The prompt arrives on STDIN, exactly as in run-chat.sh and for exactly the
# same reason: the sudoers rule names this file with no arguments, so nothing a
# signed-in user types can be read as a flag.
#
# ## Why this exists as a second file rather than a flag on run-chat.sh
#
# Two reasons, and the second is the important one.
#
# **Hermes holds one soul per instance.** It loads `SOUL.md` only from
# `$HERMES_HOME` — not the working directory, no CLI flag, no environment
# variable to point at another file. Two characters on one instance is why the
# soul is prepended to the prompt on stdin rather than installed; a second
# HERMES_HOME lets the dreamer have a real one, and its own memory, so Grogu's
# speculation does not bleed into Yoda's answers about the account.
#
# **The dreamer should not be able to reach the broker at all.** The account
# agent's Hermes registers this repo's MCP server, which exposes `place_order`.
# Sharing that instance means the only thing stopping a speculative agent
# placing an order is the sentence in souls/grogu.md telling it not to — prose
# where this repository uses a gate everywhere else. `RiskGate.evaluate` still
# runs on every order path, so the operator's four rules hold either way, but
# "it has no broker tool" and "it has one and was asked nicely" are different
# claims and only one of them is structural.
#
# So this instance's registry must NOT contain the bot's MCP server. That is a
# deployment step, not something this script can enforce, and the Dreaming page
# reports which instance it is actually talking to rather than assuming.
#
# ## Setting it up
#
#   sudo -u hermes env HERMES_HOME=/home/hermes/dreamer hermes   # first run
#   # then, as root:
#   #   - copy souls/grogu.md to /home/hermes/dreamer/SOUL.md
#   #   - ensure /home/hermes/dreamer/.hermes/config.yaml has NO mcp_servers entry
#   #     pointing at run-mcp.sh
#   #   - add to /etc/sudoers.d/mudhorn-chat:
#   #       mudhorn ALL=(hermes) NOPASSWD: /opt/mudhorn/deploy/run-dream.sh
#
# Same ownership requirement as run-chat.sh and run-mcp.sh: root-owned, not
# writable by `mudhorn`, or the sudoers rule becomes arbitrary code execution as
# `hermes`. `bootstrap.sh` chowns deploy/ to root:root for that reason.

set -euo pipefail

# The whole point of the file: a different home, so a different soul, a
# different memory and a different MCP registry.
HERMES_HOME="${HERMES_DREAM_HOME:-/home/hermes/dreamer}"
HERMES_BIN="${HERMES_BIN:-/home/hermes/.local/bin/hermes}"

# sudo does not reset HOME on every distribution, and Hermes keeps its config,
# memory and MCP registry under $HOME. Getting this wrong yields an agent that
# starts fine with none of its own settings — the silent blindness run-mcp.sh
# exists to prevent, in a different costume.
export HOME="$HERMES_HOME"
cd "$HERMES_HOME"

if [[ ! -x "$HERMES_BIN" ]]; then
  echo "Hermes is not installed at $HERMES_BIN. See docs/HERMES_SETUP.md." >&2
  exit 127
fi

# ---------------------------------------------------- which endpoint answers
#
# The same arrangement as run-chat.sh, deliberately duplicated rather than
# factored into a shared file. These two wrappers are already parallel copies
# for the same reason: they are moved one at a time so a failure is
# attributable, and this one is read end to end by
# `tests/test_agent_behaviour.py` to prove what the dreamer's instance can
# reach. Logic that had moved out of the file would be logic that test no
# longer sees.
#
# **It reads THIS instance's home, which is the point.** Grogu can be pointed
# at a different model from Yoda and the Armorer with no code change at all,
# which is where the "allocate tasks to better models" idea genuinely belongs:
# none of the three proposes an order, and the dreamer's whole product is
# depth. See run-chat.sh for the file format and for why the key does not live
# in /opt/mudhorn/.env.
INFERENCE_ENV="$HERMES_HOME/inference.env"
if [[ -f "$INFERENCE_ENV" ]]; then
  # shellcheck disable=SC1090
  . "$INFERENCE_ENV"
fi

DO_INFERENCE_KEY="${DO_INFERENCE_KEY:-}"
DO_INFERENCE_MODEL="${DO_INFERENCE_MODEL:-}"
DO_INFERENCE_BASE_URL="${DO_INFERENCE_BASE_URL:-https://inference.do-ai.run}"

if [[ -n "$DO_INFERENCE_KEY" ]]; then
  # Refuse rather than fall back, in every branch. A dream produced quietly by
  # the old provider while the operator believed the switch was thrown is a
  # reading of nothing.
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
    echo "answered a turn is not visible from here -- 'hermes -z' returns the" >&2
    echo "response text and nothing else. Pin a foundation-model slug instead." >&2
    exit 78
  fi
  if [[ "$DO_INFERENCE_BASE_URL" != https://* ]]; then
    echo "DO_INFERENCE_BASE_URL is not an https:// URL ($DO_INFERENCE_BASE_URL)." >&2
    echo "Fix it or blank DO_INFERENCE_KEY. This does not fall back to Anthropic." >&2
    exit 78
  fi

  # `ANTHROPIC_MODEL` does NOT select the model — Hermes reads `model.default`
  # from its own config.yaml, and this wrapper used to print a slug it had no
  # power to set. Observed on the chat instance: the banner named one model
  # while Hermes asked for another. Checked rather than asserted; see the same
  # block in `run-chat.sh` for the full account.
  #
  # This matters MORE here than there. The dreamer's whole reason for a separate
  # `HERMES_HOME` is that Grogu can run a different model from Yoda and the
  # Armorer — and if the model came from the environment that would work, while
  # in fact it comes from a per-home config file. Without this check the two
  # instances could silently share a model while the deployment claimed
  # otherwise.
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
    echo "Set model.default to the DigitalOcean serving slug, or change" >&2
    echo "DO_INFERENCE_MODEL to match what is configured." >&2
    exit 78
  fi

  export ANTHROPIC_BASE_URL="$DO_INFERENCE_BASE_URL"
  # Replaces any Anthropic credential here, so a client that ignores the base
  # URL fails with a 401 rather than quietly answering from the old provider.
  # Hermes DOES honour it — measured 12 Aug 2026 on the chat instance.
  export ANTHROPIC_API_KEY="$DO_INFERENCE_KEY"
  export ANTHROPIC_MODEL="$DO_INFERENCE_MODEL"
  unset ANTHROPIC_AUTH_TOKEN
  echo "inference: DigitalOcean $DO_INFERENCE_BASE_URL, model $DO_INFERENCE_MODEL" >&2
  echo "inference: endpoint and model both checked; which model ANSWERED is still not visible here" >&2
else
  # Blanking the key is the documented rollback, so it has to be a complete
  # one: a leftover base URL would send the turn to DigitalOcean with an
  # Anthropic key while this line claimed otherwise.
  unset ANTHROPIC_BASE_URL
  echo "inference: Anthropic direct (no DO_INFERENCE_KEY in $INFERENCE_ENV)" >&2
fi

prompt="$(cat)"

if [[ -z "${prompt//[[:space:]]/}" ]]; then
  echo "empty prompt" >&2
  exit 2
fi

exec "$HERMES_BIN" -z "$prompt"
