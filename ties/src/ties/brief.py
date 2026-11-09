"""Pre-meeting briefs: everything worth knowing before you meet someone, on one screen."""

from __future__ import annotations

from datetime import date

from . import memory, repo, scoring
from .db import Vault


def person_brief(vault: Vault, entity_id: str, today: date, my_contacts: set[str]) -> dict:
    ent = repo.get_entity(vault, entity_id)
    rels = repo.relations(vault, entity_id)
    facts = repo.facts(vault, entity_id)
    topics = [f["value_text"] for f in facts if f["predicate_id"] in ("interest", "seeking",
                                                                      "offering", "family_detail")]
    events = [e for e in repo.list_events(vault, entity_id=entity_id, status="occurred", limit=3)]
    shared = [r for r in rels if r["other_id"] in my_contacts and r["other_type"] == "person"]
    return {
        "entity": ent,
        "summary": memory.get_summary(vault, entity_id, today)["text"],
        "status": scoring.contact_status(vault, entity_id, today),
        "changes": memory.changed_since_last_contact(vault, entity_id, today)[:8],
        "loops": repo.commitments(vault, entity_id),
        "recent": events,
        "shared": shared,
        "topics": topics,
        "work": [r for r in rels if r["predicate_id"] == "employed_by" and r["outgoing"]
                 and not r["valid_to"]],
        "suggestions": [i for i in repo.inferences(vault, entity_id) if i["status"] == "proposed"],
    }


def event_brief(vault: Vault, event_id: str, today: date | None = None) -> dict:
    today = today or date.today()
    ev = repo.get_event(vault, event_id)
    if not ev:
        raise ValueError("Unknown event")
    ego = vault.get_meta("ego_id")
    contacts = {r["id"] for r in vault.all(
        "select id from entity where engagement='contact' and status='active'")}
    people = [p for p in ev["participants"] if p["id"] != ego and p["type_id"] == "person"]
    briefs = [person_brief(vault, p["id"], today, contacts) for p in people]
    ids = {p["id"] for p in people}
    between = []
    for b in briefs:
        for r in repo.relations(vault, b["entity"]["id"]):
            if r["other_id"] in ids and r["outgoing"]:
                between.append(f"{b['entity']['display_name']} · {r['label'].lower()} · {r['other_name']}")
    return {"event": ev, "people": briefs, "between": between}
