"""Signal Stack — FastAPI entrypoint.

Run:  uvicorn backend.main:app --reload --port 8000
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import API_HOST, API_PORT, missing_config_report
from .routes import router

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
)

app = FastAPI(
    title="Signal Stack",
    description=(
        "Competitive intelligence agent with persistent memory. Signals are "
        "stored in Hindsight memory banks (one per competitor) and synthesized "
        "across the complete chronological timeline."
    ),
    version="1.0.0",
)

# Streamlit runs on its own port and calls this API from the browser.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8501",
        "http://127.0.0.1:8501",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.on_event("startup")
def bootstrap() -> None:
    """Warn about missing config, and populate memory on a cold start.

    A freshly deployed container has no banks at all, which renders as three
    empty competitors and looks broken. If the keys are present we seed the
    demo dataset once; it is idempotent, so restarts cost one read, not a
    rewrite. Set AUTOSEED=0 to never write on boot.
    """
    log = logging.getLogger("signal_stack")
    problems = missing_config_report()
    if problems:
        log.warning("Configuration incomplete:\n  - " + "\n  - ".join(problems))
        return

    log.info("Signal Stack API ready.")

    from .config import AUTOSEED, BANK_PREFIX
    from . import hindsight_client

    if not AUTOSEED:
        log.info("AUTOSEED disabled — not writing to memory on boot.")
        return

    # Ask Hindsight directly, not list_competitors(): that one unions in the
    # local display-name registry, so a competitor whose bank was wiped would
    # still be listed with zero facts and we'd skip seeding a genuinely empty
    # project. "Is there memory?" is a question about banks, not about names.
    try:
        populated = [
            b
            for b in hindsight_client.client.list_banks()
            if (b.get("bank_id") or "").startswith(f"{BANK_PREFIX}-")
            and (b.get("fact_count") or 0) > 0
        ]
    except Exception as exc:  # noqa: BLE001 - never block boot on a probe
        log.warning("Could not read memory to decide whether to seed: %s", exc)
        return

    if populated:
        log.info("Memory already populated (%d banks) — skipping seed.", len(populated))
        return

    log.info("Memory is empty — seeding the demo dataset on boot.")
    try:
        from .ingestion import seed_from_file
        result = seed_from_file()
        log.info(
            "Seeded %d signals across %d competitors (%d errors).",
            result["written"], result["competitors"], result["errors"],
        )
    except Exception as exc:  # noqa: BLE001 - a failed seed must not kill the app
        log.warning("Auto-seed failed (%s). The app is still serving; POST to /signals or run "
                    "scripts/seed_data.py to populate memory.", exc)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    # Bind address comes from config: loopback by default (the UI reaches it
    # over 127.0.0.1), overridable to 0.0.0.0 if you deliberately want the API
    # exposed on your LAN.
    uvicorn.run("backend.main:app", host=API_HOST, port=API_PORT, reload=True)
