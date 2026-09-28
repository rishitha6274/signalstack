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
API_BASE = os.getenv("SIGNAL_STACK_API", "http://localhost:8000").rstrip("/")
REQUEST_TIMEOUT = int(os.getenv("SIGNAL_STACK_TIMEOUT", "180"))

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


# --------------------------------------------------------------------------
# API helpers
# --------------------------------------------------------------------------
def api(method: str, path: str, **kwargs) -> Any:
    response = requests.request(
        method, f"{API_BASE}{path}", timeout=REQUEST_TIMEOUT, **kwargs
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


# --------------------------------------------------------------------------
# Page setup
# --------------------------------------------------------------------------
st.set_page_config(
    page_title="Signal Stack",
    page_icon="🛰",
    layout="wide",
    initial_sidebar_state="expanded",
)

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
    raw = st.text_area(
        "Raw text",
        height=110,
        placeholder=(
            "e.g. Nimbus AI opens a Senior Solutions Architect role in the "
            "enterprise segment, the third such posting this quarter."
        ),
        label_visibility="collapsed",
    )
    if st.button("Log Signal", type="primary", disabled=not raw.strip()):
        with st.spinner("Extracting structure and writing to Hindsight…"):
            try:
                new_signal = api(
                    "POST", "/signals", json={"competitor": selected, "raw_text": raw}
                )
            except Exception as exc:  # noqa: BLE001
                st.error(f"Ingestion failed: {exc}")
            else:
                st.success(
                    f"Stored as **{new_signal['signal_type']}** "
                    f"dated **{new_signal['date']}** in `{bank_id}`"
                )
                st.json(new_signal, expanded=False)
                fetch_competitors.clear()


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

    if clicked:
        with st.spinner(f"Reading {len(signals)} months of memory…"):
            try:
                read = api("POST", "/synthesize", json={"competitor": selected})
            except Exception as exc:  # noqa: BLE001
                st.error(f"Strategic read failed: {exc}")
            else:
                meta_col1, meta_col2 = st.columns([3, 2])
                # The count appears once, here. timeline_window carries the
                # date span only — it used to repeat the count, which rendered
                # as "Built from 9 signals (start to end (9 signals))".
                meta_col1.caption(
                    f"Built from **{read['signal_count']} signals** "
                    f"({read['timeline_window']}) via `{read['model_used']}`"
                )
                meta_col2.caption(confidence_badge(read.get("confidence", "none")))
                # The model was asked twice and failed the validators both times.
                # The read below is the measured facts, not a narrative, and saying
                # so is the difference between an honest degraded answer and a
                # broken one. The reason is already in the caption above.
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
