"""Reading and writing vault records.

Public functions take a Vault and run in one transaction. Functions whose
name starts with an underscore take an open connection `c` and are used to
compose larger writes inside a single transaction.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import secrets
import time
import unicodedata
from datetime import date
from typing import Any, Iterable

from . import textindex
from .db import Vault
from .registry import TIER_CADENCE, seed

INDEXED_SOURCE_KINDS = {"note", "markdown", "web", "capture", "import"}

# ── ids and normalization ────────────────────────────────────────────────────


def new_id() -> str:
    """Time-sortable 32-hex-character id (48-bit ms timestamp + 80 random bits)."""
    return f"{int(time.time() * 1000):012x}{secrets.token_hex(10)}"


def normalize_name(name: str) -> str:
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^\w\s]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


NICKNAMES = {
    "bob": "robert", "bobby": "robert", "rob": "robert", "robbie": "robert",
    "bill": "william", "will": "william", "billy": "william", "liam": "william",
    "liz": "elizabeth", "beth": "elizabeth", "lizzie": "elizabeth", "eliza": "elizabeth",
    "kate": "katherine", "katie": "katherine", "kathy": "katherine", "cathy": "catherine",
    "jim": "james", "jimmy": "james", "jamie": "james", "mike": "michael", "mikey": "michael",
    "dave": "david", "dan": "daniel", "danny": "daniel", "tom": "thomas", "tommy": "thomas",
    "tony": "anthony", "alex": "alexander", "sam": "samuel", "chris": "christopher",
    "matt": "matthew", "nick": "nicholas", "joe": "joseph", "joey": "joseph",
    "jen": "jennifer", "jenny": "jennifer", "steve": "steven", "rich": "richard",
    "rick": "richard", "dick": "richard", "andy": "andrew", "drew": "andrew",
    "ben": "benjamin", "pat": "patrick", "meg": "margaret", "peggy": "margaret",
    "maggie": "margaret", "sue": "susan", "ted": "edward", "ed": "edward", "eddie": "edward",
    "greg": "gregory", "jon": "jonathan", "josh": "joshua", "larry": "lawrence",
    "pete": "peter", "ron": "ronald", "tim": "timothy", "vicky": "victoria", "zach": "zachary",
    "abby": "abigail", "becky": "rebecca", "jess": "jessica", "jessie": "jessica",
    "mandy": "amanda", "nate": "nathan", "fred": "frederick", "freddie": "frederick",
}


def _canon_token(tok: str) -> str:
    return NICKNAMES.get(tok, tok)


def normalize_identifier(scheme: str, value: str) -> str:
    value = (value or "").strip()
    if scheme == "email":
        return value.lower()
    if scheme == "phone":
        digits = re.sub(r"[^\d+]", "", value)
        return digits
    if scheme == "url":
        value = re.sub(r"^https?://(www\.)?", "", value.lower())
        return value.rstrip("/")
    if scheme == "handle":
        return value.lstrip("@").lower()
    return value


def date_precision(value: str | None) -> str | None:
    if not value:
        return None
    return {4: "year", 7: "month", 10: "day"}.get(len(value[:10]), "day")


def parse_day(value: str | None) -> date | None:
    """Date part of an ISO date or datetime string. Partial dates use the 1st."""
    if not value:
        return None
    v = value[:10]
    try:
        if len(v) == 4:
            return date(int(v), 1, 1)
        if len(v) == 7:
            return date(int(v[:4]), int(v[5:7]), 1)
        return date.fromisoformat(v)
    except ValueError:
        return None


def split_list(text: str | Iterable[str] | None) -> list[str]:
    if text is None:
        return []
    if isinstance(text, str):
        parts = re.split(r"[,\n;]", text)
    else:
        parts = list(text)
    seen, out = set(), []
    for p in parts:
        p = (p or "").strip()
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out


# ── vault setup ──────────────────────────────────────────────────────────────


def prepare(vault: Vault) -> None:
    """Seed registries. Safe to run on every start."""
    seed(vault)


def init_vault(vault: Vault, my_name: str) -> str:
    prepare(vault)
    ego = vault.get_meta("ego_id")
    if ego:
        return ego
    with vault.tx() as c:
        ego = _create_entity(c, vault, "person", my_name, engagement="ego")
    vault.set_meta("ego_id", ego)
    return ego


def ego_id(vault: Vault) -> str:
    ego = vault.get_meta("ego_id")
    if not ego:
        raise RuntimeError("Vault has no owner yet. Run `ties init`.")
    return ego


# ── sources ──────────────────────────────────────────────────────────────────


def _create_source(c, kind: str, content: str = "", title: str | None = None,
                   uri: str | None = None, metadata: dict | None = None,
                   external_key: str | None = None) -> str:
    sid = new_id()
    digest = hashlib.sha256((content or "").encode("utf-8")).hexdigest()
    c.execute(
        "insert into source(id, kind, external_key, uri, title, content, content_hash, metadata) "
        "values(?,?,?,?,?,?,?,?)",
        (sid, kind, external_key, uri, title, content, digest, json.dumps(metadata or {})),
    )
    if content and kind in INDEXED_SOURCE_KINDS:
        textindex.index_source(c, sid, content)
    return sid


# ── entities ─────────────────────────────────────────────────────────────────


def _add_name(c, entity_id: str, name: str, kind: str = "primary") -> None:
    c.execute(
        "insert into entity_name(entity_id, name, name_norm, kind) values(?,?,?,?)",
        (entity_id, name, normalize_name(name), kind),
    )


def _create_entity(c, vault: Vault, type_id: str, name: str, engagement: str | None = None,
                   aliases: Iterable[str] = (), tags: Iterable[str] = (),
                   identifiers: Iterable[tuple[str, str]] = (),
                   description: str | None = None, source_id: str | None = None) -> str:
    name = name.strip()
    if not name:
        raise ValueError("A name is required.")
    eid = new_id()
    c.execute(
        "insert into entity(id, type_id, display_name, engagement) values(?,?,?,?)",
        (eid, type_id, name, engagement),
    )
    _add_name(c, eid, name, "primary")
    for alias in split_list(aliases):
        if normalize_name(alias) != normalize_name(name):
            _add_name(c, eid, alias, "alias")
    src = source_id or _create_source(c, "manual", f"created {type_id}: {name}")
    for scheme, value in identifiers:
        if value and value.strip():
            _add_identifier(c, eid, scheme, value, src)
    _set_tags(c, "entity", eid, tags)
    if description:
        _add_claim(c, eid, "description", value=description, source_id=src)
    if engagement == "contact" and type_id == "person":
        c.execute("insert or ignore into relationship_profile(entity_id) values(?)", (eid,))
    vault.audit(c, "write", eid, {"op": "create_entity", "type": type_id, "name": name})
    textindex.reindex_entity(c, eid)
    return eid


def create_entity(vault: Vault, type_id: str, name: str, engagement: str | None = None,
                  aliases: Iterable[str] | str = (), tags: Iterable[str] | str = (),
                  identifiers: Iterable[tuple[str, str]] = (),
                  description: str | None = None) -> str:
    with vault.tx() as c:
        return _create_entity(c, vault, type_id, name, engagement, split_list(aliases),
                              split_list(tags), identifiers, description)


def resolve_ref(c, vault: Vault, ref: str | None, default_type: str,
                default_engagement: str = "reference") -> str | None:
    """Turn a form value into an entity id.

    Values are either an existing id or ``new:<type>:<name>``, which the
    autocomplete widget emits when you choose "Create ...".
    """
    if not ref:
        return None
    ref = ref.strip()
    if ref.startswith("new:"):
        _, type_id, name = (ref.split(":", 2) + ["", ""])[:3]
        type_id = type_id or default_type
        engagement = default_engagement
        if type_id != "person" and engagement == "acquaintance":
            engagement = "reference"
        return _create_entity(c, vault, type_id, name, engagement=engagement)
    row = c.execute("select id, merged_into from entity where id=?", (ref,)).fetchone()
    if not row:
        raise ValueError(f"Unknown entity: {ref}")
    return row["merged_into"] or row["id"]


def get_entity(vault: Vault, entity_id: str) -> dict[str, Any] | None:
    row = vault.one("select * from entity where id=?", (entity_id,))
    if row and row["merged_into"]:
        return get_entity(vault, row["merged_into"])
    return row


def update_entity(vault: Vault, entity_id: str, display_name: str, engagement: str | None,
                  aliases: Iterable[str] | str, tags: Iterable[str] | str) -> None:
    display_name = display_name.strip()
    if not display_name:
        raise ValueError("A name is required.")
    with vault.tx() as c:
        ent = c.execute("select * from entity where id=?", (entity_id,)).fetchone()
        if not ent:
            raise ValueError("Unknown entity")
        if ent["display_name"] != display_name:
            c.execute(
                "update entity_name set kind='former', valid_to=date('now') "
                "where entity_id=? and kind='primary'", (entity_id,))
            _add_name(c, entity_id, display_name, "primary")
            c.execute("update entity set display_name=? where id=?", (display_name, entity_id))
        wanted = {normalize_name(a): a for a in split_list(aliases)}
        wanted.pop(normalize_name(display_name), None)
        existing = {r["name_norm"]: r["name"] for r in c.execute(
            "select name, name_norm from entity_name where entity_id=? and kind='alias'",
            (entity_id,)).fetchall()}
        for norm in existing.keys() - wanted.keys():
            c.execute("delete from entity_name where entity_id=? and kind='alias' and name_norm=?",
                      (entity_id, norm))
        for norm in wanted.keys() - existing.keys():
            _add_name(c, entity_id, wanted[norm], "alias")
        if ent["engagement"] != "ego":
            c.execute("update entity set engagement=? where id=?", (engagement, entity_id))
            if engagement == "contact" and ent["type_id"] == "person":
                c.execute("insert or ignore into relationship_profile(entity_id) values(?)",
                          (entity_id,))
        _set_tags(c, "entity", entity_id, split_list(tags))
        vault.audit(c, "write", entity_id, {"op": "update_entity"})
        textindex.reindex_entity(c, entity_id)


def archive_entity(vault: Vault, entity_id: str) -> None:
    with vault.tx() as c:
        c.execute("update entity set status='archived' where id=?", (entity_id,))
        vault.audit(c, "write", entity_id, {"op": "archive"})


# ── identifiers ──────────────────────────────────────────────────────────────


def _add_identifier(c, entity_id: str, scheme: str, value: str, source_id: str | None) -> None:
    norm = normalize_identifier(scheme, value)
    if norm:
        c.execute(
            "insert or ignore into identifier(entity_id, scheme, value, source_id) values(?,?,?,?)",
            (entity_id, scheme, norm, source_id),
        )


def add_identifier(vault: Vault, entity_id: str, scheme: str, value: str) -> None:
    with vault.tx() as c:
        src = _create_source(c, "manual", f"{scheme}: {value}")
        _add_identifier(c, entity_id, scheme, value, src)
        vault.audit(c, "write", entity_id, {"op": "add_identifier", "scheme": scheme})


def remove_identifier(vault: Vault, entity_id: str, scheme: str, value: str) -> None:
    with vault.tx() as c:
        c.execute("delete from identifier where entity_id=? and scheme=? and value=?",
                  (entity_id, scheme, value))
        vault.audit(c, "write", entity_id, {"op": "remove_identifier", "scheme": scheme})


def identifiers(vault: Vault, entity_id: str) -> list[dict]:
    return vault.all("select scheme, value from identifier where entity_id=? order by scheme, value",
                     (entity_id,))


# ── tags ─────────────────────────────────────────────────────────────────────


def _parse_tag(text: str) -> tuple[str, str]:
    text = text.strip().lstrip("#")
    for sep in (":", "/"):
        if sep in text:
            ns, name = text.split(sep, 1)
            return ns.strip().lower(), name.strip().lower()
    return "", text.lower()


def tag_label(ns: str, name: str) -> str:
    return f"{ns}:{name}" if ns else name


def _set_tags(c, kind: str, target_id: str, tags: Iterable[str]) -> None:
    c.execute("delete from tagging where target_kind=? and target_id=?", (kind, target_id))
    for t in tags:
        ns, name = _parse_tag(t)
        if not name:
            continue
        row = c.execute("select id from tag where namespace=? and name=?", (ns, name)).fetchone()
        tid = row["id"] if row else new_id()
        if not row:
            c.execute("insert into tag(id, namespace, name) values(?,?,?)", (tid, ns, name))
        c.execute("insert or ignore into tagging(tag_id, target_kind, target_id) values(?,?,?)",
                  (tid, kind, target_id))


def tags_for(vault: Vault, kind: str, target_id: str) -> list[str]:
    rows = vault.all(
        "select t.namespace, t.name from tagging g join tag t on t.id=g.tag_id "
        "where g.target_kind=? and g.target_id=? order by t.namespace, t.name",
        (kind, target_id))
    return [tag_label(r["namespace"], r["name"]) for r in rows]


def all_tags(vault: Vault) -> list[dict]:
    rows = vault.all(
        "select t.namespace, t.name, count(*) as n from tag t join tagging g on g.tag_id=t.id "
        "group by t.id order by t.namespace, t.name")
    return [{"label": tag_label(r["namespace"], r["name"]), "n": r["n"]} for r in rows]


# ── claims (facts and relationships) ─────────────────────────────────────────


def predicate(vault_or_c, pid: str) -> dict | None:
    if isinstance(vault_or_c, Vault):
        return vault_or_c.one("select * from predicate where id=?", (pid,))
    return vault_or_c.execute("select * from predicate where id=?", (pid,)).fetchone()


def predicates(vault: Vault, range_kind: str | None = None, subject_type: str | None = None) -> list[dict]:
    rows = vault.all("select * from predicate order by sort, label")
    out = []
    for r in rows:
        if range_kind == "entity" and r["range_kind"] != "entity":
            continue
        if range_kind == "value" and r["range_kind"] == "entity":
            continue
        dom = json.loads(r["domain_types"])
        if subject_type and "*" not in dom and subject_type not in dom:
            continue
        if r["inverse_id"] and r["id"] == "child_of":
            continue  # offered as "Parent of" from the other side; keeps the list short
        if r["id"] == "mentee_of":
            continue
        out.append(r)
    return out


def _add_claim(c, subject_id: str, predicate_id: str, value: Any = None,
               object_id: str | None = None, basis: str = "observed",
               confidence: float = 1.0, valid_from: str | None = None,
               valid_to: str | None = None, qualifiers: dict | None = None,
               source_id: str | None = None, asserted_by: str = "user",
               status: str = "accepted") -> str:
    pred = predicate(c, predicate_id)
    if not pred:
        raise ValueError(f"Unknown predicate: {predicate_id}")
    if pred["range_kind"] == "entity":
        if not object_id:
            raise ValueError(f"'{pred['label']}' needs a linked person, organization or place.")
        if object_id == subject_id:
            raise ValueError("An entity cannot be related to itself.")
        value_json = None
    else:
        if value is None or str(value).strip() == "":
            raise ValueError(f"'{pred['label']}' needs a value.")
        value_json = json.dumps(str(value).strip())
        object_id = None

    # Skip exact duplicates of a current claim.
    dup = c.execute(
        "select id from claim where subject_id=? and predicate_id=? and status='accepted' "
        "and coalesce(object_id,'')=coalesce(?, '') and coalesce(value,'')=coalesce(?, '') "
        "and coalesce(valid_from,'')=coalesce(?, '') and coalesce(valid_to,'')=coalesce(?, '')",
        (subject_id, predicate_id, object_id, value_json, valid_from or None, valid_to or None),
    ).fetchone()
    if dup:
        return dup["id"]
    if pred["symmetric"] and object_id:
        dup = c.execute(
            "select id from claim where subject_id=? and predicate_id=? and object_id=? "
            "and status='accepted'", (object_id, predicate_id, subject_id)).fetchone()
        if dup:
            return dup["id"]

    cid = new_id()
    c.execute(
        """insert into claim(id, subject_id, predicate_id, object_id, value, qualifiers,
               valid_from, valid_to, time_precision, confidence, status, basis, asserted_by)
           values(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (cid, subject_id, predicate_id, object_id, value_json, json.dumps(qualifiers or {}),
         valid_from or None, valid_to or None, date_precision(valid_from or valid_to),
         confidence, status, basis, asserted_by),
    )
    if pred["cardinality"] == "one" and status == "accepted" and not _is_research(c):
        c.execute(
            "update claim set status='superseded', superseded_by=? where subject_id=? "
            "and predicate_id=? and status='accepted' and id<>?",
            (cid, subject_id, predicate_id, cid))
    if source_id:
        c.execute("insert or ignore into claim_evidence(claim_id, source_id) values(?,?)",
                  (cid, source_id))
    textindex.reindex_entity(c, subject_id)
    textindex.reindex_entity(c, object_id)
    return cid


def _is_research(c) -> bool:
    """Research vaults keep competing claims side by side instead of superseding."""
    row = c.execute("select value from meta where key='vault_kind'").fetchone()
    return bool(row and row["value"] == "research")


def add_claim(vault: Vault, subject_id: str, predicate_id: str, value: Any = None,
              object_ref: str | None = None, basis: str = "observed", confidence: float = 1.0,
              valid_from: str | None = None, valid_to: str | None = None,
              note: str | None = None) -> str:
    with vault.tx() as c:
        pred = predicate(c, predicate_id)
        if not pred:
            raise ValueError(f"Unknown predicate: {predicate_id}")
        default_type = "person"
        if pred["range_types"]:
            types = json.loads(pred["range_types"])
            default_type = types[0] if types and types[0] != "*" else "person"
        object_id = resolve_ref(c, vault, object_ref, default_type) if object_ref else None
        quals = {"note": note.strip()} if note and note.strip() else {}
        src = _create_source(c, "manual", f"{predicate_id}: {value or object_ref}")
        cid = _add_claim(c, subject_id, predicate_id, value=value, object_id=object_id,
                         basis=basis, confidence=confidence, valid_from=valid_from,
                         valid_to=valid_to, qualifiers=quals, source_id=src)
        vault.audit(c, "write", subject_id, {"op": "add_claim", "claim": cid,
                                             "predicate": predicate_id})
        return cid


def end_claim(vault: Vault, claim_id: str, valid_to: str) -> str:
    """Record that a fact stopped being true. The old claim is kept as superseded."""
    with vault.tx() as c:
        old = c.execute("select * from claim where id=?", (claim_id,)).fetchone()
        if not old:
            raise ValueError("Unknown claim")
        cid = new_id()
        c.execute(
            """insert into claim(id, subject_id, predicate_id, object_id, value, qualifiers,
                   valid_from, valid_to, time_precision, confidence, status, basis, asserted_by)
               select ?, subject_id, predicate_id, object_id, value, qualifiers, valid_from, ?,
                   time_precision, confidence, 'accepted', basis, 'user' from claim where id=?""",
            (cid, valid_to, claim_id))
        c.execute("update claim set status='superseded', superseded_by=? where id=?",
                  (cid, claim_id))
        c.execute("insert into claim_evidence(claim_id, source_id) "
                  "select ?, source_id from claim_evidence where claim_id=?", (cid, claim_id))
        vault.audit(c, "write", old["subject_id"], {"op": "end_claim", "claim": claim_id})
        textindex.reindex_entity(c, old["subject_id"])
        return cid


def retract_claim(vault: Vault, claim_id: str) -> None:
    """Mark a fact as wrong. It stays in history as rejected."""
    with vault.tx() as c:
        old = c.execute("select subject_id from claim where id=?", (claim_id,)).fetchone()
        c.execute("update claim set status='rejected' where id=?", (claim_id,))
        vault.audit(c, "write", old["subject_id"] if old else None,
                    {"op": "retract_claim", "claim": claim_id})
        if old:
            textindex.reindex_entity(c, old["subject_id"])


def _claim_rows(vault: Vault, where: str, params: tuple) -> list[dict]:
    rows = vault.all(
        f"""select cl.*, p.label as predicate_label, p.reverse_label, p.symmetric,
                   p.inverse_id, p.range_kind, p.sensitivity, p.cardinality,
                   o.display_name as object_name, o.type_id as object_type,
                   s.display_name as subject_name, s.type_id as subject_type,
                   (select min(src.captured_at) from claim_evidence ev
                      join source src on src.id=ev.source_id where ev.claim_id=cl.id) as source_at
            from claim cl
            join predicate p on p.id=cl.predicate_id
            join entity s on s.id=cl.subject_id
            left join entity o on o.id=cl.object_id
            where {where}
            order by p.sort, cl.recorded_at""", params)
    for r in rows:
        r["value_text"] = json.loads(r["value"]) if r["value"] else None
        r["qual"] = json.loads(r["qualifiers"] or "{}")
    return rows


def facts(vault: Vault, entity_id: str) -> list[dict]:
    """Current attribute claims (values, not relationships)."""
    return _claim_rows(vault, "cl.subject_id=? and cl.status='accepted' and cl.object_id is null "
                       "and cl.basis<>'inferred'", (entity_id,))


def inferences(vault: Vault, entity_id: str) -> list[dict]:
    """Your own inferences (accepted, basis inferred) and suggestions waiting for review."""
    rows = _claim_rows(vault, "(cl.subject_id=? or cl.object_id=?) and ("
                       "(cl.status='accepted' and cl.basis='inferred') or cl.status='proposed')",
                       (entity_id, entity_id))
    for r in rows:
        _orient(vault, r, entity_id)
    return rows


def claim_history(vault: Vault, entity_id: str) -> list[dict]:
    rows = _claim_rows(vault, "(cl.subject_id=? or cl.object_id=?)", (entity_id, entity_id))
    return sorted(rows, key=lambda r: r["recorded_at"], reverse=True)


def relations(vault: Vault, entity_id: str) -> list[dict]:
    """Current relationships, read from this entity's side."""
    out = []
    for r in _claim_rows(vault, "cl.status='accepted' and cl.object_id is not null "
                         "and cl.basis<>'inferred' and (cl.subject_id=? or cl.object_id=?)",
                         (entity_id, entity_id)):
        out.append(_orient(vault, r, entity_id))
    return out


def _orient(vault: Vault, r: dict, entity_id: str) -> dict:
    """Add label/other_* fields so a claim reads from entity_id's side."""
    outgoing = r["subject_id"] == entity_id or not r["object_id"]
    if outgoing:
        label, other_id, other_name, other_type = (
            r["predicate_label"], r["object_id"], r["object_name"], r["object_type"])
    else:
        if r["symmetric"]:
            label = r["predicate_label"]
        elif r["inverse_id"]:
            inv = predicate(vault, r["inverse_id"])
            label = inv["label"] if inv else r["predicate_label"]
        else:
            label = r["reverse_label"] or f"{r['predicate_label']} (from)"
        other_id, other_name, other_type = r["subject_id"], r["subject_name"], r["subject_type"]
    if r["qual"].get("label"):
        label = str(r["qual"]["label"]).replace("_", " ").capitalize()
    r.update(label=label, other_id=other_id, other_name=other_name, other_type=other_type,
             outgoing=outgoing)
    return r


def current_value(vault: Vault, entity_id: str, predicate_id: str) -> dict | None:
    rows = _claim_rows(vault, "cl.subject_id=? and cl.predicate_id=? and cl.status='accepted' "
                       "and cl.valid_to is null", (entity_id, predicate_id))
    return rows[-1] if rows else None


# ── relationship profile ─────────────────────────────────────────────────────


def get_profile(vault: Vault, entity_id: str) -> dict | None:
    return vault.one(
        "select rp.*, ib.display_name as introduced_by_name from relationship_profile rp "
        "left join entity ib on ib.id=rp.introduced_by where rp.entity_id=?", (entity_id,))


def update_profile(vault: Vault, entity_id: str, tier: str, cadence_days: int | None,
                   closeness_self: int | None, how_met: str | None,
                   introduced_by_ref: str | None) -> None:
    if tier not in TIER_CADENCE:
        raise ValueError("Unknown tier")
    with vault.tx() as c:
        introduced_by = resolve_ref(c, vault, introduced_by_ref, "person", "acquaintance")
        c.execute("insert or ignore into relationship_profile(entity_id) values(?)", (entity_id,))
        c.execute(
            "update relationship_profile set tier=?, cadence_days=?, closeness_self=?, "
            "how_met=?, introduced_by=? where entity_id=?",
            (tier, cadence_days, closeness_self, (how_met or "").strip() or None,
             introduced_by, entity_id))
        if introduced_by:
            src = _create_source(c, "manual", "introduced_by")
            _add_claim(c, entity_id, "introduced_by", object_id=introduced_by, source_id=src,
                       basis="observed")
        c.execute("update entity set engagement='contact' where id=? and engagement<>'ego'",
                  (entity_id,))
        vault.audit(c, "write", entity_id, {"op": "update_profile"})
        textindex.reindex_entity(c, entity_id)


# ── events ───────────────────────────────────────────────────────────────────


def create_event(vault: Vault, *, kind: str, status: str, start_at: str,
                 participants: Iterable[str] = (), title: str | None = None,
                 channel: str | None = None, location_ref: str | None = None,
                 initiated_by: str | None = None, counts_as_contact: bool = True,
                 include_me: bool = True, notes: str | None = None,
                 tags: Iterable[str] | str = (), follow_ups: Iterable[dict] = ()) -> str:
    """Log a past interaction or schedule a future one.

    ``initiated_by`` is "me", "them" or an entity id. ``follow_ups`` items are
    dicts with ``text``, optional ``due`` and ``owner`` ("me" or "them").
    """
    if not start_at:
        raise ValueError("A date is required.")
    if parse_day(start_at) is None:
        raise ValueError("Dates look like 2026-11-09 (optionally with a time).")
    ego = ego_id(vault)
    with vault.tx() as c:
        people = []
        for ref in participants:
            pid = resolve_ref(c, vault, ref, "person", "acquaintance")
            if pid and pid != ego and pid not in people:
                people.append(pid)
        location_id = resolve_ref(c, vault, location_ref, "place")
        if not title:
            names = [c.execute("select display_name from entity where id=?", (p,)).fetchone()
                     ["display_name"] for p in people[:3]]
            extra = f" +{len(people) - 3}" if len(people) > 3 else ""
            title = f"{kind.replace('_', ' ').title()}" + (
                f" with {', '.join(names)}{extra}" if names else "")
        initiator = None
        if initiated_by == "me":
            initiator = ego
        elif initiated_by == "them":
            initiator = people[0] if people else None
        elif initiated_by:
            initiator = initiated_by
        notes_source = _create_source(c, "note", notes, title=title) if notes and notes.strip() \
            else None
        eid = _create_entity(c, vault, "event", title, engagement=None)
        c.execute(
            """insert into event_detail(entity_id, kind, status, start_at, time_precision,
                   location_id, channel, initiated_by, counts_as_contact, notes_source_id)
               values(?,?,?,?,?,?,?,?,?,?)""",
            (eid, kind, status, start_at, "minute" if "T" in start_at else "day", location_id,
             channel or None, initiator, 1 if counts_as_contact else 0, notes_source))
        if include_me:
            c.execute("insert into participation(event_id, entity_id, role) values(?,?,?)",
                      (eid, ego, "self"))
        for pid in people:
            c.execute("insert or ignore into participation(event_id, entity_id, role) "
                      "values(?,?,?)", (eid, pid, "attendee"))
        _set_tags(c, "event", eid, split_list(tags))
        for fu in follow_ups:
            text = (fu.get("text") or "").strip()
            if not text:
                continue
            counterpart = _mentioned_participant(c, text, people)
            mine = fu.get("owner", "me") != "them"
            from_id, to_id = (ego, counterpart) if mine else (counterpart or ego, ego)
            _add_commitment(c, from_id, to_id, text, fu.get("due") or None, eid)
        vault.audit(c, "write", eid, {"op": "create_event", "status": status})
        return eid


def _mentioned_participant(c, text: str, people: list[str]) -> str | None:
    """The participant a follow-up is about: the first one named in its text, else the first."""
    if not people:
        return None
    norm_text = f" {normalize_name(text)} "
    best: tuple[int, str] | None = None
    for pid in people:
        for row in c.execute("select name_norm from entity_name where entity_id=?", (pid,)):
            tokens = row["name_norm"].split()
            for candidate in {row["name_norm"], tokens[0] if tokens else ""}:
                if candidate and len(candidate) > 1:
                    pos = norm_text.find(f" {candidate} ")
                    if pos >= 0 and (best is None or pos < best[0]):
                        best = (pos, pid)
    return best[1] if best else people[0]


def set_event_status(vault: Vault, event_id: str, status: str) -> None:
    with vault.tx() as c:
        c.execute("update event_detail set status=? where entity_id=?", (status, event_id))
        vault.audit(c, "write", event_id, {"op": "event_status", "status": status})


def add_event_note(vault: Vault, event_id: str, text: str) -> None:
    """Append to an event's notes. The earlier text is kept as its own source."""
    if not text.strip():
        return
    with vault.tx() as c:
        ev = c.execute("select ed.notes_source_id, e.display_name from event_detail ed "
                       "join entity e on e.id=ed.entity_id where ed.entity_id=?",
                       (event_id,)).fetchone()
        old = ""
        if ev["notes_source_id"]:
            old = c.execute("select content from source where id=?",
                            (ev["notes_source_id"],)).fetchone()["content"] or ""
        combined = (old + "\n\n" + text.strip()).strip()
        sid = _create_source(c, "note", combined, title=ev["display_name"],
                             metadata={"previous": ev["notes_source_id"]})
        c.execute("update event_detail set notes_source_id=? where entity_id=?", (sid, event_id))
        vault.audit(c, "write", event_id, {"op": "event_note"})


def get_event(vault: Vault, event_id: str) -> dict | None:
    ev = vault.one(
        """select e.id, e.display_name as title, ed.*, loc.display_name as location_name,
                  src.content as notes, ini.display_name as initiated_by_name
           from entity e join event_detail ed on ed.entity_id=e.id
           left join entity loc on loc.id=ed.location_id
           left join entity ini on ini.id=ed.initiated_by
           left join source src on src.id=ed.notes_source_id
           where e.id=?""", (event_id,))
    if ev:
        ev["participants"] = vault.all(
            "select en.id, en.display_name, en.type_id, p.role from participation p "
            "join entity en on en.id=p.entity_id where p.event_id=? order by p.role, en.display_name",
            (event_id,))
        ev["tags"] = tags_for(vault, "event", event_id)
    return ev


def list_events(vault: Vault, *, entity_id: str | None = None, status: str | None = None,
                kind: str | None = None, tag: str | None = None, start: str | None = None,
                end: str | None = None, limit: int = 200, descending: bool = True) -> list[dict]:
    where, params = ["e.status='active'"], []
    if entity_id:
        where.append("(exists(select 1 from participation p where p.event_id=e.id and "
                     "p.entity_id=?) or ed.location_id=?)")
        params += [entity_id, entity_id]
    if status:
        where.append("ed.status=?")
        params.append(status)
    if kind:
        where.append("ed.kind=?")
        params.append(kind)
    if start:
        where.append("ed.start_at>=?")
        params.append(start)
    if end:
        where.append("ed.start_at<=?")
        params.append(end + "T99")
    if tag:
        ns, name = _parse_tag(tag)
        where.append("exists(select 1 from tagging g join tag t on t.id=g.tag_id where "
                     "g.target_kind='event' and g.target_id=e.id and t.namespace=? and t.name=?)")
        params += [ns, name]
    rows = vault.all(
        f"""select e.id, e.display_name as title, ed.kind, ed.status, ed.start_at, ed.channel,
                   ed.counts_as_contact, loc.display_name as location_name,
                   src.content as notes
            from entity e join event_detail ed on ed.entity_id=e.id
            left join entity loc on loc.id=ed.location_id
            left join source src on src.id=ed.notes_source_id
            where {' and '.join(where)}
            order by ed.start_at {'desc' if descending else 'asc'} limit ?""",
        (*params, limit))
    ego = vault.get_meta("ego_id")
    for r in rows:
        r["people"] = vault.all(
            "select en.id, en.display_name from participation p join entity en on en.id=p.entity_id "
            "where p.event_id=? and p.entity_id<>? order by en.display_name", (r["id"], ego))
        r["tags"] = tags_for(vault, "event", r["id"])
    return rows


# ── commitments (follow-ups) ─────────────────────────────────────────────────


def _add_commitment(c, from_id: str, to_id: str | None, description: str,
                    due_at: str | None, source_event_id: str | None = None,
                    kind: str = "promise") -> str:
    cid = new_id()
    c.execute(
        "insert into commitment(id, from_id, to_id, kind, description, due_at, source_event_id) "
        "values(?,?,?,?,?,?,?)", (cid, from_id, to_id, kind, description, due_at, source_event_id))
    return cid


def add_commitment(vault: Vault, entity_id: str, description: str, due_at: str | None,
                   owner: str = "me") -> str:
    ego = ego_id(vault)
    with vault.tx() as c:
        from_id, to_id = (ego, entity_id) if owner != "them" else (entity_id, ego)
        cid = _add_commitment(c, from_id, to_id, description.strip(), due_at or None)
        vault.audit(c, "write", entity_id, {"op": "add_commitment", "id": cid})
        return cid


def set_commitment_status(vault: Vault, commitment_id: str, status: str) -> None:
    with vault.tx() as c:
        c.execute("update commitment set status=?, done_at=case when ?='done' "
                  "then datetime('now') end where id=?", (status, status, commitment_id))
        vault.audit(c, "write", commitment_id, {"op": "commitment_status", "status": status})


def commitments(vault: Vault, entity_id: str | None = None, status: str = "open") -> list[dict]:
    ego = vault.get_meta("ego_id")
    where, params = ["c.status=?"], [status]
    if entity_id:
        where.append("(c.from_id=? or c.to_id=?)")
        params += [entity_id, entity_id]
    rows = vault.all(
        f"""select c.*, f.display_name as from_name, t.display_name as to_name
            from commitment c join entity f on f.id=c.from_id left join entity t on t.id=c.to_id
            where {' and '.join(where)}
            order by c.due_at is null, c.due_at, c.created_at""", params)
    for r in rows:
        r["mine"] = r["from_id"] == ego
        r["other_id"] = r["to_id"] if r["mine"] else r["from_id"]
        r["other_name"] = r["to_name"] if r["mine"] else r["from_name"]
    return rows


# ── search and duplicate detection ───────────────────────────────────────────


def _context(vault: Vault, entity_id: str, type_id: str) -> str:
    bits = []
    if type_id == "person":
        for pid in ("employed_by", "lives_in"):
            v = current_value(vault, entity_id, pid)
            if v:
                bits.append(v["object_name"])
        if not bits:
            t = current_value(vault, entity_id, "title")
            if t:
                bits.append(t["value_text"])
    elif type_id in ("organization", "place"):
        v = current_value(vault, entity_id, "located_in")
        if v:
            bits.append(v["object_name"])
    elif type_id == "event":
        ev = vault.one("select start_at from event_detail where entity_id=?", (entity_id,))
        if ev:
            bits.append(ev["start_at"][:10])
    return " · ".join(bits)


def search_entities(vault: Vault, q: str, type_id: str | None = None,
                    limit: int = 8, include_ego: bool = False) -> list[dict]:
    norm = normalize_name(q)
    if not norm:
        return []
    tokens = norm.split()
    variants = {tokens[0], _canon_token(tokens[0])}
    variants |= {k for k, v in NICKNAMES.items() if v == _canon_token(tokens[0])}
    clauses = " or ".join(["n.name_norm like ?"] * len(variants))
    params: list[Any] = [f"%{v}%" for v in variants]
    where = f"e.status='active' and ({clauses})"
    if type_id:
        where += " and e.type_id=?"
        params.append(type_id)
    if not include_ego:
        where += " and coalesce(e.engagement,'')<>'ego'"
    rows = vault.all(
        f"""select e.id, e.display_name, e.type_id, e.engagement, n.name_norm, n.kind as name_kind
            from entity_name n join entity e on e.id=n.entity_id where {where}""", params)
    best: dict[str, dict] = {}
    for r in rows:
        name_tokens = r["name_norm"].split()
        score = 0.0
        for tok in tokens:
            canon = _canon_token(tok)
            if any(nt == tok or _canon_token(nt) == canon for nt in name_tokens):
                score += 3
            elif any(nt.startswith(tok) for nt in name_tokens):
                score += 2
            elif tok in r["name_norm"]:
                score += 1
            else:
                score -= 2
        if r["name_norm"].startswith(norm):
            score += 2
        score += {"contact": 1.0, "acquaintance": 0.5}.get(r["engagement"] or "", 0)
        if r["id"] not in best or best[r["id"]]["score"] < score:
            best[r["id"]] = {**r, "score": score}
    ranked = sorted((b for b in best.values() if b["score"] > 0),
                    key=lambda b: (-b["score"], b["display_name"]))[:limit]
    for r in ranked:
        r["context"] = _context(vault, r["id"], r["type_id"])
        r["matched_alias"] = r["name_kind"] != "primary"
    return ranked


def duplicate_candidates(vault: Vault, type_id: str, name: str,
                         idents: Iterable[tuple[str, str]] = ()) -> list[dict]:
    """Existing entities that might be the same as a new one, with the reason."""
    found: dict[str, dict] = {}

    def add(row, reason):
        if row["id"] not in found:
            found[row["id"]] = {"id": row["id"], "display_name": row["display_name"],
                                "type_id": row["type_id"], "reasons": []}
        if reason not in found[row["id"]]["reasons"]:
            found[row["id"]]["reasons"].append(reason)

    for scheme, value in idents:
        norm = normalize_identifier(scheme, value or "")
        if not norm:
            continue
        for row in vault.all(
                "select e.id, e.display_name, e.type_id from identifier i join entity e "
                "on e.id=i.entity_id where i.scheme=? and i.value=? and e.status='active'",
                (scheme, norm)):
            add(row, f"same {scheme}")

    target = normalize_name(name)
    t_tokens = target.split()
    if t_tokens:
        rows = vault.all(
            "select e.id, e.display_name, e.type_id, n.name_norm from entity_name n "
            "join entity e on e.id=n.entity_id where e.status='active' and e.type_id=? "
            "and coalesce(e.engagement,'')<>'ego'", (type_id,))
        for row in rows:
            other = row["name_norm"]
            o_tokens = other.split()
            if other == target:
                add(row, "same name")
                continue
            if len(t_tokens) > 1 and len(o_tokens) > 1 and t_tokens[-1] == o_tokens[-1]:
                a, b = t_tokens[0], o_tokens[0]
                if _canon_token(a) == _canon_token(b):
                    add(row, "nickname of the same name")
                    continue
                if a[0] == b[0] and (len(a) == 1 or len(b) == 1):
                    add(row, "same initial and family name")
                    continue
            if difflib.SequenceMatcher(None, target, other).ratio() >= 0.85:
                add(row, "similar spelling")
    for f in found.values():
        f["context"] = _context(vault, f["id"], f["type_id"])
    return list(found.values())


def list_entities(vault: Vault, type_id: str | None = None, engagement: str | None = None,
                  tag: str | None = None, q: str | None = None) -> list[dict]:
    where, params = ["e.status='active'", "e.type_id<>'event'",
                     "coalesce(e.engagement,'')<>'ego'"], []
    if type_id:
        where.append("e.type_id=?")
        params.append(type_id)
    if engagement:
        where.append("e.engagement=?")
        params.append(engagement)
    if tag:
        ns, name = _parse_tag(tag)
        where.append("exists(select 1 from tagging g join tag t on t.id=g.tag_id where "
                     "g.target_kind='entity' and g.target_id=e.id and t.namespace=? and t.name=?)")
        params += [ns, name]
    if q:
        where.append("exists(select 1 from entity_name n where n.entity_id=e.id and "
                     "n.name_norm like ?)")
        params.append(f"%{normalize_name(q)}%")
    rows = vault.all(
        f"select e.* from entity e where {' and '.join(where)} order by e.display_name collate nocase",
        params)
    for r in rows:
        r["context"] = _context(vault, r["id"], r["type_id"])
        r["tags"] = tags_for(vault, "entity", r["id"])
    return rows


# ── export ───────────────────────────────────────────────────────────────────


def export_all(vault: Vault) -> dict:
    tables = [r["name"] for r in vault.all(
        "select name from sqlite_master where type='table' and name not like 'sqlite_%' "
        "and name not like '%_fts%' and name not in ('embedding') order by name")]
    return {
        "format": "ties-export",
        "format_version": 1,
        "exported_at": date.today().isoformat(),
        "schema_version": vault.get_meta("schema_version"),
        "tables": {t: vault.all(f'select * from "{t}"') for t in tables},
    }


# ── links (the per-entity link library) ──────────────────────────────────────


def _add_link(c, entity_id: str, url: str, note: str | None = None,
              title: str | None = None) -> str | None:
    url = (url or "").strip()
    if not url:
        return None
    if not re.match(r"^[a-z]+://", url):
        url = "https://" + url
    existing = c.execute(
        "select s.id from source s join entity_source es on es.source_id=s.id "
        "where es.entity_id=? and s.uri=? and es.role='link'", (entity_id, url)).fetchone()
    if existing:
        return existing["id"]
    sid = _create_source(c, "web", note or "", title=title or url, uri=url)
    c.execute("insert into entity_source(entity_id, source_id, role, note) values(?,?,?,?)",
              (entity_id, sid, "link", (note or "").strip() or None))
    return sid


def add_link(vault: Vault, entity_id: str, url: str, note: str | None = None) -> str | None:
    with vault.tx() as c:
        sid = _add_link(c, entity_id, url, note)
        vault.audit(c, "write", entity_id, {"op": "add_link"})
        return sid


def remove_link(vault: Vault, entity_id: str, source_id: str) -> None:
    with vault.tx() as c:
        c.execute("delete from entity_source where entity_id=? and source_id=? and role='link'",
                  (entity_id, source_id))
        vault.audit(c, "write", entity_id, {"op": "remove_link"})


def links(vault: Vault, entity_id: str) -> list[dict]:
    return vault.all(
        "select s.id, s.uri, s.title, es.note, es.added_at from entity_source es "
        "join source s on s.id=es.source_id where es.entity_id=? and es.role='link' "
        "order by es.added_at desc", (entity_id,))


# ── inferences: confirm and reject ───────────────────────────────────────────


def confirm_claim(vault: Vault, claim_id: str, basis: str = "observed") -> None:
    """Accept a proposed claim. Inferences become facts with the basis you choose."""
    with vault.tx() as c:
        row = c.execute("select * from claim where id=?", (claim_id,)).fetchone()
        if not row:
            raise ValueError("Unknown claim")
        quals = json.loads(row["qualifiers"] or "{}")
        quals["confirmed_from"] = row["asserted_by"]
        new_basis = basis if row["basis"] == "inferred" else row["basis"]
        c.execute("update claim set status='accepted', basis=?, qualifiers=? where id=?",
                  (new_basis, json.dumps(quals), claim_id))
        pred = predicate(c, row["predicate_id"])
        if pred and pred["cardinality"] == "one":
            c.execute("update claim set status='superseded', superseded_by=? where subject_id=? "
                      "and predicate_id=? and status='accepted' and id<>?",
                      (claim_id, row["subject_id"], row["predicate_id"], claim_id))
        vault.audit(c, "write", row["subject_id"], {"op": "confirm_claim", "claim": claim_id})
        textindex.reindex_entity(c, row["subject_id"])
        textindex.reindex_entity(c, row["object_id"])


def reject_claim(vault: Vault, claim_id: str) -> None:
    """Reject a proposed claim. Rule-based suggestions for the same pair do not come back."""
    retract_claim(vault, claim_id)


def resolve_dispute(vault: Vault, claim_id: str, keep_new: bool) -> None:
    """A disputed claim (an import that disagreed with a hand-entered fact)."""
    with vault.tx() as c:
        row = c.execute("select * from claim where id=?", (claim_id,)).fetchone()
        if not row:
            raise ValueError("Unknown claim")
        if keep_new:
            c.execute("update claim set status='superseded', superseded_by=? where subject_id=? "
                      "and predicate_id=? and status='accepted'",
                      (claim_id, row["subject_id"], row["predicate_id"]))
            c.execute("update claim set status='accepted' where id=?", (claim_id,))
        else:
            c.execute("update claim set status='rejected' where id=?", (claim_id,))
        vault.audit(c, "write", row["subject_id"], {"op": "resolve_dispute", "keep_new": keep_new})
        textindex.reindex_entity(c, row["subject_id"])


def proposed_claims(vault: Vault, status: str = "proposed") -> list[dict]:
    rows = _claim_rows(vault, "cl.status=?", (status,))
    for r in rows:
        _orient(vault, r, r["subject_id"])
        if status == "disputed":
            cur = _claim_rows(vault, "cl.subject_id=? and cl.predicate_id=? and cl.status='accepted'",
                              (r["subject_id"], r["predicate_id"]))
            r["current"] = cur[-1] if cur else None
    return rows


# ── the post-meeting debrief ─────────────────────────────────────────────────


def set_next_contact(c, entity_id: str, when: str | None) -> None:
    c.execute("update relationship_profile set next_contact_at=?, next_contact_set_at=date('now') "
              "where entity_id=?", (when or None, entity_id))


def debrief_event(vault: Vault, event_id: str, *, summary: str = "",
                  learned: Iterable[dict] = (), inferences_: Iterable[dict] = (),
                  follow_ups: Iterable[dict] = (), mentioned: Iterable[dict] = (),
                  links_: Iterable[dict] = (), next_contact: str | None = None,
                  mark_occurred: bool = True) -> dict:
    """Record everything that came out of a meeting in one step.

    learned / inferences_: {subject, predicate, value | object, basis, confidence, why}
    follow_ups: {owner: me|them, who, text, due}
    mentioned: {ref, predicate, to, basis}  (a new or existing person and how they relate)
    links_: {url, note, target}
    Subjects and targets are entity ids, or "event" for the meeting itself.
    """
    ego = ego_id(vault)
    counts = {"facts": 0, "inferences": 0, "follow_ups": 0, "people": 0, "links": 0}
    with vault.tx() as c:
        ev = c.execute("select e.display_name from entity e join event_detail ed on "
                       "ed.entity_id=e.id where e.id=?", (event_id,)).fetchone()
        if not ev:
            raise ValueError("Unknown event")
        attendees = [r["entity_id"] for r in c.execute(
            "select entity_id from participation where event_id=? and entity_id<>?",
            (event_id, ego))]
        src = _create_source(c, "note", summary.strip(), title=ev["display_name"]) \
            if summary.strip() else _create_source(c, "manual", f"debrief {event_id}")
        if summary.strip():
            old = c.execute("select s.content from event_detail ed join source s on "
                            "s.id=ed.notes_source_id where ed.entity_id=?", (event_id,)).fetchone()
            combined = ((old["content"] + "\n\n") if old and old["content"] else "") + summary.strip()
            nsrc = _create_source(c, "note", combined, title=ev["display_name"])
            c.execute("update event_detail set notes_source_id=? where entity_id=?",
                      (nsrc, event_id))
        if mark_occurred:
            c.execute("update event_detail set status='occurred' where entity_id=? and "
                      "status='planned'", (event_id,))

        def subject_of(ref):
            return event_id if ref in (None, "", "event") else resolve_ref(c, vault, ref, "person",
                                                                           "acquaintance")

        for row, is_inference in [(r, False) for r in learned] + [(r, True) for r in inferences_]:
            pid = (row.get("predicate") or "").strip()
            if not pid or not (row.get("value") or row.get("object")):
                continue
            pred = predicate(c, pid)
            if not pred:
                continue
            subj = subject_of(row.get("subject"))
            obj = None
            if pred["range_kind"] == "entity":
                types = json.loads(pred["range_types"] or '["person"]')
                obj = resolve_ref(c, vault, row.get("object"), types[0] if types[0] != "*"
                                  else "person", "reference")
            quals = {"from_event": event_id}
            if row.get("why"):
                quals["why"] = row["why"]
            _add_claim(c, subj, pid, value=row.get("value") if not obj else None, object_id=obj,
                       basis="inferred" if is_inference else (row.get("basis") or "told"),
                       confidence=float(row.get("confidence") or (0.6 if is_inference else 1.0)),
                       qualifiers=quals, source_id=src)
            counts["inferences" if is_inference else "facts"] += 1

        for fu in follow_ups:
            text = (fu.get("text") or "").strip()
            if not text:
                continue
            who = fu.get("who") or _mentioned_participant(c, text, attendees)
            mine = fu.get("owner", "me") != "them"
            from_id, to_id = (ego, who) if mine else (who or ego, ego)
            _add_commitment(c, from_id, to_id, text, fu.get("due") or None, event_id)
            counts["follow_ups"] += 1

        for m in mentioned:
            if not m.get("ref"):
                continue
            pid = resolve_ref(c, vault, m["ref"], "person", "acquaintance")
            c.execute("insert or ignore into entity_source(entity_id, source_id, role) "
                      "values(?,?,'mention')", (pid, src))
            if m.get("predicate") and m.get("to"):
                to = resolve_ref(c, vault, m["to"], "person", "acquaintance")
                if to and to != pid:
                    _add_claim(c, pid, m["predicate"], object_id=to,
                               basis=m.get("basis") or "told",
                               qualifiers={"from_event": event_id}, source_id=src)
            counts["people"] += 1

        for ln in links_:
            if not (ln.get("url") or "").strip():
                continue
            target = subject_of(ln.get("target"))
            _add_link(c, target, ln["url"], ln.get("note"))
            counts["links"] += 1

        if next_contact:
            for a in attendees:
                c.execute("insert or ignore into relationship_profile(entity_id) "
                          "select id from entity where id=? and engagement='contact'", (a,))
                set_next_contact(c, a, next_contact)
        vault.audit(c, "write", event_id, {"op": "debrief", **counts})
        for a in attendees:
            textindex.reindex_entity(c, a)
    return counts


def find_by_name(c, name: str, type_id: str | None = None) -> list[dict]:
    norm = normalize_name(name)
    sql = ("select distinct e.id, e.type_id, e.engagement, e.display_name from entity_name n "
           "join entity e on e.id=n.entity_id where n.name_norm=? and e.status='active'")
    params: list = [norm]
    if type_id:
        sql += " and e.type_id=?"
        params.append(type_id)
    return c.execute(sql, params).fetchall()
