"""A local MCP server (stdio) so agents can read the vault and propose changes.

Agents can read freely, but every write is a *proposal*: claims arrive with status
"proposed" and follow-ups with status "proposed", both shown in Review. Every call
is written to the audit log with the agent as the actor.

Run with:  ties mcp            (configure your MCP client to launch this command)
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, is_dataclass
from datetime import date
from typing import Any

from . import brief, graph, repo, scoring, strategy, textindex
from .db import Vault
from .repo import _add_claim, _create_source, new_id

PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]


def _json(obj: Any) -> Any:
    if is_dataclass(obj):
        return _json(asdict(obj))
    if isinstance(obj, dict):
        return {k: _json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_json(v) for v in obj]
    if isinstance(obj, date):
        return obj.isoformat()
    return obj


TOOLS = [
    {"name": "search", "description": "Search people, organizations, facts and notes.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}},
                     "required": ["query"]}},
    {"name": "get_profile", "description": "Facts, relationships, recent events, follow-ups and "
     "contact status for one entry. Use an id from search.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "due_outreach", "description": "Who is due for contact, with reasons, plus open follow-ups.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "find_paths", "description": "Strongest introduction paths from the owner to a person.",
     "inputSchema": {"type": "object", "properties": {"to": {"type": "string"}}, "required": ["to"]}},
    {"name": "brief", "description": "Pre-meeting brief for an event id.",
     "inputSchema": {"type": "object", "properties": {"event_id": {"type": "string"}},
                     "required": ["event_id"]}},
    {"name": "upcoming", "description": "Planned events in the next N days (default 14).",
     "inputSchema": {"type": "object", "properties": {"days": {"type": "integer"}}}},
    {"name": "weekly_review", "description": "Summary of the past week and suggestions for the next.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "intro_suggestions", "description": "People looking for something another person offers.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "propose_fact", "description": "Propose a fact or relationship. It waits in Review until "
     "the owner confirms it. predicate is a registry id (e.g. title, interest, employed_by, knows).",
     "inputSchema": {"type": "object", "properties": {
         "subject_id": {"type": "string"}, "predicate": {"type": "string"},
         "value": {"type": "string"}, "object_id": {"type": "string"},
         "why": {"type": "string"}, "source_url": {"type": "string"}},
         "required": ["subject_id", "predicate", "why"]}},
    {"name": "propose_follow_up", "description": "Propose a follow-up for the owner. It waits in Review.",
     "inputSchema": {"type": "object", "properties": {
         "person_id": {"type": "string"}, "text": {"type": "string"}, "due": {"type": "string"},
         "owner": {"type": "string", "enum": ["me", "them"]}},
         "required": ["person_id", "text"]}},
]


class Server:
    def __init__(self, vault: Vault, agent: str = "mcp"):
        self.vault, self.agent = vault, agent

    def _audit(self, action: str, tool: str, detail: dict) -> None:
        with self.vault.tx() as c:
            self.vault.audit(c, action, tool, detail, actor=f"agent:{self.agent}")

    def call(self, name: str, args: dict) -> Any:
        v, today = self.vault, date.today()
        self._audit("read" if not name.startswith("propose") else "write", name,
                    {k: (str(val)[:80]) for k, val in args.items()})
        if name == "search":
            res = textindex.search(v, args["query"])
            return {"entities": [{k: e[k] for k in ("id", "display_name", "type_id", "snippet")}
                                 for e in res["entities"]],
                    "notes": [{"owner": n["owner"], "snippet": n["snippet"]} for n in res["notes"]]}
        if name == "get_profile":
            e = repo.get_entity(v, args["id"])
            if not e:
                raise ValueError("Unknown id")
            return {"entity": e,
                    "facts": [{"field": f["predicate_label"], "value": f["value_text"],
                               "basis": f["basis"]} for f in repo.facts(v, e["id"])],
                    "relationships": [{"relation": r["label"], "with": r["other_name"],
                                       "id": r["other_id"]} for r in repo.relations(v, e["id"])],
                    "recent_events": [{"date": x["start_at"], "title": x["title"],
                                       "notes": (x.get("notes") or "")[:500]}
                                      for x in repo.list_events(v, entity_id=e["id"], limit=5)],
                    "follow_ups": [{"text": l["description"], "owed_by": "owner" if l["mine"] else "them",
                                    "due": l["due_at"]} for l in repo.commitments(v, e["id"])],
                    "status": scoring.contact_status(v, e["id"], today)}
        if name == "due_outreach":
            t = scoring.today_view(v, today)
            return {"due": [{"id": s.entity_id, "name": s.name, "reasons": s.reasons} for s in t["due"]],
                    "follow_ups": [{"text": l["description"], "with": l["other_name"],
                                    "due": l["due_at"], "owed_by": "owner" if l["mine"] else "them"}
                                   for l in t["loops"]]}
        if name == "find_paths":
            return graph.warm_paths(v, args["to"], today=today)
        if name == "brief":
            return brief.event_brief(v, args["event_id"], today)
        if name == "upcoming":
            from datetime import timedelta
            days = int(args.get("days") or 14)
            return repo.list_events(v, status="planned", start=today.isoformat(),
                                    end=(today + timedelta(days=days)).isoformat(), descending=False)
        if name == "weekly_review":
            r = strategy.weekly_review(v, today)
            return {"interactions": len(r["events"]), "people": list(r["people"].values()),
                    "opened": len(r["opened"]), "closed": len(r["closed"]),
                    "became_overdue": [s.name for s in r["decaying"]],
                    "next_week": [e["title"] for e in r["next_week"]],
                    "suggested": [s.name for s in r["suggested"]]}
        if name == "intro_suggestions":
            return strategy.intro_suggestions(v)
        if name == "propose_fact":
            with v.tx() as c:
                src = _create_source(c, "manual", args.get("why", ""), title=f"agent:{self.agent}",
                                     uri=args.get("source_url"))
                cid = _add_claim(c, args["subject_id"], args["predicate"], value=args.get("value"),
                                 object_id=args.get("object_id"), basis="public" if
                                 args.get("source_url") else "heard", confidence=0.7,
                                 qualifiers={"why": args.get("why", ""), "source": "agent"},
                                 source_id=src, asserted_by=f"agent:{self.agent}", status="proposed")
            return {"proposed": cid, "status": "waiting in Review"}
        if name == "propose_follow_up":
            ego = repo.ego_id(v)
            with v.tx() as c:
                cid = new_id()
                mine = args.get("owner", "me") != "them"
                c.execute("insert into commitment(id, from_id, to_id, description, due_at, status) "
                          "values(?,?,?,?,?, 'proposed')",
                          (cid, ego if mine else args["person_id"], args["person_id"] if mine else ego,
                           args["text"], args.get("due")))
            return {"proposed": cid, "status": "waiting in Review"}
        raise ValueError(f"Unknown tool {name}")

    def handle(self, msg: dict) -> dict | None:
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:  # notification
            return None
        try:
            if method == "initialize":
                asked = (msg.get("params") or {}).get("protocolVersion")
                version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
                result = {"protocolVersion": version, "capabilities": {"tools": {}},
                          "serverInfo": {"name": "ties", "version": "0.2.0"},
                          "instructions": "Personal relationship graph. Reads are free; writes are "
                                          "proposals the owner reviews."}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                params = msg.get("params") or {}
                try:
                    out = self.call(params.get("name"), params.get("arguments") or {})
                    result = {"content": [{"type": "text",
                                           "text": json.dumps(_json(out), default=str, indent=1)}]}
                except (ValueError, KeyError, RuntimeError) as exc:
                    result = {"content": [{"type": "text", "text": f"Error: {exc}"}], "isError": True}
            else:
                return {"jsonrpc": "2.0", "id": mid,
                        "error": {"code": -32601, "message": f"Method not found: {method}"}}
            return {"jsonrpc": "2.0", "id": mid, "result": result}
        except Exception as exc:  # pragma: no cover - defensive
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(exc)}}


def serve_stdio(vault: Vault, agent: str = "mcp", stdin=None, stdout=None) -> None:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    server = Server(vault, agent)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            stdout.write(json.dumps({"jsonrpc": "2.0", "id": None,
                                     "error": {"code": -32700, "message": "Parse error"}}) + "\n")
            stdout.flush()
            continue
        reply = server.handle(msg)
        if reply is not None:
            stdout.write(json.dumps(reply, default=str) + "\n")
            stdout.flush()
