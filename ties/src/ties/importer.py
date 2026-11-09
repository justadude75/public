"""The importer contract (plan section 4.10).

Every importer turns its input into ImportRecords. `apply` then:
  1. resolves each record to an entity (earlier import > identifier > exact name > new),
  2. writes what is new, skips what is unchanged, retires what disappeared from the
     same origin (claims superseded, follow-ups dropped, events archived),
  3. never overwrites a hand-entered single-valued fact: the import is stored as a
     disputed claim for the review inbox instead,
  4. records every derived item so the batch can be rolled back.
`apply(..., dry_run=True)` runs the same code inside a transaction that is rolled
back, which produces the preview.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from . import textindex
from .db import Vault
from .repo import (_add_claim, _add_commitment, _add_identifier, _add_link, _add_name,
                   _create_entity, _create_source, _set_tags, ego_id, find_by_name, new_id,
                   normalize_identifier, normalize_name, predicate, set_next_contact)


@dataclass
class Ref:
    """A pointer to an entity: by import key, identifiers or name."""
    name: str | None = None
    type: str | None = None
    key: str | None = None            # origin of another record in the same import
    identifiers: list[tuple[str, str]] = field(default_factory=list)
    entity_id: str | None = None
    engagement: str = "reference"
    create: bool = True               # False: only link if it already exists

    def label(self) -> str:
        return self.name or self.key or self.entity_id or "?"


@dataclass
class ClaimSpec:
    predicate: str
    value: Any = None
    obj: Ref | None = None
    subject: Ref | None = None        # None means the record's own entity
    valid_from: str | None = None
    valid_to: str | None = None
    basis: str = "observed"
    confidence: float = 1.0
    qualifiers: dict = field(default_factory=dict)
    status: str = "accepted"


@dataclass
class CommitmentSpec:
    text: str
    due: str | None = None
    owner: str = "me"                 # me | them
    counterpart: Ref | None = None
    done: bool = False


@dataclass
class LinkSpec:
    url: str
    note: str | None = None
    target: Ref | None = None


@dataclass
class EventSpec:
    start: str
    kind: str = "meeting"
    status: str = "occurred"
    channel: str | None = None
    title: str | None = None
    location: Ref | None = None
    participants: list[Ref] = field(default_factory=list)
    initiated_by: str | None = None   # me | them | None
    counts_as_contact: bool = True
    next_contact: str | None = None


@dataclass
class ImportRecord:
    origin: str
    kind: str = "entity"              # entity | event | note
    entity_type: str = "person"
    name: str = ""
    aliases: list[str] = field(default_factory=list)
    identifiers: list[tuple[str, str]] = field(default_factory=list)
    engagement: str | None = None
    tags: list[str] = field(default_factory=list)
    profile: dict | None = None
    claims: list[ClaimSpec] = field(default_factory=list)
    commitments: list[CommitmentSpec] = field(default_factory=list)
    links: list[LinkSpec] = field(default_factory=list)
    mentions: list[Ref] = field(default_factory=list)
    event: EventSpec | None = None
    notes: str | None = None
    title: str | None = None
    entity_id: str | None = None
    attrs: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass
class Report:
    importer: str
    dry_run: bool
    batch_id: str | None = None
    records: int = 0
    created: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)
    claims_added: list[str] = field(default_factory=list)
    claims_retired: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    events_added: list[str] = field(default_factory=list)
    events_retired: list[str] = field(default_factory=list)
    follow_ups_added: list[str] = field(default_factory=list)
    links_added: list[str] = field(default_factory=list)
    unchanged: int = 0
    warnings: list[str] = field(default_factory=list)
    origin_entities: dict[str, str] = field(default_factory=dict)

    @property
    def changes(self) -> int:
        return sum(len(x) for x in (self.created, self.claims_added, self.claims_retired,
                                    self.conflicts, self.events_added, self.events_retired,
                                    self.follow_ups_added, self.links_added))


class _DryRun(Exception):
    pass


def _key(*parts: Any) -> str:
    raw = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def _ref_key(ref: Ref | None) -> str:
    if ref is None:
        return "self"
    return ref.entity_id or ref.key or (normalize_name(ref.name) if ref.name
                                        else json.dumps(sorted(ref.identifiers)))


class _Applier:
    def __init__(self, c, vault: Vault, importer: str, report: Report, batch_id: str):
        self.c, self.vault, self.importer, self.report, self.batch = c, vault, importer, report, batch_id
        self.ego = ego_id(vault)
        self.by_origin: dict[str, str] = {}
        self.touched: set[str] = set()
        self.asserted_by = f"import:{importer}"

    # ── bookkeeping ────────────────────────────────────────────────────
    def existing(self, origin: str) -> dict[tuple[str, str], str]:
        return {(r["item_key"], r["kind"]): r["item_id"] for r in self.c.execute(
            "select item_key, kind, item_id from import_item where origin=? and active=1",
            (origin,))}

    def record(self, origin: str, key: str, kind: str, item_id: str) -> None:
        self.c.execute(
            "insert into import_item(origin, item_key, kind, item_id, batch_id, active) "
            "values(?,?,?,?,?,1) on conflict(origin, item_key, kind) do update set "
            "item_id=excluded.item_id, batch_id=excluded.batch_id, active=1",
            (origin, key, kind, item_id, self.batch))

    # ── entity resolution ──────────────────────────────────────────────
    def resolve(self, ref: Ref | None, default_type: str = "person") -> str | None:
        if ref is None:
            return None
        if ref.entity_id:
            return ref.entity_id
        if ref.key and ref.key in self.by_origin:
            return self.by_origin[ref.key]
        if ref.key:
            row = self.c.execute("select item_id from import_item where origin=? and kind='entity' "
                                 "and item_key='self'", (ref.key,)).fetchone()
            if row:
                return row["item_id"]
        for scheme, value in ref.identifiers:
            norm = normalize_identifier(scheme, value or "")
            if norm:
                row = self.c.execute(
                    "select i.entity_id from identifier i join entity e on e.id=i.entity_id "
                    "where i.scheme=? and i.value=? and e.status='active'", (scheme, norm)).fetchone()
                if row:
                    return row["entity_id"]
        if ref.name:
            if ref.name.strip().lower() in ("me", "myself", "i"):
                return self.ego
            matches = find_by_name(self.c, ref.name, ref.type)
            if not matches and ref.type is None:
                matches = find_by_name(self.c, ref.name)
            if len(matches) > 1:
                contacts = [m for m in matches if m["engagement"] in ("contact", "ego")]
                if len(contacts) == 1:
                    matches = contacts
                else:
                    self.report.warnings.append(
                        f"“{ref.name}” matches {len(matches)} entries; used the first. "
                        "Add an alias or identifier to disambiguate.")
            if matches:
                return matches[0]["id"]
            if not ref.create:
                return None
            eid = _create_entity(self.c, self.vault, ref.type or default_type, ref.name,
                                 engagement=ref.engagement if (ref.type or default_type) == "person"
                                 or ref.engagement == "reference" else "reference")
            for scheme, value in ref.identifiers:
                _add_identifier(self.c, eid, scheme, value, None)
            self.report.created.append(f"{ref.name} ({ref.type or default_type})")
            self.record(f"ref:{self.importer}", _key(ref.type, normalize_name(ref.name)),
                        "entity", eid)
            return eid
        return None

    def resolve_self(self, rec: ImportRecord) -> str:
        if rec.entity_id:
            self.report.matched.append(f"{rec.name} (already linked)")
            return rec.entity_id
        row = self.c.execute("select i.item_id from import_item i join entity e on e.id=i.item_id "
                             "where i.origin=? and i.kind='entity' and i.item_key='self' "
                             "and e.status='active'", (rec.origin,)).fetchone()
        if row:
            eid = self.c.execute("select coalesce(merged_into, id) as id from entity where id=?",
                                 (row["item_id"],)).fetchone()["id"]
            self.report.unchanged += 1
            return eid
        eid = self.resolve(Ref(name=rec.name, type=rec.entity_type, identifiers=rec.identifiers,
                               create=False))
        if eid:
            self.report.matched.append(f"{rec.name} (matched existing)")
        else:
            eid = _create_entity(self.c, self.vault, rec.entity_type, rec.name,
                                 engagement=rec.engagement or (
                                     "acquaintance" if rec.entity_type == "person" else "reference"))
            self.report.created.append(f"{rec.name} ({rec.entity_type})")
        self.record(rec.origin, "self", "entity", eid)
        return eid

    # ── applying one record ────────────────────────────────────────────
    def apply_entity_fields(self, rec: ImportRecord, eid: str, src: str, seen: set) -> None:
        c = self.c
        names = {r["name_norm"] for r in c.execute(
            "select name_norm from entity_name where entity_id=?", (eid,))}
        if rec.name and normalize_name(rec.name) not in names:
            _add_name(c, eid, rec.name, "alias")
            names.add(normalize_name(rec.name))
        for alias in rec.aliases:
            if alias and normalize_name(alias) not in names:
                _add_name(c, eid, alias, "alias")
                names.add(normalize_name(alias))
        for scheme, value in rec.identifiers:
            _add_identifier(c, eid, scheme, value, src)
        if rec.tags:
            current = [r["label"] for r in c.execute(
                "select case when t.namespace<>'' then t.namespace||':'||t.name else t.name end "
                "as label from tagging g join tag t on t.id=g.tag_id where g.target_kind='entity' "
                "and g.target_id=?", (eid,))]
            merged = list(dict.fromkeys(current + [t.lower() for t in rec.tags]))
            _set_tags(c, "entity", eid, merged)
        if rec.engagement and rec.entity_type == "person":
            c.execute("update entity set engagement=? where id=? and engagement<>'ego' and "
                      "coalesce(engagement,'')<>'contact'", (rec.engagement, eid))
        if rec.profile:
            prof = rec.profile
            c.execute("update entity set engagement='contact' where id=? and engagement<>'ego'",
                      (eid,))
            c.execute("insert or ignore into relationship_profile(entity_id) values(?)", (eid,))
            for col in ("tier", "cadence_days", "closeness_self", "how_met"):
                if prof.get(col) not in (None, ""):
                    c.execute(f"update relationship_profile set {col}=? where entity_id=?",
                              (prof[col], eid))
            if prof.get("introduced_by"):
                intro = self.resolve(prof["introduced_by"], "person")
                if intro:
                    c.execute("update relationship_profile set introduced_by=? where entity_id=?",
                              (intro, eid))
                    self.claim(rec, eid, ClaimSpec("introduced_by", obj=Ref(entity_id=intro)),
                               src, seen)
        if rec.attrs:
            row = c.execute("select attrs from entity where id=?", (eid,)).fetchone()
            attrs = json.loads(row["attrs"] or "{}")
            attrs.update(rec.attrs)
            c.execute("update entity set attrs=? where id=?", (json.dumps(attrs), eid))

    def claim(self, rec: ImportRecord, self_id: str, spec: ClaimSpec, src: str, seen: set) -> None:
        c = self.c
        pred = predicate(c, spec.predicate)
        if not pred:
            self.report.warnings.append(f"{rec.name or rec.origin}: unknown field “{spec.predicate}”")
            return
        subject = self.resolve(spec.subject) if spec.subject else self_id
        if not subject:
            return
        obj = None
        if pred["range_kind"] == "entity":
            types = json.loads(pred["range_types"] or '["person"]')
            default = types[0] if types and types[0] != "*" else "person"
            if spec.obj and not spec.obj.type and len(types) == 1 and types[0] != "*":
                spec.obj.type = types[0]
            obj = self.resolve(spec.obj, default)
            if not obj or obj == subject:
                return
        elif spec.value in (None, ""):
            return
        key = _key("claim", _ref_key(spec.subject), spec.predicate,
                   _ref_key(spec.obj) if spec.obj else str(spec.value), spec.valid_from,
                   spec.valid_to, spec.basis, spec.status)
        seen.add((key, "claim"))
        if (key, "claim") in self.existing_items:
            self.report.unchanged += 1
            return
        status = spec.status
        if pred["cardinality"] == "one" and status == "accepted":
            hand = c.execute(
                "select value, object_id from claim where subject_id=? and predicate_id=? "
                "and status='accepted' and asserted_by='user'", (subject, spec.predicate)).fetchone()
            if hand:
                same = (obj and hand["object_id"] == obj) or (
                    not obj and hand["value"] == json.dumps(str(spec.value).strip()))
                if same:
                    self.record(rec.origin, key, "claim", "existing")
                    self.report.unchanged += 1
                    return
                status = "disputed"
        cid = _add_claim(c, subject, spec.predicate, value=None if obj else spec.value,
                         object_id=obj, basis=spec.basis, confidence=spec.confidence,
                         valid_from=spec.valid_from, valid_to=spec.valid_to,
                         qualifiers=spec.qualifiers, source_id=src, asserted_by=self.asserted_by,
                         status=status)
        self.record(rec.origin, key, "claim", cid)
        self.touched.update({subject, obj} - {None})
        label = f"{spec.predicate}: {spec.obj.label() if spec.obj else spec.value}"
        if status == "disputed":
            self.report.conflicts.append(f"{rec.name or 'note'} · {label} (differs from what you entered)")
        else:
            self.report.claims_added.append(f"{rec.name or 'note'} · {label}")

    def retire(self, origin: str, seen: set) -> None:
        c = self.c
        for (key, kind), item_id in self.existing_items.items():
            if (key, kind) in seen or key == "self":
                continue
            if kind == "claim" and item_id != "existing":
                row = c.execute("select subject_id, predicate_id from claim where id=? and "
                                "status in ('accepted','proposed','disputed')", (item_id,)).fetchone()
                if row:
                    c.execute("update claim set status='superseded' where id=?", (item_id,))
                    self.touched.add(row["subject_id"])
                    self.report.claims_retired.append(f"{row['predicate_id']} (removed from source)")
            elif kind == "commitment":
                c.execute("update commitment set status='dropped' where id=? and status='open'",
                          (item_id,))
            elif kind == "event":
                c.execute("update entity set status='archived' where id=?", (item_id,))
                self.report.events_retired.append(item_id)
            elif kind == "link":
                eid, sid = item_id.split(":", 1)
                c.execute("delete from entity_source where entity_id=? and source_id=? and "
                          "role='link'", (eid, sid))
            c.execute("update import_item set active=0 where origin=? and item_key=? and kind=?",
                      (origin, key, kind))

    def source_for(self, rec: ImportRecord) -> str:
        """One source per origin; a new version only when the content changed."""
        c = self.c
        kind = "markdown" if self.importer == "markdown" else "import"
        content = rec.notes or ""
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        row = c.execute("select id, content_hash from source where kind=? and external_key=?",
                        (kind, rec.origin)).fetchone()
        if row and row["content_hash"] == digest:
            return row["id"]
        if row:
            c.execute("update source set external_key=null where id=?", (row["id"],))
        return _create_source(c, kind, content, title=rec.title or rec.name or rec.origin,
                              metadata={"origin": rec.origin, "importer": self.importer,
                                        "previous": row["id"] if row else None},
                              external_key=rec.origin)

    def apply_record(self, rec: ImportRecord) -> None:
        c = self.c
        self.existing_items = self.existing(rec.origin)
        seen: set[tuple[str, str]] = set()
        self.report.warnings.extend(rec.warnings)
        src = self.source_for(rec)

        if rec.kind == "event" and rec.event:
            ev = rec.event
            key = _key("event", ev.start, ev.kind, ev.status, ev.title,
                       sorted(_ref_key(p) for p in ev.participants))
            seen.add((key, "event"))
            eid = self.existing_items.get((key, "event"))
            if eid:
                self.report.unchanged += 1
                if rec.notes:
                    c.execute("update event_detail set notes_source_id=? where entity_id=?",
                              (src, eid))
            else:
                people = [p for p in (self.resolve(r, "person") for r in ev.participants)
                          if p and p != self.ego]
                people = list(dict.fromkeys(people))
                location = self.resolve(ev.location, "place")
                title = ev.title or rec.title
                if not title:
                    pn = [c.execute("select display_name from entity where id=?", (p,)).fetchone()
                          ["display_name"] for p in people[:3]]
                    title = ev.kind.replace("_", " ").title() + (f" with {', '.join(pn)}" if pn else "")
                eid = _create_entity(c, self.vault, "event", title)
                initiator = self.ego if ev.initiated_by == "me" else (
                    people[0] if ev.initiated_by == "them" and people else None)
                c.execute(
                    """insert into event_detail(entity_id, kind, status, start_at, time_precision,
                           location_id, channel, initiated_by, counts_as_contact, notes_source_id)
                       values(?,?,?,?,?,?,?,?,?,?)""",
                    (eid, ev.kind, ev.status, ev.start, "minute" if "T" in ev.start else "day",
                     location, ev.channel, initiator, 1 if ev.counts_as_contact else 0,
                     src if rec.notes else None))
                c.execute("insert into participation(event_id, entity_id, role) values(?,?,?)",
                          (eid, self.ego, "self"))
                for p in people:
                    c.execute("insert or ignore into participation(event_id, entity_id, role) "
                              "values(?,?,?)", (eid, p, "attendee"))
                self.record(rec.origin, key, "event", eid)
                self.report.events_added.append(f"{ev.start[:10]} {title}")
            self.by_origin[rec.origin] = eid
            self_id = eid
            if rec.tags:
                _set_tags(c, "event", eid, rec.tags)
            if ev.next_contact:
                for p in c.execute("select entity_id from participation where event_id=? and "
                                   "entity_id<>?", (eid, self.ego)).fetchall():
                    set_next_contact(c, p["entity_id"], ev.next_contact)
            attendees = [r["entity_id"] for r in c.execute(
                "select entity_id from participation where event_id=? and entity_id<>?",
                (eid, self.ego))]
        elif rec.kind == "entity":
            self_id = self.by_origin.get(rec.origin) or self.resolve_self(rec)
            self.by_origin[rec.origin] = self_id
            self.apply_entity_fields(rec, self_id, src, seen)
            attendees = [self_id]
            if rec.notes:
                c.execute("insert or ignore into entity_source(entity_id, source_id, role) "
                          "values(?,?,'note')", (self_id, src))
        else:  # free-standing note
            self_id = None
            attendees = []

        self.rec_self = self_id
        for spec in rec.claims:
            if self_id or spec.subject:
                self.claim(rec, self_id, spec, src, seen)

        for cm in rec.commitments:
            key = _key("commitment", cm.text, cm.owner, _ref_key(cm.counterpart))
            seen.add((key, "commitment"))
            existing = self.existing_items.get((key, "commitment"))
            if existing:
                if cm.done:
                    c.execute("update commitment set status='done', done_at=datetime('now') "
                              "where id=? and status='open'", (existing,))
                c.execute("update commitment set due_at=? where id=?", (cm.due, existing))
                self.report.unchanged += 1
                continue
            if cm.counterpart:
                other = self.resolve(cm.counterpart, "person")
            elif rec.kind == "entity":
                other = self_id
            else:
                other = attendees[0] if attendees else None
            from_id, to_id = (self.ego, other) if cm.owner != "them" else (other or self.ego, self.ego)
            cid = _add_commitment(c, from_id, to_id, cm.text, cm.due,
                                  self_id if rec.kind == "event" else None)
            if cm.done:
                c.execute("update commitment set status='done', done_at=datetime('now') where id=?",
                          (cid,))
            self.record(rec.origin, key, "commitment", cid)
            self.report.follow_ups_added.append(cm.text)

        for ln in rec.links:
            target = self.resolve(ln.target) if ln.target else self_id
            if not target:
                continue
            key = _key("link", ln.url, _ref_key(ln.target))
            seen.add((key, "link"))
            if (key, "link") in self.existing_items:
                continue
            sid = _add_link(c, target, ln.url, ln.note)
            self.record(rec.origin, key, "link", f"{target}:{sid}")
            self.report.links_added.append(ln.url)

        for m in rec.mentions:
            mid = self.resolve(m)
            if mid:
                c.execute("insert or ignore into entity_source(entity_id, source_id, role) "
                          "values(?,?,'mention')", (mid, src))

        self.retire(rec.origin, seen)
        if self_id:
            self.touched.add(self_id)


def apply(vault: Vault, importer: str, records: list[ImportRecord], dry_run: bool = False,
          label: str | None = None) -> Report:
    report = Report(importer=importer, dry_run=dry_run, records=len(records))
    batch_id = new_id()
    try:
        with vault.tx() as c:
            c.execute("insert into import_batch(id, importer, label) values(?,?,?)",
                      (batch_id, importer, label))
            app = _Applier(c, vault, importer, report, batch_id)
            # entities first, so events and claims can refer to them
            ordered = [r for r in records if r.kind == "entity"] + \
                      [r for r in records if r.kind != "entity"]
            # first create or match every entity, so records can refer to each other in any order
            for rec in ordered:
                if rec.kind == "entity":
                    app.by_origin[rec.origin] = app.resolve_self(rec)
            for rec in ordered:
                app.apply_record(rec)
            for eid in app.touched:
                textindex.reindex_entity(c, eid)
            report.origin_entities = dict(app.by_origin)
            c.execute("update import_batch set stats=? where id=?", (json.dumps({
                "created": len(report.created), "claims": len(report.claims_added),
                "conflicts": len(report.conflicts), "events": len(report.events_added),
                "follow_ups": len(report.follow_ups_added), "retired": len(report.claims_retired),
            }), batch_id))
            vault.audit(c, "write", batch_id, {"op": "import", "importer": importer,
                                               "changes": report.changes})
            if dry_run:
                raise _DryRun()
    except _DryRun:
        return report
    report.batch_id = batch_id
    return report


def rollback(vault: Vault, batch_id: str) -> int:
    """Undo everything one import batch created. Returns the number of items undone."""
    n = 0
    with vault.tx() as c:
        items = c.execute("select * from import_item where batch_id=?", (batch_id,)).fetchall()
        claim_ids = [i["item_id"] for i in items if i["kind"] == "claim" and i["item_id"] != "existing"]
        for cid in claim_ids:
            c.execute("update claim set status='accepted', superseded_by=null where superseded_by=? "
                      "and status='superseded'", (cid,))
            c.execute("update claim set status='rejected' where id=?", (cid,))
        for i in items:
            if i["kind"] in ("entity", "event"):
                c.execute("update entity set status='archived' where id=? and "
                          "coalesce(engagement,'')<>'ego'", (i["item_id"],))
            elif i["kind"] == "commitment":
                c.execute("update commitment set status='dropped' where id=?", (i["item_id"],))
            elif i["kind"] == "link":
                eid, sid = i["item_id"].split(":", 1)
                c.execute("delete from entity_source where entity_id=? and source_id=? and "
                          "role='link'", (eid, sid))
            n += 1
        c.execute("delete from import_item where batch_id=?", (batch_id,))
        c.execute("update import_batch set status='rolled_back' where id=?", (batch_id,))
        vault.audit(c, "write", batch_id, {"op": "rollback", "items": n})
        textindex_entities = {r["id"] for r in c.execute("select id from entity where status='active'")}
        for eid in textindex_entities:
            textindex.reindex_entity(c, eid)
    return n


def batches(vault: Vault, limit: int = 50) -> list[dict]:
    rows = vault.all("select * from import_batch order by started_at desc limit ?", (limit,))
    for r in rows:
        r["stats"] = json.loads(r["stats"] or "{}")
    return rows
