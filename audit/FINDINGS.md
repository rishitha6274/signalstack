# Findings — three audited defects, fixed, then verified live

Date: 2026-09-28. Commit `186a2f8` (local, not pushed). One additional 10-competitor
sweep after the fixes, run once, 10s apart, honouring `Retry-After`.

## Result

| | baseline | first post-fix sweep | **after the 3 fixes** |
|---|---|---|---|
| A figures measured | 3 fail | 0 | **0** |
| B staleness acknowledged | 2 fail | 0 | **0** |
| C forecast follows the evidence | 1 fail | 0 | **0** |
| D calibration | 7 fail | 0 | **0** |
| E cadence grounded | 6 fail | 0 | **0** |
| withheld narratives | — | 3 | **0** |
| refusals | 3 | 3 | 5 |

10/10 returned a validated read. 5 narratives, 5 refusals, **0 withheld**.

Narratives: Brightline Retail (medium, 56d overdue), Corvus Data (high),
Halcyon Mobility (high), Lumen Health (low, 22d overdue), Nimbus AI (low, 26d overdue).

Refusals: Ferrous Systems, Palisade Security, Pathfinder Labs, Tidewater Analytics,
Vertex Cloud.

## The three defects, and the evidence each fix is real

**1. The notice contradicted the validator.** The retry notice said confidence must be
one of high/medium/low; the validator required `none` for a refusal. A refusal was
rejected, told to report one of the three, and rejected again.

Fixed by making `validators.allowed_confidence_values(refused)` the only statement of
the contract, with the notice asking that function rather than restating it. Live
evidence: the previous sweep rejected two refusals for this; this sweep has **no
rejection of that kind**, and no read was withheld.

**2. Retries did not converge.** The notice listed the true figures without saying the
list was closed, so Lumen's retry replaced 14 days with 7 and Halcyon's wrote 30 days;
both were withheld.

Fixed by enumerating the permitted counts and stating the enumeration is exhaustive.
Live evidence: **Lumen was rejected once for `interval claim '14 days'`, retried, and
came back validated** at `low`. The same competitor, the same complaint, now converges.
In the first sweep that exact retry produced a second wrong number and a withheld read.

**3. Spelling decided whether a claim was checked.** The interval pattern was
digits-only, so `2 weeks` was checked and `two weeks` was not.

Fixed by reading amounts in words, treating the article as not-a-quantity, and gating
bare "a month" on a nearby cadence cue. Live evidence: **Brightline was rejected for
`interval claim 'two weeks'`** — in the previous sweep that phrasing would have passed
the check untouched — retried, and came back validated at `medium`.

## Rate limiting

9 × HTTP 429, every one honoured with the server's own wait (4s–30s), none exhausted.
Two `OSError(49)` connection failures (ephemeral-port exhaustion on this machine): one
on `/competitors`, which fell back to the local registry, and one on a Groq call, which
succeeded on attempt 2. Neither cost a read.

## One auditor false positive, corrected

The first run of the corrected code reported C FAIL on Nimbus AI: "its own declared
cycle is hiring -> pricing -> messaging -> feature; ... the forecast predicts a feature
signal, skipping it."

**That is a defect in my audit harness, not in the read.** Nimbus's `patterns` opens
"Earlier signals show a **one-off sequence** Funding -> Messaging ... Hiring -> Pricing
... Pricing -> Messaging ... Messaging -> Feature" and then states that the only
transition that repeats is Feature -> Hiring. The C rule was a bare regex for a run of
stage names, so it read an enumeration of one-offs as a declared four-stage cycle.

The rule now requires the match to sit in a sentence that actually declares repetition
and skips sentences that disclaim it. Positive control: a read that genuinely declares
the four-stage cycle and predicts the wrong stage still FAILS, with the same message.
The rule was narrowed, not disabled.

Nimbus's numbers check out independently: intervals `[9, 6, 6, 13, 22, 21, 14, 14, 21,
21, 35]`, median 14, last signal 2026-08-19, 40 days old, 26 days past its own rhythm —
which is what the read says.

## Open, not fixed — needs your call

**Palisade Security refused with 11 signals.** Its first attempt was rejected with
`a refusal must report confidence 'none'`, which means `_is_refusal` classified a
narrative as a refusal; the retry then wrote a real refusal. The outcome is defensible —
I checked, all ten of Palisade's transitions are unique, so "no repeating ordering can
be identified" is true — but it was reached by a misfire, and Palisade produced a
narrative in the first sweep. The heuristic is imprecise, not the data.

Not touched, per your instruction. `_is_refusal` is in `backend/synthesis.py`.

## Unverified

- The offline double refuses below 6 signals, so it proves the app *surfaces* a
  refusal, not that Groq refuses there. Only the live run covers that band.
- Hindsight credit balance before/after is not queryable; only the dashboard shows it.
- The 5 refusals were not each independently re-run; they are as the model wrote them
  on this one pass.
