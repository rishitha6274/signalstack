"""A contract double for Hindsight Cloud 0.10.1, faithful to the published OpenAPI.

This is not a "fake that returns what the app wants". It is a double that
enforces the real API's rules, so that a client which is wrong about Hindsight
fails here rather than in a demo. Every behaviour below is taken from
https://api.hindsight.vectorize.io/openapi.json (info.version 0.10.1):

  BankListResponse         {banks:[BankListItem], total, limit, offset}   (all required)
  RecallRequest            {query (required in practice; plain str, no
                             declared minLength), types, prefer_observations,
                             budget: low|mid|high = mid, max_tokens, trace,
                             query_timestamp, include, tags, tag_groups,
                             min_scores, temporal_window,
                             tags_match: any|all|any_strict|all_strict|exact = any}
  RecallResponse           {results:[RecallResult] (REQUIRED), trace, entities,
                             chunks, source_facts, source_facts_truncated}
  RecallResult             id (REQUIRED), text (REQUIRED), type, entities,
                             context, occurred_start, occurred_end, mentioned_at,
                             document_id, metadata (str->str), chunk_id, tags,
                             source_fact_ids, scores, attachments
                             -- NOTE: there is NO `date` field on a recall
                             result. The date is occurred_start/mentioned_at.
  BankListItem             bank_id, name, fact_count, last_document_at, last_write_at
  ListMemoryUnitsResponse  {items:[MemoryUnitListItem], total, limit, offset}
  MemoryUnitListItem       id, text, context, date, fact_type, document_id,
                           mentioned_at, occurred_start, entities (CSV *string*),
                           tags, metadata, state
  RetainRequest            {items:[MemoryItem], async, document_tags, operation_id}
  MemoryItem               content (REQUIRED), timestamp, context, metadata,
                           document_id, entities:[EntityInput{text,type}], tags
  CreateBankRequest        retain_mission, retain_extraction_mode, ... (no required fields)
  DeleteResponse           {success, message, deleted_count}

The rules it enforces that a naive mock would skip:

  * `MemoryItem.metadata` values must be STRINGS (`additionalProperties:
    {type: string}`). A nested object is a 422, not a shrug.
  * `MemoryItem.content` is required.
  * `tags_match` is NOT "OR of my tags". The real semantics are:
        any        OR,  includes untagged rows
        all        AND, includes untagged rows
        any_strict OR,  excludes untagged rows
        all_strict AND, excludes untagged rows
        exact      set equality on the full scope
    The list endpoint's DEFAULT is `any` — which means a client that filters on
    `tags=["signal"]` and omits `tags_match` silently receives every untagged
    fact in the bank as well.
  * The bank listing paginates and sorts by `last_write_at` DESC.
  * `time_field` filtering/ordering EXCLUDES rows with no value on that column,
    so a `time_field` query can return `total: 0` on a non-empty bank.

It also injects untagged noise facts into every bank, so the `tags_match`
semantics above are load-bearing rather than theoretical.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BANKS: dict[str, dict] = {}
UNITS: list[dict] = []

# Rows Hindsight's observation consolidation adds on top of the facts you
# retained. Modelled from the REAL API on 2026-09-27, where retaining 12 signals
# produced 12 `world` facts plus 10 `observation` rows. The observed behaviour
# is subtle and easy to get wrong:
#
#   * observations DO inherit the source fact's TAGS   (so a tag filter with
#     tags_match=all_strict does NOT exclude them)
#   * observations do NOT inherit metadata               (metadata comes back {})
#   * observations have document_id = None
#   * observations have fact_type = "observation"
#
# The practical consequence: the thing that keeps a derived narrative off the
# timeline is NOT tag scoping, it is the `metadata.signal_uid` guard in
# _unit_to_signal. This double reproduces that so the guard is actually tested.
def _derive_observations(bank_id: str) -> None:
    sources = [u for u in UNITS if u["bank_id"] == bank_id and u.get("fact_type") == "world"]
    for src in sources[:2]:
        UNITS.append(
            {
                "id": str(uuid.uuid4()),
                "bank_id": bank_id,
                "text": f"{bank_id} appears to be shifting strategy based on recent activity.",
                "context": "derived",
                "date": src.get("date") or _now(),
                "fact_type": "observation",
                "document_id": None,          # observed: never set
                "mentioned_at": src.get("mentioned_at"),
                "occurred_start": None,
                "state": "valid",
                "tags": list(src.get("tags") or []),  # observed: inherited
                "metadata": {},                 # observed: NOT inherited
                "entities": "Signal Stack",
            }
        )
    # One untagged, metadata-free row: exercises tags_match scoping.
    UNITS.append(
        {
            "id": str(uuid.uuid4()),
            "bank_id": bank_id,
            "text": "Competitive intelligence signals include pricing and hiring categories.",
            "context": "derived",
            "date": _now(),
            "fact_type": "observation",
            "document_id": None,
            "mentioned_at": _now(),
            "occurred_start": None,
            "state": "valid",
            "tags": [],
            "metadata": {},
            "entities": "Signal Stack",
        }
    )

_TIME_FIELDS = {"created_at", "updated_at", "mentioned_at", "occurred_start", "occurred_end"}
_TAG_MATCHES = {"any", "all", "any_strict", "all_strict", "exact"}

# The app's evidence floor, so this double refuses exactly when the real
# validator would reject a forecast. Read from the source of truth with a
# literal fallback, because the double must stay importable on its own
# (`python tests/hindsight_double.py`) without the app on the path.
try:
    from backend.facts import MIN_SIGNALS_FOR_EVIDENCE as _MIN_SIGNALS_FOR_EVIDENCE
except ImportError:  # pragma: no cover - standalone invocation
    _MIN_SIGNALS_FOR_EVIDENCE = 5


def _now() -> str:
    return "2026-09-27T12:00:00Z"


def _numbered_types(prompt: str, n: int) -> list[str]:
    """Signal types in timeline order, for the entries the prompt numbers.

    Read alongside the dates rather than assuming a length, so a timeline of
    any size produces a chain of matching length. Returns [] when the prompt
    does not number its entries, which the caller treats as "no chain to
    describe" instead of indexing into an empty list.
    """
    rows = re.findall(
        r"^\d+\.\s+\[(\d{4}-\d{2}-\d{2})\]\s*\(([a-z_]+)\)", prompt, re.M
    )
    return [t for _, t in rows][:n]


def _seed_noise(bank_id: str) -> None:
    """Placeholder kept for the enable_observations=False path (no derived rows)."""
    return None


def _match_tags(unit_tags: list[str], wanted: list[str], mode: str) -> bool:
    """Real `tags_match` semantics. See module docstring."""
    have = set(unit_tags or [])
    want = set(wanted or [])
    if mode == "exact":
        return have == want
    if mode in ("any", "any_strict"):
        hit = bool(have & want)
    else:  # all / all_strict
        hit = want.issubset(have)
    if mode in ("any", "all"):
        return hit or not have  # untagged rows are included by the loose modes
    return hit


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # quiet
        pass

    def _send(self, code, payload):
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self):
        # Read once, then cache. Every do_* handler drains the body FIRST, before
        # any routing or early return: on a keep-alive connection an undrained
        # body leaves the next request line sitting in the socket, so the
        # following request is parsed as garbage ("Bad request syntax") and the
        # failure surfaces in a completely unrelated call. Caching keeps the
        # later call sites working without reading the socket twice.
        if getattr(self, "_body_cache", None) is None:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            self._body_cache = json.loads(raw or b"{}")
        return self._body_cache

    def _drain(self):
        try:
            self._read_json()
        except Exception:
            self._body_cache = {}
        return self._body_cache

    def handle_one_request(self):
        # A handler instance is per CONNECTION, not per request, so without this
        # reset a body read on one request would be handed to the next request
        # on the same keep-alive socket.
        self._body_cache = None
        super().handle_one_request()

    def _authed(self) -> bool:
        return bool(self.headers.get("Authorization"))

    # ------------------------------------------------------------------ GET
    def do_GET(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)

        if path in ("/v1/openai/v1/models", "/v1/models", "/models"):
            # Mirrors a real Groq account: the primary is served, plus one
            # fallback. `qwen/qwen3-32b` is deliberately absent because that id
            # does not exist on Groq at all, which is how the client learned to
            # filter its candidate list against this endpoint.
            return self._send(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": "openai/gpt-oss-120b", "context_window": 131072},
                        {"id": "qwen/qwen3.8-27b", "context_window": 131072},
                        {"id": "openai/gpt-oss-20b", "context_window": 131072},
                    ],
                },
            )

        if path in ("/health", "/health/ready", "/health/live"):
            return self._send(200, {"status": "ok"} if self._authed() else (401, {"detail": "auth required"}))

        if not self._authed():
            return self._send(401, {"detail": "Authentication failed: API key required"})

        if path == "/v1/default/banks":
            limit = int(query.get("limit", ["100"])[0])
            offset = int(query.get("offset", ["0"])[0])
            q = (query.get("q") or [""])[0].lower()
            rows = [
                b
                for b in BANKS.values()
                if not q or q in b["bank_id"].lower() or (b.get("name") or "").lower().find(q) >= 0
            ]
            # most recently written first
            rows.sort(key=lambda b: b.get("last_write_at") or "", reverse=True)
            total = len(rows)
            out = []
            for b in rows[offset : offset + limit]:
                facts = [u for u in UNITS if u["bank_id"] == b["bank_id"]]
                out.append(
                    {
                        "bank_id": b["bank_id"],
                        "name": b.get("name"),
                        "mission": b.get("mission"),
                        "created_at": b.get("created_at"),
                        "updated_at": b.get("updated_at"),
                        "fact_count": len(facts),
                        "last_document_at": b.get("last_document_at"),
                        "last_write_at": b.get("last_write_at"),
                        "disposition": {"skepticism": 3, "literalism": 3, "empathy": 3},
                    }
                )
            return self._send(200, {"banks": out, "total": total, "limit": limit, "offset": offset})

        m = re.fullmatch(r"/v1/default/banks/([^/]+)/memories/list", path)
        if m:
            bank_id = m.group(1)
            if bank_id not in BANKS:
                return self._send(404, {"detail": "The bank does not exist."})

            limit = int(query.get("limit", ["100"])[0])
            offset = int(query.get("offset", ["0"])[0])
            state = (query.get("state") or [None])[0]
            doc_id = (query.get("document_id") or [None])[0]
            time_field = (query.get("time_field") or [None])[0]
            if time_field and time_field not in _TIME_FIELDS:
                return self._send(422, {"detail": f"invalid time_field {time_field}"})

            rows = [u for u in UNITS if u["bank_id"] == bank_id]
            if state:
                rows = [u for u in rows if u.get("state") == state]
            if doc_id:
                rows = [u for u in rows if u.get("document_id") == doc_id]

            tags = [t for t in (query.get("tags") or []) if t]
            mode = (query.get("tags_match") or ["any"])[0]  # real default is "any"
            if mode not in _TAG_MATCHES:
                return self._send(422, {"detail": f"invalid tags_match {mode}"})
            if tags:
                rows = [u for u in rows if _match_tags(u.get("tags") or [], tags, mode)]

            # time_field filters AND orders, and drops rows missing the value.
            if time_field:
                rows = [u for u in rows if u.get(time_field)]
                rows.sort(key=lambda u: u.get(time_field) or "", reverse=True)
            else:
                # default ordering: mentioned_at DESC
                rows.sort(key=lambda u: u.get("mentioned_at") or "", reverse=True)

            total = len(rows)
            page = [
                {k: v for k, v in u.items() if k != "bank_id"}
                for u in rows[offset : offset + limit]
            ]
            return self._send(200, {"items": page, "total": total, "limit": limit, "offset": offset})

        m = re.fullmatch(r"/v1/default/banks/([^/]+)/stats", path)
        if m:
            bank_id = m.group(1)
            if bank_id not in BANKS:
                return self._send(404, {"detail": "The bank does not exist."})
            facts = [u for u in UNITS if u["bank_id"] == bank_id]
            return self._send(
                200,
                {
                    "bank_id": bank_id,
                    "total_nodes": len(facts),
                    "total_links": 0,
                    "total_documents": len({u["document_id"] for u in facts if u.get("document_id")}),
                    "nodes_by_fact_type": {"world": len(facts)},
                    "links_by_fact_type": {},
                    "links_by_link_type": {},
                    "links_breakdown": {},
                    "pending_operations": 0,
                    "failed_operations": 0,
                    "last_memory_write_at": BANKS[bank_id].get("last_write_at"),
                },
            )

        return self._send(404, {"detail": path})

    # ------------------------------------------------------------------ PUT
    def do_PUT(self):
        self._drain()
        path = urlparse(self.path).path
        m = re.fullmatch(r"/v1/default/banks/([^/]+)", path)
        if not m:
            return self._send(404, {"detail": path})
        if not self._authed():
            return self._send(401, {"detail": "Authentication failed: API key required"})

        bank_id = m.group(1)
        body = self._read_json()
        known = {
            "name", "disposition", "disposition_skepticism", "disposition_literalism",
            "disposition_empathy", "mission", "background", "reflect_mission",
            "retain_mission", "retain_extraction_mode", "retain_custom_instructions",
            "retain_chunk_size", "retain_structured_chunk_size",
            "retain_max_attachments_per_chunk", "enable_observations",
            "observations_mission", "enable_text_search", "enable_temporal_retrieval",
            "enable_graph_retrieval", "enable_reranking",
        }
        if set(body) - known:
            return self._send(
                422, {"detail": f"unknown bank fields: {sorted(set(body) - known)}"}
            )
        created = bank_id not in BANKS
        BANKS[bank_id] = {
            "bank_id": bank_id,
            "name": body.get("name"),
            "mission": body.get("mission"),
            "retain_mission": body.get("retain_mission"),
            "created_at": BANKS.get(bank_id, {}).get("created_at") or _now(),
            "updated_at": _now(),
            "last_write_at": BANKS.get(bank_id, {}).get("last_write_at"),
        }
        BANKS[bank_id]["enable_observations"] = body.get("enable_observations", True)
        return self._send(200, {"bank_id": bank_id, "created": created})

    # --------------------------------------------------------------- DELETE
    def do_DELETE(self):
        self._drain()
        path = urlparse(self.path).path
        m = re.fullmatch(r"/v1/default/banks/([^/]+)", path)
        if not m:
            return self._send(404, {"detail": path})
        bank_id = m.group(1)
        if bank_id not in BANKS:
            return self._send(404, {"detail": "The bank does not exist."})
        removed = len([u for u in UNITS if u["bank_id"] == bank_id])
        BANKS.pop(bank_id)
        UNITS[:] = [u for u in UNITS if u["bank_id"] != bank_id]
        return self._send(200, {"success": True, "deleted_count": removed, "message": "bank deleted"})

    # ---------------------------------------------------------------- POST
    def do_POST(self):
        self._drain()
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)

        m = re.fullmatch(r"/v1/default/banks/([^/]+)/memories/recall", path)
        if m:
            bank_id = m.group(1)
            if not self._authed():
                return self._send(401, {"detail": "Authentication failed: API key required"})
            if bank_id not in BANKS:
                return self._send(404, {"detail": "The bank does not exist."})
            body = self._read_json()

            # Field-for-field from the published OpenAPI (0.10.1):
            # RecallRequest.query is a plain string with no declared minLength,
            # so an absent/empty query is a 422 here rather than a 400.
            query_text = body.get("query")
            if not isinstance(query_text, str) or not query_text.strip():
                return self._send(422, {"detail": "query is required"})

            # tags_match defaults to "any", which INCLUDES untagged rows. A
            # client that filters tags=["signal"] and omits the mode therefore
            # gets every derived observation in the bank as if it were a
            # signal. The double reproduces that so the mistake is catchable.
            mode = body.get("tags_match", "any")
            if mode not in _TAG_MATCHES:
                return self._send(422, {"detail": f"invalid tags_match {mode}"})
            budget = body.get("budget", "mid")
            if budget not in ("low", "mid", "high"):
                return self._send(422, {"detail": f"invalid budget {budget}"})

            rows = [u for u in UNITS if u["bank_id"] == bank_id]
            wanted = [t for t in (body.get("tags") or []) if t]
            if wanted:
                rows = [u for u in rows if _match_tags(u.get("tags") or [], wanted, mode)]

            # Deterministic relevance: a plain token-overlap score, newest
            # tiebreak. Real recall is semantic; what the client must survive
            # is the SHAPE and the tag scoping, both of which are exact here.
            terms = {t for t in re.findall(r"[a-z0-9]+", query_text.lower()) if len(t) > 2}
            def _score(u: dict) -> tuple:
                hay = f"{u.get('text', '')} {(u.get('metadata') or {}).get('signal_summary', '')}".lower()
                overlap = sum(1 for t in terms if t in hay)
                return (-overlap, u.get("mentioned_at") or "")

            # budget controls how much is returned; low is the small set.
            cap = {"low": 5, "mid": 10, "high": 20}[budget]
            # A retrieval arm returns matches, not the whole bank. Rows with no
            # term overlap are not results, so they are dropped here rather
            # than ranked last -- a client that cannot tell "nothing matched"
            # from "everything was returned" will happily report an unrelated
            # signal as the answer to a question it has no bearing on.
            hits = [u for u in rows if _score(u)[0] < 0]
            hits.sort(key=_score)
            hits = hits[:cap]

            # RecallResult, per the spec: id and text required, and NO `date`
            # field. The date is occurred_start/mentioned_at. A double that
            # helpfully added `date` would hide the KeyError a real client
            # would hit, so it deliberately omits it.
            results = [
                {
                    "id": u["id"],
                    "text": u.get("text", ""),
                    "type": u.get("fact_type"),
                    "entities": [u.get("entities")] if u.get("entities") else None,
                    "context": u.get("context"),
                    "occurred_start": u.get("occurred_start"),
                    "mentioned_at": u.get("mentioned_at"),
                    "document_id": u.get("document_id"),
                    "metadata": u.get("metadata") or {},
                    "tags": u.get("tags") or [],
                    "source_fact_ids": None,
                }
                for u in hits
            ]
            return self._send(200, {"results": results, "trace": None, "entities": None})

        m = re.fullmatch(r"/v1/default/banks/([^/]+)/memories", path)
        if m:
            bank_id = m.group(1)
            if not self._authed():
                return self._send(401, {"detail": "Authentication failed: API key required"})
            if bank_id not in BANKS:
                return self._send(404, {"detail": "The bank does not exist."})
            body = self._read_json()

            items = body.get("items")
            if not isinstance(items, list) or not items:
                return self._send(422, {"detail": "items is required"})

            for item in items:
                # content is the only required MemoryItem field
                if "content" not in item:
                    return self._send(422, {"detail": "items[].content is required"})
                # metadata is additionalProperties:{type: string} on retain
                meta = item.get("metadata")
                if meta is not None:
                    if not isinstance(meta, dict):
                        return self._send(422, {"detail": "items[].metadata must be an object"})
                    bad = {k: v for k, v in meta.items() if not isinstance(v, str)}
                    if bad:
                        return self._send(
                            422,
                            {"detail": f"items[].metadata values must be strings, got {sorted(bad)}"},
                        )
                for ent in item.get("entities") or []:
                    if not isinstance(ent, dict) or "text" not in ent:
                        return self._send(422, {"detail": "items[].entities[].text is required"})

            BANKS[bank_id]["last_write_at"] = _now()
            for item in items:
                doc_id = item.get("document_id")
                # same document_id replaces, per update_mode=replace (the default)
                UNITS[:] = [
                    u for u in UNITS if not (u["bank_id"] == bank_id and u.get("document_id") == doc_id)
                ]
                meta = item.get("metadata") or {}
                content = item["content"]
                ts = item.get("timestamp") or _now()
                if ts == "unset":
                    ts = None
                # real extraction can split a document into >1 fact
                sentences = [s.strip() for s in re.split(r"(?<=\.)\s+", content) if s.strip()]
                for piece in (sentences or [content]):
                    UNITS.append(
                        {
                            "id": str(uuid.uuid4()),
                            "bank_id": bank_id,
                            "text": piece,
                            "context": item.get("context") or "",
                            "date": (meta.get("signal_date") or ts or ""),
                            "fact_type": "world",
                            "document_id": doc_id,
                            "mentioned_at": ts,
                            "occurred_start": ts,
                            "state": "valid",
                            "tags": item.get("tags") or [],
                            "metadata": meta,
                            "entities": ", ".join(
                                e["text"] for e in (item.get("entities") or []) if isinstance(e, dict)
                            ),
                        }
                    )
                BANKS[bank_id]["last_document_at"] = _now()
            # Real Hindsight consolidates into observations after retain,
            # unless the bank turned that off.
            if BANKS[bank_id].get("enable_observations", True):
                _derive_observations(bank_id)
            return self._send(
                200,
                {
                    "success": True,
                    "bank_id": bank_id,
                    "items_count": len(items),
                    "async": bool(body.get("async", False)),
                },
            )

        if path in ("/v1/openai/v1/chat/completions", "/v1/chat/completions"):
            return self._groq()

        return self._send(404, {"detail": path})

    # ---------------------------------------------------------------- Groq
    def _groq(self):
        """Fake Groq reproducing the gpt-oss failure modes the brief warns about."""
        body = self._read_json()
        prompt = body["messages"][-1]["content"]
        if body.get("response_format", {}).get("type") == "json_object":
            # The real 400 from Groq is NOT "not supported for this model". It is
            # this, verbatim: json_object mode additionally requires the prompt
            # to contain the word "json". Verified against Groq on 2026-09-27.
            if "json" not in prompt.lower():
                return self._send(
                    400,
                    {
                        "error": {
                            "message": "'messages' must contain the word 'json' in some form, to use 'response_format' of type 'json_object'.",
                            "type": "invalid_request_error",
                        }
                    },
                )

        if "RAW NOTE" in prompt:
            payload = {
                "signal_type": "hiring",
                "date": "2026-09-20",
                "summary": "Opened a Senior Solutions Architect role in the enterprise segment.",
                "source": "job board",
            }
        else:
            m = re.search(r"tracked signals for (.+?):", prompt)
            _COMPANY = (m.group(1).strip() if m else "This competitor")
            # Count the NUMBERED timeline entries, not every bracketed date.
            # The FACTS block repeats the last three signals in the same
            # [date] (type) shape as the timeline, so counting brackets would
            # count the timeline plus three — enough to push a 4-signal
            # competitor over the refusal threshold and fake a rich read.
            numbered = re.findall(r"^\d+\.\s+\[(\d{4}-\d{2}-\d{2})\]", prompt, re.M)
            dates = numbered or re.findall(r"\[(\d{4}-\d{2}-\d{2})\]", prompt)
            n = len(dates)
            # Behave like a date-aware model: read the injected clock and the
            # freshness verdict out of the prompt, then forecast from today.
            # If the wiring ever stops carrying them, these keys vanish and the
            # date-anchoring checks in selfcheck fail rather than pass silently.
            today_m = re.search(r"TIME REFERENCE: Today is (\d{4}-\d{2}-\d{2})", prompt)
            today = today_m.group(1) if today_m else None
            stale = "the evidence is stale" in prompt.lower()
            horizon = (
                datetime.date.fromisoformat(today) + datetime.timedelta(days=28)
                if today
                else None
            )
            # The FACTS block. A compliant fake reads its figures from here and
            # quotes them back verbatim, exactly as the prompt demands. If the
            # block is ever dropped from the prompt these come back as None and
            # the contract checks in selfcheck fail loudly instead of the double
            # quietly inventing plausible numbers.
            facts_block = re.search(r"FACTS \(computed by the application.*?\n(?=\n|TIME)", prompt, re.S)
            facts_text = facts_block.group(0) if facts_block else ""
            median_m = re.search(r"median interval: (\d+) days", facts_text)
            overdue_m = re.search(r"days overdue: (\d+)", facts_text)
            intervals_m = re.search(r"in order: \[([^\]]*)\]", facts_text)
            median = int(median_m.group(1)) if median_m else None
            overdue = int(overdue_m.group(1)) if overdue_m else None
            intervals = (
                [int(x) for x in intervals_m.group(1).split(",") if x.strip()]
                if intervals_m else []
            )
            repeat_m = re.search(r"transitions that DO repeat.*?: (.+)", facts_text)
            repeats = repeat_m.group(1) if repeat_m else ""
            single_m = re.search(r"seen only ONCE[^:]*: (.+)", facts_text)
            singles = single_m.group(1) if single_m else ""

            # The refusal threshold is the application's own evidence floor, not
            # a number invented here. It used to be a hardcoded `n < 6`, one
            # signal stricter than backend.facts.MIN_SIGNALS_FOR_EVIDENCE (5),
            # and that quietly made the UI's headline demo impossible to verify:
            # the app would permit a forecast at 5 signals while the double still
            # refused, so the refusal->forecast flip never happened in the suite.
            # A fake that is stricter than the real thing tests the wrong
            # behaviour, so the constant is imported rather than restated.
            if n < _MIN_SIGNALS_FOR_EVIDENCE:
                # A refusal: calibrated by construction, and exempt from the
                # interval/quote checks because it asserts no pattern.
                missing = (
                    f"At least {_MIN_SIGNALS_FOR_EVIDENCE - n} more signal(s) spanning a "
                    "second occurrence of any signal-type transition would show whether "
                    "an ordering exists at all."
                )
                payload = {
                    "patterns": (
                        f"Only {n} signals are on record, with no repeated cadence and no clear "
                        "ordering between them. Every signal-type transition in the FACTS block "
                        "occurs once, so nothing here repeats. The evidence is insufficient to "
                        "identify a pattern."
                    ),
                    "inferred_intent": (
                        f"With {n} unrelated signals on record I cannot distinguish a strategy from "
                        "routine product maintenance. Inferring intent here would be guessing."
                    ),
                    "predicted_next_move": (
                        "No reliable prediction can be made from the available evidence."
                    ),
                    "recommendation": (
                        "Keep logging signals for this competitor before drawing conclusions."
                    ),
                    "confidence": "none",
                    "missing_evidence": missing,
                }
                if median is None and intervals:
                    raise AssertionError(
                        "FACTS block did not expose the intervals to the model"
                    )
            else:
                forecast = (
                    f"On or before {horizon.isoformat()} they ship role-based access control, "
                    "scoped admin analytics, and put 'Contact sales' back on the pricing page as "
                    "the primary CTA. " if horizon else
                    "They ship role-based access control and restore 'Contact sales' as the "
                    "primary CTA. "
                )
                # A compliant forecasting read. Every number it states is copied
                # from the FACTS block, every "repeats" claim is backed by a
                # transition the block says occurs >= 2 times, and the overdue
                # figure is surfaced in the prediction with confidence capped.
                stale_clause = ""
                confidence = "medium"
                if overdue:
                    stale_clause = (
                        f"The stream has been quiet for {overdue} day(s) past its {median}-day "
                        f"rhythm, and no signal has arrived since {dates[-1]}, so the expected "
                        "move did not land on schedule and the timing below is a projection from a "
                        "rhythm that has already broken. "
                    )
                    confidence = "low"
                elif stale:
                    stale_clause = (
                        f"This rests on evidence that stopped at {dates[-1]} and has been quiet "
                        "since, so re-check their release notes and careers page before acting on "
                        "the date. "
                    )
                cadence_sentence = (
                    f"The measured intervals are {intervals} days, a median of {median}."
                    if intervals and median
                    else "The FACTS block does not support a cadence claim."
                )
                repeat_sentence = (
                    f"The only transition that repeats is {repeats}."
                    if repeats else
                    "No signal-type transition in the FACTS block occurs more than once, so this "
                    "is one observed instance rather than a repeating cycle."
                )
                # The forecast branch used to narrate Nimbus's actual story with
                # hardcoded positions -- `dates[5]` for the price cut -- so it
                # raised IndexError on any timeline shorter than six signals. For
                # Vertex that meant the UI's headline demo could not be verified
                # at all: the double 500'd, the client saw a disconnect, and the
                # read silently degraded to the fallback. The chain is now read
                # off the timeline in front of us, so a 5-signal competitor gets
                # a real forecast instead of a crash, and Nimbus still gets the
                # enterprise land-grab story because the types genuinely are
                # funding, hiring, pricing, messaging.
                _types = _numbered_types(prompt, n)
                _first = _types[0] if _types else ""
                _last = _types[-1] if _types else ""
                _chain = ", then ".join(
                    f"{t} on {d}" for t, d in zip(_types, dates)
                ) or f"{n} signals between {dates[0]} and {dates[-1]}"
                # A funded-then-repriced-then-enterprise sequence is the story
                # the audit read for Nimbus; a timeline without those types
                # gets a plainer one rather than a borrowed claim.
                _enterprise = (
                    _first == "funding" and "pricing" in _types
                    and ("messaging" in _types or "feature" in _types)
                )
                if _enterprise:
                    # The real summary, quoted from the timeline the prompt
                    # handed over, so the audit's founding read ("Series C, then
                    # an enterprise land-grab") is still what the double says.
                    # Naming the type alone ("funding, then pricing") lost the
                    # actual claim and failed the grounding check.
                    _opening = (
                        f"a Series C on {dates[0]}, a GTM hiring cluster immediately after, "
                        f"a Pro price cut on {dates[_types.index('pricing')] if 'pricing' in _types else dates[0]}, "
                        "then a homepage rewrite to 'enterprise-ready' messaging, then SSO/SCIM "
                        "at GA, then a second pricing move that restricted volume discounts to "
                        "enterprise contracts."
                    )
                    _chain = ""
                    _intent = (
                        f"{_COMPANY} is converting newly funded go-to-market capacity into an "
                        "enterprise land-grab: cheapen entry to drive volume, then move the "
                        "margin and the conversation onto procurement-led deals."
                    )
                    _falsifier = (
                        "Falsified if the self-serve tier stays the primary conversion path or "
                        "volume hiring does not resume."
                    )
                else:
                    _opening = ""
                    _intent = (
                        f"The {n} signals run {_chain}. Each step follows the last without "
                        "reversing direction, which reads as one deliberate sequence rather than "
                        "unrelated maintenance."
                    )
                    _falsifier = (
                        "Falsified if the next signal reverses that direction or arrives on an "
                        "unrelated theme."
                    )
                payload = {
                    "patterns": (
                        f"{n} signals between {dates[0]} and {dates[-1]} form an "
                        f"ordered chain: {_opening} {_chain}. "
                        f"{cadence_sentence} {repeat_sentence}"
                    ),
                    "inferred_intent": _intent,
                    "predicted_next_move": (
                        f"Counting from today ({today}), " + stale_clause + forecast +
                        _falsifier
                    ),
                    "recommendation": (
                        f"Re-check {_COMPANY}'s changelog and open roles this week to confirm "
                        "the cadence has resumed, and in parallel prepare a matching response so "
                        "a pre-emptive offer is ready rather than late."
                    ),
                    "confidence": confidence,
                    # The validator requires that when no transition repeats,
                    # missing_evidence says so -- and it checks that, rather
                    # than trusting a fluent-sounding answer. The double used
                    # to write a timing-shaped missing_evidence regardless, so
                    # any forecast over a no-repeat timeline was rejected and
                    # the UI's demo read came back as the deterministic stub.
                    "missing_evidence": (
                        (
                            "Nothing has repeated yet: a second instance of any "
                            "signal-type transition would be the first repeat, and "
                            f"a further {median}-day interval with no signal would "
                            "confirm the stream has gone quiet rather than merely "
                            f"irregular. Any dated announcement after {dates[-1]} "
                            "would also narrow the timing."
                        )
                        if not repeats else
                        f"A further {median}-day interval with no signal would confirm the "
                        f"stream has gone quiet rather than merely irregular; any dated "
                        f"announcement after {dates[-1]} would do the same."
                    ),
                }

        content = f"<think>reasoning about {len(prompt)} chars</think>\n"
        content += "Sure! Here is the analysis:\n```json\n" + json.dumps(payload) + "\n```"
        return self._send(200, {"choices": [{"message": {"role": "assistant", "content": content}}]})


def serve(port: int | None = None) -> ThreadingHTTPServer:
    ThreadingHTTPServer.allow_reuse_address = True
    # `port or ...` would treat an explicit 0 as "unset" and hand back the
    # default port, which is already taken when a second double is wanted. 0
    # means "any free port", so the check has to be for None.
    if port is None:
        port = int(os.getenv("FAKE_PORT", "8899"))
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return httpd


if __name__ == "__main__":
    httpd = serve()
    print(f"hindsight+groq contract double on http://127.0.0.1:{httpd.server_address[1]}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
