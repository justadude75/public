"""Standard importers: vCard contacts, iCalendar events, CSV, and JSON restore.

Each turns its input into ImportRecords for importer.apply, so all of them get the
preview, safe re-import, conflict handling and batch rollback.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path

from .importer import ClaimSpec, EventSpec, ImportRecord, LinkSpec, Ref

# ── vCard ────────────────────────────────────────────────────────────────────


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _unescape(v: str) -> str:
    return v.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace(
        "\\;", ";").replace("\\\\", "\\").strip()


def _prop(line: str) -> tuple[str, dict, str] | None:
    if ":" not in line:
        return None
    head, value = line.split(":", 1)
    parts = head.split(";")
    name = parts[0].split(".")[-1].upper()
    params = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v.strip('"')
        else:
            params.setdefault("TYPE", p)
    return name, params, value


def _vcard_date(v: str) -> str | None:
    v = v.strip()
    m = re.match(r"^(\d{4})-?(\d{2})-?(\d{2})", v)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"^--(\d{2})-?(\d{2})", v)
    if m:
        return f"--{m.group(1)}-{m.group(2)}"
    return None


def parse_vcard(text: str, as_contacts: bool = False) -> list[ImportRecord]:
    records = []
    card: list[tuple[str, dict, str]] | None = None
    for line in _unfold(text):
        if line.strip().upper() == "BEGIN:VCARD":
            card = []
        elif line.strip().upper() == "END:VCARD":
            if card is not None:
                rec = _card_to_record(card, as_contacts)
                if rec:
                    records.append(rec)
            card = None
        elif card is not None:
            p = _prop(line)
            if p:
                card.append(p)
    return records


def _card_to_record(props, as_contacts: bool) -> ImportRecord | None:
    get = {}
    for name, params, value in props:
        get.setdefault(name, []).append((params, value))
    name = _unescape(get.get("FN", [({}, "")])[0][1])
    if not name and "N" in get:
        n = get["N"][0][1].split(";") + ["", ""]
        name = " ".join(x for x in (_unescape(n[1]), _unescape(n[0])) if x)
    org_name = _unescape(get.get("ORG", [({}, "")])[0][1].split(";")[0]) if "ORG" in get else ""
    if not name and org_name:
        return ImportRecord(origin=f"vcard:org:{org_name.lower()}", entity_type="organization",
                            name=org_name, engagement="reference")
    if not name:
        return None
    emails = [_unescape(v) for _, v in get.get("EMAIL", [])]
    uid = get.get("UID", [({}, "")])[0][1].strip()
    origin = "vcard:" + (uid or hashlib.sha1((name + "|" + ",".join(emails)).encode())
                         .hexdigest()[:16])
    rec = ImportRecord(origin=origin, entity_type="person", name=name,
                       engagement="contact" if as_contacts else "acquaintance",
                       profile={"tier": "active"} if as_contacts else None)
    rec.identifiers = [("email", e) for e in emails]
    rec.identifiers += [("phone", _unescape(v)) for _, v in get.get("TEL", [])]
    for _, v in get.get("NICKNAME", []):
        rec.aliases += [a.strip() for a in _unescape(v).split(",") if a.strip()]
    if org_name:
        rec.claims.append(ClaimSpec("employed_by", obj=Ref(name=org_name, type="organization"),
                                    basis="public"))
    for _, v in get.get("TITLE", []):
        rec.claims.append(ClaimSpec("title", value=_unescape(v), basis="public"))
    for _, v in get.get("BDAY", []):
        d = _vcard_date(v)
        if d:
            rec.claims.append(ClaimSpec("birthday", value=d, basis="public"))
    for params, v in get.get("ADR", []):
        parts = v.split(";") + [""] * 7
        city = _unescape(parts[3])
        if city:
            rec.claims.append(ClaimSpec("lives_in", obj=Ref(name=city, type="place"), basis="public"))
            break
    for _, v in get.get("URL", []):
        rec.links.append(LinkSpec(_unescape(v)))
    for _, v in get.get("CATEGORIES", []):
        rec.tags += [t.strip().lower() for t in _unescape(v).split(",") if t.strip()]
    notes = [_unescape(v) for _, v in get.get("NOTE", [])]
    rec.notes = "\n".join(n for n in notes if n) or None
    return rec


# ── iCalendar ───────────────────────────────────────────────────────────────


def _ical_dt(value: str, params: dict) -> str | None:
    v = value.strip()
    m = re.match(r"^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})?(Z)?)?$", v)
    if not m:
        return None
    y, mo, d, hh, mm, ss, z = m.groups()
    if hh is None or params.get("VALUE") == "DATE":
        return f"{y}-{mo}-{d}"
    dt = datetime(int(y), int(mo), int(d), int(hh), int(mm))
    if z:
        dt = dt.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M")


def parse_ical(text: str, my_emails: set[str], today: date | None = None,
               only_with_others: bool = True, max_contact_size: int = 8) -> list[ImportRecord]:
    today = today or date.today()
    records = []
    ev: list | None = None
    depth = 0
    for line in _unfold(text):
        up = line.strip().upper()
        if up == "BEGIN:VEVENT":
            ev, depth = [], 0
            continue
        if ev is not None and up.startswith("BEGIN:"):
            depth += 1
        if ev is not None and up.startswith("END:") and up != "END:VEVENT":
            depth -= 1
            continue
        if up == "END:VEVENT":
            if ev is not None:
                rec = _event_record(ev, my_emails, today, only_with_others, max_contact_size)
                if rec:
                    records.append(rec)
            ev = None
            continue
        if ev is not None and depth == 0:
            p = _prop(line)
            if p:
                ev.append(p)
    return records


def _event_record(props, my_emails, today, only_with_others, max_size) -> ImportRecord | None:
    get = {}
    for name, params, value in props:
        get.setdefault(name, []).append((params, value))
    if "DTSTART" not in get:
        return None
    params, value = get["DTSTART"][0]
    start = _ical_dt(value, params)
    if not start:
        return None
    people = []
    for name in ("ORGANIZER", "ATTENDEE"):
        for params, value in get.get(name, []):
            email = re.sub(r"^mailto:", "", value.strip(), flags=re.I).lower()
            if email in my_emails:
                continue
            cn = params.get("CN", "").strip() or email.split("@")[0]
            people.append(Ref(name=cn, type="person", engagement="acquaintance",
                              identifiers=[("email", email)] if "@" in email else []))
    if only_with_others and not people:
        return None
    summary = _unescape(get.get("SUMMARY", [({}, "")])[0][1]) or None
    status_raw = get.get("STATUS", [({}, "")])[0][1].strip().upper()
    day = date.fromisoformat(start[:10])
    status = "cancelled" if status_raw == "CANCELLED" else ("planned" if day >= today else "occurred")
    location = _unescape(get.get("LOCATION", [({}, "")])[0][1])
    loc_ref = Ref(name=location, type="place") if location and len(location) <= 60 and \
        not re.search(r"https?://|zoom|meet\.google|teams", location, re.I) else None
    desc = _unescape(get.get("DESCRIPTION", [({}, "")])[0][1])
    online = bool(re.search(r"zoom\.us|meet\.google|teams\.microsoft", desc + location, re.I))
    uid = get.get("UID", [({}, "")])[0][1].strip() or hashlib.sha1(
        (start + (summary or "")).encode()).hexdigest()[:16]
    rec = ImportRecord(
        origin=f"ical:{uid}:{start}", kind="event", title=summary,
        notes=desc[:4000] or None,
        event=EventSpec(start=start, kind="video" if online else "meeting",
                        channel="video" if online else "in_person", status=status,
                        title=summary, location=loc_ref, participants=people,
                        counts_as_contact=len(people) <= max_size))
    if "RRULE" in get:
        rec.warnings.append(f"“{summary}” repeats; only its first occurrence was imported.")
    return rec


# ── CSV ──────────────────────────────────────────────────────────────────────

CSV_FIELDS = ["", "name", "first_name", "last_name", "email", "phone", "organization", "title",
              "city", "birthday", "url", "tags", "notes", "how_met", "tier", "aliases"]
_HEADER_GUESS = {
    "name": ["name", "full name", "display name", "contact"],
    "first_name": ["first name", "given name", "firstname", "first"],
    "last_name": ["last name", "family name", "surname", "lastname", "last"],
    "email": ["email", "e-mail", "email address", "e-mail address", "e-mail 1 - value", "mail"],
    "phone": ["phone", "mobile", "phone number", "telephone", "cell", "phone 1 - value"],
    "organization": ["company", "organization", "organisation", "org", "employer",
                     "organization 1 - name", "organization name"],
    "title": ["title", "position", "job title", "role", "organization 1 - title"],
    "city": ["city", "location", "town", "home city"],
    "birthday": ["birthday", "birth date", "date of birth", "dob"],
    "url": ["url", "linkedin", "profile", "website", "link"],
    "tags": ["tags", "labels", "groups", "group membership", "categories"],
    "notes": ["notes", "note", "comments", "description"],
    "how_met": ["connected on", "met", "how met", "how we met"],
    "tier": ["tier", "circle"],
    "aliases": ["nickname", "nicknames", "aliases", "other names"],
}


def guess_mapping(headers: list[str]) -> dict[str, str]:
    mapping = {}
    for h in headers:
        key = h.strip().lower()
        for field, options in _HEADER_GUESS.items():
            if key in options and field not in mapping.values():
                mapping[h] = field
                break
    return mapping


def _read_csv(text: str) -> tuple[list[str], list[dict]]:
    sample = text[:4096]
    # LinkedIn exports start with a few lines of notes before the header row.
    lines = text.splitlines()
    start = 0
    for i, line in enumerate(lines[:10]):
        if line.count(",") >= 2 or line.count(";") >= 2 or line.count("\t") >= 2:
            start = i
            break
    text = "\n".join(lines[start:])
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    rows = [r for r in reader]
    return reader.fieldnames or [], rows


def parse_csv(text: str, mapping: dict[str, str] | None = None,
              as_contacts: bool = False) -> tuple[list[ImportRecord], dict[str, str], list[str]]:
    headers, rows = _read_csv(text)
    mapping = mapping or guess_mapping(headers)
    records = []
    for row in rows:
        vals: dict[str, str] = {}
        for col, field in mapping.items():
            if field and (row.get(col) or "").strip():
                vals[field] = row[col].strip()
        name = vals.get("name") or " ".join(x for x in (vals.get("first_name"), vals.get("last_name")) if x)
        if not name:
            continue
        key = vals.get("email") or vals.get("url") or name
        rec = ImportRecord(origin=f"csv:{hashlib.sha1(key.lower().encode()).hexdigest()[:16]}",
                           entity_type="person", name=name,
                           engagement="contact" if as_contacts else "acquaintance")
        if as_contacts or vals.get("tier"):
            rec.profile = {"tier": vals.get("tier", "active").lower()}
        if vals.get("email"):
            rec.identifiers.append(("email", vals["email"]))
        if vals.get("phone"):
            rec.identifiers.append(("phone", vals["phone"]))
        if vals.get("organization"):
            rec.claims.append(ClaimSpec("employed_by", obj=Ref(name=vals["organization"],
                                                                type="organization"), basis="public"))
        if vals.get("title"):
            rec.claims.append(ClaimSpec("title", value=vals["title"], basis="public"))
        if vals.get("city"):
            rec.claims.append(ClaimSpec("lives_in", obj=Ref(name=vals["city"], type="place"),
                                        basis="public"))
        if vals.get("birthday"):
            rec.claims.append(ClaimSpec("birthday", value=vals["birthday"], basis="told"))
        if vals.get("url"):
            rec.links.append(LinkSpec(vals["url"]))
        if vals.get("tags"):
            rec.tags = [t.strip().lower() for t in re.split(r"[,;|]|:::", vals["tags"]) if t.strip()]
        if vals.get("aliases"):
            rec.aliases = [a.strip() for a in vals["aliases"].split(",") if a.strip()]
        if vals.get("how_met"):
            rec.profile = {**(rec.profile or {}), "how_met": vals["how_met"]} if as_contacts \
                else rec.profile
            rec.attrs["how_met"] = vals["how_met"]
        rec.notes = vals.get("notes")
        records.append(rec)
    return records, mapping, headers


# ── JSON restore ─────────────────────────────────────────────────────────────

RESTORE_ORDER = ["meta", "source", "entity", "entity_name", "identifier", "claim",
                 "claim_evidence", "event_detail", "participation", "tag", "tagging",
                 "relationship_profile", "commitment", "entity_source", "import_batch",
                 "import_item", "md_note", "summary", "outreach_state", "resolution_decision",
                 "audit_log"]


def restore(vault, data: dict) -> dict[str, int]:
    """Load a ties JSON export into a brand-new vault (one with no entries yet)."""
    from . import textindex

    if data.get("format") != "ties-export":
        raise ValueError("This is not a ties export file.")
    existing = vault.scalar("select count(*) from entity")
    if existing:
        raise ValueError("Restore only works into a new, empty vault.")
    tables = data.get("tables", {})
    counts = {}
    with vault.tx() as c:
        c.execute("PRAGMA defer_foreign_keys = ON")
        known = {r["name"] for r in c.execute("select name from sqlite_master where type='table'")}
        order = RESTORE_ORDER + [t for t in tables if t not in RESTORE_ORDER]
        for t in order:
            rows = tables.get(t) or []
            if t not in known or t in ("entity_type", "predicate") or "_fts" in t or not rows:
                continue
            if t == "meta":
                rows = [r for r in rows if r["key"] != "schema_version"]
            cols = list(rows[0].keys())
            sql = (f'insert or replace into "{t}"({",".join(cols)}) '
                   f'values({",".join("?" * len(cols))})')
            for r in rows:
                c.execute(sql, [r[k] for k in cols])
            counts[t] = len(rows)
    textindex.rebuild(vault)
    return counts


def read_upload(path_or_text: str | Path) -> str:
    p = Path(str(path_or_text))
    if len(str(path_or_text)) < 4096 and p.exists():
        return p.read_text(encoding="utf-8", errors="replace")
    return str(path_or_text)


def my_emails(vault) -> set[str]:
    ego = vault.get_meta("ego_id")
    return {r["value"] for r in vault.all(
        "select value from identifier where entity_id=? and scheme='email'", (ego,))}
