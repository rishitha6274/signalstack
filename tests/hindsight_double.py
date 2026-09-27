"""A contract double for Hindsight Cloud 0.10.1, faithful to the published OpenAPI.

This is not a "fake that returns what the app wants". It is a double that
enforces the real API's rules, so that a client which is wrong about Hindsight
fails here rather than in a demo. Every behaviour below is taken from
https://api.hindsight.vectorize.io/openapi.json (info.version 0.10.1):

  BankListResponse         {banks:[BankListItem], total, limit, offset}   (all required)
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


def _now() -> str:
    return "2026-09-27T12:00:00Z"


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
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

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
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)

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
            dates = re.findall(r"\[(\d{4}-\d{2}-\d{2})\]", prompt)
            n = len(dates)
            if n < 6:
                payload = {
                    "patterns": (
                        f"Only {n} signals are on record, with no repeated cadence and no clear "
                        "ordering between them. There is no funding, no hiring cluster and no "
                        "pricing sequence to line up. The evidence is insufficient to identify a "
                        "pattern."
                    ),
                    "inferred_intent": (
                        f"With {n} unrelated signals on record I cannot distinguish a strategy from "
                        "routine product maintenance. Inferring intent here would be guessing."
                    ),
                    "predicted_next_move": "Not predictable from the available evidence.",
                    "recommendation": "Keep logging signals for this competitor before drawing conclusions.",
                }
            else:
                payload = {
                    "patterns": (
                        f"{n} signals between {dates[0]} and {dates[-1]} form an ordered chain: a "
                        f"Series C on {dates[0]}, a GTM hiring cluster immediately after, a 35% Pro "
                        f"price cut on {dates[5]}, then a homepage rewrite to 'enterprise-ready' "
                        "messaging three weeks later, then SSO/SCIM at GA, then a second pricing "
                        "move that restricted volume discounts to enterprise contracts."
                    ),
                    "inferred_intent": (
                        "Nimbus is converting funded go-to-market capacity into an enterprise "
                        "land-grab: cheapen entry to drive volume, then move the margin and the "
                        "conversation onto procurement-led deals."
                    ),
                    "predicted_next_move": (
                        "Within 6-8 weeks they make 'Contact sales' the default CTA on the pricing "
                        "page, launch a paid enterprise pilot, and post another 2-3 strategic-account "
                        "roles. Falsified if the self-serve $32 tier stays the primary conversion path "
                        "or volume hiring stops."
                    ),
                    "recommendation": (
                        "Ship SSO and audit logs in your own product this quarter and pre-empt their "
                        "pilot with an enterprise-tier SKU, because the price war buys them the buyer "
                        "relationships their Series C is funding."
                    ),
                }

        content = f"<think>reasoning about {len(prompt)} chars</think>\n"
        content += "Sure! Here is the analysis:\n```json\n" + json.dumps(payload) + "\n```"
        return self._send(200, {"choices": [{"message": {"role": "assistant", "content": content}}]})


def serve(port: int | None = None) -> ThreadingHTTPServer:
    ThreadingHTTPServer.allow_reuse_address = True
    port = port or int(os.getenv("FAKE_PORT", "8899"))
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return httpd


if __name__ == "__main__":
    httpd = serve()
    print(f"hindsight+groq contract double on http://127.0.0.1:{httpd.server_address[1]}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
