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


# Index rows use the same rowid as their chunk / entity row. FTS5 can then delete a row
# by rowid directly; deleting by an UNINDEXED column would scan the whole index.
FTS_LAYOUT = "2"
SEARCHABLE_SOURCES = ("note", "markdown", "web", "capture", "import", "research")


def index_source(c, source_id: str, text: str | None) -> None:
    from .repo import new_id

    for r in c.execute("select rowid, id from chunk where source_id=?", (source_id,)).fetchall():
        c.execute("delete from chunk_fts where rowid=?", (r["rowid"],))
        c.execute("delete from embedding where target_kind='chunk' and target_id=?", (r["id"],))
    c.execute("delete from chunk where source_id=?", (source_id,))
    for i, part in enumerate(chunk_text(text or "")):
        cid = new_id()
        rowid = c.execute("insert into chunk(id, source_id, ordinal, text) values(?,?,?,?)",
                          (cid, source_id, i, part)).lastrowid
        c.execute("insert into chunk_fts(rowid, text, chunk_id) values(?,?,?)", (rowid, part, cid))


_FACTS_SQL = """
  coalesce((select group_concat(p.label || ' ' || coalesce(json_extract(cl.value, '$'), o.display_name, ''), ' · ')
            from claim cl join predicate p on p.id=cl.predicate_id
            left join entity o on o.id=cl.object_id
            where cl.subject_id=e.id and cl.status in ('accepted','proposed')), '')
  || ' · ' ||
  coalesce((select group_concat(p.label || ' ' || s.display_name, ' · ')
            from claim cl join predicate p on p.id=cl.predicate_id
            join entity s on s.id=cl.subject_id
            where cl.object_id=e.id and cl.status='accepted'), '')
  || ' · ' ||
  coalesce((select group_concat(t.namespace || ' ' || t.name, ' ')
            from tagging g join tag t on t.id=g.tag_id
            where g.target_kind='entity' and g.target_id=e.id), '')
"""
_NAMES_SQL = "(select group_concat(name, ' ') from entity_name where entity_id=e.id)"


def reindex_entity(c, entity_id: str | None) -> None:
    if not entity_id:
        return
    ent = c.execute("select rowid, status from entity where id=?", (entity_id,)).fetchone()
    if not ent:
        return
    c.execute("delete from entity_fts where rowid=?", (ent["rowid"],))
    if ent["status"] != "active":
        return
    c.execute(f"insert into entity_fts(rowid, entity_id, names, facts) "
              f"select e.rowid, e.id, {_NAMES_SQL}, {_FACTS_SQL} from entity e where e.id=?",
              (entity_id,))


def rebuild(vault: Vault, rechunk: bool = False) -> dict:
    """Rebuild the search index with a few set-based statements.

    Existing chunks (and their embeddings) are kept unless rechunk=True; sources that
    have no chunks yet are chunked.
    """
    marks = ",".join("?" * len(SEARCHABLE_SOURCES))
    with vault.tx() as c:
        c.execute("delete from entity_fts")
        c.execute(f"insert into entity_fts(rowid, entity_id, names, facts) "
                  f"select e.rowid, e.id, {_NAMES_SQL}, {_FACTS_SQL} from entity e "
                  f"where e.status='active'")
        n_entities = c.execute("select count(*) as n from entity_fts").fetchone()["n"]
        where = "" if rechunk else "and not exists(select 1 from chunk ch where ch.source_id=s.id)"
        todo = c.execute(f"select id, title, content from source s where kind in ({marks}) "
                         f"and content is not null and content<>'' {where}",
                         SEARCHABLE_SOURCES).fetchall()
        for s in todo:
            text = s["content"]
            index_source(c, s["id"], text)
        c.execute("delete from chunk_fts")
        c.execute("insert into chunk_fts(rowid, text, chunk_id) select rowid, text, id from chunk")
        n_sources = c.execute("select count(distinct source_id) as n from chunk").fetchone()["n"]
        c.execute("insert into meta(key, value) values('fts_layout', ?) on conflict(key) "
                  "do update set value=excluded.value", (FTS_LAYOUT,))
    return {"entities": n_entities, "sources": n_sources}


def ensure_layout(vault: Vault) -> None:
    """One-time upgrade of older vaults to the rowid-keyed index layout."""
    if vault.get_meta("fts_layout") != FTS_LAYOUT:
        rebuild(vault)


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


def _matrix(vault: Vault, model: str):
    """All vectors for a model, loaded once and cached until the vault changes."""
    def load():
        rows = vault.all("select target_kind, target_id, vector from embedding where model=?",
                         (model,))
        keys = [(r["target_kind"], r["target_id"]) for r in rows]
        try:
            import numpy as np
            if not rows:
                return keys, None
            m = np.vstack([np.frombuffer(r["vector"], dtype=np.float32) for r in rows])
            norms = np.linalg.norm(m, axis=1, keepdims=True)
            norms[norms == 0] = 1
            return keys, m / norms
        except ImportError:
            return keys, [_unpack(r["vector"]) for r in rows]
    return vault.cached(("vectors", model), load)


def nearest(vault: Vault, model: str, query_vec: list[float], k: int = 50):
    """Top-k (similarity, kind, id). Uses numpy when installed, pure Python otherwise."""
    keys, m = _matrix(vault, model)
    if not keys:
        return []
    try:
        import numpy as np
        if m is not None and not isinstance(m, list):
            q = np.asarray(query_vec, dtype=np.float32)
            q = q / (np.linalg.norm(q) or 1)
            sims = m @ q
            top = np.argsort(-sims)[:k]
            return [(float(sims[i]), keys[i][0], keys[i][1]) for i in top]
    except ImportError:
        pass
    scored = sorted(((_cos(query_vec, v), kind, tid) for (kind, tid), v in zip(keys, m)),
                    reverse=True)
    return scored[:k]


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


def _fts_query(q: str, any_term: bool = False) -> str:
    tokens = [t for t in re.findall(r"\w+", q.lower()) if not any_term or len(t) > 2]
    return (" OR " if any_term else " ").join(f'"{t}"*' for t in tokens)


def _source_owner(vault: Vault, source_id: str) -> dict | None:
    """The event or entity a source belongs to, for linking a search hit."""
    ev = vault.one("select e.id, e.display_name, 'event' as type_id from event_detail ed "
                   "join entity e on e.id=ed.entity_id where ed.notes_source_id=?", (source_id,))
    if ev:
        return ev
    return vault.one("select e.id, e.display_name, e.type_id from entity_source es "
                     "join entity e on e.id=es.entity_id where es.source_id=? limit 1",
                     (source_id,))


def search(vault: Vault, q: str, ai=None, limit: int = 20, any_term: bool = False) -> dict[str, Any]:
    """Hybrid search: keyword (BM25) plus meaning (embeddings) when a model is configured.

    Results from the two methods are merged with reciprocal rank fusion.
    """
    fq = _fts_query(q, any_term)
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
            scored = nearest(vault, ai.embed_model, qv, 50)
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
