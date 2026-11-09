"""Strategy: goals, asks-and-offers matching, introductions, and the weekly review."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date, timedelta

from . import graph, repo, scoring, textindex
from .db import Vault
from .repo import new_id

STOP = set("a an and are as at be by for from has have i in is it its of on or our the their "
           "they this to was we with who will would can could about into over new".split())


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    out = set()
    for w in words:
        if w in STOP or len(w) < 3:
            continue
        for suffix in ("ing", "ers", "er", "es", "s"):
            if w.endswith(suffix) and len(w) - len(suffix) >= 4:
                w = w[: -len(suffix)]
                break
        out.add(w)
    return out


# ── goals ───────────────────────────────────────────────────────────────────


def create_goal(vault: Vault, title: str, description: str = "", keywords: str = "",
                horizon: str | None = None) -> str:
    gid = new_id()
    with vault.tx() as c:
        c.execute("insert into goal(id, title, description, keywords, horizon) values(?,?,?,?,?)",
                  (gid, title.strip(), description.strip(), keywords.strip(), horizon or None))
        vault.audit(c, "write", gid, {"op": "create_goal"})
    return gid


def update_goal(vault: Vault, goal_id: str, **fields) -> None:
    allowed = {k: v for k, v in fields.items() if k in ("title", "description", "keywords",
                                                        "horizon", "status")}
    if not allowed:
        return
    with vault.tx() as c:
        sets = ", ".join(f"{k}=?" for k in allowed)
        c.execute(f"update goal set {sets} where id=?", (*allowed.values(), goal_id))


def link_goal(vault: Vault, goal_id: str, entity_id: str, relevance: float = 1.0,
              role: str | None = None) -> None:
    with vault.tx() as c:
        c.execute("insert into goal_link(goal_id, target_kind, target_id, relevance, role) "
                  "values(?,?,?,?,?) on conflict(goal_id, target_kind, target_id) do update set "
                  "relevance=excluded.relevance, role=excluded.role",
                  (goal_id, "entity", entity_id, relevance, role))


def unlink_goal(vault: Vault, goal_id: str, entity_id: str) -> None:
    with vault.tx() as c:
        c.execute("delete from goal_link where goal_id=? and target_kind='entity' and target_id=?",
                  (goal_id, entity_id))


def goals(vault: Vault, status: str | None = "active") -> list[dict]:
    sql = "select * from goal" + (" where status=?" if status else "") + " order by created_at"
    rows = vault.all(sql, (status,) if status else ())
    for g in rows:
        g["links"] = vault.all(
            "select gl.*, e.display_name, e.type_id from goal_link gl join entity e "
            "on e.id=gl.target_id where gl.goal_id=? and gl.target_kind='entity' "
            "and e.status='active' order by gl.relevance desc", (g["id"],))
    return rows


def goal_relevance(vault: Vault) -> dict[str, float]:
    """entity id -> highest relevance across active goals (used in Today ranking)."""
    out: dict[str, float] = {}
    for r in vault.all("select gl.target_id, max(gl.relevance) as rel from goal_link gl "
                       "join goal g on g.id=gl.goal_id where g.status='active' and "
                       "gl.target_kind='entity' group by gl.target_id"):
        out[r["target_id"]] = float(r["rel"])
    return out


def goal_detail(vault: Vault, goal_id: str, today: date | None = None, ai=None) -> dict:
    today = today or date.today()
    g = next((x for x in goals(vault, None) if x["id"] == goal_id), None)
    if not g:
        raise ValueError("Unknown goal")
    linked = {l["target_id"] for l in g["links"]}
    statuses = scoring.compute_all(vault, today)
    for l in g["links"]:
        l["status"] = statuses.get(l["target_id"])
        if l["type_id"] == "person" and l["target_id"] not in statuses:
            l["paths"] = graph.warm_paths(vault, l["target_id"], k=2, today=today)
    query = " ".join([g["keywords"] or "", g["title"]])
    found = textindex.search(vault, query, ai=ai, limit=25, any_term=True)
    suggestions = []
    for e in found["entities"]:
        if e["id"] in linked or e["type_id"] == "event" or e.get("engagement") == "ego":
            continue
        suggestions.append(e)
    return {"goal": g, "suggestions": suggestions[:12]}


# ── asks and offers ─────────────────────────────────────────────────────────


def _claims_by(vault: Vault, predicate_id: str) -> list[dict]:
    return vault.all(
        "select cl.id, cl.subject_id, cl.value, e.display_name, e.engagement from claim cl "
        "join entity e on e.id=cl.subject_id where cl.predicate_id=? and cl.status='accepted' "
        "and cl.valid_to is null and e.status='active'", (predicate_id,))


def _connected(vault: Vault, a: str, b: str) -> bool:
    return vault.one("select 1 from claim where status in ('accepted','proposed') and "
                     "((subject_id=? and object_id=?) or (subject_id=? and object_id=?))",
                     (a, b, b, a)) is not None


def intro_suggestions(vault: Vault, ai=None, min_overlap: int = 1) -> list[dict]:
    """Pairs where one person is looking for what another can offer."""
    from .triggers import hidden_keys

    seeking = _claims_by(vault, "seeking")
    offers = _claims_by(vault, "offering")
    # Expertise also counts as an offer: interests and job titles.
    offers += [{**r, "implicit": True} for r in _claims_by(vault, "interest")]
    offers += [{**r, "implicit": True} for r in _claims_by(vault, "title")]
    hidden = hidden_keys(vault, "intro", date.today())
    vectors = {}
    if ai is not None and getattr(ai, "embed_model", None):
        texts = list({json.loads(x["value"]) for x in seeking + offers})
        try:
            vectors = dict(zip(texts, ai.embed(texts)))
        except Exception:
            vectors = {}
    out, seen = [], set()
    for s in seeking:
        s_text = json.loads(s["value"])
        s_tok = _tokens(s_text)
        for o in offers:
            if o["subject_id"] == s["subject_id"]:
                continue
            o_text = json.loads(o["value"])
            overlap = s_tok & _tokens(o_text)
            score = len(overlap) / max(len(s_tok), 1)
            if vectors:
                score = max(score, textindex._cos(vectors[s_text], vectors[o_text]))
            if len(overlap) < min_overlap and score < 0.75:
                continue
            key = f"{s['subject_id']}:{o['subject_id']}"
            if key in seen or key in hidden:
                continue
            seen.add(key)
            out.append({
                "key": key, "seeker": {"id": s["subject_id"], "name": s["display_name"]},
                "offerer": {"id": o["subject_id"], "name": o["display_name"]},
                "need": s_text, "offer": o_text, "overlap": sorted(overlap),
                "score": round(score * (0.7 if o.get("implicit") else 1.0), 2),
                "already_connected": _connected(vault, s["subject_id"], o["subject_id"]),
            })
    out.sort(key=lambda x: (x["already_connected"], -x["score"]))
    return out


# ── weekly review ───────────────────────────────────────────────────────────


def weekly_review(vault: Vault, today: date | None = None) -> dict:
    today = today or date.today()
    start = today - timedelta(days=7)
    events = repo.list_events(vault, status="occurred", start=start.isoformat(),
                              end=today.isoformat(), limit=200)
    people = {}
    for e in events:
        for p in e["people"]:
            people.setdefault(p["id"], p["display_name"])
    opened = vault.all("select c.*, coalesce(t.display_name, f.display_name) as other from commitment c "
                       "join entity f on f.id=c.from_id left join entity t on t.id=c.to_id "
                       "where c.created_at >= ?", (start.isoformat(),))
    closed = vault.all("select c.* from commitment c where c.status='done' and c.done_at >= ?",
                       (start.isoformat(),))
    new_facts = vault.all(
        "select cl.*, s.display_name as subject_name, p.label from claim cl join entity s "
        "on s.id=cl.subject_id join predicate p on p.id=cl.predicate_id where cl.recorded_at >= ? "
        "and cl.status='accepted' and s.type_id<>'event' order by cl.recorded_at desc limit 40",
        (start.isoformat(),))
    for f in new_facts:
        f["value_text"] = json.loads(f["value"]) if f["value"] else None
        if f["object_id"]:
            f["value_text"] = repo.get_entity(vault, f["object_id"])["display_name"]
    statuses = scoring.compute_all(vault, today)
    decaying = sorted([s for s in statuses.values() if s.days_to_due is not None
                       and -7 <= s.days_to_due < 0], key=lambda s: s.days_to_due)
    next_week = repo.list_events(vault, status="planned", start=today.isoformat(),
                                 end=(today + timedelta(days=7)).isoformat(), descending=False)
    view = scoring.today_view(vault, today)
    goal_rows = []
    for g in goals(vault):
        ids = {l["target_id"] for l in g["links"]}
        touched = [people[i] for i in ids if i in people]
        goal_rows.append({"goal": g, "touched": touched, "linked": len(ids)})
    return {"start": start, "today": today, "events": events, "people": people,
            "opened": opened, "closed": closed, "new_facts": new_facts, "decaying": decaying,
            "next_week": next_week, "suggested": view["due"][:7], "goals": goal_rows,
            "loops_open": len(view["loops"])}


def weekly_narrative(ai, review: dict) -> str:
    lines = [f"Interactions this week: {len(review['events'])} with {len(review['people'])} people: "
             + ", ".join(review["people"].values()),
             f"Follow-ups opened: {len(review['opened'])}; closed: {len(review['closed'])}; "
             f"open overall: {review['loops_open']}",
             "Became overdue this week: " + ", ".join(s.name for s in review["decaying"]),
             "Planned next week: " + ", ".join(e["title"] for e in review["next_week"]),
             "Suggested outreach: " + ", ".join(s.name for s in review["suggested"])]
    for g in review["goals"]:
        lines.append(f"Goal '{g['goal']['title']}': touched {len(g['touched'])} of {g['linked']} linked people")
    system = ("You are the private assistant of the owner of this notebook. Write a short weekly "
              "review (5-8 bullet points): what happened, what slipped, and the 3 most useful things "
              "to do next week. Use only the facts given.")
    return ai.chat(system, "\n".join(lines))
