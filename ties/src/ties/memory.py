"""Memory: a short summary per entity, and what changed since you last spoke.

The default summary is built by rules from current facts and recent notes, so
every sentence traces to a recorded fact. A local model can write a smoother
version; it is given only the facts and notes, and told to cite them.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date

from . import repo, scoring
from .db import Vault


def _facts_snapshot(vault: Vault, entity_id: str) -> dict:
    facts = repo.facts(vault, entity_id)
    rels = repo.relations(vault, entity_id)
    events = repo.list_events(vault, entity_id=entity_id, limit=5)
    loops = repo.commitments(vault, entity_id)
    return {"facts": facts, "relations": rels, "events": events, "loops": loops}


def _hash(snapshot: dict) -> str:
    key = [(f["id"]) for f in snapshot["facts"]] + [r["id"] for r in snapshot["relations"]] + \
          [e["id"] for e in snapshot["events"]] + [l["id"] for l in snapshot["loops"]]
    return hashlib.sha1(json.dumps(key).encode()).hexdigest()


def rules_summary(vault: Vault, entity_id: str, today: date | None = None) -> str:
    today = today or date.today()
    snap = _facts_snapshot(vault, entity_id)
    ent = repo.get_entity(vault, entity_id)
    by_pred: dict[str, list[str]] = {}
    for f in snap["facts"]:
        by_pred.setdefault(f["predicate_id"], []).append(f["value_text"])
    for r in snap["relations"]:
        if r["outgoing"] and not r["valid_to"]:
            by_pred.setdefault(r["predicate_id"], []).append(r["other_name"])
    parts = []
    role = by_pred.get("title", [None])[0]
    work = by_pred.get("employed_by", [])
    if role and work:
        parts.append(f"{role} at {', '.join(work)}.")
    elif work:
        parts.append(f"Works at {', '.join(work)}.")
    elif role:
        parts.append(f"{role}.")
    if by_pred.get("lives_in"):
        parts.append(f"Lives in {by_pred['lives_in'][0]}.")
    if by_pred.get("located_in") and ent["type_id"] != "person":
        parts.append(f"Based in {', '.join(by_pred['located_in'])}.")
    if by_pred.get("description"):
        parts.append(by_pred["description"][0].rstrip(".") + ".")
    if by_pred.get("interest"):
        parts.append(f"Interested in {', '.join(by_pred['interest'])}.")
    if by_pred.get("seeking"):
        parts.append(f"Looking for {', '.join(by_pred['seeking'])}.")
    if by_pred.get("family_detail"):
        parts.append("; ".join(by_pred["family_detail"]).rstrip(".") + ".")
    st = scoring.contact_status(vault, entity_id, today)
    if st and st.last_contact:
        parts.append(f"Last in touch {st.days_since} days ago ({st.last_event_title}).")
    last_note = next((e["notes"] for e in snap["events"] if e.get("notes")), None)
    if last_note:
        first = last_note.strip().split("\n")[0]
        parts.append(f"Last notes: “{first[:180]}{'…' if len(first) > 180 else ''}”")
    mine = [l for l in snap["loops"] if l["mine"]]
    theirs = [l for l in snap["loops"] if not l["mine"]]
    if mine:
        parts.append(f"You owe them: {'; '.join(l['description'] for l in mine[:3])}.")
    if theirs:
        parts.append(f"They owe you: {'; '.join(l['description'] for l in theirs[:3])}.")
    return " ".join(parts)


def model_summary(vault: Vault, entity_id: str, ai, today: date | None = None) -> str:
    snap = _facts_snapshot(vault, entity_id)
    ent = repo.get_entity(vault, entity_id)
    lines = []
    for i, f in enumerate(snap["facts"], 1):
        lines.append(f"[F{i}] {f['predicate_label']}: {f['value_text']}")
    for i, r in enumerate(snap["relations"], 1):
        lines.append(f"[R{i}] {r['label']}: {r['other_name']}")
    for i, e in enumerate(snap["events"], 1):
        lines.append(f"[E{i}] {e['start_at'][:10]} {e['title']}: {(e.get('notes') or '')[:400]}")
    system = ("Write a 3-5 sentence private memory note about one person or organization for "
              "the owner of this notebook. Use only the facts given. Cite facts with their tags "
              "like [F1]. Plain text, no headings.")
    text = ai.chat(system, f"About: {ent['display_name']}\n" + "\n".join(lines))
    return text.strip()


def get_summary(vault: Vault, entity_id: str, today: date | None = None) -> dict:
    snap_hash = _hash(_facts_snapshot(vault, entity_id))
    row = vault.one("select * from summary where entity_id=?", (entity_id,))
    if row and row["generator"].startswith("model:"):
        return {"text": row["text"], "generator": row["generator"],
                "stale": row["basis_hash"] != snap_hash, "at": row["generated_at"]}
    return {"text": rules_summary(vault, entity_id, today), "generator": "rules", "stale": False,
            "at": None}


def regenerate(vault: Vault, entity_id: str, ai, today: date | None = None) -> dict:
    text = model_summary(vault, entity_id, ai, today)
    snap_hash = _hash(_facts_snapshot(vault, entity_id))
    with vault.tx() as c:
        c.execute("insert into summary(entity_id, text, generator, basis_hash) values(?,?,?,?) "
                  "on conflict(entity_id) do update set text=excluded.text, "
                  "generator=excluded.generator, basis_hash=excluded.basis_hash, "
                  "generated_at=datetime('now')", (entity_id, text, ai.name, snap_hash))
    return get_summary(vault, entity_id, today)


def clear_model_summary(vault: Vault, entity_id: str) -> None:
    with vault.tx() as c:
        c.execute("delete from summary where entity_id=?", (entity_id,))


def changed_since_last_contact(vault: Vault, entity_id: str, today: date | None = None) -> list[dict]:
    st = scoring.contact_status(vault, entity_id, today)
    if not st or not st.last_contact:
        return []
    cutoff = st.last_contact.isoformat() + " 23:59:59"
    rows = repo.claim_history(vault, entity_id)
    out = []
    for r in rows:
        if r["recorded_at"] > cutoff and r["status"] in ("accepted", "superseded", "proposed"):
            repo._orient(vault, r, entity_id)
            out.append(r)
    return out
