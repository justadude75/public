"""Full-text and embedding index, and hybrid search over people, facts and notes."""

from __future__ import annotations

import array
import json
import math
import re
from typing import Any

from .db import Vault

CHUNK_CHARS = 900


def chunk_text(text: str, size: int = CHUNK_CHARS) -> list[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        if len(p) > size:
            if cur:
                chunks.append(cur)
                cur = ""
            sentences = re.split(r"(?<=[.!?])\s+", p)
            buf = ""
            for s in sentences:
                if len(buf) + len(s) > size and buf:
                    chunks.append(buf.strip())
                    buf = ""
                buf += s + " "
            if buf.strip():
                chunks.append(buf.strip())
            continue
        if len(cur) + len(p) + 2 > size and cur:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks


def index_source(c, source_id: str, text: str | None) -> None:
    from .repo import new_id

    old = [r["id"] for r in c.execute("select id from chunk where source_id=?", (source_id,))]
    for cid in old:
        c.execute("delete from chunk_fts where chunk_id=?", (cid,))
        c.execute("delete from embedding where target_kind='chunk' and target_id=?", (cid,))
    c.execute("delete from chunk where source_id=?", (source_id,))
    for i, part in enumerate(chunk_text(text or "")):
        cid = new_id()
        c.execute("insert into chunk(id, source_id, ordinal, text) values(?,?,?,?)",
                  (cid, source_id, i, part))
        c.execute("insert into chunk_fts(text, chunk_id) values(?,?)", (part, cid))


def reindex_entity(c, entity_id: str | None) -> None:
    if not entity_id:
        return
    c.execute("delete from entity_fts where entity_id=?", (entity_id,))
    ent = c.execute("select status, type_id from entity where id=?", (entity_id,)).fetchone()
    if not ent or ent["status"] != "active":
        return
    names = " ".join(r["name"] for r in c.execute(
        "select name from entity_name where entity_id=?", (entity_id,)))
    parts = []
    for r in c.execute(
            """select p.label, cl.value, o.display_name as obj
               from claim cl join predicate p on p.id=cl.predicate_id
               left join entity o on o.id=cl.object_id
               where cl.subject_id=? and cl.status in ('accepted','proposed')""", (entity_id,)):
        val = json.loads(r["value"]) if r["value"] else r["obj"]
        parts.append(f"{r['label']} {val}")
    for r in c.execute(
            """select p.label, s.display_name from claim cl join predicate p on p.id=cl.predicate_id
               join entity s on s.id=cl.subject_id where cl.object_id=? and cl.status='accepted'""",
            (entity_id,)):
        parts.append(f"{r['label']} {r['display_name']}")
    for r in c.execute(
            "select t.namespace, t.name from tagging g join tag t on t.id=g.tag_id "
            "where g.target_kind='entity' and g.target_id=?", (entity_id,)):
        parts.append(f"{r['namespace']} {r['name']}")
    c.execute("insert into entity_fts(entity_id, names, facts) values(?,?,?)",
              (entity_id, names, " · ".join(parts)))


def rebuild(vault: Vault) -> dict:
    with vault.tx() as c:
        c.execute("delete from entity_fts")
        ids = [r["id"] for r in c.execute("select id from entity where status='active'")]
        for eid in ids:
            reindex_entity(c, eid)
        sources = c.execute(
            "select id, content from source where kind in ('note','markdown','web','capture') "
            "and content is not null").fetchall()
        for s in sources:
            index_source(c, s["id"], s["content"])
    return {"entities": len(ids), "sources": len(sources)}


# ── embeddings ──────────────────────────────────────────────────────────────


def _pack(vec: list[float]) -> bytes:
    return array.array("f", vec).tobytes()


def _unpack(blob: bytes) -> list[float]:
    arr = array.array("f")
    arr.frombytes(blob)
    return arr.tolist()


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def embed_pending(vault: Vault, ai, limit: int = 64) -> int:
    """Embed chunks and entity profiles that have no vector for the current model."""
    if ai is None or not ai.embed_model:
        return 0
    model = ai.embed_model
    todo = vault.all(
        """select 'chunk' as kind, ch.id, ch.text from chunk ch
           where not exists(select 1 from embedding e where e.target_kind='chunk'
                            and e.target_id=ch.id and e.model=?)
           limit ?""", (model, limit))
    todo += vault.all(
        """select 'entity' as kind, f.entity_id as id, f.names || ' · ' || f.facts as text
           from entity_fts f where not exists(select 1 from embedding e where
           e.target_kind='entity' and e.target_id=f.entity_id and e.model=?) limit ?""",
        (model, limit))
    if not todo:
        return 0
    vectors = ai.embed([t["text"][:4000] for t in todo])
    with vault.tx() as c:
        for item, vec in zip(todo, vectors):
            c.execute(
                "insert or replace into embedding(target_kind, target_id, model, dims, vector) "
                "values(?,?,?,?,?)", (item["kind"], item["id"], model, len(vec), _pack(vec)))
    return len(todo)


# ── search ──────────────────────────────────────────────────────────────────


def _fts_query(q: str) -> str:
    tokens = re.findall(r"\w+", q.lower())
    return " ".join(f'"{t}"*' for t in tokens)


def _source_owner(vault: Vault, source_id: str) -> dict | None:
    """The event or entity a source belongs to, for linking a search hit."""
    ev = vault.one("select e.id, e.display_name, 'event' as type_id from event_detail ed "
                   "join entity e on e.id=ed.entity_id where ed.notes_source_id=?", (source_id,))
    if ev:
        return ev
    return vault.one("select e.id, e.display_name, e.type_id from entity_source es "
                     "join entity e on e.id=es.entity_id where es.source_id=? limit 1",
                     (source_id,))


def search(vault: Vault, q: str, ai=None, limit: int = 20) -> dict[str, Any]:
    """Hybrid search: keyword (BM25) plus meaning (embeddings) when a model is configured.

    Results from the two methods are merged with reciprocal rank fusion.
    """
    fq = _fts_query(q)
    if not fq:
        return {"entities": [], "notes": [], "semantic": False}
    ent_kw = vault.all(
        "select entity_id, bm25(entity_fts, 0, 5.0, 1.0) as score, "
        "snippet(entity_fts, 2, '[', ']', '…', 12) as snip from entity_fts "
        "where entity_fts match ? order by score limit 50", (fq,))
    note_kw = vault.all(
        "select chunk_id, bm25(chunk_fts) as score, snippet(chunk_fts, 0, '[', ']', '…', 20) "
        "as snip from chunk_fts where chunk_fts match ? order by score limit 50", (fq,))

    ent_rank: dict[str, float] = {}
    note_rank: dict[str, float] = {}
    snippets: dict[str, str] = {}
    for i, r in enumerate(ent_kw):
        ent_rank[r["entity_id"]] = ent_rank.get(r["entity_id"], 0) + 1 / (60 + i)
        snippets[r["entity_id"]] = r["snip"]
    for i, r in enumerate(note_kw):
        note_rank[r["chunk_id"]] = note_rank.get(r["chunk_id"], 0) + 1 / (60 + i)
        snippets[r["chunk_id"]] = r["snip"]

    semantic = False
    if ai is not None and ai.embed_model:
        try:
            qv = ai.embed([q])[0]
            semantic = True
        except Exception:
            qv = None
        if qv:
            rows = vault.all("select target_kind, target_id, vector from embedding where model=?",
                             (ai.embed_model,))
            scored = sorted(((_cos(qv, _unpack(r["vector"])), r["target_kind"], r["target_id"])
                             for r in rows), reverse=True)[:50]
            for i, (sim, kind, tid) in enumerate(scored):
                if sim < 0.2:
                    break
                target = ent_rank if kind == "entity" else note_rank
                target[tid] = target.get(tid, 0) + 1 / (60 + i)

    entities = []
    for eid, score in sorted(ent_rank.items(), key=lambda x: -x[1])[:limit]:
        e = vault.one("select id, display_name, type_id, engagement from entity where id=? "
                      "and status='active'", (eid,))
        if e:
            entities.append({**e, "score": score, "snippet": snippets.get(eid, "")})
    notes = []
    for cid, score in sorted(note_rank.items(), key=lambda x: -x[1])[:limit]:
        ch = vault.one("select ch.id, ch.text, ch.source_id, s.captured_at, s.title from chunk ch "
                       "join source s on s.id=ch.source_id where ch.id=?", (cid,))
        if ch:
            notes.append({**ch, "score": score, "snippet": snippets.get(cid) or ch["text"][:200],
                          "owner": _source_owner(vault, ch["source_id"])})
    return {"entities": entities, "notes": notes, "semantic": semantic}
