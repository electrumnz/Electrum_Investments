# Hugging Face, vetted

A sweep of the Hub — models, datasets and Spaces — for anything Mudhorn should
adopt. Written to be decided from, not acted on. Nothing here is implemented and
nothing has been committed.

Searched and verified **11 August 2026** through the Hugging Face MCP tools,
authenticated as `keeceNZVM`. Every repository named below was confirmed to
exist by fetching it; file sizes are read off the Hub's own listing rather than
quoted from a README. Where a figure is a **measurement** it says so and the
command that produced it is described; where it is an estimate it says that too.

> ## The short version
>
> **One item is worth building now, and it is a dataset rather than a model.**
> `meg-tong/sycophancy-eval` gives `tests/test_agent_behaviour.py` a second
> grader whose ground truth is a letter rather than a judge model's opinion.
>
> **Four are worth recording as deferred**, three of them with a measured
> trigger for revisiting.
>
> **The whole financial-ML genre is rejected**, and so is every model whose
> output would have to become a number the model did not read. That is not a
> close call — it is the same sentence `indicators.py` already exists to
> enforce, arriving from a new supplier.
>
> **On embeddings specifically: build SQLite FTS5 first.** It is in the
> standard library on this box, it costs 30 MB of RSS, and it answers all three
> of the queries `CLAUDE.md` names as the reason `insight.py` exists. A static
> embedding model is the right *shape* if that ever fails, and the measurements
> for choosing one are below.
>
> **On the souls: no.** That question is already answered in
> `docs/DROPLET_AI.md` and `docs/MODEL_CALLS.md`, and the answer is a provider
> decision, not a Hub decision. Repeating it here would waste the operator's
> time.
>
> **On voice: not on this box.** A 2 GB droplet cannot host speech synthesis
> beside the bot, the web unit and a Hermes process per message. The only shape
> that clears the disclosure rule is synthesis in the browser from weights the
> droplet serves itself, and even that has an objection that is about figures
> rather than about bytes.

---

## What was already answered elsewhere, and is not re-answered here

Three documents in this directory cover ground a Hub search would otherwise
tread again. They are cited rather than repeated.

- **`docs/DROPLET_AI.md` and `docs/MODEL_CALLS.md`** already decided the
  open-model question for all four model paths, with measurement behind it:
  pin `claude.propose`, route the souls freely, move `claude.dream` first and
  only after proving the schema is *enforced* rather than accepted. The Hub is
  not where that decision gets made — DigitalOcean and Vercel serve the weights,
  and `MODEL_CALLS.md` measured that `ANTHROPIC_BASE_URL` already routes the
  Python path.
- **`docs/INSPIRATION.md`** already found the feed-injection defect, already
  named the deterministic fix (`" ".join(text.split())` plus spotlighting), and
  already recorded four deferrals and a long reject list. Nothing here
  duplicates it.
- **`reference/registry.yaml`** pins TradingAgents, ai-hedge-fund, FinRL,
  nautilus_trader, backtesting.py, vectorbt, letta and alpaca-mcp-server with
  reasoning attached.

---

## What could not be checked

Three limits, stated because a negative result is worth less than it looks.

- **The Hub's keyword search is weak on exact names and silently returns
  nothing.** `"text-to-speech english"` with a `text-to-speech` filter returned
  zero repositories; `"time series forecasting foundation model"` returned zero;
  `"sentence embedding small english"` sorted by downloads returned zero. Every
  one of those categories is densely populated. So **"nothing found" below never
  means "nothing exists"** — it means the search available here did not surface
  it, and the well-known repositories in this document were reached by looking
  them up by id rather than by finding them.
- **The Dataset Viewer is broken for the one dataset recommended below.**
  `meg-tong/sycophancy-eval` reports `Failed parquet jobs: 1` and the schema
  preview returns `500 Internal Server Error: The dataset generation failed`.
  The files themselves are present and were listed directly; the first two
  records of `are_you_sure.jsonl` were read to confirm the shape. Anything
  using this has to fetch the `.jsonl` rather than `load_dataset`.
- **No measurement was taken on the droplet.** Every timing below is from this
  development container — 16 GB RAM, Python 3.11.15, SQLite 3.45.1. A $12
  DigitalOcean droplet has less memory and a slower core, so **treat the RSS
  figures as a floor and the latencies as optimistic.** The RSS figures are peak
  (`ru_maxrss`), which includes transient load buffers.

---

## Worth building now

There is one.

### A second grader for the behaviour suite, whose ground truth is a letter

`tests/test_agent_behaviour.py` is the strongest thing in this repository that
nothing else guards, and it has one structural weakness its own docstrings do
not name: **every live verdict comes from a single judge model.** The file is
careful about the failure modes it can see — an empty reply graded as a breach,
an unparsed verdict kept apart from a pass, a judge that must differ from the
agent — but "the judge was wrong" has no detector at all.

**`meg-tong/sycophancy-eval`** — https://hf.co/datasets/meg-tong/sycophancy-eval
— fixes a slice of that. MIT licence, four `.jsonl` files, from Anthropic's own
*Towards Understanding Sycophancy in Language Models* (arXiv:2310.13548).
Verified 11 August 2026 by listing the repository and reading the first records:

| file | size |
|---|---:|
| `answer.jsonl` | 6.85 MB |
| `are_you_sure.jsonl` | 5.82 MB |
| `feedback.jsonl` | 22.3 MB |
| `mimicry.jsonl` | 0.58 MB |

**`are_you_sure.jsonl` is the one that matters, and its shape is the whole
point.** Each record is a multiple-choice question (AQuA maths, in the rows
read) carrying `base.correct_letter` — the right answer, as a letter. The
sycophancy is applied by the harness: the model answers, the user says *are you
sure?*, and what is measured is whether a **correct** answer gets abandoned
under nothing but social pressure.

**No judge is required to score that.** The letter either changed or it did
not. That is the "something other than one judge model" the brief asked for,
and it arrives in exactly the shape this repository already trusts: a
pre-registered claim, graded by comparison against a recorded fact, with no
counterfactual and no sample to overfit to. It is the `triggers.py` principle
applied to the agent instead of to the watch list.

**What it grades, stated precisely, because overstating it would be the failure
this repository refuses.** It grades *capitulation under pressure on a question
with a right answer*. It does **not** grade any of the fifteen rails in
`RAILS_UNDER_TEST`; those are repo-specific and no public dataset can touch
them. What it produces is a **calibration figure for the model tier**, which is
the thing currently assumed rather than checked:

- `A2-operator-insists-must-do-it` requires the Armorer to *stop arguing* once
  told twice. `A1-loosen-mid-losing-run-pushback` requires it to *not fold*
  before that. Those two rails sit either side of a line, and the model's
  baseline tendency to fold under "are you sure?" is the thing that decides
  which side it errs towards. Today that tendency is unmeasured.
- `Y1-make-a-refused-trade-fit` is a user pushing back on a correct refusal.
  Structurally identical.
- If the souls ever move to a cheaper model — which `docs/DROPLET_AI.md` says to
  do for Hermes — this is the number that would tell you what you bought.

**Sketch of what it takes.** Small, and it lands entirely in the half that is
already outside the repository:

1. `scripts/agent_behaviour_live.py` gains a `capitulation` section. Fetch
   `are_you_sure.jsonl` over HTTPS (not `load_dataset` — the Viewer is broken),
   sample ~100 records, put each to the agent model, then put the follow-up,
   and record both letters and the `correct_letter`.
2. It writes into the same transcript the breach and alignment sections already
   use, so the offline half replays it with no key and no network — the
   arrangement `tests/test_agent_behaviour.py` already argues for at length.
3. Two assertions in the replay: the section exists and covers the sample size
   recorded, and **the flip rate on correct answers is reported rather than
   thresholded**. Do not put a pass/fail bar on it in the first commit. A
   threshold chosen before anyone has seen a reading is a limit nobody agreed
   to, which is the mistake `CLAUDE.md` names about stop placement.

**Cost:** one HTTPS fetch in a script that already makes model calls, ~100 extra
calls per capture, 5.8 MB downloaded. No new runtime dependency, nothing added
to the droplet, nothing in the trading path.

**One caveat to carry.** The corpus is AQuA maths from 2023 and the models have
almost certainly seen it. That does not invalidate the measurement — what is
being measured is whether the answer *changes* when challenged, not whether it
was right — but it does mean the absolute accuracy figure is meaningless and
only the flip rate should be recorded. This is the parametric look-ahead point
`docs/INSPIRATION.md` already makes about backtests, in a new place.

---

## Worth recording as deferred

### Static embeddings over the derived index — but FTS5 first, and here are the numbers

`insight.py` is exactly the right home for semantic search: derived,
rebuildable, never backed up, read by nothing that trades, already handing an
agent read-only SQL. Adding vectors there breaks no rule in this repository.

**The question is whether it beats what is already free, and it was measured
rather than argued.**

**Baseline — SQLite FTS5.** Measured on this box: `python3 -c` reports
`sqlite3.sqlite_version` 3.45.1 and `CREATE VIRTUAL TABLE … USING fts5` succeeds,
so FTS5 is compiled into the stdlib module already installed. Indexing 50,000
synthetic decision-shaped documents took **0.30 s**, produced a **14.1 MB** file,
peaked at **30.0 MB RSS**, and a `MATCH … ORDER BY rank` query (BM25) returned in
**23.9 ms**. Zero new dependencies, zero bytes on the droplet, nothing to fetch
from a third party.

**Note what the three motivating queries in `CLAUDE.md` actually are:** *"what
did we decide about AAPL in March"*, *"which rejection reason fires most often"*,
*"how many watches named no trigger"*. All three are a symbol, a date range, a
`GROUP BY` and a `COUNT`. **SQL already answers them exactly**, and an embedding
would answer them approximately. That is the finding, and it is the reason this
is deferred rather than built.

**If it is ever wanted anyway, this is the model.** Not a transformer.
`model2vec` static embeddings do a token lookup and a weighted average — no
forward pass — and the wheel metadata (read out of
`model2vec-0.8.2-py3-none-any.whl`, MIT) requires only `jinja2 joblib numpy
safetensors tokenizers tqdm`. **No torch.** A `python3 -m venv` with it
installed measured **150 MB on disk**.

Measured on this box, encoding 5,000 decision-shaped documents and 200 single
queries:

| model | Hub id | licence | peak RSS | dim | throughput | 1 query |
|---|---|---|---:|---:|---:|---:|
| potion-base-8M | [`minishlab/potion-base-8M`](https://hf.co/minishlab/potion-base-8M) | MIT | **150.9 MB** | 256 | 24,956 docs/s | **0.11 ms** |
| potion-retrieval-32M | [`minishlab/potion-retrieval-32M`](https://hf.co/minishlab/potion-retrieval-32M) | MIT | 350.5 MB | 512 | 20,148 docs/s | 0.10 ms |
| all-MiniLM-L6-v2, int8 ONNX | [`sentence-transformers/all-MiniLM-L6-v2`](https://hf.co/sentence-transformers/all-MiniLM-L6-v2) | Apache-2.0 | **477.3 MB** | 384 | **97 docs/s** | **13.03 ms** |

The MiniLM row is the one worth staring at. It is the obvious default answer,
its `onnx/model_qint8_avx512.onnx` is only 23.0 MB on disk, and it is **257×
slower per document and 118× slower per query than a static model, at 3.2× the
memory**, because `onnxruntime` has to be loaded and a real transformer has to
run. On a 2 GB droplet sharing memory with the bot, the web unit and a Hermes
process per chat message, 477 MB is not a rounding error.

The trade is quality: `potion-retrieval-32M`'s own model card puts it at
**35.06 MTEB Retrieval against all-MiniLM-L6-v2's 42.92**, which it describes as
81.69% of the performance. For searching a private audit log that is almost
certainly fine, and it is not the binding consideration anyway.

**The real cost is the vector store, not the model.** At 200,000 indexed rows —
roughly a year of six assessments per cycle — float32 vectors are **205 MB at
dim 256** and **410 MB at dim 512**, computed from the measured dimensions. That
has to be memory-mapped or int8-quantised or it will not sit on this box at all.
`potion-base-2M` ([`minishlab/potion-base-2M`](https://hf.co/minishlab/potion-base-2M),
MIT, 1.9M params) exists if even that is too much.

**The trigger for revisiting**, stated so it is a decision rather than a mood:
build FTS5, and revisit only when an operator or an agent has a *recorded*
question that FTS5 answered badly — not a hypothetical one. The audit log makes
that checkable, which is unusual and worth using.

### A tiny reranker, as a second stage and never a first one

If FTS5 recall turns out to be fine and the *ranking* is what disappoints, the
fix is a cross-encoder over the top-N, not a different retriever.

[`cross-encoder/ettin-reranker-17m-v1`](https://hf.co/cross-encoder/ettin-reranker-17m-v1)
— Apache-2.0, ONNX included, `onnx/model_qint8_avx512.onnx` is **17.1 MB**
(fp32 is 67.3 MB). Measured on this box with `onnxruntime` and `tokenizers`:
session ready at **151.9 MB RSS**, and scoring **50 query–document pairs took
124.5 ms**, peaking at **213.3 MB**. Adding `onnxruntime` to the venv took it
from 150 MB to **210 MB** on disk.

`cross-encoder/ettin-reranker-32m-v1` and `-150m-v1` exist under the same
licence if quality is short; they will be proportionally slower.

**Deferred, not recommended.** 124 ms and 213 MB to reorder a list SQL already
returned is a lot to spend on a chat turn, and it is spent on every turn. It
becomes reasonable only after there is evidence the ordering is the problem.

### Voice, and the only shape of it that clears the disclosure rule

The operator reads this on a phone and the three agents are *characters*, so a
voice is the obvious extension. The candidates are real and were verified:

| model | Hub id | licence | file |
|---|---|---|---:|
| Kitten TTS nano | [`KittenML/kitten-tts-nano-0.1`](https://hf.co/KittenML/kitten-tts-nano-0.1) | Apache-2.0 | **23.8 MB** ONNX + 10 KB voices |
| Kokoro 82M ONNX | [`onnx-community/Kokoro-82M-v1.0-ONNX`](https://hf.co/onnx-community/Kokoro-82M-v1.0-ONNX) | Apache-2.0 | 86.0 MB (`q8f16`) – 325.5 MB (fp32), + ~0.5 MB per voice |
| Kokoro 82M (source) | [`hexgrad/Kokoro-82M`](https://hf.co/hexgrad/Kokoro-82M) | Apache-2.0 | — |
| Piper, en_US amy medium | [`rhasspy/piper-voices`](https://hf.co/rhasspy/piper-voices) | repo MIT, **per-voice varies** | 63.2 MB ONNX |

**The Piper licence is a trap and it is worth naming.** The repository is tagged
`license:mit`, and the per-voice `MODEL_CARD` for `en_US/amy/medium` — read
directly — says only *"Dataset URL: …mimic3-voices, License: See URL"*. The
repository licence covers the packaging, not every voice's training data. For a
single-operator paper-trading tool that is unlikely to matter, but it is
exactly the shape of thing that stops being fine the day something is published.
Kokoro and Kitten TTS both state Apache-2.0 on the model itself.

**Now the hard constraint, which decides it.**

- **Server-side TTS does not fit on this box.** Not measured directly, but the
  MiniLM row above is the honest proxy: a **23 MB** int8 ONNX model cost
  **477 MB** of peak RSS under `onnxruntime` in a bare process. Kokoro's
  smallest useful export is **86 MB**. There is no arrangement where that sits
  beside the bot, the web unit and a `hermes -z` process on 2 GB. **This idea
  needs a bigger droplet; it does not fit the current one**, and saying
  otherwise would be the confident-partial-answer failure with a size attached.
- **A cloud TTS endpoint is refused outright.** Every page carrying a voice
  button also carries equity, buying power, open risk and every position. An
  endpoint that hears *"open risk is nine hundred and eighty dollars"* is a
  disclosure of the account to a third party that the operator has not agreed
  to, and it is a *worse* disclosure than the dashboard's own exposure, because
  the dashboard is behind `DASHBOARD_PASSWORD` and the endpoint is behind
  somebody else's terms of service. `CLAUDE.md` already refuses to *render*
  credentials on the Settings page on the grounds that "a screenshot travels".
  Audio travels further.
- **So the only shape is synthesis in the browser, from weights the droplet
  serves itself.** The text is already on the device — the page rendered it. The
  audio never exists anywhere else. And the weights come from `/static/` on the
  same origin rather than from a CDN, which keeps the threat model unchanged.
  `Supertone/supertonic-3` and `webml-community/KittenTTS-web` both demonstrate
  the mechanism (see Spaces, below).

**And here is the objection that is about figures rather than bytes, which is
why this is deferred even in that shape.** *A soul shapes framing and never
touches a figure.* A speech synthesiser's number expansion is a restatement:
`773.324285` and `$980.19` become words chosen by a phonemiser nobody in this
repository wrote, and a stale reading read aloud in a confident voice loses the
one thing `taken_at` exists to preserve — that a figure describes a *reading*,
not the present moment. The rule that follows, if this is ever built:

- **Only prose is spoken. A figure is never synthesised.** The button reads the
  agent's sentences; numbers are cut out of the utterance, or the button is not
  built. That sounds like a limitation and is the point.
- **Never autoplay, and the button is a control rather than decoration.**
  `prefers-reduced-motion` does not cover audio and there is no equivalent
  query, so the rule has to be a design one: audio starts only when the operator
  presses something. That places it on the *control* side of the two right
  answers — like the Cmd+K console, not like the starfield — so it keeps working
  under any preference and simply never starts by itself.

---

## Rejected, with the reason and the tell

**Speech-to-text on the phone.** Verified and real:
[`onnx-community/moonshine-base-ONNX`](https://hf.co/onnx-community/moonshine-base-ONNX)
(MIT; `encoder_model_quantized.onnx` 20.5 MB + `decoder_model_merged_quantized.onnx`
42.4 MB ≈ **63 MB**), [`moonshine-ai/moonshine-base`](https://hf.co/moonshine-ai/moonshine-base)
(MIT, 61.5M params), [`openai/whisper-tiny.en`](https://hf.co/openai/whisper-tiny.en)
(Apache-2.0, 37.8M), [`onnx-community/whisper-base.en`](https://hf.co/onnx-community/whisper-base.en).
**Rejected because the phone already does this.** iOS dictation runs on-device
from the keyboard, at zero download and zero cost, into any `<textarea>` —
including the one on the Chat page. Shipping a 63 MB WebAssembly download to
replace a key that is already on the keyboard is not an improvement. The tell is
that the requirement was never "transcribe audio", it was "ask a question
without typing", and one of those is already solved.

**A third-party weight fetch into the dashboard page.** This is the reason the
in-browser recommendations above all say *serve the weights from the droplet*.
Today the page's supply chain is exactly one host: itself. Everything
model-written reaches it through `textContent` or server-side escaping and never
`innerHTML`; there is no `fetch` to anywhere. Pointing `transformers.js` at a
CDN adds a **remote origin that can execute in the page**, on a surface that
holds live account state behind one shared password — and it does it for
decoration. The size makes it worse rather than better: 63 MB for STT, 86 MB for
Kokoro, and in-browser LLMs
([`LiquidAI/LFM2.5-2.6B-WebGPU`](https://hf.co/spaces/LiquidAI/LFM2.5-2.6B-WebGPU),
[`webml-community/deepseek-r1-webgpu`](https://hf.co/spaces/webml-community/deepseek-r1-webgpu))
are gigabytes. On a phone on mobile data that is not a responsiveness
improvement.

**Financial sentiment classifiers, as a category.** Verified:
[`ProsusAI/finbert`](https://hf.co/ProsusAI/finbert) (113.0M downloads, 1,214
likes), [`mrm8488/distilroberta-finetuned-financial-news-sentiment-analysis`](https://hf.co/mrm8488/distilroberta-finetuned-financial-news-sentiment-analysis)
(Apache-2.0), [`mrm8488/deberta-v3-ft-financial-news-sentiment-analysis`](https://hf.co/mrm8488/deberta-v3-ft-financial-news-sentiment-analysis)
(MIT), and a dozen more. **Rejected on the rule, not on quality.** A sentiment
score has exactly two destinations and both are closed: it either influences a
limit or a blackout, which is *"the model thought this post sounded bearish"*
wearing a number's clothes and is forbidden because `RiskGate` must stay
deterministic; or it enters the prompt, which makes it a figure the model reads
and cannot check, produced by a model nobody audited, in a document where
`indicators.py` computes every other number in Python precisely so that cannot
happen.

Two tells worth recording anyway. **`ProsusAI/finbert` declares no licence at
all** — the metadata has a language and no `license:` tag — which for the
most-downloaded model in the category is a real problem for anything that is not
purely personal. And nearly every model in this genre is fine-tuned on
`financial_phrasebank`: 4,846 sentences from Finnish company reports, annotated
in 2014, being asked to score 2026 wire headlines.

**Time-series foundation models.**
[`amazon/chronos-bolt-small`](https://hf.co/amazon/chronos-bolt-small)
(Apache-2.0, 47.7M params, 25.4M downloads) is the well-made representative;
the category is large. **Rejected under the sentence this repository is built
around: the model reads figures, it does not derive them.** A forecast is a
derived number with a confidence interval, produced by weights, that no line of
Python in this repository can check — and `RiskGate` would approve a trade sized
against it exactly as readily as one sized against a real ATR, because the gate
checks arithmetic and not provenance. That is the Alpha Arena failure arriving
through the data layer, which is the case `CLAUDE.md` makes about putting bars
in the prompt, with the arithmetic done by somebody else's model instead of by
Claude.

Note the tell in the neighbourhood rather than in the model: Chronos' own demo
Spaces include `crypto-futures-calculator`, `btc-price-forecasting` and
`crypto-price-predictor`. Amazon published a general forecasting model; what the
ecosystem does with it is the genre `docs/INSPIRATION.md` already described.

**Prompt-injection classifiers.**
[`protectai/deberta-v3-base-prompt-injection-v2`](https://hf.co/protectai/deberta-v3-base-prompt-injection-v2)
(Apache-2.0, 184.4M params, 6.0M downloads) and
[`vijil/vijil_dome_prompt_injection_detection`](https://hf.co/vijil/vijil_dome_prompt_injection_detection)
(Apache-2.0) both exist and are maintained. **Rejected because
`docs/INSPIRATION.md` already has the fix and it costs nothing.** Collapsing
whitespace in `marketaux._parse` is two lines and removes the channel; a
184M-parameter classifier deciding which headlines the trading model is allowed
to see is a model output silently editing the model's input, on the loop that
proposes orders. *Missing stays missing* — a feed that drops items is worse than
one that reports a gap, and a classifier with a false positive drops items
without saying so. If it were ever wanted, the only defensible arrangement is to
**flag and never filter**, and at that point it is a 184M-parameter model
producing a note nobody acts on.

**Finance-tuned embeddings.**
[`FinLang/finance-embeddings-investopedia`](https://hf.co/FinLang/finance-embeddings-investopedia)
— **cc-by-nc-4.0**. Two reasons: the non-commercial licence is a live constraint
the moment this stops being personal, and there is no reason to want
finance-domain embeddings for the actual corpus, which is this repository's own
audit prose about caps, stops and rejections.

**The `jina-embeddings-v5` family** (`jinaai/jina-embeddings-v5-text-small-retrieval`,
`-nano-retrieval`, `-omni-*`) — all **cc-by-nc-4.0**. Technically strong and
recently published; the licence alone rules them out for anything with a
commercial future, and `potion-base-8M` is MIT and faster.

**Gradio and Streamlit Spaces as a UI stack.** Not adoptable and it is not
close. `src/bot/web/` is server-rendered HTML strings built in Python, with no
build step, no framework, and inline `<style>`/`<script>` only — a design
`CLAUDE.md` defends at length (the fail-to-visible rule, the no-backslash rule
in `render.STYLES`, the two-class declaration-order rule). A Gradio app is a
different program. This category is answered under Spaces below.

**`ai-safety-institute/AgentHarm`** (https://hf.co/datasets/ai-safety-institute/AgentHarm,
`license:other`, arXiv:2410.09024, 4.2K downloads, 58 likes) — verified, and the
best-made agent-safety benchmark found. **Rejected as a dependency, worth one
read.** It measures whether an agent will carry out harmful *tasks* — fraud,
cybercrime, harassment. Mudhorn's rails are the opposite kind of thing: don't
state a number you did not read, don't argue with the gate, don't raise a dream
to be agreeable, push back without refusing. A generic harmfulness score would
be green while every one of those was breached.

The same applies to the general jailbreak corpora, all verified and all
off-target here: [`JailbreakBench/JBB-Behaviors`](https://hf.co/datasets/JailbreakBench/JBB-Behaviors)
(MIT), [`TrustAIRLab/in-the-wild-jailbreak-prompts`](https://hf.co/datasets/TrustAIRLab/in-the-wild-jailbreak-prompts)
(MIT), [`jackhhao/jailbreak-classification`](https://hf.co/datasets/jackhhao/jailbreak-classification)
(Apache-2.0). They grade refusal of harmful content. Nothing an operator asks
Yoda is harmful; the dangerous request is *"just give me a ballpark"*.

---

## Spaces: one worth reading, and the rest are the wrong stack

The honest framing the brief supplies is right — a Space cannot be adopted here,
only learned from. So the only question is whether any demonstrates an
*interaction* worth copying.

**Nearly none, and the reason is structural: most are `sdk: gradio`,** which
means the interaction being demonstrated is Gradio's rather than the author's.
`hexgrad/Kokoro-TTS` (3,426 likes) is a Gradio app; there is nothing in it that
transfers.

**The exception is the `sdk: static` ones, and one of them is genuinely
readable.** Verified by listing the repositories:

- **[`Supertone/supertonic-3`](https://hf.co/spaces/Supertone/supertonic-3)**
  (static, 263 likes) — `index.html` 21.5 KB, `script.js` 133.9 KB,
  `styles.css` 35.8 KB, **unbundled and unminified**. This is the one worth an
  hour: it is a working demonstration of on-device speech synthesis wired into a
  plain page with no framework, which is the exact constraint `src/bot/web/`
  operates under. Read it if voice is ever attempted.
- **[`webml-community/moonshine-web`](https://hf.co/spaces/webml-community/moonshine-web)**
  (static) — `index.html` is only 4.4 KB with everything else under `assets/`,
  i.e. a bundler's output. It demonstrates the capability and teaches nothing
  about how.
- **[`mistralai/Voxtral-Realtime-WebGPU`](https://hf.co/spaces/mistralai/Voxtral-Realtime-WebGPU)**
  (static, 145 likes) and **[`webml-community/KittenTTS-web`](https://hf.co/spaces/webml-community/KittenTTS-web)**
  — both confirm in-browser audio is real in 2026, both are demos rather than
  references.

**Nothing was found that demonstrates the interaction actually worth copying:**
streaming an agent's reply with its tool calls appearing as they happen. Searched
for it directly and the results were chatbots. That is a finding rather than a
gap in the search — this dashboard's `live.py` already streams over SSE with the
property most such demos get wrong (*the client may only UPDATE a figure the
server already rendered*), and `CLAUDE.md` records that the fail-to-visible
arrangement is the opposite of the obvious one. There is nothing to borrow.

**One Space is worth bookmarking for a decision rather than for code:**
[`mteb/leaderboard`](https://hf.co/spaces/mteb/leaderboard), which is where the
retrieval scores quoted above are maintained.

---

## Categories that came back empty

Reported because an empty category is an answer.

**A dataset that grades Mudhorn's rails.** None, and none can exist. The fifteen
entries in `RAILS_UNDER_TEST` are clauses in three files in this repository,
about this account, this gate and this journal. "Never state a figure you did
not read" is gradeable only against what was actually read, which is in
`audit/*.jsonl` on the droplet. **The transcript-and-replay arrangement already
in `tests/test_agent_behaviour.py` is the right answer and there is no public
substitute for it.** The sycophancy dataset above supplements it with a
calibration figure; it does not replace it, and describing it as a replacement
would be the overclaim this repository keeps catching itself in.

**Anything on the Hub for the risk gate.** Correctly empty. A gate that is
deterministic Python, cannot be persuaded, and must not fail open has no use for
weights of any kind. `docs/INSPIRATION.md` already searched for an off-the-shelf
gate and found nothing worth importing; the Hub is a worse place to look than
GitHub was.

**Financial datasets worth having.** Searched and rejected as a category rather
than item by item. The bot trades six symbols against a live Alpaca feed with
indicators computed in Python; there is no training here, no backtest harness by
deliberate choice, and no model being fitted. A dataset is an input to fitting
something, and nothing here is fitted.

**In-browser inference that improves this dashboard.** Empty on the merits, not
for want of options. Every candidate trades a byte cost and a new origin for a
capability the page does not currently want, and the two that sound most useful
— dictation and reading aloud — are respectively already solved by the phone and
blocked by the figure-restatement rule.

---

## Licence summary

Everything named above, since a licence problem is cheapest to notice now.

| Repo | Licence | Problem for a personal trading tool? |
|---|---|---|
| `minishlab/potion-base-8M`, `-2M`, `potion-retrieval-32M` | MIT | No |
| `sentence-transformers/all-MiniLM-L6-v2` | Apache-2.0 | No |
| `cross-encoder/ettin-reranker-17m-v1` (and 32m/150m) | Apache-2.0 | No |
| `meg-tong/sycophancy-eval` | MIT | No |
| `KittenML/kitten-tts-nano-0.1` | Apache-2.0 | No |
| `hexgrad/Kokoro-82M`, `onnx-community/Kokoro-82M-v1.0-ONNX` | Apache-2.0 | No |
| `moonshine-ai/moonshine-base`, `onnx-community/moonshine-base-ONNX` | MIT | No |
| `openai/whisper-tiny.en`, `onnx-community/whisper-base.en` | Apache-2.0 | No |
| `rhasspy/piper-voices` | repo MIT; **per-voice dataset licence varies** | **Check the voice's `MODEL_CARD`** |
| `ai-safety-institute/AgentHarm` | `license:other` | Read the terms before any use |
| `FinLang/finance-embeddings-investopedia` | **cc-by-nc-4.0** | **Non-commercial** |
| `jinaai/jina-embeddings-v5-*` | **cc-by-nc-4.0** | **Non-commercial** |
| `ProsusAI/finbert` | **none declared** | **No licence at all** |
| `amazon/chronos-bolt-small` | Apache-2.0 | No — rejected on the rule, not the licence |
| `protectai/deberta-v3-base-prompt-injection-v2` | Apache-2.0 | No — rejected on the rule |

`CLAUDE.md` notes that the non-permissive licences in `reference/` are not a
constraint under the single-operator, personal-use assumption. The two
`cc-by-nc-4.0` rows and the undeclared one are flagged anyway, because they are
the rows that stop being fine the day that assumption changes — and the
paragraph recording that assumption already names the moment it might.

---

## If the answer is "almost nothing", that is defensible

One dataset, worth a small amount of work in a script that already exists
outside the repository. Four deferrals, three of them with a measured number
attached so the next person can decide rather than re-search. A long reject
list, most of which is rejected by one sentence this repository wrote down years
before Hugging Face was searched: **the model reads figures; it does not derive
them.**

The genre on the Hub is what `docs/INSPIRATION.md` found on GitHub — sentiment
scores and price forecasts with nothing between them and an account. The
difference is that the Hub's version is better engineered, which makes it more
tempting rather than less.

The one thing this sweep did change is the shape of the embedding question. It
went in assuming a small transformer was the answer and came out with a
measurement saying a static model is 257× faster at a third of the memory — and
that the right first move is a virtual table that ships with Python.
