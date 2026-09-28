"""Environment configuration for Signal Stack.

Loads .env once (repo root) and exports the handful of constants the rest of
the app imports. Nothing here reaches the network, so it is safe to import
from scripts, tests and the Streamlit frontend alike.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import load_dotenv

# --------------------------------------------------------------------------
# Paths. REPO_ROOT is the Signal Stack folder, regardless of CWD.
# --------------------------------------------------------------------------
BACKEND_DIR = Path(__file__).resolve().parent
REPO_ROOT = BACKEND_DIR.parent

# .env is looked up in the repo root first, then wherever we happen to be.
load_dotenv(REPO_ROOT / ".env")
load_dotenv()


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def _normalise_base_url(raw: str, default: str) -> str:
    """Accept the dashboard URL people copy from the browser and normalise it.

    People paste https://ui.hindsight.vectorize.io straight into .env because
    that is the URL in the prompt/README, but the REST API is served from
    api.hindsight.vectorize.io. We accept either and land on a usable host.
    """
    url = (raw or default).strip().rstrip("/")
    if not url:
        url = default
    if not re.match(r"^https?://", url):
        url = "https://" + url
    # ui./app./console. are dashboards; the API lives on the api. host.
    url = re.sub(r"^https?://(ui|app|console)\.", "https://api.", url)
    return url


# --------------------------------------------------------------------------
# Hindsight — the memory layer. This is the load-bearing dependency.
# --------------------------------------------------------------------------
HINDSIGHT_API_KEY: str = _env("HINDSIGHT_API_KEY")
HINDSIGHT_BASE_URL: str = _normalise_base_url(
    _env("HINDSIGHT_BASE_URL"), "https://api.hindsight.vectorize.io"
)

# Every competitor gets its own Hindsight memory bank. This prefix namespaces
# our banks apart from any other agent sharing the same Hindsight project.
BANK_PREFIX: str = _env("BANK_PREFIX", "competitor")

# Guards the "fetch the complete timeline" promise: we page through
# /memories/list rather than taking a top-k slice, but never without a ceiling.
MAX_TIMELINE_PAGES: int = int(_env("MAX_TIMELINE_PAGES", "50"))
TIMELINE_PAGE_SIZE: int = int(_env("TIMELINE_PAGE_SIZE", "200"))

# --------------------------------------------------------------------------
# Groq — the LLM used for signal extraction and strategic synthesis.
# --------------------------------------------------------------------------
GROQ_API_KEY: str = _env("GROQ_API_KEY")
GROQ_BASE_URL: str = _normalise_base_url(
    _env("GROQ_BASE_URL"), "https://api.groq.com/openai/v1"
)
GROQ_MODEL: str = _env("GROQ_MODEL", "openai/gpt-oss-120b")
# Verified to exist on Groq's free tier as of 2026-09. Note that the commonly
# cited `qwen/qwen3-32b` is NOT a Groq model id — asking for it 400s, so the
# fallback is chosen from a family the account can actually serve. llm_client
# additionally filters candidates against GET /models at runtime, because
# model availability differs per plan.
GROQ_FALLBACK_MODEL: str = _env("GROQ_FALLBACK_MODEL", "qwen/qwen3.8-27b")

# Hackathon warning: gpt-oss models intermittently emit malformed or
# tool-call-shaped responses. Two retries, then fall back to the second model.
LLM_MAX_RETRIES: int = int(_env("LLM_MAX_RETRIES", "2"))
LLM_TIMEOUT_SECONDS: int = int(_env("LLM_TIMEOUT_SECONDS", "90"))
LLM_TEMPERATURE: float = float(_env("LLM_TEMPERATURE", "0.2"))

# --------------------------------------------------------------------------
# Local data
# --------------------------------------------------------------------------
_data_dir = Path(_env("DATA_DIR", str(REPO_ROOT / "data")))
DATA_DIR: Path = (
    _data_dir if _data_dir.is_absolute() else (REPO_ROOT / _data_dir)
).resolve()
SEED_FILE: Path = DATA_DIR / "seed_signals.json"
# slug -> display name. A cache, not the source of truth: Hindsight's bank
# listing is authoritative and the registry only preserves original casing.
REGISTRY_FILE: Path = DATA_DIR / "competitors.json"

# Signal vocabulary — the closed set the extraction prompt is allowed to use.
SIGNAL_TYPES: tuple[str, ...] = (
    "pricing",
    "feature",
    "hiring",
    "messaging",
    "funding",
)

API_PORT: int = int(_env("API_PORT", "8000"))
# Bind address for the API. The single-container image sets this to 127.0.0.1
# on purpose: only the Streamlit UI should be reachable from outside, and it
# talks to the API over loopback. Keeps the API off the public internet.
API_HOST: str = _env("API_HOST", "127.0.0.1")
# Where the Streamlit UI should find the API. Loopback inside one container.
SIGNAL_STACK_API: str = _env("SIGNAL_STACK_API", f"http://127.0.0.1:{API_PORT}")
# Seed the demo dataset on boot when memory is empty, so a fresh deploy comes up
# populated instead of showing three empty competitors. Set AUTOSEED=1 to opt in.
#
# Defaults OFF. Boot-time seeding writes to whichever Hindsight account the key
# points at, and nothing about that is safe to do implicitly: it ran against a
# real account during development and left a half-populated set of banks, so
# scripts/seed_data.py --reset became the honest way to manage memory. A fresh
# deploy should set AUTOSEED=1 explicitly and mean it.
AUTOSEED: str = _env("AUTOSEED", "0").lower() not in {"0", "false", "no"}


def hindsight_configured() -> bool:
    return bool(HINDSIGHT_API_KEY)


def groq_configured() -> bool:
    return bool(GROQ_API_KEY)


def missing_config_report() -> list[str]:
    """Human-readable list of what still needs to be filled in."""
    problems: list[str] = []
    if not HINDSIGHT_API_KEY:
        problems.append(
            "HINDSIGHT_API_KEY is empty — get one at "
            "https://ui.hindsight.vectorize.io (Connect page)"
        )
    if not GROQ_API_KEY:
        problems.append("GROQ_API_KEY is empty — get one at https://console.groq.com/keys")
    return problems
