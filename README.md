# Signal Stack

> **TL;DR**: Signal Stack remembers every competitor signal (typed, dated, ordered) and reasons across them to infer intent and predict the next move. It refuses to fabricate when evidence is insufficient (5-signal floor, longest gap ≤ 3× median), and it is honest when evidence goes stale. Seeded, deterministic, and verifiable offline.

## Live demo

- App: https://signalstack-frontend.onrender.com
- API: https://signalstack-backend-aca5.onrender.com

**60-second demo**

1. Select _Nimbus AI_ → switch to "Full timeline" (12 signals).
2. Click **🧠 Get Strategic Read** → see a dated, falsifiable prediction grounded in the timeline.
3. Switch to _Vertex Cloud_ (4 signals) → read refuses with `confidence: none` and names the missing evidence.
4. Switch to _Brightline Retail_ → stale evidence is acknowledged (84d quiet) rather than projected.

**Key sections:** [The problem](#the-problem) · [How Hindsight memory is used](#how-hindsight-memory-is-used) · [Demo script](#demo-script) · [Known limitations](#known-limitations) · [Audit evidence](audit/) · [Verifying without an API key](#verifying-without-an-api-key)

---

**A competitive intelligence agent that remembers every competitor signal over time, and connects them into a strategic story that gets sharper the longer it watches.**

Most competitor tracking is a list of disconnected facts. Signal Stack remembers _every_ signal — pricing changes, feature launches, hiring spikes, messaging shifts, funding rounds — and reasons across signal **types** to infer intent and predict the next move.

A single price cut is just a price cut. Three price cuts in six months, each following a funding round, is a strategy.

_Built with an AI coding agent. The rules, the offline suite and the audits in this README were written and revised in collaboration with one; every number quoted here was produced by running the code, not by asking a model what it thought the answer was._

---

## The problem

Teams track competitors sporadically: a Slack message here, a pricing-page screenshot there. The signals land in a doc nobody re-reads. No current tool holds signals long enough, or reasons across them well enough, to surface the pattern automatically.

Three things go wrong in the naive version of this product:

| Naive approach                         | Why it fails                                                                                                                                       |
| -------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| A scraper + a database, read on demand | Nothing accumulates. Day 1 and day 400 look the same.                                                                                              |
| Summarize each event and list it back  | The user gets a feed, not a conclusion. The cross-type chain is never stated.                                                                      |
| RAG with top-k similarity retrieval    | **Silently drops the oldest, least-similar memories** — which is exactly where the beginning of a six-month chain lives. The pattern is invisible. |

The third one is the trap, and it is why this project is memory-first rather than scraper-first.

---

## How Hindsight memory is used

This is the load-bearing part of the system. **There is no sidecar database.** Every signal Signal Stack knows lives in a Hindsight memory bank, and the product's value is a direct function of how much is in there.

**1. One memory bank per competitor = the namespace.**

`backend/hindsight_client.py` maps a competitor to a bank id:

```
"Nimbus AI"  ->  competitor-nimbus-ai
"Vertex Cloud" -> competitor-vertex-cloud
```

A Hindsight bank is an isolated memory store. Using banks as namespaces means isolation, entity graphing, and temporal indexing come from the memory layer rather than from a schema we maintain. `GET /v1/default/banks` is also the source of truth for the competitor list — no second registry to drift. (`data/competitors.json` exists only to preserve original casing, like "Nimbus AI" rather than "nimbus ai".)

**2. Writes are LLM-extracted, metadata-tagged, and idempotent.**

Each signal is retained with `POST /v1/default/banks/{bank_id}/memories`:

- `content` — the signal rendered as **one declarative sentence**, because Hindsight extracts _facts_ from retained content. One sentence in, one fact out, so the timeline stays one-entry-per-signal instead of fragmenting.
- `timestamp` — the signal's own event date, not ingest time. This is what makes Hindsight's temporal recall and ordering correct.
- `context` — `competitive intelligence signal (pricing)`, which is injected into the extraction prompt and actively shapes what gets extracted.
- `metadata` — `signal_uid`, `signal_type`, `signal_date`, `signal_summary`, `source`. Metadata propagates down to the extracted memory units, which is how we map a memory unit back to a typed, dated signal.
- `tags` — `signal` and `type:pricing`, used to scope retrieval to our own writes.
- `document_id` — deterministic (`sha1(competitor|date|type)`). Re-running the seeder **replaces** those documents instead of duplicating them, so seeding is safe to repeat.
- The bank carries a `retain_mission` telling Hindsight's extractor to keep exactly one dated fact per item, not to merge or embellish.

**3. Retrieval is chronological-complete, not top-k.**

This is the decision the whole product rests on, in `get_timeline()`:

```python
# GET /v1/default/banks/{bank_id}/memories/list   <- the LIST endpoint
# pages through the complete result set, then sorts by date ascending
```

We deliberately do **not** use `/memories/recall`, which is top-k semantic + graph + temporal search. Recall is built to return the _k most relevant_ memories, and a February funding round is not semantically similar to a July pricing page. Recall would return the recent enterprise-messaging cluster and quietly omit the raise that caused it — the chain would look unmotivated and the agent would have no strategy to find.

We also deliberately do not pass `time_field`: rows with no value on the chosen time column are excluded from both filtering and ordering, which is the opposite of completeness. Sorting happens client-side.

Pagination is paged to exhaustion with a ceiling (`MAX_TIMELINE_PAGES`), then deduped by `signal_uid` (the extractor can emit more than one fact per document) and sorted oldest-first.

**4. The read is grounded in the retrieved timeline, and refuses to invent.**

`backend/synthesis.py` formats the complete timeline as a numbered, dated list and uses the prompt from the brief verbatim, plus a grounding addendum that enforces two things:

- every claim cites a date that exists in the timeline;
- **whether the evidence is sufficient is decided by the timeline's shape, not by the model's willingness.** A timeline is sufficient when it has **at least 5 signals** and its **longest gap is within 3x its median interval**. Below the floor, the only accepted answer is a refusal (`confidence: none`); at or above it, a refusal is **rejected**. Above the floor with nothing repeating, confidence is capped at `medium` and `missing_evidence` must disclose that no transition has repeated.

That second rule is what makes the contrast demo work, and it is enforced in code rather than left to the prompt. Ask about a competitor with four uncorrelated signals and the agent reports that it cannot distinguish a strategy from routine maintenance — instead of manufacturing one to fill the shape of the answer. Conversely, a rich timeline is not allowed to decline: Palisade Security has 11 signals and no repeated transition pair, and it still returns a forecast, because "nothing has repeated" is a fact about the evidence and not a reason to refuse.

**4b. The read knows what day it is, and is honest when the evidence has gone stale.**

The model only ever sees dates that appear in the timeline, so with no reference point it anchors every forecast to the _last logged signal_. That produced a live, fully-grounded prediction with a deadline that had already passed — "will announce RBAC by ~2026-09-20", generated on the 28th. Every signal it cited was real and the cadence was genuinely 2–4 weeks; the forecast was simply about a window that had closed. A reader skims the confident date and stops there.

`backend/synthesis.py` now hands the model an explicit clock and a freshness verdict:

```
TIME REFERENCE: Today is 2026-09-28. The most recent signal is dated 2026-08-19,
40 day(s) ago, well beyond this competitor's usual ~14-day signal rhythm. The
evidence is STALE: the timeline may no longer reflect what this competitor is
doing, and a forecast drawn from it carries that uncertainty.
```

"Stale" is judged **against the competitor's own rhythm**, not a fixed day count — the median gap between its own signals, at 1× fresh, 2× aging, beyond that stale. A company that ships weekly and one that ships twice a year are not comparable against a shared threshold. On the seeded data this separates cleanly: Nimbus AI (14-day rhythm, 40 days silent) is stale, while Vertex Cloud (76-day rhythm, 47 days quiet) and Pathfinder Labs (70-day rhythm, 54 days quiet) are both still current — they are simply infrequent, and a fixed threshold would have flagged all three.

The rules that follow from it: forecast from _today_, never present a past date as a future deadline, and when the evidence is stale, disclose that in the prediction and lead the recommendation with the step that **refreshes** the intelligence rather than the step that acts on it. Staleness is never a reason to refuse — the pattern analysis is still the valuable output; it just stops masquerading as current.

`SynthesisResponse` carries `data_as_of`, `evidence_age_days` and `evidence_staleness` as provenance, and the UI shows a warning banner when the evidence is stale, so the reader sees the caveat before the forecast rather than after it. The no-LLM fallback path reports the same provenance.

**5. Tag scoping uses `all_strict`, and observation consolidation is switched off.**

Two things were found by running against the live API, not by reading the spec.

`GET .../memories/list` defaults to `tags_match="any"`, which is an **OR that also includes untagged rows**. Filtering on `tags=["signal"]` and inheriting the default therefore returns every untagged row in the bank too. `tags_match="all_strict"` is an AND that excludes untagged rows, which is the scope actually meant.

More importantly: Hindsight's observation consolidation, left on, adds a derived `observation` row for signals it considers connected. Measured on the live API, 12 retained signals produced **12 `world` facts plus 10 `observation` rows** — double the stored units, and half of them were Hindsight's own narrative of the pattern we were about to ask Groq to find. The observed behaviour is the subtle part: those observations **inherit the source fact's `tags`** (so `all_strict` does not exclude them) but **carry empty `metadata`** and no `document_id`.

So the bank is created with `enable_observations: False`. This app stores discrete, dated, typed signals and does its own grounded cross-signal reasoning; Hindsight's consolidation would duplicate that step and compete with it. The result is a clean 1:1 — **71 signals in, 71 memory facts out**, `fact_count == signal_count` on every bank.

Two independent guards keep a derived row off the timeline regardless: the `all_strict` tag scope, and `_unit_to_signal` returning `None` for any unit without `metadata.signal_uid`. `tests/selfcheck.py` pins both, and pins the observation behaviour itself so the double cannot drift back to a fiction.

**6. A prompt is a budget, and going over it is stated rather than hidden.**

Retrieval reads the complete timeline. A single _prompt_ cannot, and pretending otherwise is where the two halves of the product would quietly contradict each other: every figure the model is allowed to cite is computed from the signals it can see, so a silently truncated prompt produces a fluent, well-formed analysis of a fragment that reports itself as the whole history.

`MAX_SIGNALS_IN_PROMPT` (default 40, `.env`-configurable) governs one prompt. Over it, the window keeps three things and nothing else: the **first** signal, where the strategy started; the **most recent** N, where it is now; and **every signal in a repeated transition**, because a `pricing → hiring` move that happened twice is the evidence that licenses the word "repeats" and a window that kept one instance would let the model see a repeat as a one-off. Dropping the oldest, as a naive tail-window does, loses exactly that. The prompt then carries an explicit `EVIDENCE COVERAGE: PARTIAL` line giving both counts, how the window was chosen, the date span the missing signals fall in, and the instruction to say so rather than report the visible run as the lot. Under the cap it says `COMPLETE` with the count, so the model is never guessing which it has.

Two consequences are worth stating plainly, because both are real and neither is a bug to be papered over.

The window is **not contiguous** — the first signal and the recent block are kept, and the gaps between them are gone. So the cadence figures (intervals, median, staleness) are measured on the **full** timeline, not the window: a window is a subset of a history, not a shorter one, and measuring across a gap the selection itself created would invent a silence the company never had. The model is told not to recompute them, so a median derived from a signal it did not see is a fact the prompt disclosed rather than a guess. What it may not do is _quote_ the hidden stretch, and the quote check is restricted to the rendered window.

`MAX_SIGNALS_IN_PROMPT` is also a **floor for recency, not a hard ceiling**. `signal_type` has five values, so on any timeline of realistic length some consecutive pair repeats by pigeonhole, which makes nearly every signal a participant in a repeated transition and protects it. In practice a 52-signal bank is carried whole and nothing is dropped. That is the correct side of the trade — an over-long prompt beats an understated timeline — but it means the constant does **not** bound prompt size the way its name suggests. Widening `signal_type`, or capping how many repeats may pull extra signals in, is the fix if that ever matters. The suite pins the current behaviour so it changes deliberately.

`TimelineFacts.omitted` carries the omission into the validators, and `check_no_partial_claims` blocks a read that calls a window a whole history — "across their entire history", "all 12 signals", "since the beginning" — unless the read also discloses the window. The UI shows the same disclosure to the reader, and `SynthesisResponse` reports `prompt_signal_count`, `signals_omitted_from_prompt` and `prompt_omitted_span` so the gap between "what the bank holds" and "what the model saw" is never a number that disagrees with itself.

A cap of `0` raises rather than producing an empty prompt: a misconfiguration should stop the read, not silently analyse nothing. Four mutations guard this section — keeping the newest signals instead of the chain, cutting a repeated transition in half, truncating without declaring it, and deleting the whole-history check.

### Why we do not use recall, reflect or observations

Hindsight offers `recall`, `reflect` and observation consolidation, and this app uses none of them as its primary path. That is a decision, not an oversight.

- **`recall`** is a relevance-ranked semantic search. It answers "what is similar to this?", which is the wrong question for pattern detection — "what happened, in order, on which date?" Pattern detection needs the _complete ordered timeline_, because the signal that matters is often the one that never repeated, and a relevance-ranked top-k will happily drop it. A prediction grounded in a partial timeline is not a weaker prediction, it is a differently-shaped one, and the difference is invisible to the reader.
- **`reflect`** is Hindsight's own synthesis pass. Calling it and then also synthesising would mean two models producing two narratives over the same bank, with no rule for which one wins. Keeping synthesis in `backend/synthesis.py` means the grounding rules, the confidence contract and the validators all apply to a single place, and the read is reproducible: same timeline, same prompt, one output.
- **Observations** are disabled at bank creation (`enable_observations: False`). Hindsight's consolidation would derive its own summaries of each document, which duplicates work this app already does with typed, dated signals — and it would put undated derived prose on the timeline, which is precisely the material that makes a forecast unfalsifiable. The two guards described above keep derived rows off the timeline even so.

The trade is real and worth naming: "what did Nimbus say about audit logs?" is answered by string-matching the timeline rather than by Hindsight's graph search.

### Asking memory a question: the secondary `recall` path

The reasoning above rules recall _out of synthesis_. It does not rule it out of the product, and refusing to use it at all would be its own kind of dogmatism: "what did they do about audit logs?" is a question, and a question is exactly what relevance-ranked search is for. So recall exists, on its own route, for its own purpose.

- **`GET /recall/{competitor}?q=…`** calls Hindsight's `POST /v1/default/banks/{bank_id}/memories/recall` with `tags=["signal"]` and `tags_match="all_strict"`. The UI puts it behind an _"Ask this competitor's memory"_ box, labelled in the box itself as the k most relevant signals rather than the full timeline.
- **Synthesis never touches it.** `get_timeline()` remains the only retrieval a strategic read is built from. That separation is asserted two ways: the selfcheck runs `import backend.synthesis` and asserts the module exposes no recall symbol, and a mutation points synthesis at `recall_signals` and requires the check to fail.
- **The tag scope is necessary and not sufficient.** `all_strict` excludes untagged rows, but Hindsight's derived observations _inherit_ their source's tags, so an observation passes the tag filter. The load-bearing guard is the same one the timeline uses: a recall result with no `metadata.signal_uid` is dropped.
- **`RecallResult` has no `date` field** (checked against the published 0.10.1 OpenAPI, not assumed). The date comes from `metadata.signal_date`, then `occurred_start`, then `mentioned_at`. A client that reached for `result["date"]` would raise `KeyError` on every real recall and work perfectly against a mock that invented the field — which is why the double is built from the spec and the selfcheck asserts the field is _absent_.
- **A miss is not a zero.** Zero-overlap rows are not returned, an unknown bank is a 404 rather than an empty list, and a blank or over-200-character query is a 422. A question that matched nothing and a company with no memory are different answers, and a typo should not read as an absence of evidence.

### Seeing memory change the answer

The point of a persistent-memory agent is that the _answer_ moves when the
memory does, so the UI shows that movement rather than asking you to take it
on faith. Reads are cached per competitor in the session, and logging a signal
offers a side-by-side diff of the read you already generated against a fresh
one: signal count, confidence, evidence staleness, prediction, and
`missing_evidence`, with changed fields marked.

If you have not generated a read yet, **nothing is run** — you get the memory
delta and a button instead. A hidden LLM call would bill you for a read you
did not ask for, on data you had not finished entering. The sidebar also shows
the count as growth ("4 → 5 this session") rather than a bare number, because
a static count hides the event that is the whole product.

A rate-limited read is answered `200` with the deterministic read, so the UI
distinguishes it from a broken key and says _wait_, with the provider's own
retry hint. Telling someone to check their API key when their key is fine is
the worse of the two failures.

---

## Verifying without an API key

```bash
python tests/selfcheck.py
```

522 checks, no network, no credits. It runs the real application code against `tests/hindsight_double.py` — a double built from the published OpenAPI (`info.version 0.10.1`), not a mock that returns whatever the app happens to want. It enforces the rules a naive mock skips:

- `MemoryItem.metadata` values must be **strings**; a nested object is a 422
- `MemoryItem.content` is required
- `tags_match` implements the real five-mode semantics, and every bank is seeded with untagged derived observations — one of which carries an inherited `signal_uid`, the realistic consolidation case
- `PUT /banks` rejects unknown bank-config fields
- the bank listing paginates and sorts by `last_write_at` DESC
- missing banks are 404; invalid `tags_match` / `time_field` are 422

It then exercises the seeded dataset, idempotent re-seeding, timeline ordering, synthesis grounding, thin-competitor refusal, time anchoring and evidence staleness, live ingestion, and each malformed-LLM recovery path.

The last two sections are the ones worth trusting. Section 9 replays five specific defects found by auditing the real 10-competitor run — an unsupported "repeats three times", a silent 26-day gap, a skipped cycle stage, a confident read with no confidence field, a fabricated 30-day cadence — and pins the current behaviour against each. Section 10 then deletes each new rule from the source, in a mutated copy of the module, and asserts the corresponding test **stops firing**: 52 mutations, each of which must break something, so none of those tests can pass for the wrong reason. A check that fails on the real code is a bug; a check that still passes with its own rule deleted is a test that proves nothing.

Seven of those mutations re-introduce defects found by the _second_ audit, verbatim: a refusal allowed to report `high`/`medium`/`low`, the retry notice asking the wrong end of the confidence contract, the permitted-number list dropped from the notice, a withheld narrative not flagged as withheld, the digits-only interval pattern, the indefinite article read as the quantity, and the cadence cue that keeps "within a month" from being read as a measured cadence. Each is pinned to the test that caught it, so the fix cannot be reverted silently.

It is not a substitute for a live run — it proves the client matches the documented contract, not that your account is provisioned. Run it first, then point at real Hindsight.

### What the live run actually returned

Both bugs above were found by running the code against the real API rather than by reading the spec, and the two interesting results are worth stating plainly.

**On the rich competitor, the real model found the planted pattern and ignored the decoy.** Nimbus AI's 12 signals contain an ~8-week chain — funding → GTM hires → price cut → volume discount → enterprise messaging → SSO/SCIM → enterprise hiring → campaign → enterprise tier — plus one _unrelated_ funding round placed on 2026-02-27 that belongs to no chain. The model dated the sequence, estimated the 2–4 week gaps, and did not fold the stray round into the narrative. Its prediction was falsifiable and self-dated: a formal Enterprise onboarding/partner program by early October 2026, to be refuted by its absence after 2026-10-10.

**On thin competitors it refused, and that took a prompt fix.** Vertex Cloud (4 signals) and Pathfinder Labs (3) originally produced a _contradiction_: `patterns` correctly said "insufficient evidence of a recurring pattern", and then `predicted_next_move` went ahead and speculated anyway ("if they follow a typical quarterly cadence, they might…"). A hedged forecast of a strategy the model had just admitted it could not identify is a fabrication wearing a disclaimer. The grounding rules now require all four fields to agree — when `patterns` reports insufficient evidence, the prediction must decline and name the evidence that would settle it, and the recommendation must be to keep collecting. Both thin competitors now return a coherent, consistent refusal that names the specific missing signal types.

Seeded, live-verified, and left in a clean state: **71 signals retained across 10 competitors, 71 memory facts, `fact_count == signal_count` on all ten banks.**

---

## Architecture

```
                    ┌──────────────────────────────────────────┐
  paste a signal ──▶│  ingestion.py                            │
                    │  raw text → Groq → structured Signal      │
                    └───────────────────┬──────────────────────┘
                                        │ retain
                    ┌───────────────────▼──────────────────────┐
                    │  Hindsight — one bank per competitor      │
                    │  competitor-nimbus-ai                     │
                    │  metadata + tags + document_id            │
                    └───────────────────┬──────────────────────┘
                                        │ list (complete, not top-k)
                    ┌───────────────────▼──────────────────────┐
  "Get Strategic    │  synthesis.py                             │
   Read"         ──▶│  full timeline → Groq → 4-part narrative   │
                    └──────────────────────────────────────────┘
```

| File                          | Role                                                                 |
| ----------------------------- | -------------------------------------------------------------------- |
| `backend/config.py`           | env loading, Hindsight base-URL normalisation, model/timeout knobs   |
| `backend/models.py`           | Pydantic schemas, date coercion, signal uids                         |
| `backend/hindsight_client.py` | **bank-per-competitor read/write, chronological-complete retrieval** |
| `backend/llm_client.py`       | Groq wrapper: retries, model fallback, JSON salvage                  |
| `backend/ingestion.py`        | raw text → structured Signal (+ heuristic fallback)                  |
| `backend/synthesis.py`        | timeline → strategic narrative (the differentiator)                  |
| `backend/routes.py`           | HTTP endpoints                                                       |
| `backend/main.py`             | FastAPI app, CORS for Streamlit                                      |
| `scripts/seed_data.py`        | loads the synthetic dataset into Hindsight                           |
| `frontend/app.py`             | Streamlit UI                                                         |
| `tests/hindsight_double.py`   | OpenAPI-faithful Hindsight + Groq contract double                    |
| `tests/selfcheck.py`          | 506-check offline end-to-end suite, 52 mutations                     |

### API

| Method | Path                     | Purpose                                         |
| ------ | ------------------------ | ----------------------------------------------- |
| `GET`  | `/health`                | config status                                   |
| `GET`  | `/competitors`           | every competitor + size/freshness of its memory |
| `POST` | `/competitors`           | register a competitor, create its bank          |
| `POST` | `/signals`               | raw text → LLM-extracted Signal → Hindsight     |
| `GET`  | `/timeline/{competitor}` | complete chronological timeline                 |
| `POST` | `/synthesize`            | strategic read                                  |

---

## Setup

**Requirements:** Python 3.10+, a [Hindsight Cloud](https://ui.hindsight.vectorize.io) API key, a [Groq](https://console.groq.com/keys) API key.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# HINDSIGHT_API_KEY=hsk_...   (from https://ui.hindsight.vectorize.io → Connect)
# GROQ_API_KEY=gsk_...        (from https://console.groq.com/keys)
```

> **Note on the Hindsight URL.** The dashboard is `ui.hindsight.vectorize.io`; the REST API is `api.hindsight.vectorize.io`. `config.py` normalises a pasted `ui.`/`app.`/`console.` host to `api.` automatically, so either works.

### Seed the memory (do this before demoing)

```bash
python scripts/seed_data.py --reset --verify
```

This writes 71 signals across 10 competitors into Hindsight and reads them back. Re-running without `--reset` is safe _for identical data_ — signals are keyed by `document_id`, so unchanged rows are replaced rather than duplicated.

**`--reset` deletes every Signal Stack bank, not only the seeded ones.** It used to iterate the seed file, which meant a bank created by anything else survived a command that printed "reset" and exited 0. That is the worst available combination: memory that looks clean, a clean-looking command, and a synthesis read quietly reasoning over whatever actually survived — a stray probe bank is exactly how it happened here. Banks outside the seed file are now listed by name _before_ they are removed, so the operator sees what is being destroyed. If you have real data in a Signal Stack bank, this command will delete it; use `scripts/seed_data.py` without `--reset` to add to it.

**Editing a seeded signal's date orphans the original.** The uid is `slug | date | type | digest`, and that uid _is_ the Hindsight `document_id`, so changing a date produces a new document and the old one stays behind. Brightline's bank silently held 13 signals where the file had 7 — and because the orphans were the same announcements a fortnight earlier, the synthesis read them as a genuine "announce, then reinforce a week later" cadence and reported it as a finding. `--verify` now fails the command on any read-back mismatch and names the orphaned rows; `--reset` rebuilds the bank.

### Run

```bash
# terminal 1 — API
uvicorn backend.main:app --reload --port 8000

# terminal 2 — UI
streamlit run frontend/app.py
```

Open http://localhost:8501. Interactive API docs at http://localhost:8000/docs.

#### Pointing a deployed UI at a deployed API

Locally the UI finds the API on `http://localhost:8000` and needs no
configuration. When the frontend and the backend are deployed separately, set
one variable on the **frontend** service:

```bash
BACKEND_URL=https://signalstack-backend.onrender.com
```

The UI resolves its API base in this order: `BACKEND_URL`, then
`SIGNAL_STACK_API` (the original single-image name, still honoured so the
Docker path is unchanged), then `http://localhost:8000`. The variable is
documented, empty, in [`.env.example`](.env.example).

Both variables are in `.env.example` and are **optional**: leave them unset
locally and everything works as before.

#### Locking down writes on a deployed API

Unset, `POST /signals` and `POST /competitors` are unauthenticated. That is
right locally — one host, no boundary to defend — but **wrong for a public
deploy**: the backend binds `0.0.0.0`, and those two endpoints write to
Hindsight using your account key, so an open write endpoint is someone else's
cloud bill. Set one variable and it is closed:

```bash
# on BOTH services, the same value
API_KEY=<any long random string>
```

Clients then send `X-API-Key: <value>`. The Streamlit UI does this
automatically from its own `API_KEY`. Reads (`GET /timeline`,
`POST /synthesize`) stay open either way: the seeded competitor intelligence is
not private, and gating reads would add friction to the demo for no
confidentiality gain.

Two caveats worth stating. This is a shared-secret write guard, not real
authentication — it is sized for a demo deploy, not for user accounts. And
`POST /signals/explicit` (the seeder's structured write) is deliberately left
unguarded so `AUTOSEED` and the offline suite keep working; that is the first
thing to revisit before a real deploy, and it is pinned by a test so the
decision is visible rather than accidental. The destructive
`POST /demo/reset` route is behind that key **and** behind its own
`ENABLE_DEMO_RESET` flag, which is off by default.

#### The demo, start to finish

Two things make the app runnable twice in a row, which matters because logging
a signal is the one irreversible thing in the UI:

```bash
python scripts/seed_data.py --reset --verify   # wipe and re-seed the banks
```

The UI warns that logging writes to the seeded bank. The reset lives behind
**two independent gates**, because it is the most destructive thing in the app:

| Gate                  | Default | Effect                                                                                                                                      |
| --------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `ENABLE_DEMO_RESET=1` | **off** | If unset or `0`, `POST /demo/reset` is not registered and answers **404**. The UI hides its reset button and instead shows the CLI command. |
| `API_KEY` (when set)  | unset   | With a key configured, the call must present a matching `X-API-Key` or it is **401**.                                                       |

The flag is the primary gate rather than the key because with `API_KEY` unset —
the local default — the key check is a no-op, so a route guarded only by it is
effectively unguarded in the default config. `render.yaml` pins
`ENABLE_DEMO_RESET` to `0`; setting it to `1` is only appropriate for a
throwaway demo instance. When it does exist it is still behind a confirmation
checkbox, and it clears every bank and tells you the re-seed command rather
than re-seeding silently, because that would also overwrite anything a real
user logged in between. `tests/selfcheck.py` asserts the real HTTP status
codes for all four combinations (flag off, flag on, right key, wrong key) and
deletes banks named `competitor-alpha`/`competitor-beta` on a dedicated double
— names that cannot exist in any real account, so a passing delete is itself
proof the test never left loopback.

**The hero path is Vertex Cloud.** It is seeded with 4 signals, one short of
the 5-signal evidence floor, so a read refuses. Click **🧪 Try the sample**,
log it, and the read flips to a forecast at `medium` — capped, because five
signals is the minimum and nothing has repeated yet. `tests/selfcheck.py`
section 5b pins all of it, so the sample cannot quietly stop working if the
seed or the floor changes.

> **Verified against the offline double, not a live Groq run.** The refusal →
> forecast flip, the `medium` cap, the sample's date, and the whole API path
> are pinned in `tests/selfcheck.py` against `tests/hindsight_double.py`, which
> enforces Hindsight's published contract. That proves the client behaves as
> specified; it does not prove this Groq account will serve the model. A live
> hero run has **not** been completed; it is **pending Groq quota**. What
> actually happened on the last attempt is a 429 rate-limit from Groq, and the
> cause is **likely quota** on this account rather than anything about the
> request — availability is per-account and per-plan, so "it 429s" and "the id
> is wrong" are different problems with different fixes. Run the demo yourself
> before presenting it, and do not promise the flip until you have seen it.
>
> What _is_ verified about the model: a read-only `GET /models` against this
> Groq account lists both `openai/gpt-oss-120b` and `qwen/qwen3.8-27b`, so the
> configured ids are servable here and the id is not the thing that failed.
> `llm_client.verify_configured_models()` now runs that same check at startup
> and warns by name if a configured id is not served — because the symptom
> otherwise is a confusing extraction error on the first signal rather than a
> configuration mistake.

`render.yaml` ships two services — `signalstack-backend` (FastAPI, binds
`0.0.0.0:$PORT`) and `signalstack-frontend` (Streamlit, same start command
shape). `BACKEND_URL` is deliberately left unset in the blueprint because the
backend's hostname is not known until that service exists; set it in the
frontend's environment and redeploy. See [DEPLOYMENT.md](DEPLOYMENT.md).

---

## The seeded dataset

All fictional. No live scraping, so the demo cannot be broken by a rate limit or a changed page.

**Nimbus AI — 12 signals, 2026-02-18 → 2026-08-19.** A deliberately correlated chain:

```
2026-02-18  FUNDING    $120M Series C, "earmarked for go-to-market and enterprise readiness"
2026-02-27  MESSAGING  "AI for everyone" campaign renewed — no enterprise language yet
2026-03-05  HIRING     4 GTM roles incl. Enterprise AE          (2 wks after funding)
2026-03-11  FEATURE    dark mode + CSV export                   <-- DISTRACTOR
2026-03-24  HIRING     5 more GTM roles + first Solutions Architect
2026-04-15  PRICING    Pro $49 -> $32 /seat/mo, a 35% cut       (3 wks after hiring peak)
2026-05-06  PRICING    20% volume discount, annual-only >5 seats
2026-05-20  MESSAGING  "AI for everyone" -> "Enterprise-ready AI for growing companies"
2026-06-03  FEATURE    SSO + SCIM + audit logs GA
2026-06-24  HIRING     Field Engineer, Strategic AE, SA II
2026-07-15  MESSAGING  "Built for the Enterprise" campaign
2026-08-19  PRICING    Enterprise tier ships; volume discount now enterprise-only
```

The `2026-03-11` feature release is planted on purpose: it is unrelated to the strategy, so a model that pattern-matches everything rather than reasoning across types will latch onto the wrong thread.

### The rest of the corpus

Ten competitors, 71 signals, chosen so the retrieval and grounding paths get exercised rather than just the demo case. Each has a genuinely different arc, and the end dates are deliberately uneven so evidence staleness is visible in the product:

| Competitor          | n   | Arc                                     | Evidence                             |
| ------------------- | --- | --------------------------------------- | ------------------------------------ |
| Nimbus AI           | 12  | Series C → enterprise land-grab         | **stale** (14-day rhythm, 40d quiet) |
| Palisade Security   | 11  | Incident → compliance rebuild → federal | fresh (3d)                           |
| Lumen Health        | 9   | Regulated-market compliance chain       | **aging** (48d)                      |
| Corvus Data         | 8   | Open-core → commercial cloud            | fresh (4d)                           |
| Halcyon Mobility    | 8   | Utility pricing → fleet platform        | fresh (6d)                           |
| Brightline Retail   | 7   | Metronomic 28-day cadence, then silence | **stale** (28-day rhythm, 84d quiet) |
| Ferrous Systems     | 6   | Hardware → software and services        | fresh (12d)                          |
| Vertex Cloud        | 4   | Uncorrelated incumbent                  | fresh (47d, but 76-day rhythm)       |
| Pathfinder Labs     | 3   | Sparse devtools                         | fresh (54d)                          |
| Tidewater Analytics | 3   | Sparse BI vendor                        | fresh (53d)                          |

Three things are worth pulling out:

- **Brightline Retail is the stale-evidence case that matters.** Its cadence is exactly 28 days for six consecutive intervals, then stops. The gap cannot distinguish _the cadence broke_ from _the cadence continued and someone stopped watching_, and the read says so rather than projecting the old rhythm as if it were live. Live, it reports the cadence, forecasts to 2026-10-15, and adds _"this forecast rests on a signal stream that has been quiet for 84 days, so confidence is limited."_
- **Vertex Cloud refuses because it is below the evidence floor.** It has 4 signals, one short of the 5-signal floor, so `confidence: none` is the only answer the validators accept. Its intervals (76d, 83d, 43d) are regular enough to have a median of 76d, which is exactly why it is the useful contrast: the read is not refusing because the timeline looks erratic, it is refusing because there is too little of it. The refusal names the specific signal that would settle it.
- **The infrequent competitors are not stale.** Vertex Cloud (76-day rhythm), Pathfinder Labs (70-day) and Tidewater Analytics (91-day) are all quiet for 47–54 days and all read as current. A fixed 30-day threshold would have flagged all three, and warned you about companies that simply do not announce often.

---

## Demo script

1. **One signal means nothing.** Select _Nimbus AI_, set memory depth to **"1 signal (no pattern possible)"**. "That's the entire story: they raised money. Congratulations."
2. **Reveal the accumulation.** Switch to **"Full timeline"** — 12 signals over six months, colour-coded by type. "This wasn't scraped. Every one of these is a separate memory write, and they're all still there."
3. **Get the strategic read.** Click **🧠 Get Strategic Read**. The output connects funding → hiring → pricing → messaging, and ends in a falsifiable prediction. Note the caption: _built from 12 signals (2026-02-18 to 2026-08-19)_, and the warning that the evidence is 40 days old.
4. **Contrast.** Switch to _Vertex Cloud_ and read it again. It has 4 signals — one short of the 5-signal evidence floor — so the read is a refusal by rule, and it names the specific missing signal that would settle the question. "The evidence is below the floor for a confident read; one more signal would change that." The agent declines to fabricate, which is the harder and more valuable behaviour to demonstrate.
5. **Stale evidence.** Switch to _Brightline Retail_. The pattern section finds a precise 28-day cadence, the prediction is dated forward, and both carry the caveat that the timeline stopped 84 days ago. This is what a correct answer looks like when the data has gone cold.

Optional: the **Log a new signal** expander shows live ingestion — paste a raw note, the LLM extracts `{signal_type, date, summary, source}`, and it is written to that competitor's Hindsight bank and appears on the timeline.

---

## The numbers are computed, not requested

The original version of this app asked Groq for a strategic read and trusted the answer. Auditing the real 10-competitor output showed what that costs: a cadence invented as "roughly every 30 days" against a measured 28, a "repeats three times" claim where a transition occurred twice, a forecast dated after the evidence had already gone stale, and reads that stated a probability without ever naming their own confidence.

So the arithmetic now happens in Python, in `backend/facts.py`, and the result is handed to the model as a block it must obey rather than a question it must answer.

Real output for Nimbus AI, computed on 2026-09-28:

```
FACTS (computed by the application — use these, do not recompute them):
- signal count: 12
- today's date: 2026-09-28
- most recent signal: 2026-08-19
- days since the most recent signal: 40
- intervals in days between consecutive signals, in order: [9, 6, 6, 13, 22, 21, 14, 14, 21, 21, 35]
- median interval: 14 days (range 6-35 days)
- days overdue: 26 (the stream is 26 day(s) past its 14-day rhythm)
- transitions that DO repeat (>=2 occurrences): feature -> hiring occurs 2 time(s)
- transitions seen only ONCE (say 'one observed instance', never 'repeating'/'a cycle'):
  funding -> messaging occurs once, hiring -> feature occurs once, ... (9 in total)
- the three most recent signals, verbatim:
    [2026-06-24] (hiring) Nimbus AI opens three strategic-enterprise roles including a
    Field Engineer, a Strategic Account Executive and a second Solutions Architect.
    [2026-07-15] (messaging) Nimbus AI launches a 'Built for the Enterprise' campaign and
    teasers an Enterprise tier on the pricing page.
    [2026-08-19] (pricing) Nimbus AI publishes an Enterprise tier and restricts the 20%
    volume discount to Enterprise contracts, ending self-serve volume pricing.
```

and, appended to the prompt only when the stream is actually late:

```
- The stream is 26 day(s) past its 14-day rhythm. "predicted_next_move" MUST say how far
  overdue it is, and "confidence" MUST NOT be "high".
```

Note what the second line of that block says: the one transition the model may describe as repeating is `feature -> hiring`, twice. "Repeats three times" has nothing to be true of, and the nine one-off transitions are named individually so they cannot be quietly promoted into a cycle.

Five things follow from that block being authoritative:

- **A cadence claim is checked against the measured intervals.** `check_interval_claims` accepts a number if it is one of the intervals, one of the stream's other derived figures (age, overdue, median, extremes), within ±2 days of one, or stated as a _duration_ in the evidence. Units are handled separately, and so are the two directions: "about a month" is converted to 30–31 days against Brightline's 28-day median and accepted, and a bare "30 days" is accepted for the same reason — but "10 day cadence" is not, because day claims are matched only against day figures, and the tolerance window would otherwise bridge units and let Brightline's 84-day age satisfy a 10-day claim as 12 weeks.
- **The spelling of a number does not decide whether it is checked.** "2 weeks" was checked while "two weeks" was not, so the same claim got a different verdict by spelling — the rule depended on the number format, not on the fact. Amounts are now read in digits and in words, so "two weeks", "a fortnight" and "a month" are measured exactly as "14 days" and "1 month" are. The indefinite article is the one exception, and it is an exception for a reason: a bare "a month" is ambiguous between a cadence and a forecast horizon, so it counts as a measured claim only when a cadence cue is near it ("every month", "a month between signals"), while "expect a launch within a month" is left alone. A specific figure is a measurement whichever way it is spelled. The article is also not read as a quantity — "a 28 day cadence" is 28 days, not 1 and 28.
- **A date in the prediction is checked against the real signal dates.** A past ISO date is allowed only when it is an actual signal date, or when the sentence around it is narrative ("the 2026-04-02 launch was never announced") rather than a deadline. "They will ship by 2026-08-03", said on 2026-09-28, is rejected however confident it sounds.
- **Repetition is priced.** Only transitions occurring at least twice appear in the block, so "repeats three times" has nothing to be true of. A read may use the words repeat, cycle, loop or recurring only for a transition the block prices as repeating, and naming no priced transition is rejected — saying _no_ transition repeats is a disclosure, not a claim, and is never flagged.
- **Sufficiency decides whether a refusal is available at all.** A timeline is sufficient when it has at least 5 signals _and_ its longest gap is within 3x its median interval. Below that floor the only accepted answer is a refusal (`confidence: none`); at or above it a refusal is **rejected**, because a timeline that supports a read does not get to decline one. Above the floor with nothing repeating, confidence is capped at `medium` and `missing_evidence` must disclose that no transition has repeated — either as a denial ("nothing has repeated yet") or by naming the occurrence that is missing ("a second feature->hiring would be the first repeat"). Both wordings are accepted, because a check that turns on phrasing is the same defect as an interval rule that passes "14 days" and fails "two weeks". This replaced the earlier rule — refuse when no transition type repeats — which was measuring vocabulary rather than evidence: it rejected Palisade (11 signals) and Ferrous (6) in a live sweep for having no repeated pair, while waving through forecasts built on long, erratic timelines that repeated nothing simply because they were irregular. Note that the 5-signal floor is **tuned to this corpus** (it separates the three designed refusals at 3–4 signals from the seven designed forecasts at 6+), not derived from a measured threshold; the dispersion guard is not exercised by any competitor here either.
- **Staleness caps confidence.** An overdue stream may not be reported as `high`.
- **Every read carries `confidence` and `missing_evidence`.** A refusal that is not explicit about what it does not know is a refusal the reader cannot act on.

When a check fails, the response is **not** silently accepted and is not thrown away: the specific failures are appended to the prompt and the model is asked once more, which usually produces the corrected read. If the retry also fails, the response is a deterministic read rather than a narrative — HTTP 200, `confidence: none`, `narrative_withheld: true`, the real intervals and overdue figure in `patterns`, and the validator's own complaint in `model_used` and in the sentence explaining why the narrative is being withheld. The UI shows the withheld reason and what would raise confidence. The alternative — returning the second unverified attempt — is the behaviour the audit was written against.

This means a wrong number is either fixed or visibly absent. It is never presented as a finding.

### The retry has to be able to succeed

A retry is only worth sending if the instructions it carries would actually pass the validator that rejected the first attempt. The first version did not, and the audit found the cost in live output: two reads were rejected for reporting `high`/`medium`/`low` on a refusal the validator required to be `none` — the notice told the model to do exactly what the validator had just refused — and a third retry fixed its overdue gap and then invented a different wrong interval, because the notice listed the true figures without saying the list was closed.

So the rules the retry is told are now the rules the validator runs:

- **One contract, asked for by the code that enforces it.** `validators.allowed_confidence_values(facts, refused)` is the only statement of which confidence values are legal, `_correction_notice` builds its instruction from it, and the selfcheck parses the finished notice back and asserts that each branch names exactly the values the validator accepts for that branch. A refusal's branch naming the graded levels — the original defect — fails the suite, and there is a mutation that re-introduces it. The agreement is checked behaviourally rather than textually: for four representative timelines and all four confidence values, every value the notice offers is one the validator accepts and every value it omits is one the validator rejects, so the two cannot drift apart even if both were mangled the same way.
- **The permitted figures are enumerated, and the enumeration is closed.** The notice lists the exact day counts `check_interval_claims` will accept for this competitor, and says the list is the whole set. The selfcheck then feeds every number on that list back through the interval check, so the notice cannot offer a figure the validator would reject.
- **A failed retry is never an error response.** It is a 200, flagged `narrative_withheld`, carrying the measured facts. A reader sees a weaker answer and the reason for it, not a stack trace and not a silent pass.

### Honesty rules for the model itself

Two of the rules cannot be expressed as a post-hoc check and stay in the prompt, where the mutation suite records them as such:

- the forecast must follow or explicitly break the last cycle stage, and must be consistent with the **last three signals verbatim**;
- when `patterns` reports insufficient evidence, the prediction must decline and name the signal that would settle it, and the recommendation must be to keep collecting.

### Seeding is opt-in

`AUTOSEED` defaults to **off**. A fresh local checkout therefore starts empty rather than silently appearing to have memory, and only `Dockerfile` and `render.yaml` set `AUTOSEED=1`, where an empty first deploy has to bootstrap itself from the seed file. To seed locally, run `python scripts/seed_data.py --verify` once you have a key.

---

## Robustness

The brief warns that Groq's `gpt-oss` models intermittently produce malformed or tool-call-shaped responses. `backend/llm_client.py` handles it, and the paths below are all exercised:

- **`response_format` rejected (HTTP 400)** → the retry drops JSON mode and asks again, rather than repeating a request that already failed. Worst case 2 models × 3 attempts, and `call_llm_json` does not re-loop, so a bad provider cannot multiply requests. The 400 is not hypothetical: `gpt-oss` accepts `response_format` only when the prompt contains the literal word _json_ (it routes through a structured-output path), and it additionally returns a `reasoning` field that has to be stripped before parsing. Both prompts contain the word.
- **Model unavailable** → falls back from `openai/gpt-oss-120b` to `qwen/qwen3.8-27b`, skipping any candidate this account is not actually served. This is not theoretical either: the original fallback `qwen/qwen3-32b` returned no match on `GET /models` for the account this was tested against, which is why the list is now probed and filtered at call time instead of assumed.
- **Markdown fences / preamble / `<think>` block / unclosed `<tool_call>`** → stripped, then the first balanced `{...}` is extracted.
- **Truncated completion** (stop token mid-JSON, including nested) → salvaged: close the open string, drop the dangling incomplete pair, append exactly the missing closers. Verified against `{"a":1,"b":{"c":"cut` and similar.
- **Trailing commas, single quotes, smart quotes** → repaired in escalating order of desperation, and only when a strict parse has already failed. An apostrophe in prose is never mangled into a delimiter.
- **LLM fully unavailable** → `ingestion.py` falls back to a keyword/date heuristic and still writes a usable signal; `synthesis.py` returns an honest degraded read that reports the span and per-type counts instead of pretending.
- **Rate limited (HTTP 429)** → `RateLimitError` is raised rather than retried blindly. `backend/llm_client.py` honours `Retry-After` first, then Groq's in-body hint (`Rate limit reached... try again in 34.98s`), then a 20-second default, capped at 75s. The earlier behaviour — an immediate second attempt — is what turned a rate limit into a hard failure on the demo.
- **Signal date ambiguity** → `03/04/2026` is read as US month-first; `13/04/2026` can only be day-first. Separators (`/`, `.`, `-`) are normalised so one format list covers all of them.
- **Hindsight unreachable** → the API returns `502` with the underlying error, and the UI reports it rather than rendering an empty timeline.

---

## Notes and limitations

- **Synthetic data, on purpose.** Real competitor tracking needs scraping and will fail live. The seeded dataset makes the demo reproducible.
- **`GET /competitors` returns objects, not bare strings.** It adds `signal_count`, `fact_count` and `last_write_at` so the sidebar can show memory accumulating — the growth _is_ the pitch.
- **Extraction quality is only as good as the note.** The heuristic fallback is deliberately dumb; it exists so a demo button never dead-ends, not as a production path.
- **`synthesis.py` spends a whole timeline into one prompt.** Past a few hundred signals per competitor this needs chunking or Hindsight's own `reflect` operation to stay inside the context window.

---

## Known limitations

The honest edges of the system, kept apart from the feature documentation above.

- **Staleness figures are relative to the date you read this.** Every "40 days overdue", "84 days", "47 days" and "2026-09-25" in this README was computed against **2026-09-28**, the audit date, and the seeded dataset is frozen as of then. Re-run tomorrow and the overdue counts grow, the staleness bands move, and a forecast the model dates forward may be generated about a window that has already closed. The seeded dates themselves do not move, so the _cadence_ claims stay true and only the staleness claims decay.

- **The fallback model id was verified against a live account, and availability is per-account anyway.** `GET /models` on the account this was tested against serves 11 ids, including both `openai/gpt-oss-120b` and the configured `qwen/qwen3.8-27b`, and does **not** include the old `qwen/qwen3-32b` that the code once fell back to. So the default is correct for this account — but that is the point rather than a reassurance: model availability is per-account and per-plan, and an id served on one plan 400s on another. `available_models()` probes `/models` once and `_model_candidates()` drops anything this account is not served, falling back to a known-present `gpt-oss` id if neither configured model qualifies, so a stale id in `.env` degrades to a working model rather than an error. If you point this at a different account, re-run the probe; do not trust the default.

- **The evidence floor and the dispersion guard are corpus-tuned heuristics, not measured thresholds.** `MIN_SIGNALS_FOR_EVIDENCE = 5` and `MAX_INTERVAL_SPREAD = 3` were chosen so the 3–4-signal designed refusals (Vertex Cloud, Pathfinder Labs) sit below the floor and the 6+ designed forecasts sit above it. They are supported by 4 live reads — Palisade Security, Ferrous Systems, Nimbus AI, Vertex Cloud — plus 52 offline mutation tests. They are **not** measured across the full 10-competitor corpus, and no competitor comes near the dispersion limit, so that guard is exercised only by synthetic input. A larger or differently-shaped corpus would need the floor re-tuned.

- **A low-confidence read may decline to forecast rather than being treated as a refusal.** Ferrous Systems (6 signals, above the floor) returns `confidence: low` with a `predicted_next_move` of "No reliable prediction can be made at this time." The structured `confidence` field, not the prose, decides whether a read is a refusal — so this is a narrative by rule even though it declines to predict. No "must actually predict" validator was added on purpose: it would reject Ferrous and push it back to a refusal, which is precisely the vocabulary-driven behaviour the evidence-sufficiency rule exists to remove. This is a known, accepted edge, not an oversight.

- **`check_no_partial_claims` is phrase-based, and phrasing is exactly what it cannot police.** The validator blocks a truncated read that describes its window as a whole history by matching a fixed set of phrases — "across their entire history", "all 12 signals", "since the beginning" — and it is deliberately blind to everything those phrases do not cover. A model that writes "every one of these signals shows a pricing-led squeeze" passes, and so does "the timeline is unambiguous", even though both assert completeness over a fragment. The check is a floor against the specific failure mode observed in the audit, not a proof of calibration: it cannot tell a confident claim from a hedged one, and tightening it into a semantic judgement would put a language model's guess inside the validator that exists precisely because language models are not reliable judges. The coverage line in the prompt is the load-bearing disclosure; this check only catches the phrases the audit actually saw.

- **Trend consistency (failure mode C) is prompt-enforced, not programmatically verified.** No validator checks a prediction against the types of the most recent signals. The prompt asks for a trend-consistent read and the audit's mode-C heuristic is only a keyword guess, but nothing in `validators.py` enforces it — a prediction contradicting the latest signal types would pass. Staleness/overdue (mode B) _is_ enforced programmatically, by `check_overdue_acknowledged`.

- **Only manual/seeded input is supported; live scraping was scoped out deliberately.** Competitor text is a prompt-injection surface: a scraped page can carry instructions the model reads as part of the read. Until there is an isolation and sanitisation layer, ingestion is paste-a-signal plus the seeded dataset, and nothing is fetched from the web.

### Audit failure modes, before and after

Five failure modes were checked programmatically across all 10 competitors' live reads — A: overclaimed repetition · B: ignored staleness · C: prediction contradicts the latest trend · D: missing calibration or verbatim grounding · E: arithmetic or date error.

| Failure mode                        | Original baseline (prose refusal rule) | Final sweep (field refusal rule) |
| ----------------------------------- | -------------------------------------- | -------------------------------- |
| A — overclaimed repetition          | 3                                      | 0                                |
| B — ignored staleness               | 2                                      | 0                                |
| C — contradicts latest trend        | 1                                      | 0                                |
| D — missing calibration / grounding | 7                                      | 0                                |
| E — arithmetic or date error        | 6                                      | 0                                |
| **Total failures**                  | **19**                                 | **0**                            |

_Footnote — this is not a controlled A/B._ The refusal classifier changed mid-project from prose-based (grepping the narrative for "insufficient evidence") to field-based (`confidence == "none"`). The original baseline above was measured under the prose rule; only the final sweep was measured under the current field rule. The improvement is indicative, not a like-for-like experiment.

_Where the baseline came from, and why you cannot reproduce it._ The `3 / 2 / 1 / 7 / 6` figures come from the **original audit harness**, whose results are recorded in [`audit/FINDINGS.md`](audit/FINDINGS.md). That harness was **revised during the project and no surviving copy of the original remains**, so the baseline cannot be re-derived from anything checked in. Running the current scripts over the same preserved reads in [`audit/before/`](audit/before/) gives **`0 / 0 / 0 / 10 / 0`** — 10 failures, not 19 — and most of the difference is mode `D`: the `before/` reads predate the `confidence` field entirely, so all ten now fail the calibration check where the original scored seven. The current scripts, their inputs and their scored output are all in [`audit/`](audit/), including [`audit/scripts/check.py`](audit/scripts/check.py), which is the one that produces `0 / 0 / 0 / 10 / 0` from `before/`, and [`audit/report_final.json`](audit/report_final.json), the `0 / 0 / 0 / 0 / 0` result. Treat this table as a record of what was observed at the time, not as a reproducible measurement.
