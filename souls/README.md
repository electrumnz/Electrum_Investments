# Souls

A soul is the character an agent speaks in. One file per agent, plain Markdown,
loaded at runtime by `src/bot/souls.py` and prepended to that agent's prompt.

Three exist:

| File | Agent | Surface | Character |
| --- | --- | --- | --- |
| `yoda.md` | The one that answers about the account | `/chat` | Ancient, patient, sparing. Has watched people lose everything by being certain. |
| `grogu.md` | The one that dreams | `/dreaming` | Small, curious, playful. Reaches for connections nobody asked for. |
| `armorer.md` | The one that argues about the limits | `/settings` | Keeper of the creed. Equips you, and makes you say what it is for first. |

These follow the `SOUL.md` convention Hermes uses, down to the section headings
(`## Personality`, `## Style`, `## What to avoid`), so they read the way anyone
who has written one before will expect.

## Why they are injected per request rather than installed

**Hermes loads exactly one soul, from `$HERMES_HOME/SOUL.md`.** It does not look
in the working directory, there is no CLI flag or environment variable to point
at a different file, and `/personality` is a session overlay rather than a
second soul. One Hermes instance therefore has one character.

This repository needs two, on one instance, chosen per request: the Chat page
gets Yoda and the Dreaming page gets Grogu. So `HermesBridge.ask` prepends the
selected soul to the prompt on stdin instead, which is the only mechanism that
can vary per call.

Two consequences worth knowing before changing any of this:

- **Do not install one of these as `~/.hermes/SOUL.md`.** It would apply to both
  agents at once and would sit alongside whichever soul the request injected,
  so the model would receive two characters and pick. If a native soul is ever
  wanted there, it should say what is true of *both* agents and nothing about
  either one's voice.
- **The prompt goes down stdin, not argv.** That is a security property rather
  than a style choice: the sudoers rule permits `deploy/run-chat.sh` with no
  arguments, so nothing a signed-in user types can be read as a flag. A soul
  mechanism that needed a command-line argument could not be used here at all.

Verified against the Hermes documentation rather than by reading its resolver;
`hermes-agent` is not vendored in `reference/`. If that changes, this is worth
confirming from the source, in the manner of the toolset findings in CLAUDE.md.

## The one rule that matters here

**A soul shapes the framing. It never touches a figure.**

This is the whole reason these are separate files rather than personality
sprinkled through the prompts. A character voice is applied to the sentence
around a number, and never to the number, the symbol, the direction, the date
or the quantity. Those are quoted exactly as the tool returned them, and a soul
that cannot say something in character says it plainly instead.

The failure this prevents is specific and it is the one this whole repository
exists to prevent. An agent with a strong voice and a weak fact is *more*
convincing than one with neither, because fluency reads as competence. In the
Alpha Arena competition six frontier models traded real money with confident
prose and 25 to 30 per cent win rates. Adding charm to that is adding varnish
to it.

So each soul file carries a **Voice** section and a **Never** section, and the
Never section wins every time they disagree.

## What a soul is not

- **Not a permission.** No agent gains a tool by being characterful. The chat
  agent reaches the bot through the MCP server, where `RiskGate.evaluate` runs
  on every order path, and the dreamer has no order path at all.
- **Not a route to a limit.** The Armorer argues about `config/rules.yaml` and
  cannot write it. That is not enforced by its Never section: `config/` is
  root-owned on the box so the service account cannot edit its own limits, and
  what the settings surface produces is a change request in
  `data/settings_requests.db` that a person applies as root. The soul makes the
  argument good; the file ownership is what makes it safe.
- **Not memory.** Hermes holds its own memory; the dreamer's notes live in
  `data/dreams.db`. A soul is static text and is the same on every call.
- **Not a strategy.** Nothing in here says what to buy. `config/rules.yaml` and
  `src/bot/strategy.py` own that, and they are code and configuration rather
  than prose for exactly that reason.

## Editing one

Souls are prompt text, so a change to one changes how an agent behaves without
changing a line of Python. Treat an edit like a config change rather than a
copy tweak: its own commit, with a reason.

`tests/test_souls.py` checks that both files exist, that each carries the
required sections, and that the Never section still contains the clauses the
rest of the system depends on. A soul is loaded at runtime from disk, so a file
that goes missing on the box is a real failure mode; the loader degrades to a
plain, voiceless prompt rather than refusing to answer, and says so.
