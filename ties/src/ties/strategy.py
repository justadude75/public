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


def intro_suggestions(vault: Vault, ai=None, min_overlap: int = 1, limit: int = 200) -> list[dict]:
    """Pairs where one person is looking for what another can offer. Cached until a change."""
    from .triggers import hidden_keys

    use_ai = ai is not None and getattr(ai, "embed_model", None)
    raw = vault.cached(("intros", min_overlap, bool(use_ai)),
                       lambda: _intro_pairs(vault, ai if use_ai else None, min_overlap))
    hidden = hidden_keys(vault, "intro", date.today())
    return [x for x in raw if x["key"] not in hidden][:limit]


PER_SEEKER = 5        # best offers kept per request
MAX_INTROS = 500


def _intro_pairs(vault: Vault, ai, min_overlap: int) -> list[dict]:
    import heapq
    import math

    seeking = _claims_by(vault, "seeking")
    offers = _claims_by(vault, "offering")
    # Expertise also counts as an offer: interests and job titles.
    offers += [{**r, "implicit": True} for r in _claims_by(vault, "interest")]
    offers += [{**r, "implicit": True} for r in _claims_by(vault, "title")]
    for x in seeking + offers:
        x["text"] = json.loads(x["value"])
        x["tok"] = _tokens(x["text"])
    # Inverted index: token -> offers mentioning it, so each need is compared only with
    # offers that share at least one word (not with every offer in the vault).
    by_token: dict[str, list[int]] = defaultdict(list)
    for i, o in enumerate(offers):
        for t in o["tok"]:
            by_token[t].append(i)
    # Rare shared words say more than common ones (inverse document frequency).
    n_offers = max(len(offers), 1)
    idf = {t: math.log(1 + n_offers / len(ix)) for t, ix in by_token.items()}
    vectors = {}
    if ai is not None:
        texts = list({x["text"] for x in seeking + offers})
        try:
            vectors = dict(zip(texts, ai.embed(texts)))
        except Exception:
            vectors = {}
    linked = {frozenset((r["subject_id"], r["object_id"])) for r in vault.all(
        "select subject_id, object_id from claim where object_id is not null and "
        "status in ('accepted','proposed')")}
    out = []
    for s in seeking:
        s_weight = sum(idf.get(t, math.log(1 + n_offers)) for t in s["tok"]) or 1
        scores: dict[int, float] = defaultdict(float)
        # Find candidates through the request's rarer words; very common words (in more than
        # max(200, 2%) of offers) only add to the score of candidates found that way.
        common_cut = max(200, n_offers // 50)
        known = [t for t in s["tok"] if t in by_token]
        rare = [t for t in known if len(by_token[t]) <= common_cut] or \
            sorted(known, key=lambda t: len(by_token[t]))[:1]
        for t in rare:
            for i in by_token[t][:5000]:
                scores[i] += idf[t]
        for t in known:
            if t not in rare:
                for i in list(scores):
                    if t in offers[i]["tok"]:
                        scores[i] += idf[t]
        if vectors:
            for i, o in enumerate(offers):
                sim = textindex._cos(vectors[s["text"]], vectors[o["text"]])
                if sim >= 0.75:
                    scores[i] = max(scores[i], sim * s_weight)
        best: list[tuple[float, int]] = []
        seen_people = set()
        for i, raw in scores.items():
            o = offers[i]
            if o["subject_id"] == s["subject_id"]:
                continue
            overlap = s["tok"] & o["tok"]
            if len(overlap) < min_overlap and not vectors:
                continue
            score = raw / s_weight * (0.7 if o.get("implicit") else 1.0)
            item = (score, i)
            if len(best) < PER_SEEKER * 3:
                heapq.heappush(best, item)
            elif item > best[0]:
                heapq.heapreplace(best, item)
        for score, i in sorted(best, reverse=True):
            o = offers[i]
            if o["subject_id"] in seen_people or len(seen_people) >= PER_SEEKER:
                continue
            seen_people.add(o["subject_id"])
            out.append({
                "key": f"{s['subject_id']}:{o['subject_id']}",
                "seeker": {"id": s["subject_id"], "name": s["display_name"]},
                "offerer": {"id": o["subject_id"], "name": o["display_name"]},
                "need": s["text"], "offer": o["text"], "overlap": sorted(s["tok"] & o["tok"]),
                "score": round(score, 2),
                "already_connected": frozenset((s["subject_id"], o["subject_id"])) in linked,
            })
    out.sort(key=lambda x: (x["already_connected"], -x["score"]))
    return out[:MAX_INTROS]


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
