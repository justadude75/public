"""Trigger rules that add items to Today, plus snooze and dismiss.

Rules (settings stored as data in meta "trigger_settings"):
- brief:       planned events in the next N days get a "prepare" item linking to a brief
- debrief:     occurred events in the last N days with no notes get an "add notes" item
- fact_change: a contact's job, city or title changed recently, so suggest a check-in
- due / dates / follow-ups come from scoring.py and can be snoozed here
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from .db import Vault
from .repo import list_events, parse_day

DEFAULTS = {"brief_days": 3, "debrief_days": 3, "fact_change_days": 14}
FACT_CHANGE_PREDICATES = ("employed_by", "lives_in", "title")


def settings(vault: Vault) -> dict:
    raw = vault.get_meta("trigger_settings")
    out = dict(DEFAULTS)
    if raw:
        out.update({k: int(v) for k, v in json.loads(raw).items() if k in DEFAULTS})
    return out


def save_settings(vault: Vault, values: dict) -> None:
    vault.set_meta("trigger_settings", json.dumps({k: int(values.get(k, v)) for k, v in DEFAULTS.items()}))


def set_state(vault: Vault, kind: str, key: str, status: str, until: str | None = None,
              reason: str | None = None) -> None:
    with vault.tx() as c:
        c.execute("insert into outreach_state(kind, key, status, until, reason) values(?,?,?,?,?) "
                  "on conflict(kind, key) do update set status=excluded.status, "
                  "until=excluded.until, reason=excluded.reason, updated_at=datetime('now')",
                  (kind, key, status, until, reason))
        vault.audit(c, "write", key, {"op": "outreach_state", "kind": kind, "status": status})


def clear_state(vault: Vault, kind: str, key: str) -> None:
    with vault.tx() as c:
        c.execute("delete from outreach_state where kind=? and key=?", (kind, key))


def hidden_keys(vault: Vault, kind: str, today: date) -> set[str]:
    out = set()
    for r in vault.all("select key, status, until from outreach_state where kind=?", (kind,)):
        if r["status"] in ("dismissed", "done"):
            out.add(r["key"])
        elif r["status"] == "snoozed" and r["until"] and r["until"] > today.isoformat():
            out.add(r["key"])
    return out


def snoozed_due(vault: Vault, today: date) -> dict[str, str]:
    return {r["key"]: r["until"] for r in vault.all(
        "select key, until from outreach_state where kind='due' and status='snoozed' and until>?",
        (today.isoformat(),))}


def brief_items(vault: Vault, today: date) -> list[dict]:
    days = settings(vault)["brief_days"]
    events = list_events(vault, status="planned", start=today.isoformat(),
                         end=(today + timedelta(days=days)).isoformat(), descending=False)
    hidden = hidden_keys(vault, "brief", today)
    return [e for e in events if e["people"] and e["id"] not in hidden]


def debrief_items(vault: Vault, today: date) -> list[dict]:
    days = settings(vault)["debrief_days"]
    events = list_events(vault, status="occurred",
                         start=(today - timedelta(days=days)).isoformat(), end=today.isoformat())
    hidden = hidden_keys(vault, "debrief", today)
    return [e for e in events if e["people"] and not (e.get("notes") or "").strip()
            and e["id"] not in hidden]


def fact_changes(vault: Vault, today: date) -> list[dict]:
    days = settings(vault)["fact_change_days"]
    since = (today - timedelta(days=days)).isoformat()
    marks = ",".join("?" * len(FACT_CHANGE_PREDICATES))
    rows = vault.all(
        f"""select cl.id, cl.subject_id, cl.predicate_id, cl.value, cl.recorded_at, cl.valid_from,
                   p.label, s.display_name as name, o.display_name as object_name
            from claim cl join predicate p on p.id=cl.predicate_id
            join entity s on s.id=cl.subject_id
            left join entity o on o.id=cl.object_id
            where cl.status='accepted' and cl.basis<>'inferred' and cl.valid_to is null
              and cl.predicate_id in ({marks}) and cl.recorded_at >= ?
              and s.engagement='contact' and s.status='active'
              and exists(select 1 from claim old where old.subject_id=cl.subject_id
                         and old.predicate_id=cl.predicate_id and old.id<>cl.id
                         and old.id < cl.id
                         and old.status in ('accepted','superseded'))
            order by cl.recorded_at desc""", (*FACT_CHANGE_PREDICATES, since))
    hidden = hidden_keys(vault, "fact_change", today)
    out = []
    for r in rows:
        if r["id"] in hidden:
            continue
        value = r["object_name"] or (json.loads(r["value"]) if r["value"] else "")
        verb = {"employed_by": "now works at", "lives_in": "moved to", "title": "is now"}[r["predicate_id"]]
        out.append({**r, "text": f"{r['name']} {verb} {value}", "day": parse_day(r["recorded_at"])})
    return out
