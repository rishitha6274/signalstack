"""API endpoints for Signal Stack."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status

from . import hindsight_client, synthesis
from .config import (
    API_KEY,
    BANK_PREFIX,
    ENABLE_DEMO_RESET,
    groq_configured,
    hindsight_configured,
    missing_config_report,
)
from .hindsight_client import HindsightError
from .ingestion import extract_signal
from .models import (
    CompetitorCreate,
    CompetitorOut,
    HealthResponse,
    RecallResponse,
    Signal,
    SignalIngestRequest,
    SynthesisRequest,
    SynthesisResponse,
    TimelineResponse,
)

log = logging.getLogger("signal_stack.routes")
router = APIRouter()


def require_write_key(x_api_key: str | None = Header(default=None)) -> None:
    """Guard a write endpoint when API_KEY is set.

    A no-op when API_KEY is unset, which is the local-dev and single-image
    case: there is no network boundary to defend, so requiring a header would
    only add friction. When it is set, writes to Hindsight run on the account
    that owns HINDSIGHT_API_KEY, so an open write endpoint is someone else's
    bill. Comparison is constant-time to avoid leaking the secret by timing,
    and a missing header is 401 (not 403) because no credential was presented
    at all.
    """
    if not API_KEY:
        return
    import hmac

    if not x_api_key or not hmac.compare_digest(x_api_key.strip(), API_KEY):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid X-API-Key header",
        )


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        hindsight_configured=hindsight_configured(),
        groq_configured=groq_configured(),
        problems=missing_config_report(),
        demo_reset_enabled=ENABLE_DEMO_RESET,
    )


# ---------------------------------------------------------------------------
# Competitors
# ---------------------------------------------------------------------------
@router.get("/competitors")
def list_competitors() -> dict:
    """Every tracked competitor, with the size and freshness of its memory."""
    try:
        competitors = hindsight_client.client.list_competitors()
    except HindsightError as exc:
        raise HTTPException(status_code=502, detail=f"Hindsight unavailable: {exc}") from exc
    return {"competitors": [c.model_dump() for c in competitors]}


@router.post(
    "/competitors",
    response_model=CompetitorOut,
    status_code=201,
    dependencies=[Depends(require_write_key)],
)
def create_competitor(payload: CompetitorCreate) -> CompetitorOut:
    """Register a competitor, creating its Hindsight memory bank up front."""
    name = payload.name.strip()
    try:
        bank_id = hindsight_client.client.ensure_bank(name)
    except HindsightError as exc:
        raise HTTPException(status_code=502, detail=f"Hindsight unavailable: {exc}") from exc
    return CompetitorOut(name=name, slug=bank_id.split("-", 1)[-1], bank_id=bank_id)


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------
@router.post(
    "/signals",
    response_model=Signal,
    status_code=201,
    dependencies=[Depends(require_write_key)],
)
def log_signal(payload: SignalIngestRequest) -> Signal:
    """Raw text -> LLM-extracted Signal -> persisted to Hindsight."""
    try:
        return extract_signal(payload.raw_text, payload.competitor)
    except HindsightError as exc:
        raise HTTPException(
            status_code=502, detail=f"Signal extracted but could not be stored: {exc}"
        ) from exc


@router.post("/signals/explicit", response_model=Signal, status_code=201)
def log_structured_signal(signal: Signal) -> Signal:
    """Store a Signal that is already structured (used by the seeder/tests)."""
    try:
        hindsight_client.client.write_signal(signal)
    except HindsightError as exc:
        raise HTTPException(status_code=502, detail=f"Hindsight unavailable: {exc}") from exc
    return signal


def reset_demo_data() -> dict:
    """Wipe every Signal Stack bank, so a demo can be run again from scratch.

    Registered ONLY when ENABLE_DEMO_RESET=1 (see the `if` at the bottom of
    this module). When it is off the route does not exist and FastAPI
    answers 404, rather than the route existing and refusing: an endpoint that
    is present-but-blocked invites a second attempt with different parameters,
    and "not found" is the honest description of a capability that is absent.

    Both gates apply when it IS enabled: ENABLE_DEMO_RESET=1 to exist at all,
    and the write key to call. They are independent on purpose — with API_KEY
    unset locally, the key check is a no-op, so relying on it alone would mean
    the destructive route was effectively unguarded in the default config.

    Re-seeding is deliberately NOT done here: this clears the banks and lets
    the caller re-run scripts/seed_data.py, or set AUTOSEED=1 and restart. A
    reset that silently rewrites ten competitors would also rewrite anything a
    real user logged in the meantime, which is a worse surprise than an empty
    list.
    """
    try:
        removed = [
            bank["bank_id"]
            for bank in hindsight_client.client.list_banks()
            if (bank.get("bank_id") or "").startswith(f"{BANK_PREFIX}-")
        ]
    except HindsightError as exc:
        raise HTTPException(status_code=502, detail=f"Hindsight unavailable: {exc}") from exc
    for bank_id in removed:
        try:
            hindsight_client.client.delete_bank(bank_id.removeprefix(f"{BANK_PREFIX}-"))
        except HindsightError as exc:
            raise HTTPException(
                status_code=502, detail=f"Could not delete {bank_id}: {exc}"
            ) from exc
    return {
        "removed": removed,
        "count": len(removed),
        "reseed": "python scripts/seed_data.py --verify",
    }


# Conditional registration. Imported here rather than at the top so the flag
# is read at import time from the environment the app actually booted with.
if ENABLE_DEMO_RESET:
    router.post(
        "/demo/reset",
        dependencies=[Depends(require_write_key)],
    )(reset_demo_data)


@router.get("/timeline/{competitor}", response_model=TimelineResponse)
def get_timeline(competitor: str) -> TimelineResponse:
    """The complete chronological signal timeline from Hindsight memory."""
    try:
        signals = hindsight_client.client.get_timeline(competitor)
    except HindsightError as exc:
        raise HTTPException(status_code=502, detail=f"Hindsight unavailable: {exc}") from exc
    return TimelineResponse(
        competitor=competitor,
        bank_id=hindsight_client.bank_id_for(competitor),
        signal_count=len(signals),
        retrieval="chronological-complete",
        signals=signals,
    )


# ---------------------------------------------------------------------------
# Recall (secondary, question-answering only)
# ---------------------------------------------------------------------------
@router.get("/recall/{competitor}", response_model=RecallResponse)
def recall_signals(
    competitor: str,
    q: str = Query(
        ...,
        min_length=1,
        max_length=hindsight_client.RECALL_MAX_QUERY_CHARS,
        description=(
            "Natural-language question, e.g. 'what did they do about pricing?'. "
            "Answers come from semantic search over this competitor's signals "
            "only, and are not the strategic read."
        ),
    ),
) -> RecallResponse:
    """Answer a question from one competitor's memory. NOT the analysis path.

    Synthesis calls `get_timeline`, never this. Recall returns the most
    semantically similar handful of signals, which is the right answer to
    "what did they do about pricing?" and the wrong input to pattern
    detection -- a biased sample reads as a finding. Keeping them on separate
    routes makes that separation visible in the API surface rather than a
    convention someone has to remember.
    """
    query = q.strip()
    if not query:
        # min_length=1 admits " " -- a whitespace-only query is not a question,
        # and answering it with the top-k of nothing looks like "no results".
        raise HTTPException(status_code=422, detail="q must not be blank")
    if len(query) > hindsight_client.RECALL_MAX_QUERY_CHARS:
        raise HTTPException(
            status_code=422,
            detail=f"q must be at most {hindsight_client.RECALL_MAX_QUERY_CHARS} characters",
        )
    try:
        signals = hindsight_client.client.recall_signals(competitor, query)
    except hindsight_client.HindsightNotFound:
        # "No such competitor" and "nothing matched" are different answers;
        # collapsing them into an empty list makes a typo look like an absence
        # of evidence.
        raise HTTPException(
            status_code=404, detail=f"No memory bank for '{competitor}'"
        ) from None
    except HindsightError as exc:
        raise HTTPException(status_code=502, detail=f"Hindsight unavailable: {exc}") from exc
    return RecallResponse(
        competitor=competitor,
        query=query,
        bank_id=hindsight_client.bank_id_for(competitor),
        signal_count=len(signals),
        retrieval="semantic-secondary",
        signals=signals,
    )


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------
@router.post("/synthesize", response_model=SynthesisResponse)
def synthesize(payload: SynthesisRequest) -> SynthesisResponse:
    """Full-timeline retrieval + cross-signal strategic synthesis."""
    competitor = payload.competitor.strip()
    if not competitor:
        raise HTTPException(status_code=422, detail="competitor must not be empty")
    return synthesis.generate_strategic_read(competitor)
