"""Quick capture: type a note, get structured records.

Structured lines use the same syntax as the Markdown templates and are read by
fixed rules. If a local model is configured, it can also pull facts and
follow-ups out of free text; those facts arrive as *proposed* claims for review.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .importer import ClaimSpec, CommitmentSpec, ImportRecord, Ref
from .markdown import parse_text
from .repo import new_id

EXTRACT_SYSTEM = """You extract structured facts from a person's private notes about their own
relationships. Return only JSON with this shape:
{"facts": [{"subject": "<person or organization name>", "predicate": "<one of the allowed ids>",
            "value": "<text, or null when object is set>", "object": "<name of a person,
            organization or place, or null>"}],
 "follow_ups": [{"owner": "me" or "them", "person": "<name>", "text": "<what>",
                 "due": "YYYY-MM-DD or null"}],
 "people": [{"name": "<name>", "type": "person" | "organization" | "place"}]}
Only include what the note states. Do not guess. Use the exact names written in the note."""


def extract_with_model(ai, text: str, predicates: list[dict], today: date) -> dict:
    allowed = ", ".join(f"{p['id']} ({p['label']})" for p in predicates)
    user = f"Today is {today.isoformat()}.\nAllowed predicate ids: {allowed}\n\nNote:\n{text}"
    data = ai.chat_json(EXTRACT_SYSTEM, user)
    return data if isinstance(data, dict) else {}


def records_from_model(data: dict, predicate_ids: set[str], origin: str) -> ImportRecord:
    rec = ImportRecord(origin=f"{origin}:model", kind="note", title="Model extraction")
    for f in data.get("facts") or []:
        subj = (f.get("subject") or "").strip()
        pid = (f.get("predicate") or "").strip()
        if not subj or not (f.get("value") or f.get("object")):
            continue
        quals = {"source": "model"}
        if pid not in predicate_ids:
            quals["label"] = pid or "fact"
            pid = "related_to" if f.get("object") else "fact"
        rec.claims.append(ClaimSpec(
            pid, value=f.get("value") if not f.get("object") else None,
            obj=Ref(name=f["object"]) if f.get("object") else None,
            subject=Ref(name=subj, engagement="acquaintance"), basis="told",
            confidence=0.8, qualifiers=quals, status="proposed"))
    for fu in data.get("follow_ups") or []:
        if not (fu.get("text") or "").strip():
            continue
        rec.commitments.append(CommitmentSpec(
            fu["text"].strip(), due=fu.get("due") or None,
            owner="them" if fu.get("owner") == "them" else "me",
            counterpart=Ref(name=fu["person"], type="person", engagement="acquaintance")
            if fu.get("person") else None))
    for p in data.get("people") or []:
        if p.get("name"):
            rec.mentions.append(Ref(name=p["name"], type=p.get("type") or "person",
                                    engagement="acquaintance"))
    return rec


def build_records(text: str, *, with_ids: list[str], day: str, kind: str = "meeting",
                  channel: str | None = None, model_data: dict | None = None,
                  predicate_ids: set[str] | None = None, origin: str | None = None
                  ) -> list[ImportRecord]:
    origin = origin or f"capture:{new_id()}"
    fm: dict = {}
    if with_ids:
        fm = {"type": "meeting", "date": day, "kind": kind, "status": "occurred"}
        if channel:
            fm["channel"] = channel
    parsed = parse_text(text, "capture", Path("capture"), fm_override=fm or None,
                        origin_override=origin)
    records = parsed.records
    if with_ids:
        main = records[0]
        refs = []
        for w in with_ids:
            if w.startswith("new:"):
                _, type_, name = (w.split(":", 2) + ["", ""])[:3]
                refs.append(Ref(name=name, type=type_ or "person", engagement="acquaintance"))
            else:
                refs.append(Ref(entity_id=w))
        main.event.participants = refs + main.event.participants
        main.event.title = None
        main.title = None
    if model_data:
        records.append(records_from_model(model_data, predicate_ids or set(), origin))
    return records


def serialize(data: dict | None) -> str:
    return json.dumps(data or {})
