"""Signal Stack — Streamlit UI.

    streamlit run frontend/app.py

Three panels, in demo order:
  1. Timeline    — the accumulated memory, oldest first, colour-coded by type
  2. Log signal  — live ingestion, LLM extracts the structure
  3. Strategic read — cross-signal synthesis from the complete timeline

The "Memory depth" control exists for the demo: start on a single signal to
make the point that one signal is meaningless, then reveal the full timeline.
"""

from __future__ import annotations

import os
from typing import Any, Optional

import requests
import streamlit as st

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
# The API base URL is resolved in priority order:
#   BACKEND_URL        — what the deployed frontend uses to reach the deployed
#                         backend. This is the name the deploy docs and the
#                         host dashboards set.
#   SIGNAL_STACK_API   — the original single-image name, still honoured so the
#                         Docker/local path (UI and API in one container over
#                         loopback) keeps working untouched.
#   localhost:8000     — local development fallback.
API_BASE = (
    os.getenv("BACKEND_URL") or os.getenv("SIGNAL_STACK_API") or "http://localhost:8000"
).rstrip("/")
REQUEST_TIMEOUT = int(os.getenv("SIGNAL_STACK_TIMEOUT", "180"))
# Optional write key, forwarded to the API on POSTs when set. Same variable
# name the backend reads, so setting it once on both services is enough.
# Unset locally: the API's guard is a no-op, so the header is simply omitted
# rather than sent empty.
API_KEY = (os.getenv("API_KEY") or "").strip()
WRITE_HEADERS = {"X-API-Key": API_KEY} if API_KEY else {}

TYPE_COLORS = {
    "pricing": "#e8590c",
    "feature": "#1c7ed6",
    "hiring": "#0ca678",
    "messaging": "#7048e8",
    "funding": "#f08c00",
}
TYPE_ICONS = {
    "pricing": "💲",
    "feature": "🚀",
    "hiring": "👥",
    "messaging": "📣",
    "funding": "💰",
}

SECTIONS = [
    ("Patterns observed", "patterns", "🔗"),
    ("Inferred strategic intent", "inferred_intent", "🎯"),
    ("Predicted next move", "predicted_next_move", "🔮"),
    ("Recommended action", "recommendation", "⚡"),
]

# The note behind "Try the sample". Dated inside the seeded window and chosen
# so that taking Vertex Cloud from 4 signals to 5 crosses the app's evidence
# floor — at which point the read is permitted to forecast instead of
# refusing. tests/selfcheck.py section 5b pins that this actually happens, so
# the sample cannot silently stop working if the seed or the floor changes.
SAMPLE_VERTEX_SIGNAL = (
    "Vertex Cloud posted a compliance engineer opening on 2026-09-14, its first "
    "security hire, and named FedRAMP readiness as the requirement."
)


# --------------------------------------------------------------------------
# API helpers
# --------------------------------------------------------------------------
def api(method: str, path: str, **kwargs) -> Any:
    # The write key rides on writes only, and only when configured. GETs stay
    # unauthenticated on purpose so the demo's reads need no key. Keyed off the
    # method rather than assumed, because the comment above used to describe a
    # behaviour the code did not have: a shared secret sent on reads widens its
    # exposure to every log line and proxy that handles a GET.
    headers = {**(WRITE_HEADERS if method.upper() not in ("GET", "HEAD") else {}),
               **kwargs.pop("headers", {})}
    response = requests.request(
        method, f"{API_BASE}{path}", timeout=REQUEST_TIMEOUT, headers=headers,
        **kwargs
    )
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        raise RuntimeError(f"{method} {path} -> {response.status_code}: {detail}")
    return response.json()


@st.cache_data(ttl=15, show_spinner=False)
def fetch_competitors(_signature: float = 0.0) -> list[dict]:
    return api("GET", "/competitors")["competitors"]


def fetch_timeline(competitor: str) -> list[dict]:
    return api("GET", f"/timeline/{competitor}")["signals"]


def memory_delta(competitor: str, stored: dict) -> None:
    """What actually changed in memory, read back from Hindsight.

    Three things, in this order, because they answer three different questions
    a user has just after logging a note: how much do I have now (count), is
    the store consistent with that count (fact_count), and when did it last
    change (last_write_at). The new signal itself is shown as a row in the
    same shape the timeline uses, fetched back from memory rather than echoed
    from the POST response — a write that reported success but did not land
    would otherwise render as a success.

    Rendered even when the follow-up read is rate-limited or absent. The write
    is the irreversible part of the interaction, so its receipt must not be
    conditional on a second, separately-failable LLM call.
    """
    try:
        rows = fetch_competitors()
        bank = next((r for r in rows if r.get("name") == competitor), None)
    except Exception:  # noqa: BLE001
        bank = None
    if bank:
        st.caption(
            f"**{bank.get('signal_count', '?')} signals** in `{bank.get('bank_id')}`"
            f" · {bank.get('fact_count', '?')} memory facts"
            f" · last written {bank.get('last_write_at') or 'unknown'}"
        )
    else:
        st.caption(f"Stored against **{competitor}**.")

    try:
        timeline = fetch_timeline(competitor)
    except Exception as exc:  # noqa: BLE001
        st.caption(f"Could not read the timeline back: {friendly_read_error(exc)}")
        return
    match = [r for r in timeline if r.get("uid") == stored.get("uid")]
    row = match[0] if match else (timeline[-1] if timeline else None)
    if row:
        st.markdown(
            f"- `{row.get('date')}` · {row.get('signal_type')} · "
            f"{row.get('summary', '')[:160]}"
        )
    else:
        st.markdown(f"- `{stored.get('date')}` · {stored.get('signal_type')} *(read-back pending)*")


def type_badge(signal_type: str) -> str:
    color = TYPE_COLORS.get(signal_type, "#868e96")
    icon = TYPE_ICONS.get(signal_type, "•")
    return (
        f'<span style="background:{color};color:#fff;border-radius:10px;'
        f'padding:2px 9px;font-size:11px;font-weight:700;letter-spacing:.4px;'
        f'text-transform:uppercase;white-space:nowrap">{icon} {signal_type}</span>'
    )


# Confidence is a claim about the read, not decoration, so it gets a badge rather
# than a word buried in prose. The colours are ordered by how much weight the
# answer can bear, and "none" is grey and hollow to mark a refusal — an absence
# of a forecast, which is a different thing from a weak one.
CONFIDENCE_STYLES = {
    "high": ("#15803d", "#dcfce7", "▲", "high confidence"),
    "medium": ("#a16207", "#fef9c3", "◆", "medium confidence"),
    "low": ("#c2410c", "#ffedd5", "▼", "low confidence"),
    "none": ("#6b7280", "#f3f4f6", "—", "no prediction made"),
}


def confidence_badge(confidence: str | None) -> str:
    fg, bg, glyph, label = CONFIDENCE_STYLES.get(
        str(confidence or "none").strip().lower(),
        CONFIDENCE_STYLES["none"],
    )
    return (
        f'<span style="background:{bg};color:{fg};border:1px solid {fg}33;'
        f'border-radius:10px;padding:3px 11px;font-size:11px;font-weight:700;'
        f'letter-spacing:.4px;text-transform:uppercase;white-space:nowrap">'
        f"{glyph} {label}</span>"
    )


def friendly_read_error(exc: Exception) -> str:
    """Turn a failed read into something a person can act on.

    A 429 is the one failure that is not the reader's fault and not a bug, so
    it gets a wait, not a stack trace. `api` raises the response text, which
    for a rate limit carries the number of seconds the provider asked for.
    """
    text = str(exc)
    lowered = text.lower()
    if "429" in lowered or "rate limit" in lowered or "too many requests" in lowered:
        import re

        wait = re.search(r"(\d+(?:\.\d+)?)\s*s(?:econds)?", text)
        secs = f" about {int(float(wait.group(1)))}s" if wait else ""
        return (
            f"**Groq's rate limit is being hit for this minute.** Nothing is wrong with "
            f"the data or your key — wait{secs} and read again. The signals you logged "
            "are already stored."
        )
    if "401" in lowered or "unauthorized" in lowered:
        return (
            "**The API rejected the request (401).** If `API_KEY` is set, the "
            "frontend and backend need the same value."
        )
    return f"Read failed: {text}"


def read_headline(read: dict, key: str) -> str:
    """One line of a read, for the side-by-side diff."""
    value = str(read.get(key) or "").strip()
    if not value:
        return "—"
    return value if len(value) <= 400 else value[:397].rstrip() + "…"


# The fields worth diffing after a signal lands. Chosen because each one is a
# claim the reader might act on: how much memory, how much to trust it, how old
# the evidence is, what happens next, and what would change their mind. A diff
# that showed only the prose would miss the one thing a new signal usually
# moves — the confidence.
DIFF_FIELDS = [
    ("Signals in memory", "signal_count"),
    ("Confidence", "confidence"),
    ("Evidence staleness", "evidence_staleness"),
    ("Predicted next move", "predicted_next_move"),
    ("What would raise confidence", "missing_evidence"),
]


# --------------------------------------------------------------------------
# Page setup
# --------------------------------------------------------------------------
st.set_page_config(
    page_title="Signal Stack",
    page_icon="🛰",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Deferred writes to the raw-signal box, applied before the widget exists.
# A widget's key may only be assigned before that widget is instantiated in the
# current run, so a clear requested after ingestion -- and a pre-fill requested
# by the sample button -- are recorded as flags here and consumed on the next
# run. Writing st.session_state["ss_raw_signal"] anywhere below the text area
# raises StreamlitAPIException, not just on the click that triggers it but on
# every rerun that reaches that line.
_pending_value = st.session_state.pop("ss_pending_value", None)
if _pending_value is not None:
    st.session_state["ss_raw_signal"] = _pending_value
if st.session_state.pop("ss_pending_clear", False):
    st.session_state.pop("ss_raw_signal", None)

st.markdown(
    """
    <style>
      .block-container { padding-top: 2.2rem; max-width: 1150px; }
      .ss-head { font-size: 2.1rem; font-weight: 800; letter-spacing: -0.6px; margin: 0; }
      .ss-sub { color: #6b7280; font-size: 0.95rem; margin: 2px 0 18px 0; }
      .ss-stat { background:#f6f7f9; border:1px solid #e6e8eb; border-radius:10px;
                 padding:10px 14px; text-align:center; }
      .ss-stat-num { font-size:1.45rem; font-weight:800; line-height:1.1; }
      .ss-stat-lbl { font-size:0.68rem; color:#6b7280; text-transform:uppercase;
                     letter-spacing:.6px; margin-top:2px; }
      .ss-tl { border-left:3px solid #dee2e6; padding:0 0 4px 18px; margin-left:7px; }
      .ss-tl-item { position:relative; margin-bottom:16px; }
      .ss-tl-item::before { content:""; position:absolute; left:-25px; top:5px;
                            width:11px; height:11px; border-radius:50%;
                            background:#495057; border:2px solid #fff; }
      .ss-date { font-size:0.76rem; color:#6b7280; font-weight:600; letter-spacing:.3px; }
      .ss-sum { font-size:0.95rem; line-height:1.45; margin:3px 0 5px 0; }
      .ss-meta { font-size:0.72rem; color:#868e96; }
      .ss-sec { background:#fbfbfc; border:1px solid #e6e8eb; border-left:4px solid #1c7ed6;
                border-radius:8px; padding:14px 18px; margin-bottom:12px; }
      .ss-sec-t { font-size:0.76rem; font-weight:800; letter-spacing:.8px;
                  text-transform:uppercase; color:#1c7ed6; margin-bottom:6px; }
      .ss-sec-b { font-size:0.95rem; line-height:1.6; white-space:pre-wrap; color:#1f2933; }
    </style>
    """,
    unsafe_allow_html=True,
)


# --------------------------------------------------------------------------
# Sidebar
# --------------------------------------------------------------------------
with st.sidebar:
    st.markdown("### 🛰 Signal Stack")
    st.caption("Persistent-memory competitive intelligence")

    # Any rerun (selection change, button press) invalidates the competitor cache.
    competitors = fetch_competitors(0.0)

    if not competitors:
        st.warning("No competitors in memory yet.")
        st.code("python scripts/seed_data.py", language="bash")
        st.stop()

    names = [c["name"] for c in competitors]
    selected = st.selectbox("Competitor", names, label_visibility="collapsed")

    record = next((c for c in competitors if c["name"] == selected), {})
    bank_id = record.get("bank_id", "")

    st.markdown("---")
    st.markdown(
        f"""
        <div class="ss-stat"><div class="ss-stat-num">{record.get('signal_count', 0)}</div>
        <div class="ss-stat-lbl">Signals in memory</div></div>
        """,
        unsafe_allow_html=True,
    )
    st.caption(f"**Namespace** `{bank_id}`")
    st.caption(f"**Stored** {record.get('last_write_at') or '—'}")
    if record.get("first_signal"):
        st.caption(f"**Window** {record['first_signal']} → {record['last_signal']}")
    if record.get("fact_count"):
        st.caption(f"**Memory units** {record['fact_count']}")

    # The point of the sidebar is that memory accumulates, so the growth is
    # shown as growth. A raw count alone reads as a static fact; "was 4, now 5,
    # written 2 minutes ago" reads as an event, which is the product.
    _prev_counts = st.session_state.setdefault("ss_seen_counts", {})
    _now_count = record.get("signal_count", 0)
    _was = _prev_counts.get(selected)
    if _was is not None and _now_count != _was:
        _direction = "grew" if _now_count > _was else "shrank"
        st.caption(f"📈 **{_direction} {_was} → {_now_count}** this session")
    _prev_counts[selected] = _now_count

    st.markdown("---")
    # A demo that mutates the seeded bank has to be runnable twice. Without
    # this, the second pass through the hero flow cannot be rehearsed and the
    # only recovery is a terminal, mid-talk. The backend decides whether this
    # is even offered: the route only exists when ENABLE_DEMO_RESET=1, so
    # showing a button that would 404 is worse than showing nothing.
    _reset_ok = False
    try:
        _reset_ok = bool(api("GET", "/health").get("demo_reset_enabled"))
    except Exception:  # noqa: BLE001
        # Health is already reported in full below; do not double-report it.
        _reset_ok = False
    if _reset_ok:
        with st.expander("Demo controls"):
            st.caption(
                "Logging a signal writes to the shared bank. Resetting deletes every "
                "competitor's memory — irreversibly — so re-seed afterwards."
            )
            st.code("python scripts/seed_data.py --reset --verify", language="bash")
            # Two clicks, because a single misplaced one destroys ten competitors.
            if st.checkbox("I understand this deletes all stored memory"):
                if st.button("🗑 Reset demo data", type="secondary"):
                    try:
                        result = api("POST", "/demo/reset", json={})
                    except Exception as exc:  # noqa: BLE001
                        st.error(friendly_read_error(exc))
                    else:
                        st.session_state.pop("ss_reads", None)
                        st.session_state.pop("ss_last_signal", None)
                        st.session_state.pop("ss_last_diff", None)
                        st.session_state.pop("ss_seen_counts", None)
                        st.session_state.pop("ss_auto_reread", None)
                        fetch_competitors.clear()
                        st.success(
                            f"Deleted {result['count']} bank(s). Re-seed with "
                            f"`{result['reseed']}`."
                        )
    else:
        with st.expander("Demo controls"):
            st.caption(
                "Reset is disabled on this deployment (`ENABLE_DEMO_RESET=0`). "
                "To restore the demo data, run this where the API is configured:"
            )
            st.code("python scripts/seed_data.py --reset --verify", language="bash")

    st.markdown("---")
    with st.expander("System status"):
        try:
            health = api("GET", "/health")
            st.success("API reachable")
            for problem in health.get("problems", []):
                st.warning(problem)
        except Exception as exc:  # noqa: BLE001
            st.error(f"API unreachable: {exc}")
            st.code(f"uvicorn backend.main:app --port 8000", language="bash")


# --------------------------------------------------------------------------
# Main panel
# --------------------------------------------------------------------------
st.markdown('<p class="ss-head">Signal Stack</p>', unsafe_allow_html=True)
st.markdown(
    '<p class="ss-sub">Every signal this agent has ever seen for a competitor, '
    "in order — and what the pattern across them means.</p>",
    unsafe_allow_html=True,
)

st.markdown(f"## {selected}")

# --- configuration gate ---------------------------------------------------
# A missing key produces a raw 401 from Hindsight that tells the user nothing
# actionable. Check config first and show the setup steps instead.
try:
    health = api("GET", "/health")
except Exception as exc:  # noqa: BLE001
    st.error(f"API unreachable: {exc}")
    st.code("uvicorn backend.main:app --port 8000", language="bash")
    st.stop()

if not health.get("hindsight_configured"):
    st.warning("**Hindsight is not connected** — no signals can be read or stored yet.")
    for problem in health.get("problems", []):
        st.warning(f"• {problem}")
    st.code("cp .env.example .env   # then fill in HINDSIGHT_API_KEY", language="bash")
    st.code("python scripts/seed_data.py --reset --verify", language="bash")
    st.stop()

# --- timeline -------------------------------------------------------------
try:
    signals = fetch_timeline(selected)
except Exception as exc:  # noqa: BLE001
    st.error(f"Could not load the timeline: {exc}")
    st.stop()

if not signals:
    st.info("No signals in this memory bank yet. Log one below to start the timeline.")
else:
    span = (
        f"{signals[0]['date']} → {signals[-1]['date']}"
        if len(signals) > 1
        else f"{signals[0]['date']} (single signal)"
    )

    col_stats, col_depth = st.columns([3, 2])
    with col_stats:
        counts: dict[str, int] = {}
        for signal in signals:
            counts[signal["signal_type"]] = counts.get(signal["signal_type"], 0) + 1
        breakdown = "  ·  ".join(f"{k} {v}" for k, v in sorted(counts.items()))
        st.caption(f"**{len(signals)}** signals  ·  {span}  ·  {breakdown}")
    with col_depth:
        # The demo beat: one signal means nothing; the pattern needs the span.
        # A radio (not a slider) so every option's label is actually readable.
        total = len(signals)
        choices = {"1 signal (no pattern possible)": 1}
        if total > 3:
            choices[f"Last {total // 2} signals"] = total // 2
        choices["Full timeline"] = total
        labels = list(choices)
        chosen = st.radio(
            "Memory depth",
            labels,
            index=len(labels) - 1,
            horizontal=True,
            label_visibility="collapsed",
        )
        depth = choices[chosen]

    visible = signals[:depth] if depth < len(signals) else signals

    if depth < len(signals):
        st.caption(
            f"Showing the oldest {len(visible)} of {len(signals)} signals. "
            f"A {len(signals)}-signal timeline is held in Hindsight memory — "
            "the full span is retrieved on every strategic read."
        )

    rows = "\n".join(
        f"""<div class="ss-tl-item">
              <div class="ss-date">{signal['date']}</div>
              <div class="ss-sum">{signal['summary']}</div>
              {type_badge(signal['signal_type'])}
              <span class="ss-meta">&nbsp;&nbsp;{signal.get('source', '')}</span>
            </div>"""
        for signal in visible
    )
    st.markdown(f'<div class="ss-tl">{rows}</div>', unsafe_allow_html=True)


# --- live ingestion -------------------------------------------------------
with st.expander("➕ Log a new signal (live LLM extraction)", expanded=False):
    st.caption("Paste a headline, pricing snapshot, job post, or messaging excerpt.")
    # This writes to the shared memory bank, permanently and for every user of
    # this deployment. It is the least reversible thing in the app, so it says
    # so here rather than in a README nobody reads mid-demo.
    st.warning(
        "Logging a signal **writes to the seeded memory bank** and is visible to everyone "
        "using this deployment. It cannot be undone from the UI. "
        "`python scripts/seed_data.py --reset --verify` restores the demo data.",
        icon="⚠️",
    )

    # The hero path: one click fills a note that flips Vertex Cloud from a
    # refusal into a forecast. Hardcoded because a demo that depends on the
    # demoist pasting the right words at the right moment is not a demo.
    if selected == "Vertex Cloud":
        if st.button("🧪 Try the sample", help=(
            "Fills a pre-written signal that takes Vertex Cloud from 4 to 5 "
            "signals — the evidence floor — so the read below flips from a "
            "refusal to a forecast."
        )):
            st.session_state["ss_pending_value"] = SAMPLE_VERTEX_SIGNAL
            # The flag above is read at the top of the NEXT run, because this
            # run has already passed that point. Rerunning is what makes the
            # box fill: without it the pre-fill would sit in the flag until the
            # user happened to trigger an unrelated rerun.
            st.rerun()
        st.caption(
            "Vertex Cloud is seeded deliberately thin. Its read refuses today; "
            "the sample signal takes it to the 5-signal floor and it forecasts."
        )
    else:
        st.caption(
            f"Tip: **{selected}** is seeded with enough signals to forecast. "
            "For the refusal-to-forecast demo, switch to Vertex Cloud."
        )

    raw = st.text_area(
        "Raw text",
        height=110,
        key="ss_raw_signal",
        placeholder=(
            "e.g. Nimbus AI opens a Senior Solutions Architect role in the "
            "enterprise segment, the third such posting this quarter."
        ),
        label_visibility="collapsed",
    )
    if st.button("Log Signal", type="primary", disabled=not raw.strip()):
        # Held across the rerun: Streamlit clears the expander's widgets on the
        # next run, and the diff is the whole point of the click.
        with st.spinner("Extracting structure and writing to Hindsight…"):
            try:
                new_signal = api(
                    "POST", "/signals", json={"competitor": selected, "raw_text": raw}
                )
            except Exception as exc:  # noqa: BLE001
                st.error(friendly_read_error(exc))
            else:
                st.session_state["ss_last_signal"] = new_signal
                # Clearing the box stops the same note being logged twice on a
                # double click, which would silently add two signals. Deferred:
                # the text area's key was instantiated above, and Streamlit
                # forbids writing a widget key after its widget exists. The
                # block at the top of this script pops the key on the next run.
                st.session_state["ss_pending_clear"] = True
                fetch_competitors.clear()
                st.rerun()

# --- what the new signal changed -----------------------------------------
_pending = st.session_state.get("ss_last_signal")
if _pending and _pending.get("competitor") == selected:
    st.markdown("---")
    st.markdown("### 🧠 What your signal changed")
    st.success(
        f"Stored as **{_pending['signal_type']}** dated **{_pending['date']}** "
        f"in `{bank_id}`."
    )
    with st.expander("The stored signal", expanded=False):
        st.json(_pending)

    # The write's receipt comes first and unconditionally. The follow-up read
    # is a second, separately-failable LLM call, and hiding a successful write
    # behind its failure would report a loss that did not happen.
    memory_delta(selected, _pending)

    _before = st.session_state.get("ss_reads", {}).get(selected)
    _uid = _pending.get("uid", _pending.get("date"))
    if _before:
        # Automatic, because the user has already paid for one read in this
        # session and the diff is the point of logging anything: making them
        # click again to find out what their own input did is the kind of
        # friction that hides the mechanism being demonstrated. Cost is the
        # same call they already made once, and a rate limit is reported
        # below rather than swallowed.
        st.caption(
            "Comparing the read you already generated against a fresh one over the "
            "same timeline plus your new signal."
        )
        # Guarded per signal: the block re-renders on every Streamlit rerun, and
        # an unguarded call here would re-read the timeline on each interaction
        # and quietly spend the user's quota.
        _auto_done = st.session_state.setdefault("ss_auto_reread", [])
        if _uid not in _auto_done:
            _auto_done.append(_uid)
            with st.spinner("Reading the updated timeline…"):
                try:
                    _after = api("POST", "/synthesize", json={"competitor": selected})
                except Exception as exc:  # noqa: BLE001
                    st.warning(friendly_read_error(exc))
                else:
                    if _after.get("rate_limited"):
                        _wait = _after.get("retry_after_seconds")
                        st.warning(
                            "**Groq's rate limit was hit**, so the comparison read "
                            "did not run"
                            + (f" — wait about {int(_wait)}s" if _wait else "")
                            + ". Your signal is stored and the memory counts above are "
                            "real; only the side-by-side forecast is missing. Generate "
                            "a read again in a moment to see the diff."
                        )
                    else:
                        st.session_state.setdefault("ss_reads", {})[selected] = _after
                        # Tagged with the signal that produced it. Without this, a
                        # second log would show a diff against a read taken before
                        # the first, quietly attributing two signals' worth of
                        # change to one click.
                        st.session_state["ss_last_diff"] = (_before, _after, _uid)
                        st.rerun()
    else:
        # Nothing to diff against, so nothing is run. A hidden LLM call is
        # worse than an absent one: the user would be billed for a read they
        # did not ask for, on data they had not finished entering.
        st.info(
            "Memory has grown. Generate a read below to see whether the new "
            "signal changed the answer."
        )
        if st.button("🧠 Generate read to see what changed"):
            with st.spinner("Reading the updated timeline…"):
                try:
                    _first = api("POST", "/synthesize", json={"competitor": selected})
                except Exception as exc:  # noqa: BLE001
                    st.warning(friendly_read_error(exc))
                else:
                    st.session_state.setdefault("ss_reads", {})[selected] = _first
                    st.rerun()

    _diff = st.session_state.get("ss_last_diff")
    if _diff and _diff[0] is not None and _diff[2] == _pending.get(
        "uid", _pending.get("date")
    ):
        # Three values, not two: the third is the uid of the signal that
        # produced the after-read, which the guard above already matched on.
        # Trimming it here to satisfy the unpack would discard that
        # attribution and let a second log render a diff against a read taken
        # before the first.
        _b, _a, _diff_uid = _diff
        st.markdown("#### Side by side")
        _left, _right = st.columns(2)
        with _left:
            st.markdown("##### Before")
            st.caption(f"{_b.get('signal_count', '?')} signals · `{_b.get('model_used', '?')}`")
            st.markdown(confidence_badge(_b.get("confidence")), unsafe_allow_html=True)
        with _right:
            st.markdown("##### After")
            st.caption(f"{_a.get('signal_count', '?')} signals · `{_a.get('model_used', '?')}`")
            st.markdown(confidence_badge(_a.get("confidence")), unsafe_allow_html=True)

        _changed = 0
        for label, key in DIFF_FIELDS:
            _bv, _av = read_headline(_b, key), read_headline(_a, key)
            _is_changed = _bv != _av
            _changed += int(_is_changed)
            _mark = "🟡 **changed**" if _is_changed else "unchanged"
            if key == "confidence":
                # The confidence badge is the headline; a raw string diff of
                # "medium" -> "high" hides that it moved in the reader's favour.
                _bv_html = confidence_badge(_b.get("confidence"))
                _av_html = confidence_badge(_a.get("confidence"))
                st.markdown(
                    f"**{label}** — {_mark}\n\n&nbsp;&nbsp;before {_bv_html} "
                    f"→ after {_av_html}",
                    unsafe_allow_html=True,
                )
                continue
            st.markdown(
                f"**{label}** — {_mark}\n\n&nbsp;&nbsp;before: {_bv}\n\n"
                f"&nbsp;&nbsp;after: **{_av}**" if _is_changed else
                f"**{label}** — {_mark}\n\n&nbsp;&nbsp;{_bv}",
            )
        if _changed:
            st.success(
                f"{_changed} of {len(DIFF_FIELDS)} tracked fields changed after one signal."
            )
        else:
            st.info("No tracked field changed. The timeline grew, the read did not.")


# --- strategic read -------------------------------------------------------
st.markdown("---")
st.markdown("### Strategic read")

if not signals:
    st.caption("Log a few signals first — a read needs a timeline to reason across.")
else:
    left, right = st.columns([1, 2])
    with left:
        clicked = st.button(
            "🧠 Get Strategic Read",
            type="primary",
            use_container_width=True,
        )
    with right:
        st.caption(
            "Retrieves the **complete** chronological timeline from Hindsight "
            "memory — not a top-k similarity slice — and reasons across "
            "pricing, feature, hiring, messaging and funding signals."
        )

# --- ask the memory a question --------------------------------------------
# Deliberately separate from the read above, and labelled as such. This is a
# question-answering lookup that returns the most similar handful of signals;
# the strategic read reasons over the complete timeline. Presenting them as
# two buttons on the same screen without that distinction would invite the
# reader to treat a k-signal slice as an analysis of the whole history.
with st.expander("❓ Ask this competitor's memory"):
    st.caption(
        "Searches stored signals for a question — *“what did they do about "
        "pricing?”*. This returns the most relevant handful, **not** the full "
        "timeline, and it does not generate a forecast. Use **Get Strategic "
        "Read** above for analysis."
    )
    _q = st.text_input(
        "Question",
        key="ss_recall_q",
        placeholder="e.g. pricing changes, hiring, failover",
        label_visibility="collapsed",
    )
    if st.button("🔎 Search memory", disabled=not _q.strip()):
        with st.spinner("Searching this competitor's memory…"):
            try:
                _hit = api("GET", f"/recall/{selected}", params={"q": _q})
            except Exception as exc:  # noqa: BLE001
                st.error(friendly_read_error(exc))
            else:
                if not _hit.get("signals"):
                    st.info(
                        f"Nothing in **{selected}**'s memory matched that. "
                        "Memory holds "
                        f"{_hit.get('signal_count', len(signals))} stored signal(s) "
                        "for this competitor — try a broader term, or log the "
                        "signal if you have it from elsewhere."
                    )
                else:
                    st.caption(
                        f"**{_hit['signal_count']} of {len(signals)}** stored "
                        f"signal(s) matched · `{_hit.get('bank_id')}`"
                    )
                    for _s in _hit["signals"]:
                        # Plain text, never st.markdown on the summary: a stored
                        # summary is user-supplied content that came back out of
                        # a search index, and markdown would let it inject links,
                        # images or headings into this page. st.write escapes.
                        st.write(
                            f"`{_s['date']}` · **{_s['signal_type']}** — "
                            f"{_s['summary']}",
                            unsafe_allow_html=False,
                        )

    if clicked:
        with st.spinner(f"Reading {len(signals)} months of memory…"):
            try:
                read = api("POST", "/synthesize", json={"competitor": selected})
            except Exception as exc:  # noqa: BLE001
                st.error(friendly_read_error(exc))
            else:
                # Cached per competitor so that logging a signal can diff
                # against the read the user already paid for. Keyed by name, not
                # position, so switching competitors in the sidebar does not
                # show one company's timeline against another's forecast.
                st.session_state.setdefault("ss_reads", {})[selected] = read
                meta_col1, meta_col2 = st.columns([3, 2])
                # The count appears once, here. timeline_window carries the
                # date span only — it used to repeat the count, which rendered
                # as "Built from 9 signals (start to end (9 signals))".
                meta_col1.caption(
                    f"Built from **{read['signal_count']} signals** "
                    f"({read['timeline_window']}) via `{read['model_used']}`"
                )
                meta_col2.caption(confidence_badge(read.get("confidence", "none")))
                # A read built from a window is still a read, but the reader is
                # entitled to know it is one. The count in the caption above is
                # what the bank holds; this is what the model was shown, and the
                # gap between them is the honest way to present a prompt budget
                # rather than a number that quietly disagrees with itself.
                if read.get("signals_omitted_from_prompt"):
                    st.info(
                        f"**The model saw {read.get('prompt_signal_count', '?')} of "
                        f"{read['signal_count']} signals** — the first, the most "
                        f"recent, and every signal in a repeated transition. The "
                        f"{read['signals_omitted_from_prompt']} others are not in "
                        f"the prompt"
                        + (f", and fall between "
                           f"{read.get('prompt_omitted_span', 'those dates')}."
                           if read.get("prompt_omitted_span")
                           else ".")
                        + " This window is not contiguous, so a pattern that only "
                        "occurs in the missing stretch would not appear below."
                    )
                # The model was asked twice and failed the validators both times.
                # The read below is the measured facts, not a narrative, and saying
                # so is the difference between an honest degraded answer and a
                # broken one. The reason is already in the caption above.
                # A 429 is answered 200 with the deterministic read, so it never
                # raises. Checking it here is the difference between "wait 20s"
                # and "your read was rejected by the validators" — which sends
                # people hunting a problem they do not have.
                if read.get("rate_limited"):
                    _wait = read.get("retry_after_seconds")
                    st.warning(
                        f"**Groq's rate limit was hit for this minute**"
                        + (f" — wait about {int(_wait)}s and read again"
                           if _wait else " — wait a moment and read again")
                        + ". Nothing is wrong with your data or your key, and the "
                        "signals are stored. What follows is the application's own "
                        "measurement of the timeline, not a forecast."
                    )
                if read.get("narrative_withheld"):
                    st.warning(
                        "**Narrative withheld.** The model was asked twice and both "
                        "responses were rejected by the validators, so no forecast is "
                        "shown. What follows is the application's own measurement of "
                        "this timeline — exact, and not a prediction. The reason is in "
                        "the `via` note above."
                    )
                # Surface evidence freshness next to the read. Without it a
                # forecast drawn from a timeline that stopped weeks ago reads
                # exactly like a live one, which is the whole failure mode the
                # time anchoring exists to prevent.
                age = read.get("evidence_age_days")
                as_of = read.get("data_as_of")
                if age is not None and as_of:
                    staleness = read.get("evidence_staleness", "unknown")
                    if staleness == "stale":
                        st.warning(
                            f"**Evidence is {age} days old.** The last signal for this "
                            f"competitor is dated {as_of}. The read below is drawn from a "
                            "cadence that has gone quiet, so treat the timing as a "
                            "projection rather than a live commitment."
                        )
                    elif staleness == "aging":
                        st.info(
                            f"Evidence is {age} days old (last signal {as_of}) — past this "
                            "competitor's usual rhythm, so the recent end of the timeline "
                            "may be incomplete."
                        )
                    else:
                        st.caption(f"Evidence current as of {as_of} ({age} days old).")

                # How far past its own rhythm the last signal sits. Distinct
                # from the age: 40 days is unremarkable for a quarterly
                # competitor and badly overdue for a weekly one, and the reader
                # needs the difference to weigh the forecast.
                overdue = read.get("days_overdue") or 0
                if overdue > 0:
                    st.info(
                        f"**Overdue by {overdue} day(s).** No signal has arrived for "
                        f"{overdue} day(s) beyond this competitor's usual rhythm. Any "
                        "timing below is an extrapolation from a rhythm that has "
                        "already broken, not a schedule."
                    )

                # What would settle it. Shown next to the read rather than
                # buried in it, because a forecast that names the observation
                # which would confirm or refute it is the one a reader can act
                # on — and the name of that observation is also the thing to go
                # and collect.
                missing = read.get("missing_evidence")
                if missing:
                    st.caption(f"**What would raise confidence:** {missing}")

                for title, key, icon in SECTIONS:
                    st.markdown(
                        f'<div class="ss-sec"><div class="ss-sec-t">{icon} {title}</div>'
                        f'<div class="ss-sec-b">{read[key]}</div></div>',
                        unsafe_allow_html=True,
                    )
                st.download_button(
                    "Download this read (JSON)",
                    data=__import__("json").dumps(read, indent=2),
                    file_name=f"signal-stack-{selected.lower().replace(' ', '-')}-read.json",
                    mime="application/json",
                )
