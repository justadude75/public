"""Privacy gateway for optional outside reasoning (plan section 12.3).

Steps: choose only what the task needs, pseudonymize, generalize, drop, check for
leaks and unique combinations, show the exact payload, log the egress, and map
the answer back to real entities locally. Off by default.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from datetime import date
from typing import Any

from . import graph, repo, scoring
from .ai import AIClient, AIError
from .db import Vault

SIZE_BUCKETS = [(10, "tiny (≤10)"), (50, "small (11–50)"), (250, "mid-size (51–250)"),
                (1000, "large (251–1000)"), (10**9, "very large (1000+)")]
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
URL = re.compile(r"https?://\S+|www\.\S+|\b\w+\.(com|org|net|io|co)\b", re.I)

TASKS = {
    "introductions": "Suggest which of these people I should introduce to each other, and why.",
    "strategy": "Given my goal, which relationships should I invest in over the next month, "
                "and in what order?",
    "weekly": "Looking at my week, what should I prioritise next week?",
}


class GatewayError(Exception):
    pass


def _bucket(size: str | None) -> str | None:
    try:
        n = float(size)
    except (TypeError, ValueError):
        return None
    return next(label for limit, label in SIZE_BUCKETS if n <= limit)


def _names_index(vault: Vault) -> list[str]:
    """Every name and distinctive name part in the vault, for leak checks."""
    out = set()
    for r in vault.all("select n.name from entity_name n join entity e on e.id=n.entity_id "
                       "where e.type_id in ('person','organization','place')"):
        name = r["name"].strip()
        if len(name) >= 3:
            out.add(name.lower())
        for part in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", name):
            out.add(part.lower())
    common = {"head", "partnerships", "climate", "storage", "payments", "ventures", "group",
              "capital", "partners", "labs", "company", "university", "college", "school"}
    return sorted(out - common, key=len, reverse=True)


def scrub(text: str, names: list[str]) -> str:
    text = EMAIL.sub("[email]", text)
    text = URL.sub("[link]", text)
    text = PHONE.sub("[phone]", text)
    for n in names:
        text = re.sub(rf"\b{re.escape(n)}\b", "[name]", text, flags=re.I)
    return text


def leak_check(payload: dict, names: list[str]) -> list[str]:
    blob = json.dumps(payload, ensure_ascii=False)
    hits = []
    for pat, label in ((EMAIL, "email"), (PHONE, "phone number"), (URL, "link")):
        if pat.search(blob):
            hits.append(label)
    low = blob.lower()
    for n in names:
        if re.search(rf"\b{re.escape(n)}\b", low):
            hits.append(f"name “{n}”")
    return hits


def build_payload(vault: Vault, task: str, goal_id: str | None = None,
                  today: date | None = None, max_people: int = 40) -> tuple[dict, dict]:
    """Return (payload, alias_map). alias_map maps aliases back to entity ids; it never leaves."""
    if task not in TASKS:
        raise GatewayError("Unknown task")
    today = today or date.today()
    names = _names_index(vault)
    statuses = scoring.compute_all(vault, today)
    ranked = sorted(statuses.values(), key=lambda s: -(s.strength + s.goal_relevance))
    chosen = [s.entity_id for s in ranked[:max_people]]
    if goal_id:
        for r in vault.all("select target_id from goal_link where goal_id=? and target_kind='entity'",
                           (goal_id,)):
            if r["target_id"] not in chosen:
                chosen.append(r["target_id"])
    alias = {eid: f"P{i + 1}" for i, eid in enumerate(chosen)}
    sensitive = {r["id"] for r in vault.all("select id from predicate where sensitivity<>'normal'")}

    people = []
    for eid in chosen:
        ent = repo.get_entity(vault, eid)
        if not ent or ent["type_id"] != "person":
            continue
        st = statuses.get(eid)
        person: dict[str, Any] = {"id": alias[eid], "tier": st.tier if st else "not tracked"}
        if st and st.last_contact:
            person["last_contact"] = ("this month" if st.days_since <= 30 else
                                      "this quarter" if st.days_since <= 90 else
                                      "this year" if st.days_since <= 365 else "over a year ago")
        rels = [r for r in repo.relations(vault, eid) if r["predicate_id"] not in sensitive]
        for r in rels:
            if r["predicate_id"] == "employed_by" and r["outgoing"] and not r["valid_to"]:
                org = r["other_id"]
                industry = repo.current_value(vault, org, "industry")
                size = repo.current_value(vault, org, "size")
                desc = ", ".join(x for x in (
                    industry["value_text"] if industry else None,
                    _bucket(size["value_text"]) if size else None) if x)
                person["works_in"] = desc or "an organization"
        for f in repo.facts(vault, eid):
            if f["predicate_id"] in sensitive:
                continue
            if f["predicate_id"] in ("seeking", "offering", "interest"):
                person.setdefault(f["predicate_id"], []).append(scrub(f["value_text"], names))
            elif f["predicate_id"] == "title":
                person["seniority"] = _seniority(f["value_text"])
        if goal_id:
            gl = vault.one("select relevance, role from goal_link where goal_id=? and target_id=?",
                           (goal_id, eid))
            if gl:
                person["goal_role"] = gl["role"] or "linked"
        people.append(person)

    # Generalize further where a combination is unique (a simple k-anonymity check).
    combos = Counter((p.get("works_in"), p.get("seniority")) for p in people)
    for p in people:
        if combos[(p.get("works_in"), p.get("seniority"))] == 1 and p.get("works_in"):
            p["works_in"] = p["works_in"].split(",")[0]
            if Counter(x.get("works_in") for x in people)[p["works_in"]] == 1:
                p.pop("works_in", None)

    edges = []
    data = graph.build(vault, include_inferred=False, include_together=True, include_ego=False,
                       types=("person",), today=today)
    for e in data["edges"]:
        if e["source"] in alias and e["target"] in alias and e["kind"] not in sensitive:
            edges.append({"a": alias[e["source"]], "b": alias[e["target"]],
                          "tie": e["kind"].replace("_", " "), "strength": round(e["weight"], 1)})
    for eid in chosen:
        st = statuses.get(eid)
        if st:
            edges.append({"a": "me", "b": alias[eid], "tie": "my contact",
                          "strength": round(st.strength, 1)})

    payload: dict[str, Any] = {"task": TASKS[task], "people": people, "ties": edges}
    if goal_id:
        g = vault.one("select title, keywords from goal where id=?", (goal_id,))
        payload["goal"] = scrub(f"{g['title']}. {g['keywords'] or ''}".strip(" ."), names)
    payload["answer_format"] = ("JSON: {\"suggestions\": [{\"people\": [\"P1\", \"P2\"], "
                                "\"action\": \"...\", \"why\": \"...\"}]}. Refer to people only by their ids.")
    hits = leak_check({k: v for k, v in payload.items() if k != "answer_format"}, names)
    if hits:
        raise GatewayError("Blocked: the payload still contained " + ", ".join(sorted(set(hits))))
    return payload, {v: k for k, v in alias.items()}


def _seniority(title: str) -> str:
    t = title.lower()
    if re.search(r"\b(ceo|cto|cfo|coo|founder|co-founder|president|partner|chief)\b", t):
        return "executive or founder"
    if re.search(r"\b(vp|vice president|director|head)\b", t):
        return "senior leader"
    if re.search(r"\b(manager|lead|principal)\b", t):
        return "manager or lead"
    return "individual contributor"


def payload_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def outside_client(vault: Vault) -> AIClient:
    if vault.get_meta("gateway_enabled") != "1":
        raise GatewayError("Outside reasoning is turned off (Settings).")
    url, model = vault.get_meta("gateway_base_url"), vault.get_meta("gateway_model")
    if not url or not model:
        raise GatewayError("Set the outside model's address and name in Settings.")
    return AIClient(url, model, None, api_key=os.environ.get("TIES_GATEWAY_KEY"),
                    require_local=False)


def send(vault: Vault, payload: dict, alias_map: dict, client=None, where: str = "outside") -> dict:
    """Send an already-previewed payload and map the answer back to real entities."""
    names = _names_index(vault)
    hits = leak_check({k: v for k, v in payload.items() if k != "answer_format"}, names)
    if hits:
        raise GatewayError("Blocked: " + ", ".join(sorted(set(hits))))
    if client is None:
        client = outside_client(vault) if where == "outside" else AIClient.from_vault(vault)
        if client is None:
            raise GatewayError("No local model configured.")
    with vault.tx() as c:
        vault.audit(c, "egress" if where == "outside" else "read", "gateway",
                    {"where": where, "task": payload.get("task"), "payload_hash": payload_hash(payload),
                     "people": len(payload.get("people", [])),
                     "endpoint": getattr(client, "base_url", "?")}, actor="gateway")
    system = ("You advise someone on their professional and personal network. People are "
              "pseudonymous (P1, P2...). Answer only in the JSON format requested.")
    try:
        raw = client.chat_json(system, json.dumps(payload, ensure_ascii=False))
    except AIError as exc:
        raise GatewayError(str(exc)) from exc
    return reidentify(vault, raw, alias_map)


def reidentify(vault: Vault, raw: Any, alias_map: dict) -> dict:
    suggestions = []
    items = raw.get("suggestions", []) if isinstance(raw, dict) else []
    for s in items:
        people = []
        for a in s.get("people", []):
            eid = alias_map.get(str(a))
            ent = repo.get_entity(vault, eid) if eid else None
            if ent:
                people.append({"id": ent["id"], "name": ent["display_name"]})

        def swap(text: str) -> str:
            def rep(m):
                eid = alias_map.get(m.group(0))
                ent = repo.get_entity(vault, eid) if eid else None
                return ent["display_name"] if ent else m.group(0)
            return re.sub(r"\bP\d+\b", rep, text or "")
        suggestions.append({"people": people, "action": swap(s.get("action", "")),
                            "why": swap(s.get("why", ""))})
    return {"suggestions": suggestions}


def egress_log(vault: Vault, limit: int = 20) -> list[dict]:
    rows = vault.all("select * from audit_log where actor='gateway' order by id desc limit ?", (limit,))
    for r in rows:
        r["detail"] = json.loads(r["detail"] or "{}")
    return rows
