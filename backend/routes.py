"""API endpoints for Signal Stack."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from . import hindsight_client, synthesis
from .config import groq_configured, hindsight_configured, missing_config_report
from .hindsight_client import HindsightError
from .ingestion import extract_signal
from .models import (
    CompetitorCreate,
    CompetitorOut,
    HealthResponse,
    Signal,
    SignalIngestRequest,
    SynthesisRequest,
    SynthesisResponse,
    TimelineResponse,
)

log = logging.getLogger("signal_stack.routes")
router = APIRouter()


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        hindsight_configured=hindsight_configured(),
        groq_configured=groq_configured(),
        problems=missing_config_report(),
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


@router.post("/competitors", response_model=CompetitorOut, status_code=201)
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
@router.post("/signals", response_model=Signal, status_code=201)
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
# Synthesis
# ---------------------------------------------------------------------------
@router.post("/synthesize", response_model=SynthesisResponse)
def synthesize(payload: SynthesisRequest) -> SynthesisResponse:
    """Full-timeline retrieval + cross-signal strategic synthesis."""
    competitor = payload.competitor.strip()
    if not competitor:
        raise HTTPException(status_code=422, detail="competitor must not be empty")
    return synthesis.generate_strategic_read(competitor)
