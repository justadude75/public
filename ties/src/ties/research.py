"""Research vault: projects, graded sources, evidence, contradictions, as-of views, hypotheses.

A research vault is an ordinary vault with meta vault_kind = "research". Differences:
- competing single-valued claims are kept side by side (no automatic supersede);
- sources carry Admiralty grades (reliability A-F, credibility 1-6) and can be
  marked as copies of another source so corroboration counts independent ones only;
- claims are shown with their support: independent sources and the best grade.
"""

from __future__ import annotations

import hashlib
import json
import urllib.request
from collections import defaultdict
from datetime import date
from typing import Any

from . import repo, textindex
from .db import Vault
from .registry import CREDIBILITY, RELIABILITY
from .repo import _add_claim, _create_source, new_id, parse_day

GRADE_SCORE = {"A": 1.0, "B": 0.85, "C": 0.65, "D": 0.4, "E": 0.2, "F": 0.5}
CRED_SCORE = {1: 1.0, 2: 0.85, 3: 0.65, 4: 0.4, 5: 0.2, 6: 0.5}
STANCES = {"CC": 2, "C": 1, "N": 0, "I": -1, "II": -2}


def is_research(vault: Vault) -> bool:
    return vault.get_meta("vault_kind") == "research"


def make_research(vault: Vault) -> None:
    vault.set_meta("vault_kind", "research")


# ── projects ────────────────────────────────────────────────────────────────


def create_project(vault: Vault, title: str, description: str = "") -> str:
    pid = new_id()
    with vault.tx() as c:
        c.execute("insert into project(id, title, description) values(?,?,?)",
                  (pid, title.strip(), description.strip()))
        vault.audit(c, "write", pid, {"op": "create_project"})
    return pid


def add_to_project(vault: Vault, project_id: str, kind: str, target_id: str) -> None:
    with vault.tx() as c:
        c.execute("insert or ignore into project_item(project_id, target_kind, target_id) "
                  "values(?,?,?)", (project_id, kind, target_id))


def remove_from_project(vault: Vault, project_id: str, kind: str, target_id: str) -> None:
    with vault.tx() as c:
        c.execute("delete from project_item where project_id=? and target_kind=? and target_id=?",
                  (project_id, kind, target_id))


def projects(vault: Vault) -> list[dict]:
    rows = vault.all("select * from project order by created_at desc")
    for p in rows:
        counts = {r["target_kind"]: r["n"] for r in vault.all(
            "select target_kind, count(*) as n from project_item where project_id=? "
            "group by target_kind", (p["id"],))}
        p["counts"] = counts
        p["hypotheses"] = vault.scalar("select count(*) from hypothesis where project_id=?", (p["id"],))
    return rows


def project_detail(vault: Vault, project_id: str) -> dict:
    p = vault.one("select * from project where id=?", (project_id,))
    if not p:
        raise ValueError("Unknown project")
    ents = vault.all("select e.* from project_item pi join entity e on e.id=pi.target_id where "
                     "pi.project_id=? and pi.target_kind='entity' and e.status='active' "
                     "order by e.type_id, e.display_name", (project_id,))
    srcs = vault.all("select s.* from project_item pi join source s on s.id=pi.target_id where "
                     "pi.project_id=? and pi.target_kind='source' order by s.published_at desc",
                     (project_id,))
    events = [e for e in repo.list_events(vault, limit=1000, descending=False)
              if vault.one("select 1 from participation where event_id=? and entity_id in "
                           "(select target_id from project_item where project_id=?)",
                           (e["id"], project_id))]
    return {"project": p, "entities": ents, "sources": srcs, "events": events,
            "hypotheses": vault.all("select * from hypothesis where project_id=? order by created_at",
                                    (project_id,))}


def project_entity_ids(vault: Vault, project_id: str) -> set[str]:
    return {r["target_id"] for r in vault.all(
        "select target_id from project_item where project_id=? and target_kind='entity'",
        (project_id,))}


# ── sources ─────────────────────────────────────────────────────────────────


def add_source(vault: Vault, *, title: str, uri: str | None = None, author: str | None = None,
               published_at: str | None = None, source_type: str = "other",
               reliability: str | None = None, credibility: int | None = None,
               excerpt: str = "", independent_of: str | None = None,
               project_id: str | None = None, external_key: str | None = None) -> str:
    with vault.tx() as c:
        if external_key:
            row = c.execute("select id from source where kind='research' and external_key=?",
                            (external_key,)).fetchone()
            if row:
                return row["id"]
        sid = _create_source(c, "research", excerpt or "", title=title.strip(), uri=uri or None,
                             external_key=external_key)
        c.execute("update source set author=?, published_at=?, source_type=?, reliability=?, "
                  "credibility=?, independent_of=? where id=?",
                  (author or None, published_at or None, source_type, (reliability or None),
                   credibility, independent_of or None, sid))
        if excerpt:
            textindex.index_source(c, sid, f"{title}\n\n{excerpt}")
        if project_id:
            c.execute("insert or ignore into project_item(project_id, target_kind, target_id) "
                      "values(?,?,?)", (project_id, "source", sid))
        vault.audit(c, "write", sid, {"op": "add_source"})
    return sid


def update_source_grade(vault: Vault, source_id: str, reliability: str | None,
                        credibility: int | None, independent_of: str | None = None) -> None:
    with vault.tx() as c:
        c.execute("update source set reliability=?, credibility=?, independent_of=? where id=?",
                  (reliability or None, credibility, independent_of or None, source_id))


def sources(vault: Vault, q: str = "", project_id: str | None = None) -> list[dict]:
    sql = ("select s.*, (select count(*) from claim_evidence ev join claim cl on cl.id=ev.claim_id "
           "where ev.source_id=s.id and cl.status<>'rejected') as n_claims from source s "
           "where s.kind in ('research','web','import','markdown')")
    params: list = []
    if project_id:
        sql += (" and s.id in (select target_id from project_item where project_id=? and "
                "target_kind='source')")
        params.append(project_id)
    if q:
        sql += " and (s.title like ? or s.author like ? or s.uri like ?)"
        params += [f"%{q}%"] * 3
    return vault.all(sql + " order by coalesce(s.published_at, s.captured_at) desc limit 500", params)


def source_detail(vault: Vault, source_id: str) -> dict:
    s = vault.one("select * from source where id=?", (source_id,))
    if not s:
        raise ValueError("Unknown source")
    claims = repo._claim_rows(vault, "cl.id in (select claim_id from claim_evidence where source_id=?)",
                              (source_id,))
    for cl in claims:
        ev = vault.one("select note from claim_evidence where claim_id=? and source_id=?",
                       (cl["id"], source_id))
        cl["quote"] = ev["note"] if ev else None
    blob = vault.one("select content_type, sha256, fetched_at, archive_url, length(data) as size "
                     "from source_blob where source_id=?", (source_id,))
    copies = vault.all("select id, title from source where independent_of=?", (source_id,))
    original = vault.one("select id, title from source where id=?", (s["independent_of"],)) \
        if s["independent_of"] else None
    return {"source": s, "claims": claims, "blob": blob, "copies": copies, "original": original,
            "reliability_label": RELIABILITY.get(s["reliability"] or ""),
            "credibility_label": CREDIBILITY.get(s["credibility"] or 0)}


def add_evidence_claim(vault: Vault, source_id: str, subject_ref: str, predicate_id: str,
                       value: str | None = None, object_ref: str | None = None,
                       valid_from: str | None = None, valid_to: str | None = None,
                       quote: str | None = None, confidence: float = 1.0) -> str:
    """Record a claim as stated by a source (basis 'public'), with the supporting quote."""
    with vault.tx() as c:
        pred = repo.predicate(c, predicate_id)
        if not pred:
            raise ValueError("Unknown field")
        subject = repo.resolve_ref(c, vault, subject_ref, "person", "reference")
        obj = None
        if pred["range_kind"] == "entity":
            types = json.loads(pred["range_types"] or '["person"]')
            obj = repo.resolve_ref(c, vault, object_ref, types[0] if types[0] != "*" else "person",
                                   "reference")
        existing = c.execute(
            "select id from claim where subject_id=? and predicate_id=? and status='accepted' and "
            "coalesce(object_id,'')=coalesce(?,'') and coalesce(value,'')=coalesce(?,'') and "
            "coalesce(valid_from,'')=coalesce(?,'') and coalesce(valid_to,'')=coalesce(?,'')",
            (subject, predicate_id, obj, json.dumps(str(value).strip()) if value and not obj else None,
             valid_from or None, valid_to or None)).fetchone()
        if existing:
            cid = existing["id"]   # corroboration: same claim, another source
        else:
            cid = _add_claim(c, subject, predicate_id, value=None if obj else value, object_id=obj,
                             basis="public", confidence=confidence, valid_from=valid_from,
                             valid_to=valid_to, asserted_by="user")
        c.execute("insert or replace into claim_evidence(claim_id, source_id, note) values(?,?,?)",
                  (cid, source_id, (quote or "").strip() or None))
        vault.audit(c, "write", subject, {"op": "evidence", "claim": cid, "source": source_id})
        return cid


def _root(sources_by_id: dict, sid: str) -> str:
    seen = set()
    while sources_by_id.get(sid, {}).get("independent_of") and sid not in seen:
        seen.add(sid)
        sid = sources_by_id[sid]["independent_of"]
    return sid


def support(vault: Vault, claim_ids: list[str] | None = None) -> dict[str, dict]:
    """Per claim: independent source count, best grade, and an effective confidence."""
    srcs = {r["id"]: r for r in vault.all(
        "select id, reliability, credibility, independent_of, title from source")}
    sql = "select ev.claim_id, ev.source_id from claim_evidence ev"
    rows = vault.all(sql)
    by_claim: dict[str, set] = defaultdict(set)
    for r in rows:
        if claim_ids is None or r["claim_id"] in claim_ids:
            by_claim[r["claim_id"]].add(r["source_id"])
    out = {}
    for cid, sids in by_claim.items():
        graded = [s for s in sids if srcs.get(s, {}).get("reliability") or srcs.get(s, {}).get("credibility")]
        roots = {_root(srcs, s) for s in graded}
        best = None
        conf_missing = 1.0
        for s in roots:
            src = srcs.get(s, {})
            score = GRADE_SCORE.get(src.get("reliability") or "F", 0.5) * \
                CRED_SCORE.get(src.get("credibility") or 6, 0.5)
            conf_missing *= (1 - score)
            grade = f"{src.get('reliability') or '?'}{src.get('credibility') or '?'}"
            if best is None or score > best[0]:
                best = (score, grade)
        out[cid] = {"sources": len(sids), "independent": len(roots),
                    "best_grade": best[1] if best else None,
                    "confidence": round(1 - conf_missing, 2) if roots else None}
    return out


# ── contradictions ──────────────────────────────────────────────────────────


def _overlap(a_from, a_to, b_from, b_to) -> bool:
    a0 = parse_day(a_from) or date.min
    a1 = parse_day(a_to) or date.max
    b0 = parse_day(b_from) or date.min
    b1 = parse_day(b_to) or date.max
    return max(a0, b0) <= min(a1, b1)


def contradictions(vault: Vault, entity_ids: set[str] | None = None) -> list[dict]:
    """Single-valued facts with different accepted values whose time ranges overlap."""
    rows = repo._claim_rows(vault, "cl.status='accepted' and p.cardinality='one'", ())
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        if entity_ids and r["subject_id"] not in entity_ids:
            continue
        groups[(r["subject_id"], r["predicate_id"])].append(r)
    sup = support(vault)
    out = []
    for (subject, pred), claims in groups.items():
        if len(claims) < 2:
            continue
        for i, a in enumerate(claims):
            for b in claims[i + 1:]:
                va, vb = a["object_id"] or a["value"], b["object_id"] or b["value"]
                if va == vb:
                    continue
                if _overlap(a["valid_from"], a["valid_to"], b["valid_from"], b["valid_to"]):
                    for x in (a, b):
                        x["support"] = sup.get(x["id"])
                    out.append({"subject_id": subject, "subject_name": a["subject_name"],
                                "predicate": a["predicate_label"], "a": a, "b": b})
    return out


# ── as-of views ─────────────────────────────────────────────────────────────


def entity_as_of(vault: Vault, entity_id: str, known_at: str | None = None,
                 valid_at: str | None = None) -> list[dict]:
    """Claims about an entity as they were recorded by `known_at` and/or true at `valid_at`.

    known_at uses recorded time: a claim counts if it was recorded by then and had not yet
    been superseded (the superseding claim's recorded time) or rejected.
    """
    rows = repo._claim_rows(vault, "(cl.subject_id=? or cl.object_id=?) and cl.status<>'proposed'",
                            (entity_id, entity_id))
    recorded = {r["id"]: r["recorded_at"] for r in rows}
    extra = {r["id"]: r["recorded_at"] for r in vault.all(
        "select id, recorded_at from claim where id in (select superseded_by from claim where "
        "subject_id=? or object_id=?)", (entity_id, entity_id))}
    recorded.update(extra)
    out = []
    for r in rows:
        if known_at:
            cutoff = known_at + " 23:59:59"
            if r["recorded_at"] > cutoff:
                continue
            if r["status"] == "superseded" and r["superseded_by"] and \
                    recorded.get(r["superseded_by"], "9999") <= cutoff:
                continue
            if r["status"] == "rejected":
                continue
        elif r["status"] not in ("accepted", "disputed"):
            continue
        if valid_at and not _overlap(r["valid_from"], r["valid_to"], valid_at, valid_at):
            continue
        repo._orient(vault, r, entity_id)
        out.append(r)
    return out


# ── hypotheses (analysis of competing hypotheses) ───────────────────────────


def add_hypothesis(vault: Vault, project_id: str, title: str, description: str = "") -> str:
    hid = new_id()
    with vault.tx() as c:
        c.execute("insert into hypothesis(id, project_id, title, description) values(?,?,?,?)",
                  (hid, project_id, title.strip(), description.strip()))
    return hid


def add_evidence_item(vault: Vault, project_id: str, label: str, kind: str = "note",
                      target_id: str | None = None) -> str:
    eid = new_id()
    with vault.tx() as c:
        c.execute("insert into evidence_item(id, project_id, target_kind, target_id, label) "
                  "values(?,?,?,?,?)", (eid, project_id, kind, target_id, label.strip()))
    return eid


def rate(vault: Vault, hypothesis_id: str, evidence_id: str, stance: str, note: str = "") -> None:
    if stance not in STANCES and stance != "":
        raise ValueError("Unknown rating")
    with vault.tx() as c:
        if stance == "":
            c.execute("delete from hypothesis_rating where hypothesis_id=? and evidence_id=?",
                      (hypothesis_id, evidence_id))
        else:
            c.execute("insert into hypothesis_rating(hypothesis_id, evidence_id, stance, note) "
                      "values(?,?,?,?) on conflict(hypothesis_id, evidence_id) do update set "
                      "stance=excluded.stance, note=excluded.note",
                      (hypothesis_id, evidence_id, stance, note or None))


def ach_matrix(vault: Vault, project_id: str) -> dict:
    hyps = vault.all("select * from hypothesis where project_id=? order by created_at", (project_id,))
    items = vault.all("select * from evidence_item where project_id=? order by created_at",
                      (project_id,))
    sup = support(vault)
    for it in items:
        it["support"] = sup.get(it["target_id"]) if it["target_kind"] == "claim" else None
        if it["target_kind"] == "source":
            s = vault.one("select reliability, credibility from source where id=?", (it["target_id"],))
            it["grade"] = f"{s['reliability'] or '?'}{s['credibility'] or '?'}" if s else None
    ratings = {(r["hypothesis_id"], r["evidence_id"]): r["stance"] for r in vault.all(
        "select hr.* from hypothesis_rating hr join hypothesis h on h.id=hr.hypothesis_id "
        "where h.project_id=?", (project_id,))}
    for h in hyps:
        # ACH ranks by inconsistency: the hypothesis with the least evidence against it leads.
        h["inconsistency"] = sum(-STANCES[ratings[(h["id"], it["id"])]] for it in items
                                 if ratings.get((h["id"], it["id"])) in ("I", "II"))
        h["consistency"] = sum(STANCES[ratings[(h["id"], it["id"])]] for it in items
                               if ratings.get((h["id"], it["id"])) in ("C", "CC"))
    ranked = sorted(hyps, key=lambda h: (h["inconsistency"], -h["consistency"]))
    # Diagnostic evidence: rated differently across hypotheses.
    for it in items:
        vals = {ratings.get((h["id"], it["id"])) for h in hyps} - {None}
        it["diagnostic"] = len(vals) > 1
    return {"hypotheses": hyps, "ranked": ranked, "evidence": items, "ratings": ratings}


# ── snapshots (network; off unless allowed) ─────────────────────────────────


def snapshot(vault: Vault, source_id: str, timeout: float = 20) -> dict:
    if vault.get_meta("allow_network") != "1":
        raise ValueError("Fetching is off. Allow network access in Settings first.")
    s = vault.one("select uri from source where id=?", (source_id,))
    if not s or not s["uri"]:
        raise ValueError("This source has no address to fetch.")
    req = urllib.request.Request(s["uri"], headers={"User-Agent": "ties-research/0.2"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        data = res.read(20 * 1024 * 1024)
        ctype = res.headers.get_content_type()
    digest = hashlib.sha256(data).hexdigest()
    with vault.tx() as c:
        c.execute("insert or replace into source_blob(source_id, content_type, data, sha256, "
                  "archive_url) values(?,?,?,?,?)", (source_id, ctype, data, digest,
                                                     f"https://web.archive.org/web/{s['uri']}"))
        vault.audit(c, "egress", source_id, {"op": "snapshot", "uri": s["uri"]}, actor="user")
    return {"sha256": digest, "size": len(data), "content_type": ctype}
