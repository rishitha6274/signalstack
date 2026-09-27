"""Hindsight memory layer — one memory bank per competitor.

Everything Signal Stack knows lives in Hindsight. There is no sidecar
database, which is the point: the product's value *is* the accumulated memory,
so retrieval is designed for chronological completeness rather than
similarity.

Endpoint reference (Hindsight 0.10.x, docs.hindsight.vectorize.io):
  PUT    /v1/default/banks/{bank_id}                 create/update a memory bank
  DELETE /v1/default/banks/{bank_id}                 delete a bank (seeder --reset)
  GET    /v1/default/banks                           list banks
  POST   /v1/default/banks/{bank_id}/memories        retain (write)
  GET    /v1/default/banks/{bank_id}/memories/list   list memory units (paginated)

Design notes worth knowing before editing this file:

1. A competitor's namespace IS a Hindsight bank (`competitor-nimbus-ai`), so
   banks give us isolation, and `GET /banks` gives us the competitor list
   without a second source of truth.

2. `get_timeline` uses the *list* endpoint, not `memories/recall`. Recall is
   top-k semantic search: it silently drops old or lexically dissimilar
   memories, which is precisely the failure mode that hides a six-month
   funding -> hiring -> pricing -> messaging chain. We page through the full
   result set instead and sort by date ourselves.

3. Each signal is retained with a deterministic `document_id` (its uid), so
   re-running the seeder replaces that signal's document instead of
   duplicating it, and with `metadata`/`tags` that survive down to the
   extracted memory units, so we can map memory units back to signals.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Iterable

import requests

from .config import (
    BANK_PREFIX,
    HINDSIGHT_API_KEY,
    HINDSIGHT_BASE_URL,
    MAX_TIMELINE_PAGES,
    REGISTRY_FILE,
    TIMELINE_PAGE_SIZE,
)
from .models import CompetitorOut, Signal, slugify_competitor

log = logging.getLogger("signal_stack.hindsight")

API_PREFIX = "/v1/default/banks"
SIGNAL_TAG = "signal"

# Hindsight extracts facts from retained content. This mission keeps the
# extraction honest: one dated signal in, one dated signal out, no merging,
# no editorialising. Set on the bank so every future write inherits it.
RETAIN_MISSION = (
    "This bank stores competitive-intelligence signals about one company. "
    "Each retained item is a single dated event (pricing change, feature "
    "release, hiring signal, messaging change, or funding event). Store "
    "exactly one fact per item, preserving the event date and the signal type "
    "verbatim. Do not merge items, do not split one item into several facts, "
    "and do not add facts that are not stated in the item."
)

_registry_lock = threading.Lock()


class HindsightError(RuntimeError):
    """Raised when the memory layer rejects or fails a request."""


class HindsightNotFound(HindsightError):
    """404 from Hindsight: bank or memory does not exist."""


def bank_id_for(competitor: str) -> str:
    """Namespace mapping: 'Nimbus AI' -> 'competitor-nimbus-ai'."""
    return f"{BANK_PREFIX}-{slugify_competitor(competitor)}"


# ---------------------------------------------------------------------------
# Local competitor registry (slug -> display name)
#
# Hindsight's bank listing is the source of truth for *which* competitors
# exist. The registry exists only to preserve original casing ("Nimbus AI",
# not "nimbus ai") across restarts.
# ---------------------------------------------------------------------------
def _read_registry() -> dict[str, str]:
    try:
        with REGISTRY_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_registry(registry: dict[str, str]) -> None:
    REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with REGISTRY_FILE.open("w", encoding="utf-8") as fh:
        json.dump(registry, fh, indent=2, sort_keys=True)
        fh.write("\n")


def register_competitor(competitor: str) -> str:
    """Record the display name for a competitor. Returns its bank id."""
    slug = slugify_competitor(competitor)
    with _registry_lock:
        registry = _read_registry()
        registry.setdefault(slug, competitor.strip())
        _write_registry(registry)
    return f"{BANK_PREFIX}-{slug}"


def _display_name(slug: str) -> str:
    return _read_registry().get(slug) or slug.replace("-", " ").title()


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------
class HindsightClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: int = 120,
    ) -> None:
        self.base_url = (base_url or HINDSIGHT_BASE_URL).rstrip("/")
        self.api_key = api_key if api_key is not None else HINDSIGHT_API_KEY
        self.timeout = timeout
        self._session = requests.Session()
        if self.api_key:
            self._session.headers.update(
                {
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                }
            )

    # -- low level ---------------------------------------------------------
    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _request(
        self, method: str, path: str, *, params: dict | None = None, json_body: Any = None
    ):
        try:
            response = self._session.request(
                method,
                self._url(path),
                params=params,
                json=json_body,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:  # network/DNS/TLS/timeout
            raise HindsightError(f"{method} {path} failed: {exc}") from exc

        if response.status_code == 404:
            raise HindsightNotFound(f"{method} {path} -> 404")
        if response.status_code >= 400:
            raise HindsightError(
                f"{method} {path} -> {response.status_code}: {response.text[:400]}"
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise HindsightError(f"{method} {path} returned non-JSON body") from exc

    # -- health ------------------------------------------------------------
    def ping(self) -> dict:
        return self._request("GET", "/health")

    # -- banks -------------------------------------------------------------
    def ensure_bank(self, competitor: str) -> str:
        """Create the competitor's memory bank if it does not exist yet.

        PUT is create-or-update, so this is idempotent. A 404/422 here is
        non-fatal: Hindsight auto-creates banks on first retain in some
        deployments, and we do not want a config quirk to block a demo write.
        """
        bank_id = bank_id_for(competitor)
        try:
            self._request(
                "PUT",
                f"{API_PREFIX}/{bank_id}",
                json_body={
                    "retain_mission": RETAIN_MISSION,
                    # We store discrete, dated, typed signals and do our own
                    # cross-signal reasoning in synthesis.py. Hindsight's
                    # observation consolidation would additionally write
                    # narrative rows ("Nimbus's messaging strategy has shifted
                    # from X to Y") that duplicate our synthesis step while
                    # competing with it for the reader's attention. Verified on
                    # the real API: 12 retained signals produced 12 `world`
                    # facts plus 10 derived `observation` rows — double the
                    # stored units, none of which we asked for. Off by default
                    # is the right call for this product.
                    "enable_observations": False,
                },
            )
        except HindsightNotFound:
            pass
        except HindsightError as exc:
            log.warning("ensure_bank(%s) soft-failed, will rely on retain: %s", bank_id, exc)
        return bank_id

    def delete_bank(self, competitor: str) -> bool:
        bank_id = bank_id_for(competitor)
        try:
            self._request("DELETE", f"{API_PREFIX}/{bank_id}")
            return True
        except HindsightNotFound:
            return False

    def list_banks(self) -> list[dict]:
        """All banks visible to this API key, following pagination."""
        banks: list[dict] = []
        offset, limit = 0, 100
        while offset < 1000:  # hard ceiling; a demo project has a handful
            page = self._request("GET", API_PREFIX, params={"limit": limit, "offset": offset})
            batch = page.get("banks") or []
            banks.extend(batch)
            total = page.get("total", len(banks))
            offset += limit
            if len(batch) < limit or offset >= total:
                break
        return banks

    # -- write -------------------------------------------------------------
    def write_signal(self, signal: Signal) -> dict:
        """Persist one signal into its competitor's memory bank.

        The retained content is a single declarative sentence so Hindsight's
        fact extractor yields one memory unit per signal. Metadata and tags
        ride along onto every extracted unit, which is what lets
        `get_timeline` map memory units back to typed, dated signals.
        """
        bank_id = self.ensure_bank(signal.competitor)
        register_competitor(signal.competitor)

        body = {
            "items": [
                {
                    "content": signal.as_memory_content(),
                    "context": f"competitive intelligence signal ({signal.signal_type})",
                    "timestamp": f"{signal.date}T12:00:00Z",
                    "document_id": f"signal-{signal.uid}",
                    "metadata": {
                        "signal_uid": signal.uid,
                        "signal_type": signal.signal_type,
                        "signal_date": signal.date,
                        "signal_summary": signal.summary,
                        "source": signal.source,
                        "competitor": signal.competitor,
                        "raw_notes": signal.raw_notes[:1500],
                    },
                    "tags": [SIGNAL_TAG, f"type:{signal.signal_type}"],
                    "entities": [{"text": signal.competitor, "type": "ORG"}],
                }
            ],
            "async": False,  # synchronous: the demo must not race ingestion
        }

        result = self._request("POST", f"{API_PREFIX}/{bank_id}/memories", json_body=body)
        return result

    def write_signals(self, signals: Iterable[Signal]) -> dict[str, Any]:
        """Retain a batch of signals, one document per signal, for one bank.

        Groups by competitor so a single request can carry many signals while
        still giving each its own document_id (and therefore its own
        replace-on-reseed semantics).
        """
        grouped: dict[str, list[Signal]] = {}
        for signal in signals:
            grouped.setdefault(signal.competitor, []).append(signal)

        written, errors = 0, []
        for competitor, group in grouped.items():
            bank_id = self.ensure_bank(competitor)
            register_competitor(competitor)
            items = [
                {
                    "content": s.as_memory_content(),
                    "context": f"competitive intelligence signal ({s.signal_type})",
                    "timestamp": f"{s.date}T12:00:00Z",
                    "document_id": f"signal-{s.uid}",
                    "metadata": {
                        "signal_uid": s.uid,
                        "signal_type": s.signal_type,
                        "signal_date": s.date,
                        "signal_summary": s.summary,
                        "source": s.source,
                        "competitor": competitor,
                        "raw_notes": s.raw_notes[:1500],
                    },
                    "tags": [SIGNAL_TAG, f"type:{s.signal_type}"],
                    "entities": [{"text": competitor, "type": "ORG"}],
                }
                for s in group
            ]
            try:
                self._request(
                    "POST",
                    f"{API_PREFIX}/{bank_id}/memories",
                    json_body={"items": items, "async": False},
                )
                written += len(items)
            except HindsightError as exc:
                errors.append(f"{competitor}: {exc}")
        return {"written": written, "errors": errors}

    # -- read --------------------------------------------------------------
    def _list_memory_units(self, bank_id: str, *, tags: list[str] | None) -> list[dict]:
        """Page through every memory unit in a bank.

        Deliberately NOT using `memories/recall`, and deliberately NOT passing
        `time_field`: rows with no value on the chosen time column are excluded
        from both the filter and the ordering, which is the opposite of
        chronological completeness. Sorting happens in `get_timeline`.

        `tags_match="all_strict"` is load-bearing. Hindsight's default is
        `any`, which is an OR *and includes untagged rows* — so filtering on
        `tags=["signal"]` with the default would quietly return every untagged
        unit in the bank as well. `all_strict` is an AND that excludes untagged
        rows, which is the scope we actually mean: only our own signals.
        """
        params: dict[str, Any] = {
            "limit": TIMELINE_PAGE_SIZE,
            "offset": 0,
            "state": "valid",
        }
        if tags:
            params["tags"] = tags
            params["tags_match"] = "all_strict"

        units: list[dict] = []
        for _ in range(MAX_TIMELINE_PAGES):
            page = self._request(
                "GET", f"{API_PREFIX}/{bank_id}/memories/list", params=params
            )
            batch = page.get("items") or []
            units.extend(batch)
            total = page.get("total", len(units))
            params["offset"] += TIMELINE_PAGE_SIZE
            if not batch or len(units) >= total:
                break
        return units

    @staticmethod
    def _unit_to_signal(unit: dict, competitor: str) -> Signal | None:
        metadata = unit.get("metadata") or {}
        if not metadata.get("signal_uid"):
            return None  # a fact Hindsight derived that is not one of our signals
        try:
            return Signal(
                competitor=metadata.get("competitor") or competitor,
                date=metadata.get("signal_date") or unit.get("date") or "",
                signal_type=metadata.get("signal_type") or "feature",
                summary=metadata.get("signal_summary") or unit.get("text", "").strip(),
                raw_notes=metadata.get("raw_notes", ""),
                source=metadata.get("source", "manual entry"),
            )
        except Exception:  # a malformed row must not break the whole timeline
            return None

    def get_timeline(self, competitor: str) -> list[Signal]:
        """The COMPLETE signal timeline for a competitor, oldest first.

        This is the retrieval choice the whole product rests on. Recall would
        hand us the k most semantically similar memories and quietly drop the
        older half of the timeline; pattern detection over a six-month chain
        needs every link in the chain present.
        """
        bank_id = bank_id_for(competitor)
        try:
            units = self._list_memory_units(bank_id, tags=[SIGNAL_TAG])
            if not units:
                # Older server, or tags not applied on this deployment: fall
                # back to the complete unfiltered listing and filter locally.
                units = self._list_memory_units(bank_id, tags=None)
        except HindsightNotFound:
            return []

        by_uid: dict[str, Signal] = {}
        for unit in units:
            signal = self._unit_to_signal(unit, competitor)
            if signal is None:
                continue
            # The extractor can emit more than one fact per document; the
            # first unit for a uid wins, which keeps 1 signal = 1 timeline row.
            by_uid.setdefault(signal.uid, signal)

        return sorted(by_uid.values(), key=lambda s: (s.date, s.signal_type))

    def list_competitors(self) -> list[CompetitorOut]:
        """Every competitor with a memory bank, plus how much memory it holds.

        Hindsight's bank listing is authoritative. The local registry is
        unioned in so a competitor still shows up if its bank was removed
        server-side, and so original casing survives.
        """
        try:
            banks = self.list_banks()
        except HindsightError as exc:
            log.warning("list_banks failed, falling back to local registry: %s", exc)
            banks = []

        registry = _read_registry()
        by_slug: dict[str, dict] = {}
        for bank in banks:
            bank_id = bank.get("bank_id") or ""
            if not bank_id.startswith(f"{BANK_PREFIX}-"):
                continue  # not one of ours; another agent may share this project
            slug = bank_id[len(BANK_PREFIX) + 1 :]
            by_slug[slug] = {
                "name": registry.get(slug) or (bank.get("name") or "").strip() or _display_name(slug),
                "slug": slug,
                "bank_id": bank_id,
                "fact_count": bank.get("fact_count", 0) or 0,
                "last_write_at": bank.get("last_write_at"),
            }

        for slug, name in registry.items():
            by_slug.setdefault(
                slug,
                {
                    "name": name,
                    "slug": slug,
                    "bank_id": f"{BANK_PREFIX}-{slug}",
                    "fact_count": 0,
                    "last_write_at": None,
                },
            )

        competitors = [CompetitorOut(**data) for data in by_slug.values()]
        for competitor in competitors:
            try:
                signals = self.get_timeline(competitor.name)
            except HindsightError:
                signals = []
            competitor.signal_count = len(signals)
            if signals:
                competitor.first_signal = signals[0].date
                competitor.last_signal = signals[-1].date
        return sorted(competitors, key=lambda c: c.name.lower())


# Shared instance. HindsightClient is stateless apart from the requests
# Session, which is thread-safe enough for FastAPI's threadpool.
client = HindsightClient()
