"""Duplicate resolution across the whole vault: find candidates, merge, undo, not-the-same.

A merge moves everything from the dropped entry onto the kept one and records each
move, so undo can put every row back. "Not the same" is remembered, so the pair is
never suggested again.
"""

from __future__ import annotations

import difflib
import json
from collections import defaultdict
from itertools import combinations

from . import textindex
from .db import Vault
from .repo import _canon_token, _context, new_id

CHECK_TYPES = ("person", "organization", "place")


def _not_same_pairs(vault: Vault) -> set[frozenset]:
    out = set()
    for r in vault.all("select payload from resolution_decision where kind='not_same' "
                       "and reverted_by is null"):
        p = json.loads(r["payload"])
        out.add(frozenset((p["a"], p["b"])))
    return out


def score_pair(a: dict, b: dict) -> tuple[float, list[str]]:
    reasons, score = [], 0.0
    shared = a["idents"] & b["idents"]
    if shared:
        scheme = sorted(shared)[0][0]
        reasons.append(f"same {scheme}")
        score = max(score, 0.95)
    for na in a["names"]:
        for nb in b["names"]:
            if na == nb:
                reasons.append("same name")
                score = max(score, 0.8)
                continue
            ta, tb = na.split(), nb.split()
            if len(ta) > 1 and len(tb) > 1 and ta[-1] == tb[-1]:
                if _canon_token(ta[0]) == _canon_token(tb[0]):
                    reasons.append("nickname of the same name")
                    score = max(score, 0.75)
                    continue
                if ta[0][0] == tb[0][0] and (len(ta[0]) == 1 or len(tb[0]) == 1):
                    reasons.append("same initial and family name")
                    score = max(score, 0.6)
                    continue
            ratio = difflib.SequenceMatcher(None, na, nb).ratio()
            if ratio >= 0.88:
                reasons.append("similar spelling")
                score = max(score, ratio * 0.75)
    return score, list(dict.fromkeys(reasons))


def candidates(vault: Vault, min_score: float = 0.55, limit: int = 100) -> list[dict]:
    rows = vault.all(
        f"select id, type_id, display_name, engagement from entity where status='active' and "
        f"type_id in ({','.join('?' * len(CHECK_TYPES))}) and coalesce(engagement,'')<>'ego'",
        CHECK_TYPES)
    info: dict[str, dict] = {}
    for r in rows:
        info[r["id"]] = {**r, "names": set(), "idents": set()}
    for n in vault.all("select entity_id, name_norm from entity_name"):
        if n["entity_id"] in info and n["name_norm"]:
            info[n["entity_id"]]["names"].add(n["name_norm"])
    for i in vault.all("select entity_id, scheme, value from identifier"):
        if i["entity_id"] in info:
            info[i["entity_id"]]["idents"].add((i["scheme"], i["value"]))

    blocks: dict[tuple, list[str]] = defaultdict(list)
    for eid, e in info.items():
        for name in e["names"]:
            toks = name.split()
            if toks:
                blocks[(e["type_id"], "last", toks[-1])].append(eid)
                blocks[(e["type_id"], "pre", name[:4])].append(eid)
        for ident in e["idents"]:
            blocks[("id",) + ident].append(eid)
    skip = _not_same_pairs(vault)
    seen, out = set(), []
    for members in blocks.values():
        if len(members) > 200:
            continue
        for x, y in combinations(sorted(set(members)), 2):
            pair = frozenset((x, y))
            if pair in seen or pair in skip:
                continue
            seen.add(pair)
            a, b = info[x], info[y]
            if a["type_id"] != b["type_id"] and not (a["idents"] & b["idents"]):
                continue
            score, reasons = score_pair(a, b)
            if score >= min_score:
                # keep the better-connected one first
                if (b["engagement"] == "contact") and a["engagement"] != "contact":
                    a, b = b, a
                out.append({"a": {"id": a["id"], "display_name": a["display_name"],
                                  "context": _context(vault, a["id"], a["type_id"])},
                            "b": {"id": b["id"], "display_name": b["display_name"],
                                  "context": _context(vault, b["id"], b["type_id"])},
                            "score": score, "reasons": reasons})
    out.sort(key=lambda d: -d["score"])
    return out[:limit]


def not_same(vault: Vault, a: str, b: str) -> str:
    did = new_id()
    with vault.tx() as c:
        c.execute("insert into resolution_decision(id, kind, payload) values(?,?,?)",
                  (did, "not_same", json.dumps({"a": a, "b": b})))
        vault.audit(c, "write", a, {"op": "not_same", "other": b})
    return did


def merge(vault: Vault, keep: str, drop: str, rationale: str | None = None) -> str:
    """Merge `drop` into `keep`. Returns the decision id (use it to undo)."""
    if keep == drop:
        raise ValueError("Cannot merge an entry with itself.")
    did = new_id()
    moves: dict[str, list] = defaultdict(list)
    with vault.tx() as c:
        k = c.execute("select * from entity where id=? and status='active'", (keep,)).fetchone()
        d = c.execute("select * from entity where id=? and status='active'", (drop,)).fetchone()
        if not k or not d:
            raise ValueError("Both entries must exist and be active.")
        if d["engagement"] == "ego":
            keep, drop, k, d = drop, keep, d, k
        for r in c.execute("select id, subject_id, object_id from claim where "
                           "(subject_id=? and object_id=?) or (subject_id=? and object_id=?)",
                           (keep, drop, drop, keep)).fetchall():
            c.execute("update claim set status='rejected' where id=?", (r["id"],))
            moves["self_claims"].append(r["id"])
        for r in c.execute("select id from claim where subject_id=?", (drop,)).fetchall():
            c.execute("update claim set subject_id=? where id=?", (keep, r["id"]))
            moves["claim_subject"].append(r["id"])
        for r in c.execute("select id from claim where object_id=?", (drop,)).fetchall():
            c.execute("update claim set object_id=? where id=?", (keep, r["id"]))
            moves["claim_object"].append(r["id"])
        for r in c.execute("select event_id, role from participation where entity_id=?",
                           (drop,)).fetchall():
            c.execute("delete from participation where event_id=? and entity_id=? and role=?",
                      (r["event_id"], drop, r["role"]))
            exists = c.execute("select 1 from participation where event_id=? and entity_id=?",
                               (r["event_id"], keep)).fetchone()
            if not exists:
                c.execute("insert into participation(event_id, entity_id, role) values(?,?,?)",
                          (r["event_id"], keep, r["role"]))
            moves["participation"].append([r["event_id"], r["role"], not exists])
        for r in c.execute("select scheme, value from identifier where entity_id=?", (drop,)).fetchall():
            c.execute("delete from identifier where entity_id=? and scheme=? and value=?",
                      (drop, r["scheme"], r["value"]))
            c.execute("insert or ignore into identifier(entity_id, scheme, value) values(?,?,?)",
                      (keep, r["scheme"], r["value"]))
            moves["identifier"].append([r["scheme"], r["value"]])
        have = {r["name_norm"] for r in c.execute("select name_norm from entity_name where entity_id=?",
                                                  (keep,))}
        for r in c.execute("select name, name_norm from entity_name where entity_id=?",
                           (drop,)).fetchall():
            if r["name_norm"] not in have:
                c.execute("insert into entity_name(entity_id, name, name_norm, kind) values(?,?,?,'alias')",
                          (keep, r["name"], r["name_norm"]))
                moves["alias"].append(r["name_norm"])
                have.add(r["name_norm"])
        for r in c.execute("select tag_id from tagging where target_kind='entity' and target_id=?",
                           (drop,)).fetchall():
            added = c.execute("insert or ignore into tagging(tag_id, target_kind, target_id) "
                              "values(?, 'entity', ?)", (r["tag_id"], keep)).rowcount
            if added:
                moves["tag"].append(r["tag_id"])
        for col in ("from_id", "to_id"):
            for r in c.execute(f"select id from commitment where {col}=?", (drop,)).fetchall():
                c.execute(f"update commitment set {col}=? where id=?", (keep, r["id"]))
                moves[f"commitment_{col}"].append(r["id"])
        for r in c.execute("select source_id, role from entity_source where entity_id=?",
                           (drop,)).fetchall():
            c.execute("delete from entity_source where entity_id=? and source_id=? and role=?",
                      (drop, r["source_id"], r["role"]))
            added = c.execute("insert or ignore into entity_source(entity_id, source_id, role) "
                              "values(?,?,?)", (keep, r["source_id"], r["role"])).rowcount
            moves["entity_source"].append([r["source_id"], r["role"], bool(added)])
        if not c.execute("select 1 from relationship_profile where entity_id=?", (keep,)).fetchone() \
                and c.execute("select 1 from relationship_profile where entity_id=?", (drop,)).fetchone():
            c.execute("update relationship_profile set entity_id=? where entity_id=?", (keep, drop))
            moves["profile"].append(True)
            if k["engagement"] != "ego":
                c.execute("update entity set engagement='contact' where id=?", (keep,))
                moves["engagement_was"].append(k["engagement"])
        for r in c.execute("select gid from md_note where entity_id=?", (drop,)).fetchall():
            c.execute("update md_note set entity_id=? where gid=?", (keep, r["gid"]))
            moves["md_note"].append(r["gid"])
        c.execute("update entity set status='merged', merged_into=? where id=?", (keep, drop))
        c.execute("insert into resolution_decision(id, kind, payload, rationale) values(?,?,?,?)",
                  (did, "merge", json.dumps({"keep": keep, "drop": drop, "moves": moves}), rationale))
        vault.audit(c, "write", keep, {"op": "merge", "drop": drop, "decision": did})
        textindex.reindex_entity(c, keep)
        textindex.reindex_entity(c, drop)
    return did


def unmerge(vault: Vault, decision_id: str) -> str:
    """Undo a merge exactly, using the moves recorded at merge time."""
    with vault.tx() as c:
        row = c.execute("select * from resolution_decision where id=? and kind='merge' and "
                        "reverted_by is null", (decision_id,)).fetchone()
        if not row:
            raise ValueError("No such merge, or it was already undone.")
        p = json.loads(row["payload"])
        keep, drop, m = p["keep"], p["drop"], p["moves"]
        for cid in m.get("claim_subject", []):
            c.execute("update claim set subject_id=? where id=?", (drop, cid))
        for cid in m.get("claim_object", []):
            c.execute("update claim set object_id=? where id=?", (drop, cid))
        for cid in m.get("self_claims", []):
            c.execute("update claim set status='accepted' where id=? and status='rejected'", (cid,))
        for event_id, role, inserted in m.get("participation", []):
            if inserted:
                c.execute("delete from participation where event_id=? and entity_id=? and role=?",
                          (event_id, keep, role))
            c.execute("insert or ignore into participation(event_id, entity_id, role) values(?,?,?)",
                      (event_id, drop, role))
        for scheme, value in m.get("identifier", []):
            c.execute("delete from identifier where entity_id=? and scheme=? and value=?",
                      (keep, scheme, value))
            c.execute("insert or ignore into identifier(entity_id, scheme, value) values(?,?,?)",
                      (drop, scheme, value))
        for norm in m.get("alias", []):
            c.execute("delete from entity_name where entity_id=? and name_norm=? and kind='alias'",
                      (keep, norm))
        for tag_id in m.get("tag", []):
            c.execute("delete from tagging where tag_id=? and target_kind='entity' and target_id=?",
                      (tag_id, keep))
        for col in ("from_id", "to_id"):
            for cid in m.get(f"commitment_{col}", []):
                c.execute(f"update commitment set {col}=? where id=?", (drop, cid))
        for source_id, role, added in m.get("entity_source", []):
            if added:
                c.execute("delete from entity_source where entity_id=? and source_id=? and role=?",
                          (keep, source_id, role))
            c.execute("insert or ignore into entity_source(entity_id, source_id, role) values(?,?,?)",
                      (drop, source_id, role))
        if m.get("profile"):
            c.execute("update relationship_profile set entity_id=? where entity_id=?", (drop, keep))
            for was in m.get("engagement_was", []):
                c.execute("update entity set engagement=? where id=?", (was, keep))
        for gid in m.get("md_note", []):
            c.execute("update md_note set entity_id=? where gid=?", (drop, gid))
        c.execute("update entity set status='active', merged_into=null where id=?", (drop,))
        undo = new_id()
        c.execute("insert into resolution_decision(id, kind, payload) values(?,?,?)",
                  (undo, "unmerge", json.dumps({"merge": decision_id})))
        c.execute("update resolution_decision set reverted_by=? where id=?", (undo, decision_id))
        vault.audit(c, "write", keep, {"op": "unmerge", "decision": decision_id})
        textindex.reindex_entity(c, keep)
        textindex.reindex_entity(c, drop)
    return undo


def merges(vault: Vault, limit: int = 30) -> list[dict]:
    rows = vault.all("select * from resolution_decision where kind='merge' order by decided_at desc "
                     "limit ?", (limit,))
    for r in rows:
        p = json.loads(r["payload"])
        r["keep"] = vault.one("select id, display_name from entity where id=?", (p["keep"],))
        r["drop"] = vault.one("select id, display_name from entity where id=?", (p["drop"],))
    return rows
