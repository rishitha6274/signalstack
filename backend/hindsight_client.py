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
    SEED_FILE,
    TIMELINE_PAGE_SIZE,
)
from .models import CompetitorOut, Signal, slugify_competitor

log = logging.getLogger("signal_stack.hindsight")

API_PREFIX = "/v1/default/banks"
SIGNAL_TAG = "signal"

# Recall is a question-answering lookup, not the analysis substrate, so its
# result set is deliberately small. A user asking a question wants the handful
# of signals that answer it; returning the whole bank would make the cap a lie
# and quietly reintroduce the "just show me everything" path that recall was
# added to avoid.
RECALL_MAX_RESULTS = 10
# The route validates this too; the client refuses an over-long query as well
# so a caller bypassing HTTP cannot push an unbounded string at the API.
RECALL_MAX_QUERY_CHARS = 200

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
def _seed_file_names() -> dict[str, str]:
    """slug -> display name, taken from the seed dataset.

    The seed file ships inside the container, so it is the one source of true
    display names available on a cold deploy. Hindsight's bank listing is not:
    it echoes bank_id back as `name`, which would make the UI show
    "competitor-nimbus-ai" and — worse — resolve lookups to
    "competitor-competitor-nimbus-ai", silently rendering 0 signals.
    """
    try:
        payload = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    names: dict[str, str] = {}
    for block in payload.get("competitors", []):
        name = (block.get("name") or "").strip()
        if name:
            names[slugify_competitor(name)] = name
    return names


def _read_registry() -> dict[str, str]:
    try:
        with REGISTRY_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        registry = data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        registry = {}

    # Self-initialise on a cold deploy. The registry is gitignored, so a fresh
    # container has none, and without this every competitor falls back to its
    # bank_id — the deploy renders three empty timelines for no visible reason.
    if not registry:
        registry = _seed_file_names()
    return registry


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

    def recall_signals(
        self, competitor: str, query: str, limit: int = RECALL_MAX_RESULTS
    ) -> list[Signal]:
        """The signals most relevant to a free-text question. SECONDARY, never
        the analysis substrate.

        This is a question-answering lookup over one bank. It is deliberately
        NOT a replacement for `get_timeline`, and the two are kept apart on
        purpose: recall returns the k most semantically similar memories and
        silently drops the rest, so feeding it to pattern detection would
        analyse a biased sample of the timeline and report the bias as a
        finding. Synthesis reads `get_timeline` only. Recall exists to answer
        "what do I know about their pricing?", where missing the rest of the
        timeline is not a correctness problem.

        Scope, and why it is this narrow:
          * `tags=[SIGNAL_TAG]` with `tags_match="all_strict"` -- the spec's
            default is `any`, which *includes untagged rows*, so omitting the
            match mode silently returns every derived observation in the bank
            as if it were a signal. `all_strict` is the only mode that is
            both AND and untagged-excluding.
          * units without `metadata.signal_uid` are dropped, not rendered.
            Hindsight derives extra facts from each document; those are real
            memories but they are not signals, and presenting one as a dated
            signal would invent history the user never logged.
          * deduped by `signal_uid`, because one signal can produce several
            recalled units and the answer to "what changed on the 12th" should
            be one row, not three.

        Verified against the published OpenAPI for 0.10.1 rather than assumed:
        `RecallRequest` carries query/tags/tags_match/budget; `RecallResponse`
        requires `results`; and each `RecallResult` has NO `date` field -- the
        date is `occurred_start`/`mentioned_at`, with the authoritative value in
        the `signal_date` metadata this app writes. `metadata` is typed
        `additionalProperties: {type: string}`, so these are strings.
        """
        bank_id = bank_id_for(competitor)
        body = {
            "query": query,
            "tags": [SIGNAL_TAG],
            "tags_match": "all_strict",
            "budget": "low",
        }
        # Raises HindsightNotFound for an unknown bank, which the route maps to
        # a 404 rather than an empty list: "no such competitor" and "nothing
        # matched" are different answers and must not look the same.
        response = self._request(
            "POST", f"{API_PREFIX}/{bank_id}/memories/recall", json_body=body
        )

        by_uid: dict[str, Signal] = {}
        for unit in response.get("results") or []:
            signal = self._recall_result_to_signal(unit, competitor)
            if signal is None:
                continue
            by_uid.setdefault(signal.uid, signal)
            if len(by_uid) >= limit:
                break

        # Oldest first, like the timeline, so recall results read in the same
        # direction as everything else in the UI.
        return sorted(by_uid.values(), key=lambda s: (s.date, s.signal_type))

    @staticmethod
    def _recall_result_to_signal(result: dict, competitor: str) -> Signal | None:
        """Map one RecallResult back to a typed Signal, or None if it is not one.

        Separate from `_unit_to_signal` because the two response shapes differ
        in a way that matters: the list endpoint's items carry `date`, and
        `RecallResult` does not. Falling back to `result["date"]` here would
        raise KeyError on every real recall, so the date comes from the
        metadata this app writes, then `occurred_start` (truncated to the day),
        then the unit's own text.
        """
        metadata = result.get("metadata") or {}
        if not metadata.get("signal_uid"):
            return None  # a derived observation, not one of our signals
        occurred = result.get("occurred_start") or result.get("mentioned_at") or ""
        try:
            return Signal(
                competitor=metadata.get("competitor") or competitor,
                date=metadata.get("signal_date") or occurred[:10],
                signal_type=metadata.get("signal_type") or "feature",
                summary=metadata.get("signal_summary") or (result.get("text") or "").strip(),
                raw_notes=metadata.get("raw_notes", ""),
                source=metadata.get("source", "manual entry"),
            )
        except Exception:  # a malformed row must not break the whole answer
            return None

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
            # Hindsight echoes bank_id back as `name` on some deployments, so an
            # echo is not a display name. Prefer, in order: our registry, the
            # seed dataset, a bank name that is genuinely different, then a
            # title-cased slug.
            echoed = (bank.get("name") or "").strip()
            name = (
                registry.get(slug)
                or (echoed if echoed and echoed != bank_id else "")
                or _display_name(slug)
            )
            by_slug[slug] = {
                "name": name,
                "slug": slug,
                "bank_id": bank_id,
                "fact_count": bank.get("fact_count", 0) or 0,
                "last_write_at": bank.get("last_write_at"),
            }

        # Union in registry-only competitors: a bank removed server-side should
        # not silently disappear from the UI, and the registry is the only place
        # that remembers the original casing.
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
