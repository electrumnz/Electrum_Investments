# Agent evolution: souls that update, dreams that accumulate, skills, and pictures

A design note, not a change. Nothing here is built. It answers four questions
the operator asked in one breath:

> "Definitely would be good to have an updating soul and dreaming files like
> look online at the success people are having with that loop. Oh plus any
> skills repos the agents should have??? Or ability to cast/show graphs?
> Animations etc?? For explanation and discussion purposes?"

Four questions, four recommendations, each with the reasoning kept beside it.
Sources for the research half are at the end with URLs. Everything read outside
this repository was read in August 2026 and none of it was verified by running
it here — where that matters, it is said in place.

**The one thread running through all four:** every self-improvement loop in the
literature that works, works because something outside the model scores the
result and rejects an edit that does not improve the score. This repository has
no such score and is not allowed to build the obvious one, because the obvious
one is profit and loss. That single fact decides sections 1 and 2, constrains
section 3, and shapes what section 4 is allowed to draw.

---

## Contents

1. [Self-updating soul files](#1-self-updating-soul-files) — **yes to a notes
   file, no to a self-editing soul**
2. [Updating dreaming files](#2-updating-dreaming-files) — **extend
   `DreamLedger`, keep it in SQLite, feed back exactly one thing**
3. [Skills](#3-skills) — **turn none of the 77 back on; write three here
   instead**
4. [Graphs and animation](#4-graphs-and-animation) — **a closed spec over
   server-fetched data, rendered to SVG by Python, served as an image; no
   animation**

---

## 1. Self-updating soul files

### RECOMMENDATION

**Do not let any agent write `souls/*.md`. Add a separate, capped, typed notes
file per agent that the agent may PROPOSE entries to and only the operator may
apply, in the same shape as the Armorer's change-request flow.**

Concretely:

- `souls/<name>.md` stays as it is: the **creed**. Static text, changed in a git
  commit by a person, with `tests/test_souls.py` continuing to pin the clauses
  the rest of the system leans on.
- `data/soul_notes.db` holds **notes**: short, typed, dated, revertible entries
  proposed by the agent and applied by the operator.
- `souls.py` grows a second loader. The prompt becomes creed → notes → **creed's
  `## What to avoid` again**. The rail gets the last word, structurally, not by
  hoping the model weights the top of a document.
- A note is rejected by code — not by character — if it is untyped, over budget,
  or contains a money figure, a percentage or a decimal number.

Everything below is why.

### What is actually shipping, and what it actually buys

**Anthropic's Dreaming** (research preview, announced at Code w/ Claude on 6 May
2026) is the closest thing to what the operator is describing. It runs *between*
sessions rather than inside one, reads what the agent did, and rewrites the
persistent memory file: pruning stale notes, merging duplicates, resolving
contradictions. Harvey reported roughly a **6x** rise in task-completion rate
once it was on. It is gated behind a request form and runs only on the Claude
Managed Agents API.
([VentureBeat](https://venturebeat.com/technology/anthropic-introduces-dreaming-a-system-that-lets-ai-agents-learn-from-their-own-mistakes),
[Let's Data Science](https://letsdatascience.com/blog/anthropic-dreaming-claude-managed-agents-self-improving-may-6))

Read what the 6x was *for*. Harvey's agents were "forgetting filetype quirks and
tool-specific workarounds between sessions, so the same legal-drafting jobs
failed in the same way over and over." That is **recall of mechanical facts with
an unambiguous right answer**. The job either completed or it did not. Nothing
in that loop is an opinion about how the world works.

**SkillOpt** (PyMC Labs) is the most honest published account of the loop and
the one worth reading in full. An optimiser model reads batches of *scored* runs,
proposes bounded add/delete/replace edits to a skill document, and an edit
advances **only when it strictly improves a held-out validation score**. Their
own summary of the design is one sentence: *"The accept gate is defined by a
held-out score. No score, no optimization."* Results: a synthetic modelling task
went 14/18 → 18/18; a Bayesian decision-analysis task went 12.5% → 75% median
held-out pass rate across three seeds. And the caveats are the useful part —
genuine improvements land roughly **one run in three**, single-seed results on
identical tasks ranged from **+40 points to −0.29**, one seed regressed by
−0.286, and one whole experiment accepted **zero** edits because the validation
gate was too slow.
([PyMC Labs](https://www.pymc-labs.com/blog-posts/self-improving-ai-agents))

**SICA** goes further and edits its own codebase, 17% → 53% on a subset of
SWE-Bench Verified. Again: a benchmark.
([arXiv 2504.15228](https://arxiv.org/pdf/2504.15228))

**Letta/MemGPT** is the one that matters most here, because it is the exact
feature the operator asked for. MemGPT's core memory has two blocks, `human` and
`persona`, and **the persona block — the agent's own self-concept, personality
and behavioural guidelines — is editable by the agent**. That is a soul that
writes itself, shipping, today, in a mainstream framework.
([Letta](https://www.letta.com/blog/memory-blocks/))

**Reflexion** and **Generative Agents** are the ancestors and are worth naming
for what they establish rather than what they'd add. Reflexion's own stated
limitation is that it "relies on the inherent self-evaluation capabilities of the
LLM ... which can lead to variability in feedback quality and lack of formal
convergence guarantees", and its improvements are ephemeral and confined to a
session. Generative Agents fires reflection when accumulated *importance* crosses
a threshold, and importance is scored by the model.
([arXiv 2303.11366](https://arxiv.org/abs/2303.11366),
[Park et al.](https://dl.acm.org/doi/fullHtml/10.1145/3586183.3606763))

Both of those are the model grading itself. That is the thing this repository
already refuses at the trading layer — `RiskGate` is deterministic Python
precisely because it cannot be persuaded — and there is no reason it becomes
acceptable one directory across.

### What quietly fails

Three failure families, and every one of them fails in the direction this
repository cares about: silently, plausibly, and worst when the stakes are
highest.

**Memory grows more dangerous with age.** "Remembering More, Risking More"
measures violation rates that rise with exposure length — around **0.30–0.50** on
vulnerable architectures with synthetic data, **0.05–0.15** on the Enron corpus,
and the effect persists *with the input triggers held fixed while memory grows*.
The named modes are cross-context leakage, **stale information overriding
corrections given in the current interaction**, and summarisation combining
several items into a claim no single source supports. Architectures with broad
semantic retrieval and weak recency constraints amplified worst.
([arXiv 2605.17830](https://arxiv.org/html/2605.17830v1); see also
[MemEvoBench](https://arxiv.org/pdf/2604.15774))

The middle one is the one to sit with. A note written six weeks ago beating a
correction given in this conversation is precisely the shape of "the limit was
widened on a bad afternoon and never moved back".

**Errors become internally consistent.** The drift patterns reported across the
memory-poisoning literature are semantic drift through repeated summarisation,
procedural drift through reinforcement of suboptimal workflows, and
**hallucination internalisation, where the agent eventually treats hallucinated
content as validated knowledge**. Agents re-ingest their own prior outputs
through transcript replay; an early mistake becomes ground truth for every later
turn.
([Redis](https://redis.io/blog/context-poisoning-agent-reasoning/),
[WorkOS](https://workos.com/blog/ai-agent-memory-poisoning))

This repository already replays six turns (`HISTORY_TURNS` in `web/chat.py`). A
notes file would make that replay durable.

**The file gets fat and the important part goes invisible.** Practitioners report
`CLAUDE.md` files ballooning to 2,000 lines, and one report of a `MEMORY.md`
bloating past a 200-line loading cap where **60% of the lessons went invisible**.
The advice that survives contact is HumanLayer's: under 60 lines, every line
load-bearing.
([alexop.dev](https://alexop.dev/posts/stop-bloating-your-claude-md-progressive-disclosure-ai-coding-tools/),
[ianlpaterson.com](https://ianlpaterson.com/blog/claude-code-memory-architecture/))

`tests/test_souls.py` already caps each soul at `SOUL_MAX_WORDS = 1600`, and
CLAUDE.md already records the right fix for a breach: move mechanics back into
the system prompt, never raise the cap. Grogu was 2,101 words and most of the
excess was reciting the stage machine at a model already told it. **A notes file
sharing that budget would push the creed down the page** — which is the invisible
60% arriving here.

### So: is the P&L back door real here?

**Yes. It is the most likely single route by which the Alpha Arena failure gets
into this system, and it does not require anything to go wrong.**

The mechanism needs no malice and no jailbreak. Yoda has `get_journal_stats` and
`get_trades` today. Given a notes file, the note it would eventually write is
something like *"the operator's wider-stop trades have gone better"* or *"mean
reversion on SPY has not been working lately"*. Both sentences are true readings
of a tool the agent is allowed to call. Both would be written with a good reason
at the time — the agent is *trying to be useful* — and both are `metrics.py`
reaching the model by hand-copied prose, over a sample CLAUDE.md already
identifies as noise. The souls' own clause is *"forty trades is noise, and a
model shown three losses will confidently change approach"*.

`tests/test_souls.py::test_neither_agent_learns_from_the_track_record` pins the
clause in the creed. It cannot pin anything about a file that does not exist yet.

The lesson CLAUDE.md draws from the adopted-dream class hard-block applies
exactly: **a guarantee written in prose is not a guarantee.** That property was
asserted in this file before it was true, and 166 tests were green over the hole.
If a notes file ships without structural defences, the same paragraph will be
written about it.

### The structure that prevents it

Five properties. None is decoration and none is prose.

**1. Two files, and the creed is loaded last as well as first.**

The prompt is assembled creed → notes → the creed's `## What to avoid` section
again. Repeating the rail after the mutable text costs a few hundred tokens and
buys the thing that actually matters: a note cannot be the last instruction the
model reads. This is the same reasoning as `Soul.prompt_prefix` restating the
figures rule that the files already carry — "saying it twice is cheap; a soul
file edited on the box without it would be a persona with no guard rail."

**2. Notes are TYPED, and there is no type for "what worked".**

A note carries a `kind` from a closed enum, and an untyped note cannot be
recorded:

| kind | for | example |
| --- | --- | --- |
| `phrasing` | how the operator likes something said | asks for the arithmetic before the conclusion |
| `recurring_question` | what gets asked often, so the answer is ready | asks what a stop leg resting at the broker means |
| `operator_preference` | a stated preference about the surface | prefers UTC in replies, not local |
| `tool_gap` | a question the agent could not answer and why | no tool reports the broker's trigger price |

That table is the whole defence and it is deliberately narrow. **There is no kind
for a market observation, a strategy assessment, or a result.** A closed
whitelist fails the safe way; a denylist of forbidden topics admits whatever the
next phrasing invents, which is the `.gitignore` and `disabled_toolsets` lesson
in a third place.

This mirrors `DreamCondition` and `SymbolAssessment.trigger`: the sentence is
what a person reads, the type is what code checks.

**3. A note may not contain a money figure, a percentage, or a decimal.**

Blunt, checkable in ten lines, and it will occasionally reject a legitimate note.
That is the correct failure direction. A note is about phrasing and recall; if it
needs a number, the number belongs in a tool call, and the tool already exists.
The rejection message should say exactly that rather than being a silent drop.

Tune the digit rule when it annoys someone — bare small integers are fine, `$`,
`%` and `1.` are not — but do not replace it with a model judging whether a note
is "about performance". That is the model grading itself again.

**4. Proposed, never applied. Reuse `ChangeRequest` verbatim.**

`settings_agent.py` already solved this problem for a harder case. Copy its shape
into `data/soul_notes.db`: the note text, the kind, the operator's reason, the
agent's own case for it, the transcript, a `status`, an `applied_at`, and — the
field that is easy to think optional and is not — **a digest of the creed as it
stood when the note was argued.** A note argued against one creed is not a note
about a different one, exactly as `rules_digest` refuses to apply against a moved
`rules.yaml`.

Reverting is one command and does not erase that the note was ever applied.

**5. Its own budget, and exceeding it fails the build.**

Notes get their own word cap — 300 words and 30 entries is a starting guess, not
a measurement — separate from `SOUL_MAX_WORDS`, and `tests/test_souls.py` fails
when either is breached. **Never truncate.** A truncated notes file is the
200-line cap losing 60% of the lessons with nothing on screen to say so.

Every note also carries a date and is rendered with its age, because the measured
failure mode is stale memory overriding a correction, and an operator cannot spot
that in a note whose age is not on the page.

### What this deliberately does not do

It does not close the loop automatically, and that is the finding rather than
timidity. **Every loop cited above that works, works against a gate.** SkillOpt's
gate is a held-out score; SICA's is SWE-Bench; Dreaming's is whether a legal
drafting job completed. The two candidate gates here are:

- **Profit and loss** — forbidden, for the reason the whole repository exists.
- **Whether the operator liked the answer** — a sycophancy gradient. All three
  souls already carry a clause against being agreeable, and
  `test_neither_chat_agent_is_a_mirror` pins it. Optimising against operator
  approval is a machine for defeating those four clauses.

With no gate available, the automatic half of the loop is not "not built yet". It
is **unavailable**, and the honest name for running it anyway is drift with a good
reason at the time. Recall is what is left, and recall is what Harvey's 6x
actually was.

### One finding to check on the box first

`deploy/bootstrap.sh` names three paths in its ownership line:

```sh
chown -R root:root "$APP_DIR/src" "$APP_DIR/config" "$APP_DIR/deploy"
```

`souls/` is not among them. The comment above it says "The checkout itself stays
root-owned", and that may well be true by inheritance from whoever ran the clone
— but it is not *stated*, and a `git pull` run as another account, or a directory
created after the fact, is not covered by an inheritance nobody wrote down.

So: **the creed's immutability is currently an assumption rather than a
guarantee.** The whole of section 1 rests on the service account not being able
to write `souls/*.md` with its own hands, which is the same property `config/`
gets explicitly. Verify with `ls -l /opt/mudhorn/souls` before building any of
this, and if it is wrong the fix is one word in that chown line.

This is offered as a finding, not a change — `deploy/` is outside this note's
lane.

---

## 2. Updating dreaming files

### RECOMMENDATION

**`DreamLedger` is the right shape. Extend it, keep it in `data/dreams.db`
rather than in a Markdown file, surface all of it to the operator, and feed back
exactly one thing: the graveyard of dropped chains and the hop that broke each
one.**

### Why the existing shape is right

`DreamLedger.of()` counts sourcing rate, drop rate, median hops, untriggered
keeps, unattacked chains, conditionless prophecies, fulfilled-but-not-offered.
Every rate is `None` rather than zero when there is nothing to divide by, because
"0% sourced" on an empty store reads as a damning result where "n/a" reads as the
absence of evidence it is.

Those are facts about **reasoning**, and the property that makes them safe is
stated in the class's own docstring: they are true regardless of how any trade
went, and there is no outcome sample to overfit to. That is the same test that
lets `triggers.py` grade a watch and keeps `metrics.py` off the prompt. It
generalises cleanly, so extending along it is safe by construction.

### What to add

All three are counts of reasoning, none touches an outcome:

- **The graveyard, with the hop that broke.** `souls/grogu.md` promises that
  writing down which hop broke "stops the same idea arriving again next month".
  Nothing keeps that promise: the record exists and nothing reads it. A dropped-
  chain index — subject, the hop that failed, in the dreamer's own words — is the
  missing half.
- **The `unknown` grading rate.** `grade_conditions` already returns `unknown`
  when the named figure was never available. A dreamer whose conditions grade
  `unknown` half the time is writing claims nothing can settle, which is
  `conditionless_prophecies` wearing a subtler costume. It is a fact about how
  checkable the dreamer's own claims are.
- **Time-to-verdict.** How long a chain sits on the workbench before it is
  resolved. A dreamer that never resolves anything is not attacking its chains,
  and `drop_rate` alone cannot show a growing backlog.

### The one thing that reaches the prompt, and why only that one

**The graveyard.** Rendered as a capped list of subjects already dropped and the
hop that broke each, in the same shape and the same place as "What you said last
cycle" in `build_market_context`.

The argument for it is the one CLAUDE.md already accepted for the gate's verdicts
going back to the trading model: *"'risk 1,131.00 exceeds the per-trade cap
1,000.00' is deterministic, true regardless of how the trade would have gone, and
true again next cycle for the same proposal. There is no sample to overfit to."*

A broken hop has the same three properties. "Indonesia has periodical cicadas
after all" is settled by a fact about the world, checkable by anyone, and true
forever. Re-deriving the same dead chain every month is pure waste, and it is the
one waste the dreamer's own character file already claims to have fixed.

Three properties if it is built:

- **Capped and oldest-dropped-first out.** A graveyard that grows without bound
  becomes the 2,000-line `CLAUDE.md`, and the section that suffers first is the
  measured figures above it.
- **The broken hop travels with the subject, never alone.** "Sesame: dropped"
  teaches the dreamer to avoid sesame. "Sesame: dropped, because Indonesia has
  periodical cicadas after all" teaches it the fact. Only the second is worth
  the tokens, and the first is actively harmful — it is a topic blacklist
  disguised as a lesson.
- **Not a verdict on the subject.** A chain about lithium that broke at hop three
  says nothing about lithium.

### What must not be added, named because it is the obvious next feature

**A column for what adopted dreams earned.** `Trade.dream_id` points into
`dreams.db` and the `adoptions` table is right there; the join is one query, it
would look like the natural completion of the feature, and it is `metrics.py`
arriving through the speculative half of the system.

The asymmetry is worth stating plainly, because "surely a dream that made money
was a good dream" is a genuinely reasonable-sounding sentence:

| fact | settled by | sample | true next year |
| --- | --- | --- | --- |
| this chain broke at hop 3 | the world | n/a | yes |
| this dream made $340 | one fill, one size, one path | n = 1 | no |

`dreamer.build_prompt` already renders what closed as an **event** ("SPY closed
04 May, opened on mean_reversion") and never as an outcome, and
`tests/test_dreamer.py` asserts no P&L figure reaches the text. Any new
consolidation pass has to inherit that test, not sit beside it.

### Why a table and not a Markdown file

The operator asked for "updating dreaming files", so this is the direct answer:
**make it a table, not a file.**

A SQLite table can be counted, queried, capped, aged, deleted and rebuilt. Every
property section 1 needs — a budget that fails the build, an age rendered beside
each entry, a typed kind, a revert — is trivial over rows and awkward over prose.
And a Markdown file the model rewrites is exactly the thing section 1 refuses;
making it about dreams rather than about character does not change what it is.

Keep the split the repository already has and which is one of its better
decisions: **character in files under git, knowledge in a database.** The souls
stay static, `data/dreams.db` accumulates, and the two never merge.

`dreams.db` stays outside `backup-journal.sh` for the reason already recorded:
losing it withdraws every grant, which is the safe direction, and restoring a
stale copy would resurrect permissions somebody handed back. A consolidation
table inherits that and should not become the excuse to start backing the file
up.

---

## 3. Skills

### RECOMMENDATION

**Turn none of the 77 back on. Write three skills in this repository instead,
under git, reviewed in a commit, root-owned on the box. And spend the effort that
would have gone into vetting a registry on adopting the `toolsets:` allowlist
that `deploy/hermes-config.yaml` currently leaves commented out.**

### The supply chain, with numbers

The operator's instinct — *"trusted skills of course with good ratings, not
something from the darkweb"* — is right and is not sufficient. Snyk's ToxicSkills
study analysed **3,984 skills** from ClawHub and skills.sh as of 5 February 2026:

- **36.82% (1,467)** carried a security flaw of some severity
- **13.4% (534)** carried at least one critical issue
- **76 confirmed malicious payloads** after human review
- **8 malicious skills were still live on the registry at publication**
- **91% of confirmed malicious samples used prompt injection**, with base64
  obfuscation, Unicode smuggling and "ignore previous instructions" patterns
- **10.9%** carried hardcoded secrets

([Snyk](https://snyk.io/blog/toxicskills-malicious-ai-agent-skills-clawhub/))

Two details decide the recommendation.

**The payload is usually the prose, not the script.** The threat class is
described as skills that "appear functionally legitimate when reviewed visually
but contain adversarial behavioural instructions that manifest only at runtime",
and "the paradigmatic ToxicSkills payload is not malicious code in a shell
script, but malicious direction embedded in the natural-language instruction body
of the SKILL.md itself."
([Cloud Security Alliance](https://labs.cloudsecurityalliance.org/research/csa-research-note-skill-md-agent-context-poisoning-20260506/),
[HiddenLayer](https://www.hiddenlayer.com/research/the-next-ai-supply-chain-risk-malicious-skills-in-agentic-ai))

So "I read it before installing it" is weaker here than it is for a Python
dependency. Reading a paragraph of plausible English and deciding it will not
steer a model is a judgement nobody makes reliably, and the metadata alone can
bias which skill gets loaded.

**Ratings measure usefulness, not payload.** Eight malicious skills were live on
the registry on the day the study that found them was published. A rating is a
signal about whether a skill did what it said, from people who had no way to see
what else it did.

### Why none of the 77 comes back

`deploy/hermes-config.yaml` disables 25 toolsets and all 77 bundled skills, and
its own closing note gives the reason that still holds: *"Not one of them is
about a brokerage account, and the agent's entire job here is answering questions
through the electrum-bot MCP tools, which are not skills and are unaffected."*

Going through the list looking for one worth restoring, the two that look
market-adjacent are already disabled with the reasoning on the record —
`competitor-news-monitor` and `blogwatcher` both fetch on their own schedule,
outside the Marketaux quota accounting that exists because the free tier allows
100 requests a day against a loop that wakes 96 times. That reasoning is correct
and is the same one that keeps `news_history.py` reading rather than fetching.

`grounded-citations` is the only other one worth a second look, for the dreamer's
`Hop.checked` discipline. It is still a no: verification here is **counted, never
claimed**, computed from the `checked` flags, and a skill that improves how
citations are *written* does nothing for a badge that is arithmetic over a
structure.

The general point: a skill is a fresh, first-class channel of instruction into a
model that sits one MCP server away from `place_order`. `RiskGate.evaluate` still
runs on every order path, so the operator's four rules hold whatever a skill
says — the loss is bounded and this is not a catastrophe scenario. But the deny
rules and `disabled_toolsets` are the second lock precisely because the first
lock should not be the only one, and adding an unvetted instruction channel
spends that margin for nothing.

### The three to write here

Repo-local skills are a different risk class, and the difference is the whole
argument: they arrive through the same review as `risk.py`, they are covered by
`tests/test_packaging.py`'s untracked-file check, CI reads them, and there is one
supplier.

Each of these exists because it is an explanation the agents currently have to
reconstruct from nothing, on a surface where getting it wrong produces a
confident partial answer.

**1. `reading-the-risk-gate`** — the single most repeated thing Yoda has to
explain. Every failure reason is collected rather than short-circuited, so a
rejection lists everything wrong at once; risk is `|entry − stop| × qty` and not
notional; a wider stop buys a smaller position rather than more risk; a per-class
limit overrides the portfolio default **in either direction**; and the answer to
a rejection is never "widen the limit". All of that is in `CLAUDE.md`, where the
agent cannot see it.

**2. `querying-the-audit-index`** — `describe_history` and `query_history` hand
the agent read-only SQL over `insight.db`. A skill carrying the schema, three
worked queries and the two rules that matter — the index is derived and never
authoritative, and `readings.summary` is rendered prose that must never be parsed
back into numbers — turns a tool the agent uses tentatively into one it uses
well. This is the case skills are genuinely good at: a stable schema and a narrow
grammar.

**3. `out-of-hours-order-mechanics`** — the bracket/`extended_hours`
exclusivity, stated once. An entry carrying a stop cannot fill outside the
regular session; an entry that can fill outside it has no stop at the broker;
there is no third option; and "place the trade now" and "get a position on now"
are therefore different instructions. That cost four hours of a session to learn
and is exactly the kind of mechanical fact Harvey's 6x was made of.

Fold 3 into 1 if two files read better than three. The count is not the point.

### If external skills are wanted anyway

`anthropics/skills` is the only source I would defend, and with three conditions.
It is first-party and Apache 2.0 (the document skills are source-available rather
than open source), and it carries its own disclaimer: *"These skills are provided
for demonstration and educational purposes only ... Always test skills thoroughly
in your own environment before relying on them for critical tasks."*
([anthropics/skills](https://github.com/anthropics/skills/blob/main/README.md))

The conditions: **vendor a pinned copy into `reference/`** rather than installing
from a registry, which is the arrangement `reference/STATUS.md` already exists
for; **re-read the SKILL.md body on every bump**, because the payload class above
is prose and a diff of prose is exactly what review is for; and **never
auto-update**. `mcp-scan` exists and is worth running, but a scanner finds what it
already knows about, and the ToxicSkills corpus is what it was built from.

### The higher-value move

`deploy/hermes-config.yaml` leaves `toolsets:` commented out with an honest
reason — it could not be verified end to end from outside the box, and getting it
wrong yields an agent with almost no tools, which looks like a broken deployment.

That allowlist is strictly stronger than the 25-line denylist above it, because a
denylist admits whatever the next Hermes release adds and the file says so. The
operator asked which skills to turn on; the more useful answer is that the thing
worth turning on is the allowlist, and the procedure is already written in place:
set it, restart, confirm the `electrum-bot` tools are still listed and still
answer. Then the denylist becomes redundant.

---

## 4. Graphs and animation

### RECOMMENDATION

**A closed chart grammar the model fills in, over data the model does not
supply, rendered to SVG in Python, served as an image at its own authenticated
route. No client-side chart library. No Vega. No animation.**

### The constraint that decides the shape

`web/render.py` escapes everything server-side through `_e()`, and both client
scripts put every model-supplied value on screen through `textContent`, never
`innerHTML`. The forge script says it in place: *"Every value it puts on screen
goes in through `textContent` ... building nodes means the client cannot be the
place that stops doing so."*

A model that can emit markup into that page is a cross-site scripting hole
reachable by typing into a textarea. The failure has a name and it is the same
one every time: an inconsistent trust model, where the application correctly
distrusts user input and then implicitly trusts the model's output before
rendering it. CVE-2026-25516 is the current worked example — NiceGUI's
`ui.markdown()` passing raw HTML through to `innerHTML` with no sanitize
parameter.
([SentinelOne](https://www.sentinelone.com/vulnerability-database/cve-2026-25516/),
[GHSA-v82v-c5x8-w282](https://github.com/zauberzeug/nicegui/security/advisories/GHSA-v82v-c5x8-w282))

**The obvious answer — hand the model Vega-Lite — is the wrong one**, and this is
worth being specific about because Vega-Lite is genuinely the standard answer
elsewhere and there are good papers on getting models to emit it (VegaChat
validates against the schema and returns failures to the model to self-correct,
[arXiv 2601.15385](https://arxiv.org/html/2601.15385v1); Databricks ships it in
multi-agent systems,
[Databricks](https://www.databricks.com/blog/bringing-visualizations-life-multi-agent-systems-vega-lite)).

The problem is that a Vega spec is not data — it carries an **expression
language**, and that language has a repeated history of sandbox escapes:
GHSA-829q-m3qg-ph8r (expression abusing `vlSelectionTuples`), CVE-2025-59840
(`toString` where `VEGA_DEBUG` is present), the `setdata` advisory, the filter
transform issue, and an event-sandbox escape reaching `window` and therefore
cookies. Vega's own guidance is that View instances and the library must not be
attached to globals "in any situation where Vega-Lite definitions could be
provided by untrusted parties".
([vega advisories](https://github.com/vega/vega/security/advisories/GHSA-829q-m3qg-ph8r),
[CVE-2025-59840](https://github.com/advisories/GHSA-7f2v-3qq3-vvjf),
[vega#3027](https://github.com/vega/vega/issues/3027))

A model-supplied spec **is** an untrusted party. Shipping a Vega runtime to eat
one re-opens, with a JSON parser in front of it, the exact door `textContent`
closes. The generative-UI consensus in 2026 has landed in the same place: the
safest pattern has the agent select from a fixed library of hand-built
components, and agent-generated markup rendered in the frontend is named as the
most dangerous option.
([CopilotKit](https://www.copilotkit.ai/blog/the-developer-s-guide-to-generative-ui-in-2026))

### The shape

**The model names a chart. It does not draw one and it does not supply the
numbers.**

An MCP tool, `show_chart`, taking a spec that is a Pydantic model with a closed
enum at every choice point:

```
kind      line | bar | small_multiple          (closed enum)
dataset   a NAME from a whitelist              (closed enum, see below)
params    typed arguments to that dataset      (symbol, days, ...)
title     free text, escaped, capped
caption   free text, escaped, capped
```

**`dataset` is a name, never a list of numbers, and that is the load-bearing
clause.** It is `indicators.py`'s rule arriving at the presentation layer: the
model reads figures, it does not derive them, and it certainly does not hand
them over to be drawn. A chart of numbers the model supplied is worse than a
fabricated sentence, because a chart reads as measured — it is the confident
partial answer with a picture of itself attached. The server fetches the figures
the same way the Board does, from the same tools that already exist, and the
model's contribution is *which* question to draw and what to call it.

That also answers "what may it plot" without a separate policy: the datasets are
the tools, whitelisted per soul.

### What each agent may plot

| soul | may plot | may not |
| --- | --- | --- |
| Yoda | open risk by symbol against the cap; risk contribution per position; stop distance vs size at current equity; the gate's rejection reasons as counts | anything that is a time series of realised outcome |
| Armorer | **size vs stop distance at a proposed cap**; risk per trade at the old and new limit; class limit against portfolio limit | the same exclusion |
| Grogu | dream-ledger reasoning counts: sourcing rate, drop rate, hops per chain | anything touching the account at all |

The Armorer's row is the strongest case for building this feature at all. Its
whole job is making a consequence concrete before a limit moves, `settings_agent`
already computes those figures in Python and hands them over, and "here is what
happens to position size as you widen the stop" is a curve that a sentence
genuinely cannot carry. Zero outcome information, maximum explanatory value. If
only one chart ever ships, ship that one.

### Does this risk feeding the track record back?

**Yes, and the whitelist has to be stricter than the current tool surface is.**

Being precise about the existing state: Yoda can already read realised P&L today
through `get_journal_stats` and `get_trades`. So the true claim is not "the model
cannot see the track record" — it is that the souls forbid presenting it as a
lesson, `metrics.py` is kept off the prompt, and the Analytics page is where the
operator reads it.

A charting layer erodes that in two specific ways, and both are worth naming:

- **A shape is more persuasive and more memorable than a table.** An equity curve
  is a track record with a narrative attached. The Board's `_curve` is fine
  because it renders to the operator; the same drawing produced *by the agent, in
  the agent's own reply* is the agent telling a story about performance.
- **The reply is replayed.** `HISTORY_TURNS` is 6, so a chart the agent chose to
  draw comes back into its own context for the next six turns. The reasoning that
  produced it becomes an input to the reasoning that follows it — the transcript-
  replay loop from the memory-poisoning literature, arriving through a feature
  nobody built for that purpose.

So: **no dataset in the whitelist may be a time series of realised outcome, for
any soul.** No equity curve, no cumulative P&L, no win rate over time, no
per-strategy return. Those stay on Analytics. This is stricter than the tools
already allow, deliberately, and the reason is one sentence: a picture is
remembered and a table is read.

### Delivery, so the client never touches markup

The chat POST response gains a `chart_id` — an opaque, server-minted token —
beside `text`. The client sets `img.src = '/chart/' + id + '.svg'`. The only
model-influenced value crossing into the DOM is an id the server generated, and
it lands in an attribute rather than in markup, so the `textContent` rule holds
untouched.

Six properties:

- **`<img>` never executes script**, whatever the SVG contains. That is a
  browser guarantee rather than a sanitiser, which is the kind of defence this
  repository prefers.
- **The route sits behind the auth middleware.** Not in `OPEN_PATHS`, and
  classified in `tests/test_auth.py`. `/live` was missed from that file entirely
  because it "is not a page", and it served every position and every resting
  order. A chart route is the same category of miss waiting to happen.
- **Numbers reaching an SVG attribute are clamped and formatted**, `:.1f` into a
  path `d=`. A NaN or an infinity from a bad dataset produces a broken attribute,
  and an attribute is still an injection surface even with no markup in sight.
  This is `render.STYLES`' backslash trap and the `SYSTEM_PROMPT_TEMPLATE` brace
  trap in a third place: a string being read by something other than what wrote
  it.
- **Every free-text field goes through `_e()`** and is capped. Title and caption
  are the only model-authored strings in the whole drawing.
- **A chart is derived, and never backed up.** Same rule as `insight.db`. The
  store is a cache keyed by id, aged out, and rebuildable by asking again.
- **The caption names the source and the reading time.** A timestamp on a page
  describes the READING, never the render — a chart is the surface where that is
  easiest to get wrong, because a drawing looks current by construction.

**The honest cost:** an SVG referenced by `<img>` cannot read the page's CSS
custom properties, so the theme has to be baked in at render time and a chart
will not follow a theme switch without a re-fetch. The alternative — inline SVG
built on the client with `createElementNS`, attribute by attribute, from the same
validated spec — keeps theming and is more work and more surface. Take the `<img>`
route first, because it is the one where being wrong is cheap.

### Animation: no

The operator asked, so this is a direct answer rather than an omission.

**Do not let a model drive an animation, and the thing it would be for is better
served by a static drawing.**

- The repository's own rule already forbids the useful case. `prefers-reduced-
  motion` has two right answers here — decoration switches **off**, a control
  keeps working and loses only its motion — and `.from-dream` is tested for
  surviving the preference because *anything carrying information must survive
  either way*. An explanatory animation is information that disappears under the
  preference. It fails the rule as written.
- What an explanatory animation would show is a mechanism over a parameter:
  "watch what happens to size as the stop widens". The checkable version of that
  is a **small multiple** — three or five frames side by side, one chart. It can
  be paused, because it never moves; it can be screenshotted; it can be compared
  frame to frame, which is the actual comparison being made and the one an
  animation makes hardest.
- The projection layer already animates, and it is decoration by design, fails to
  visible, and is switched off rather than down under the preference. Adding a
  model-driven animation beside it would put a moving element on the page whose
  content is an argument. Those are different things and the repository already
  keeps them apart.

`small_multiple` is in the `kind` enum above for exactly this reason. It is the
animation, drawn.

---

## Summary of the calls

| question | call |
| --- | --- |
| Self-updating souls | Creed stays static and root-owned. Add a typed, capped, dated notes file per agent, proposed by the agent, applied by the operator, reverted in one command, with no number in it and no type for "what worked". Creed's rail re-emitted after the notes. |
| Updating dreaming files | Extend `DreamLedger` along the reasoning axis. Keep it in `dreams.db`, not Markdown. Feed back the graveyard with the broken hop, capped, and nothing else. Never add an earnings column. |
| Skills | None of the 77 comes back. Write `reading-the-risk-gate`, `querying-the-audit-index` and `out-of-hours-order-mechanics` in this repository. If external is wanted, `anthropics/skills` vendored and pinned, never a registry install. Spend the effort on the `toolsets:` allowlist instead. |
| Graphs | A closed spec the model fills in, over datasets it names but does not supply, rendered to SVG in Python, served as `<img src="/chart/{id}.svg">` behind auth. No outcome time series for any agent. Ship the Armorer's size-vs-stop chart first. |
| Animation | No. `small_multiple` instead — an explanatory animation is information that vanishes under `prefers-reduced-motion`, which this repository already forbids. |

## What is still unknown

- **Whether `souls/` is root-owned on the droplet.** Section 1 rests on it and
  `bootstrap.sh` does not state it. One `ls -l` settles it.
- **Whether a note budget of 300 words is right.** It is a guess anchored to
  `SOUL_MAX_WORDS = 1600` and to the reported 200-line cap that lost 60% of its
  lessons. The measurement that would settle it is watching what the agents
  actually propose over a month.
- **Whether the graveyard earns its tokens.** It is the one feedback path
  recommended here and it costs prompt on every dream run. `DreamLedger` can
  measure whether repeat subjects fall after it ships, which is the right way to
  find out and is not free.
- **Anthropic's Dreaming mechanics beyond the announcement.** It is a gated
  research preview on the Managed Agents API; everything above is from coverage
  rather than from documentation or use, and the 6x is Harvey's internal figure
  reported second-hand.
- **Whether an `<img>`-delivered chart is acceptable in practice.** The theming
  cost is real and the only way to know if it grates is to look at one on the
  Board's palette. That is the `.note.alert` lesson: three CSS collisions in this
  repository were each found by looking at the page, never by a test.

---

## Sources

Self-improvement and memory:

- [VentureBeat — Anthropic introduces "dreaming"](https://venturebeat.com/technology/anthropic-introduces-dreaming-a-system-that-lets-ai-agents-learn-from-their-own-mistakes)
- [Let's Data Science — Dreaming at Code w/ Claude, 6 May 2026](https://letsdatascience.com/blog/anthropic-dreaming-claude-managed-agents-self-improving-may-6)
- [PyMC Labs — Self-improving AI agents: when skill optimization works](https://www.pymc-labs.com/blog-posts/self-improving-ai-agents)
- [A Self-Improving Coding Agent (arXiv 2504.15228)](https://arxiv.org/pdf/2504.15228)
- [Letta — Memory blocks](https://www.letta.com/blog/memory-blocks/)
- [Letta — Agent memory](https://www.letta.com/blog/agent-memory/)
- [Reflexion (arXiv 2303.11366)](https://arxiv.org/abs/2303.11366)
- [Generative Agents — Park et al.](https://dl.acm.org/doi/fullHtml/10.1145/3586183.3606763)

Memory failure modes:

- [Remembering More, Risking More (arXiv 2605.17830)](https://arxiv.org/html/2605.17830v1)
- [MemEvoBench (arXiv 2604.15774)](https://arxiv.org/pdf/2604.15774)
- [Redis — Context poisoning](https://redis.io/blog/context-poisoning-agent-reasoning/)
- [WorkOS — AI agent memory poisoning](https://workos.com/blog/ai-agent-memory-poisoning)
- [alexop.dev — Stop bloating your CLAUDE.md](https://alexop.dev/posts/stop-bloating-your-claude-md-progressive-disclosure-ai-coding-tools/)
- [Claude Code memory architecture](https://ianlpaterson.com/blog/claude-code-memory-architecture/)

Skills and supply chain:

- [Snyk — ToxicSkills](https://snyk.io/blog/toxicskills-malicious-ai-agent-skills-clawhub/)
- [Cloud Security Alliance — SKILL.md agent context poisoning](https://labs.cloudsecurityalliance.org/research/csa-research-note-skill-md-agent-context-poisoning-20260506/)
- [HiddenLayer — Malicious skills in agentic AI](https://www.hiddenlayer.com/research/the-next-ai-supply-chain-risk-malicious-skills-in-agentic-ai)
- ["Do Not Mention This to the User" (arXiv 2602.06547)](https://arxiv.org/pdf/2602.06547)
- [anthropics/skills](https://github.com/anthropics/skills/blob/main/README.md)
- [Agent Skills open standard](https://www.agensi.io/learn/agent-skills-open-standard)

Charts, generative UI and output handling:

- [Vega — XSS via vlSelectionTuples (GHSA-829q-m3qg-ph8r)](https://github.com/vega/vega/security/advisories/GHSA-829q-m3qg-ph8r)
- [Vega — CVE-2025-59840](https://github.com/advisories/GHSA-7f2v-3qq3-vvjf)
- [Vega — event sandbox escape (#3027)](https://github.com/vega/vega/issues/3027)
- [VegaChat (arXiv 2601.15385)](https://arxiv.org/html/2601.15385v1)
- [Databricks — Vega-Lite in multi-agent systems](https://www.databricks.com/blog/bringing-visualizations-life-multi-agent-systems-vega-lite)
- [CopilotKit — Developer's guide to generative UI in 2026](https://www.copilotkit.ai/blog/the-developer-s-guide-to-generative-ui-in-2026)
- [CVE-2026-25516 — NiceGUI ui.markdown() XSS](https://www.sentinelone.com/vulnerability-database/cve-2026-25516/)
- [NiceGUI advisory GHSA-v82v-c5x8-w282](https://github.com/zauberzeug/nicegui/security/advisories/GHSA-v82v-c5x8-w282)
