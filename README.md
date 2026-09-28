# Signal Stack

**A competitive intelligence agent that remembers every competitor signal over time, and connects them into a strategic story that gets sharper the longer it watches.**

Most competitor tracking is a list of disconnected facts. Signal Stack remembers *every* signal — pricing changes, feature launches, hiring spikes, messaging shifts, funding rounds — and reasons across signal **types** to infer intent and predict the next move.

A single price cut is just a price cut. Three price cuts in six months, each following a funding round, is a strategy.

---

## The problem

Teams track competitors sporadically: a Slack message here, a pricing-page screenshot there. The signals land in a doc nobody re-reads. No current tool holds signals long enough, or reasons across them well enough, to surface the pattern automatically.

Three things go wrong in the naive version of this product:

| Naive approach | Why it fails |
|---|---|
| A scraper + a database, read on demand | Nothing accumulates. Day 1 and day 400 look the same. |
| Summarize each event and list it back | The user gets a feed, not a conclusion. The cross-type chain is never stated. |
| RAG with top-k similarity retrieval | **Silently drops the oldest, least-similar memories** — which is exactly where the beginning of a six-month chain lives. The pattern is invisible. |

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

- `content` — the signal rendered as **one declarative sentence**, because Hindsight extracts *facts* from retained content. One sentence in, one fact out, so the timeline stays one-entry-per-signal instead of fragmenting.
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

We deliberately do **not** use `/memories/recall`, which is top-k semantic + graph + temporal search. Recall is built to return the *k most relevant* memories, and a February funding round is not semantically similar to a July pricing page. Recall would return the recent enterprise-messaging cluster and quietly omit the raise that caused it — the chain would look unmotivated and the agent would have no strategy to find.

We also deliberately do not pass `time_field`: rows with no value on the chosen time column are excluded from both filtering and ordering, which is the opposite of completeness. Sorting happens client-side.

Pagination is paged to exhaustion with a ceiling (`MAX_TIMELINE_PAGES`), then deduped by `signal_uid` (the extractor can emit more than one fact per document) and sorted oldest-first.

**4. The read is grounded in the retrieved timeline, and refuses to invent.**

`backend/synthesis.py` formats the complete timeline as a numbered, dated list and uses the prompt from the brief verbatim, plus a grounding addendum that enforces two things:

- every claim cites a date that exists in the timeline;
- **if the timeline has fewer than 4 signals, or shows no repeated pattern and no ordering, the model must say the evidence is insufficient.**

That second rule is what makes the contrast demo work. Ask about a competitor with four uncorrelated signals and the agent reports that it cannot distinguish a strategy from routine maintenance — instead of manufacturing one to fill the shape of the answer.

**4b. The read knows what day it is, and is honest when the evidence has gone stale.**

The model only ever sees dates that appear in the timeline, so with no reference point it anchors every forecast to the *last logged signal*. That produced a live, fully-grounded prediction with a deadline that had already passed — "will announce RBAC by ~2026-09-20", generated on the 28th. Every signal it cited was real and the cadence was genuinely 2–4 weeks; the forecast was simply about a window that had closed. A reader skims the confident date and stops there.

`backend/synthesis.py` now hands the model an explicit clock and a freshness verdict:

```
TIME REFERENCE: Today is 2026-09-28. The most recent signal is dated 2026-08-19,
40 day(s) ago, well beyond this competitor's usual ~14-day signal rhythm. The
evidence is STALE: the timeline may no longer reflect what this competitor is
doing, and a forecast drawn from it carries that uncertainty.
```

"Stale" is judged **against the competitor's own rhythm**, not a fixed day count — the median gap between its own signals, at 1× fresh, 2× aging, beyond that stale. A company that ships weekly and one that ships twice a year are not comparable against a shared threshold. On the seeded data this separates cleanly: Nimbus AI (14-day rhythm, 40 days silent) is stale, while Vertex Cloud (76-day rhythm, 47 days quiet) and Pathfinder Labs (70-day rhythm, 54 days quiet) are both still current — they are simply infrequent, and a fixed threshold would have flagged all three.

The rules that follow from it: forecast from *today*, never present a past date as a future deadline, and when the evidence is stale, disclose that in the prediction and lead the recommendation with the step that **refreshes** the intelligence rather than the step that acts on it. Staleness is never a reason to refuse — the pattern analysis is still the valuable output; it just stops masquerading as current.

`SynthesisResponse` carries `data_as_of`, `evidence_age_days` and `evidence_staleness` as provenance, and the UI shows a warning banner when the evidence is stale, so the reader sees the caveat before the forecast rather than after it. The no-LLM fallback path reports the same provenance.

**5. Tag scoping uses `all_strict`, and observation consolidation is switched off.**

Two things were found by running against the live API, not by reading the spec.

`GET .../memories/list` defaults to `tags_match="any"`, which is an **OR that also includes untagged rows**. Filtering on `tags=["signal"]` and inheriting the default therefore returns every untagged row in the bank too. `tags_match="all_strict"` is an AND that excludes untagged rows, which is the scope actually meant.

More importantly: Hindsight's observation consolidation, left on, adds a derived `observation` row for signals it considers connected. Measured on the live API, 12 retained signals produced **12 `world` facts plus 10 `observation` rows** — double the stored units, and half of them were Hindsight's own narrative of the pattern we were about to ask Groq to find. The observed behaviour is the subtle part: those observations **inherit the source fact's `tags`** (so `all_strict` does not exclude them) but **carry empty `metadata`** and no `document_id`.

So the bank is created with `enable_observations: False`. This app stores discrete, dated, typed signals and does its own grounded cross-signal reasoning; Hindsight's consolidation would duplicate that step and compete with it. The result is a clean 1:1 — **71 signals in, 71 memory facts out**, `fact_count == signal_count` on every bank.

Two independent guards keep a derived row off the timeline regardless: the `all_strict` tag scope, and `_unit_to_signal` returning `None` for any unit without `metadata.signal_uid`. `tests/selfcheck.py` pins both, and pins the observation behaviour itself so the double cannot drift back to a fiction.

---

## Verifying without an API key

```bash
python tests/selfcheck.py
```

140 checks, no network, no credits. It runs the real application code against `tests/hindsight_double.py` — a double built from the published OpenAPI (`info.version 0.10.1`), not a mock that returns whatever the app happens to want. It enforces the rules a naive mock skips:

- `MemoryItem.metadata` values must be **strings**; a nested object is a 422
- `MemoryItem.content` is required
- `tags_match` implements the real five-mode semantics, and every bank is seeded with untagged derived observations — one of which carries an inherited `signal_uid`, the realistic consolidation case
- `PUT /banks` rejects unknown bank-config fields
- the bank listing paginates and sorts by `last_write_at` DESC
- missing banks are 404; invalid `tags_match` / `time_field` are 422

It then exercises the seeded dataset, idempotent re-seeding, timeline ordering, synthesis grounding, thin-competitor refusal, time anchoring and evidence staleness, live ingestion, and each malformed-LLM recovery path.

It is not a substitute for a live run — it proves the client matches the documented contract, not that your account is provisioned. Run it first, then point at real Hindsight.

### What the live run actually returned

Both bugs above were found by running the code against the real API rather than by reading the spec, and the two interesting results are worth stating plainly.

**On the rich competitor, the real model found the planted pattern and ignored the decoy.** Nimbus AI's 12 signals contain an ~8-week chain — funding → GTM hires → price cut → volume discount → enterprise messaging → SSO/SCIM → enterprise hiring → campaign → enterprise tier — plus one *unrelated* funding round placed on 2026-02-27 that belongs to no chain. The model dated the sequence, estimated the 2–4 week gaps, and did not fold the stray round into the narrative. Its prediction was falsifiable and self-dated: a formal Enterprise onboarding/partner program by early October 2026, to be refuted by its absence after 2026-10-10.

**On thin competitors it refused, and that took a prompt fix.** Vertex Cloud (4 signals) and Pathfinder Labs (3) originally produced a *contradiction*: `patterns` correctly said "insufficient evidence of a recurring pattern", and then `predicted_next_move` went ahead and speculated anyway ("if they follow a typical quarterly cadence, they might…"). A hedged forecast of a strategy the model had just admitted it could not identify is a fabrication wearing a disclaimer. The grounding rules now require all four fields to agree — when `patterns` reports insufficient evidence, the prediction must decline and name the evidence that would settle it, and the recommendation must be to keep collecting. Both thin competitors now return a coherent, consistent refusal that names the specific missing signal types.

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

| File | Role |
|---|---|
| `backend/config.py` | env loading, Hindsight base-URL normalisation, model/timeout knobs |
| `backend/models.py` | Pydantic schemas, date coercion, signal uids |
| `backend/hindsight_client.py` | **bank-per-competitor read/write, chronological-complete retrieval** |
| `backend/llm_client.py` | Groq wrapper: retries, model fallback, JSON salvage |
| `backend/ingestion.py` | raw text → structured Signal (+ heuristic fallback) |
| `backend/synthesis.py` | timeline → strategic narrative (the differentiator) |
| `backend/routes.py` | HTTP endpoints |
| `backend/main.py` | FastAPI app, CORS for Streamlit |
| `scripts/seed_data.py` | loads the synthetic dataset into Hindsight |
| `frontend/app.py` | Streamlit UI |
| `tests/hindsight_double.py` | OpenAPI-faithful Hindsight + Groq contract double |
| `tests/selfcheck.py` | 140-check offline end-to-end suite |

### API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | config status |
| `GET` | `/competitors` | every competitor + size/freshness of its memory |
| `POST` | `/competitors` | register a competitor, create its bank |
| `POST` | `/signals` | raw text → LLM-extracted Signal → Hindsight |
| `GET` | `/timeline/{competitor}` | complete chronological timeline |
| `POST` | `/synthesize` | strategic read |

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

This writes 71 signals across 10 competitors into Hindsight and reads them back. Re-running without `--reset` is safe *for identical data* — signals are keyed by `document_id`, so unchanged rows are replaced rather than duplicated.

**Editing a seeded signal's date orphans the original.** The uid is `slug | date | type | digest`, and that uid *is* the Hindsight `document_id`, so changing a date produces a new document and the old one stays behind. Brightline's bank silently held 13 signals where the file had 7 — and because the orphans were the same announcements a fortnight earlier, the synthesis read them as a genuine "announce, then reinforce a week later" cadence and reported it as a finding. `--verify` now fails the command on any read-back mismatch and names the orphaned rows; `--reset` rebuilds the bank.

### Run

```bash
# terminal 1 — API
uvicorn backend.main:app --reload --port 8000

# terminal 2 — UI
streamlit run frontend/app.py
```

Open http://localhost:8501. Interactive API docs at http://localhost:8000/docs.

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

| Competitor | n | Arc | Evidence |
|---|---|---|---|
| Nimbus AI | 12 | Series C → enterprise land-grab | **stale** (14-day rhythm, 40d quiet) |
| Palisade Security | 11 | Incident → compliance rebuild → federal | fresh (3d) |
| Lumen Health | 9 | Regulated-market compliance chain | **aging** (48d) |
| Corvus Data | 8 | Open-core → commercial cloud | fresh (4d) |
| Halcyon Mobility | 8 | Utility pricing → fleet platform | fresh (6d) |
| Brightline Retail | 7 | Metronomic 28-day cadence, then silence | **stale** (28-day rhythm, 84d quiet) |
| Ferrous Systems | 6 | Hardware → software and services | fresh (12d) |
| Vertex Cloud | 4 | Uncorrelated incumbent | fresh (47d, but 76-day rhythm) |
| Pathfinder Labs | 3 | Sparse devtools | fresh (54d) |
| Tidewater Analytics | 3 | Sparse BI vendor | fresh (53d) |

Three things are worth pulling out:

- **Brightline Retail is the stale-evidence case that matters.** Its cadence is exactly 28 days for six consecutive intervals, then stops. The gap cannot distinguish *the cadence broke* from *the cadence continued and someone stopped watching*, and the read says so rather than projecting the old rhythm as if it were live. Live, it reports the cadence, forecasts to 2026-10-15, and adds *"this forecast rests on a signal stream that has been quiet for 84 days, so confidence is limited."*
- **Vertex Cloud is the borderline case.** At 4 signals it clears the prompt's count rule ("fewer than 4 signals") but not its substance rule. Live, the model computes the intervals itself (77d, 83d, 43d), finds no cadence, and refuses on that basis — which exercises the second clause of the rule independently of the first. The offline double refuses below 6 signals, so it can only prove the app *surfaces* that refusal; this band is verified against Groq, not the double.
- **The infrequent competitors are not stale.** Vertex Cloud (76-day rhythm), Pathfinder Labs (70-day) and Tidewater Analytics (91-day) are all quiet for 47–54 days and all read as current. A fixed 30-day threshold would have flagged all three, and warned you about companies that simply do not announce often.

---

## Demo script

1. **One signal means nothing.** Select *Nimbus AI*, set memory depth to **"1 signal (no pattern possible)"**. "That's the entire story: they raised money. Congratulations."
2. **Reveal the accumulation.** Switch to **"Full timeline"** — 12 signals over six months, colour-coded by type. "This wasn't scraped. Every one of these is a separate memory write, and they're all still there."
3. **Get the strategic read.** Click **🧠 Get Strategic Read**. The output connects funding → hiring → pricing → messaging, and ends in a falsifiable prediction. Note the caption: *built from 12 signals (2026-02-18 to 2026-08-19)*, and the warning that the evidence is 40 days old.
4. **Contrast.** Switch to *Vertex Cloud* and read it again: "Only 4 signals, no repeated cadence, no ordering. The evidence is insufficient to identify a pattern." The agent declines to fabricate — which is the harder and more valuable behaviour to demonstrate.
5. **Stale evidence.** Switch to *Brightline Retail*. The pattern section finds a precise 28-day cadence, the prediction is dated forward, and both carry the caveat that the timeline stopped 84 days ago. This is what a correct answer looks like when the data has gone cold.

Optional: the **Log a new signal** expander shows live ingestion — paste a raw note, the LLM extracts `{signal_type, date, summary, source}`, and it is written to that competitor's Hindsight bank and appears on the timeline.

---

## Robustness

The brief warns that Groq's `gpt-oss` models intermittently produce malformed or tool-call-shaped responses. `backend/llm_client.py` handles it, and the paths below are all exercised:

- **`response_format` rejected (HTTP 400)** → the retry drops JSON mode and asks again, rather than repeating a request that already failed. Worst case 2 models × 3 attempts, and `call_llm_json` does not re-loop, so a bad provider cannot multiply requests. The 400 is not hypothetical: `gpt-oss` accepts `response_format` only when the prompt contains the literal word *json* (it routes through a structured-output path), and it additionally returns a `reasoning` field that has to be stripped before parsing. Both prompts contain the word.
- **Model unavailable** → falls back from `openai/gpt-oss-120b` to `qwen/qwen3.8-27b`, skipping any candidate this account is not actually served. This is not theoretical either: the original fallback `qwen/qwen3-32b` returned no match on `GET /models` for the account this was tested against, which is why the list is now probed and filtered at call time instead of assumed.
- **Markdown fences / preamble / `<think>` block / unclosed `<tool_call>`** → stripped, then the first balanced `{...}` is extracted.
- **Truncated completion** (stop token mid-JSON, including nested) → salvaged: close the open string, drop the dangling incomplete pair, append exactly the missing closers. Verified against `{"a":1,"b":{"c":"cut` and similar.
- **Trailing commas, single quotes, smart quotes** → repaired in escalating order of desperation, and only when a strict parse has already failed. An apostrophe in prose is never mangled into a delimiter.
- **LLM fully unavailable** → `ingestion.py` falls back to a keyword/date heuristic and still writes a usable signal; `synthesis.py` returns an honest degraded read that reports the span and per-type counts instead of pretending.
- **Signal date ambiguity** → `03/04/2026` is read as US month-first; `13/04/2026` can only be day-first. Separators (`/`, `.`, `-`) are normalised so one format list covers all of them.
- **Hindsight unreachable** → the API returns `502` with the underlying error, and the UI reports it rather than rendering an empty timeline.

---

## Notes and limitations

- **Synthetic data, on purpose.** Real competitor tracking needs scraping and will fail live. The seeded dataset makes the demo reproducible.
- **`GET /competitors` returns objects, not bare strings.** It adds `signal_count`, `fact_count` and `last_write_at` so the sidebar can show memory accumulating — the growth *is* the pitch.
- **Extraction quality is only as good as the note.** The heuristic fallback is deliberately dumb; it exists so a demo button never dead-ends, not as a production path.
- **Semantic recall is unused by design.** That is the right call for pattern detection, but it means "what did Nimbus say about audit logs?" is answered by string-matching the timeline rather than by Hindsight's graph search. Adding it as a *secondary* lookup would be a natural next step — never as the primary timeline source.
- **`synthesis.py` spends a whole timeline into one prompt.** Past a few hundred signals per competitor this needs chunking or Hindsight's own `reflect` operation to stay inside the context window.
