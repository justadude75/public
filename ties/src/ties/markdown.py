"""Read Obsidian-style Markdown notes into ImportRecords (plan section 4.11, Appendix C).

Plain Markdown is enough; no plugins are assumed. With `write_gid` on (the
default) the importer adds one `gid:` line to a typed note's frontmatter so the
note keeps its link to its entity after a rename or move.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from .importer import ClaimSpec, CommitmentSpec, EventSpec, ImportRecord, LinkSpec, Ref
from .repo import new_id

WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]")
INLINE_FIELD = re.compile(
    r"^\s*(?:[-*+]\s+)?(?:\[\[(?P<subj>[^\]|]+)(?:\|[^\]]*)?\]\]\s+)?"
    r"(?P<key>[A-Za-z][\w-]*)::\s*(?P<val>.*?)\s*(?:\{(?P<q>[^{}]*)\})?\s*$")
TASK = re.compile(r"^\s*[-*+]\s+\[(?P<done>[ xX])\]\s+(?P<text>.*)$")
BARE_LINK_BULLET = re.compile(r"^(?P<indent>\s*)[-*+]\s+\[\[(?P<name>[^\]|]+)(?:\|[^\]]*)?\]\]"
                              r"\s*(?:[—–-]\s*(?P<rest>[^{]*))?\s*(?:\{(?P<q>[^{}]*)\})?\s*$")
URL_LINE = re.compile(r"^\s*(?:[-*+]\s+)?(?P<url>https?://\S+)\s*(?:[—–#-]+\s*(?P<note>.*))?$")
DUE = re.compile(r"(?:due::\s*|📅\s*)(\d{4}-\d{2}-\d{2})")
DATE_NAME = re.compile(r"(\d{4}-\d{2}-\d{2})")
TIME_PREFIX = re.compile(r"^\s*[-*+]\s+(\d{1,2}:\d{2})\s+")

INTERACTION_KEYS = {"met": ("meeting", "in_person"), "call": ("call", "call"),
                    "video": ("video", "video"), "message": ("message", "message"),
                    "email": ("email", "email")}

KEY_MAP = {
    "works_at": "employed_by", "employer": "employed_by", "works_for": "employed_by",
    "company": "employed_by", "joining": "employed_by", "employed_by": "employed_by",
    "worked_with": "colleague_of", "colleague": "colleague_of", "works_with": "colleague_of",
    "friend": "friend_of", "partner": "partner_of", "spouse": "partner_of",
    "parent": "parent_of", "child": "child_of", "mentor": "mentor_of", "mentee": "mentee_of",
    "lives": "lives_in", "lives_in": "lives_in", "city": "lives_in", "based_in": "lives_in",
    "location": "located_in", "located": "located_in",
    "studied": "studied_at", "school": "studied_at", "alma_mater": "studied_at",
    "member": "member_of", "invested": "invested_in", "investor_in": "invested_in",
    "likes": "interest", "interested_in": "interest", "interests": "interest",
    "prefers": "preference", "family": "family_detail", "role": "title", "position": "title",
    "headcount": "size", "employees": "size", "needs": "seeking", "looking_for": "seeking",
    "offers": "offering", "can_help_with": "offering", "about": "description",
}
ENTITY_PREDICATES = {"employed_by", "colleague_of", "friend_of", "partner_of", "parent_of",
                     "child_of", "mentor_of", "mentee_of", "lives_in", "located_in", "studied_at",
                     "member_of", "invested_in", "knows", "family_of", "introduced_by",
                     "attended", "part_of", "related_to", "shares_connector"}
VALUE_PREDICATES = {"title", "birthday", "interest", "preference", "family_detail", "industry",
                    "description", "fact", "size", "seeking", "offering"}
ENTITY_TYPES = {"person": "person", "people": "person", "contact": "person",
                "org": "organization", "organization": "organization", "company": "organization",
                "place": "place", "city": "place", "location": "place",
                "meeting": "event", "event": "event", "interaction": "event"}
FRONTMATTER_SKIP = {"type", "gid", "aliases", "alias", "tags", "tag", "cssclasses",
                    "created", "updated", "modified"}
SECTION_BASIS = {"learned": "told", "inferences": "inferred", "inference": "inferred"}


@dataclass
class ParsedNote:
    path: Path
    rel: str
    gid: str | None
    needs_gid: bool
    content_hash: str
    note_type: str | None
    records: list[ImportRecord] = field(default_factory=list)


def _flatten(value: Any) -> Any:
    """YAML reads an unquoted [[Name]] as a nested list; turn it back into a wikilink."""
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], list) \
            and len(value[0]) == 1 and isinstance(value[0][0], str):
        return f"[[{value[0][0]}]]"
    if isinstance(value, list):
        return [_flatten(v) for v in value]
    if isinstance(value, (date,)):
        return value.isoformat()
    return value


def split_frontmatter(text: str) -> tuple[dict, str, bool]:
    if text.startswith("---"):
        parts = text.split("\n")
        for i in range(1, len(parts)):
            if parts[i].strip() == "---":
                raw = "\n".join(parts[1:i])
                try:
                    data = yaml.safe_load(raw) or {}
                except yaml.YAMLError:
                    data = {}
                if not isinstance(data, dict):
                    data = {}
                return {k: _flatten(v) for k, v in data.items()}, "\n".join(parts[i + 1:]), True
    return {}, text, False


def _links(text: str) -> list[str]:
    return [m.group(1).strip() for m in WIKILINK.finditer(str(text or ""))]


def _ref(name: str, type_: str | None = None, engagement: str = "reference",
         create: bool = True) -> Ref:
    return Ref(name=name.strip(), type=type_, engagement=engagement, create=create)


def _as_list(value: Any) -> list:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        out = []
        for v in value:
            out.extend(_as_list(v))
        return out
    if isinstance(value, str) and "," in value and "[[" not in value:
        return [v.strip() for v in value.split(",") if v.strip()]
    return [value]


def _quals(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        data = yaml.safe_load("{" + raw + "}")
        return {k: _flatten(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except yaml.YAMLError:
        return {"note": raw.strip()}


def _date_str(v: Any) -> str | None:
    if v in (None, ""):
        return None
    return v.isoformat() if isinstance(v, date) else str(v)


def _predicate_for(key: str, has_link: bool, has_text: bool) -> tuple[str, dict, bool]:
    """Pick the predicate for `key:: value`. Returns (predicate, qualifiers, use_link)."""
    k = key.lower().replace("-", "_")
    pid = KEY_MAP.get(k, k)
    if has_link and not has_text:
        if pid == "family_detail":
            return "family_of", {}, True
        if pid in ENTITY_PREDICATES:
            return pid, {}, True
        return "related_to", {"label": k}, True
    if has_link and pid in ENTITY_PREDICATES:
        return pid, {}, True
    if pid in VALUE_PREDICATES:
        return pid, {}, False
    if has_link:
        return "related_to", {"label": k}, True
    return "fact", {"label": k}, False


def _claim_from_field(key: str, val: str, q: dict, basis: str, subject: str | None) -> ClaimSpec | None:
    val = (val or "").strip()
    links = _links(val)
    rest = WIKILINK.sub("", val).strip(" ,;—-")
    pid, extra, use_link = _predicate_for(key, bool(links), bool(rest))
    quals = {**extra}
    spec = ClaimSpec(pid, basis=str(q.pop("basis", basis)),
                     subject=_ref(subject, None, "acquaintance") if subject else None)
    if "from" in q:
        spec.valid_from = _date_str(q.pop("from"))
    if "to" in q:
        spec.valid_to = _date_str(q.pop("to"))
    if "confidence" in q:
        try:
            spec.confidence = float(q.pop("confidence"))
        except (TypeError, ValueError):
            pass
    if q.pop("sensitive", None):
        quals["sensitive"] = True
    for k, v in q.items():
        quals[k] = ", ".join(_links(v)) if isinstance(v, str) and "[[" in v else v
    spec.qualifiers = quals
    if use_link:
        spec.obj = _ref(links[0])
        if rest:
            spec.qualifiers.setdefault("note", rest)
    else:
        text = WIKILINK.sub(lambda mm: mm.group(2) or mm.group(1), val).strip()
        if not text:
            return None
        spec.value = text
    return spec


def _task(line: str, default_counterpart: str | None) -> CommitmentSpec | None:
    m = TASK.match(line)
    if not m:
        return None
    text = m.group("text").strip()
    due = None
    d = DUE.search(text)
    if d:
        due = d.group(1)
        text = DUE.sub("", text).strip()
    owner, counterpart = "me", None
    theirs = re.match(r"^\[\[([^\]|]+)(?:\|[^\]]*)?\]\]\s*:\s*(.*)$", text)
    if theirs:
        owner, counterpart, text = "them", theirs.group(1), theirs.group(2)
    else:
        text = re.sub(r"^\((mine|me)\)\s*", "", text)
        names = _links(text)
        counterpart = names[0] if names else default_counterpart
    clean = WIKILINK.sub(lambda mm: mm.group(2) or mm.group(1), text).strip()
    clean = re.sub(r"^\((theirs|mine)\)\s*", "", clean)
    if not clean:
        return None
    return CommitmentSpec(clean, due=due, owner=owner,
                          counterpart=_ref(counterpart, "person", "acquaintance")
                          if counterpart else None,
                          done=m.group("done").lower() == "x")


def _interaction(line: str, day: str | None, origin: str) -> ImportRecord | None:
    m = INLINE_FIELD.match(TIME_PREFIX.sub("- ", line))
    if not m or m.group("key").lower() not in INTERACTION_KEYS or not day:
        return None
    kind, channel = INTERACTION_KEYS[m.group("key").lower()]
    t = TIME_PREFIX.match(line)
    start = f"{day}T{int(t.group(1).split(':')[0]):02d}:{t.group(1).split(':')[1]}" if t else day
    val = m.group("val")
    names = _links(val)
    if not names:
        return None
    q = _quals(m.group("q"))
    note = WIKILINK.sub(lambda mm: mm.group(2) or mm.group(1), val).strip(" ,")
    participants = [_ref(n, "person", "acquaintance") for n in names]
    location = None
    at = re.search(r"\bat\s+\[\[([^\]|]+)", val)
    if at:
        location = _ref(at.group(1), "place")
        participants = [p for p in participants if p.name != at.group(1).strip()]
    key = hashlib.sha1(line.strip().encode()).hexdigest()[:12]
    tags = [str(t) for t in _as_list(q.get("tags"))]
    return ImportRecord(origin=f"{origin}:line:{key}", kind="event", notes=note, tags=tags,
                        event=EventSpec(start=start, kind=kind, channel=channel,
                                        participants=participants, location=location,
                                        title=None))


def parse_note(path: Path, root: Path, known_gids: dict[str, str] | None = None) -> ParsedNote:
    text = path.read_text(encoding="utf-8")
    return parse_text(text, str(path.relative_to(root)), path, known_gids)


def parse_text(text: str, rel: str, path: Path, known_gids: dict[str, str] | None = None,
               fm_override: dict | None = None, origin_override: str | None = None) -> ParsedNote:
    fm, body, has_fm = split_frontmatter(text)
    if fm_override:
        fm = {**fm, **fm_override}
    raw_type = str(fm.get("type", "")).strip().lower()
    note_type = ENTITY_TYPES.get(raw_type)
    gid = str(fm["gid"]).strip() if fm.get("gid") else None
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    parsed = ParsedNote(path, rel, gid, False, digest, note_type)
    day_match = DATE_NAME.search(path.stem)
    note_day = _date_str(fm.get("date")) or (day_match.group(1) if day_match else None)

    if note_type is None:
        # Untyped notes: interaction lines (daily notes), facts about a named [[subject]],
        # and tasks that name a person.
        origin = origin_override or f"md:path:{rel}"
        claims = []
        for line in body.splitlines():
            fm_line = INLINE_FIELD.match(line)
            if fm_line and fm_line.group("subj") and \
                    fm_line.group("key").lower() not in INTERACTION_KEYS:
                spec = _claim_from_field(fm_line.group("key"), fm_line.group("val"),
                                         _quals(fm_line.group("q")), "observed",
                                         fm_line.group("subj"))
                if spec:
                    claims.append(spec)
        for line in body.splitlines():
            rec = _interaction(line, note_day, origin)
            if rec:
                parsed.records.append(rec)
        tasks = [t for t in (_task(l, None) for l in body.splitlines()) if t and t.counterpart]
        if tasks or claims:
            parsed.records.append(ImportRecord(origin=origin, kind="note", commitments=tasks,
                                               claims=claims, title=path.stem))
        return parsed

    if not gid:
        gid = (known_gids or {}).get(rel) or new_id()
        parsed.gid, parsed.needs_gid = gid, True
    origin = origin_override or f"md:{gid}"
    title = path.stem
    h1 = re.search(r"^#\s+(.+)$", body, re.M)
    rec = ImportRecord(origin=origin, title=title)
    rec.tags = [str(t).lstrip("#") for t in _as_list(fm.get("tags") or fm.get("tag"))]

    if note_type == "event":
        rec.kind = "event"
        start = note_day or date.today().isoformat()
        if fm.get("time"):
            hh, mm = (str(fm["time"]).split(":") + ["00"])[:2]
            start = f"{start}T{int(hh):02d}:{mm[:2]}"
        attendees = [_ref(n, "person", "acquaintance") for v in _as_list(fm.get("attendees"))
                     for n in _links(v) or [str(v)]]
        location = None
        if fm.get("location"):
            loc = _links(fm["location"]) or [str(fm["location"])]
            location = _ref(loc[0], "place")
        initiated = fm.get("initiated_by")
        rec.event = EventSpec(
            start=start, kind=str(fm.get("kind", "meeting")), status=str(fm.get("status", "occurred")),
            channel=fm.get("channel"), title=h1.group(1).strip() if h1 else title,
            location=location, participants=attendees,
            initiated_by="me" if str(initiated).lower() == "me" else ("them" if initiated else None),
            next_contact=_date_str(fm.get("next_contact")))
        default_counterpart = attendees[0].name if attendees else None
    else:
        rec.kind = "entity"
        rec.entity_type = note_type
        rec.name = title
        rec.aliases = [str(a) for a in _as_list(fm.get("aliases") or fm.get("alias"))]
        default_counterpart = None
        if note_type == "person":
            rec.engagement = str(fm.get("engagement") or ("contact" if fm.get("tier") else
                                                          "acquaintance"))
            profile = {}
            if fm.get("tier"):
                profile["tier"] = str(fm["tier"])
            if fm.get("cadence"):
                profile["cadence_days"] = int(fm["cadence"])
            if fm.get("closeness"):
                profile["closeness_self"] = int(fm["closeness"])
            if fm.get("how_met"):
                profile["how_met"] = ", ".join(_links(fm["how_met"])) or str(fm["how_met"])
            if fm.get("introduced_by"):
                names = _links(fm["introduced_by"]) or [str(fm["introduced_by"])]
                profile["introduced_by"] = _ref(names[0], "person", "acquaintance")
            rec.profile = profile or None
        for scheme in ("email", "phone"):
            for v in _as_list(fm.get(scheme)):
                rec.identifiers.append((scheme, str(v)))
        for v in _as_list(fm.get("website")):
            rec.identifiers.append(("url", str(v)))
        for key, value in fm.items():
            k = key.lower()
            if k in FRONTMATTER_SKIP or k in ("email", "phone", "website", "links", "tier",
                                              "cadence", "closeness", "how_met", "introduced_by",
                                              "engagement"):
                continue
            for v in _as_list(value):
                vs = _date_str(v) if isinstance(v, date) else str(v)
                pid_key = {"employer": "employed_by", "city": "lives_in" if note_type == "person"
                           else "located_in"}.get(k, k)
                spec = _claim_from_field(pid_key, vs, {}, "observed", None)
                if spec and spec.predicate == "fact" and spec.qualifiers.get("label") == k:
                    rec.attrs[k] = vs
                    rec.warnings.append(f"{rel}: kept “{k}” as a note attribute (no matching field)")
                elif spec:
                    rec.claims.append(spec)
    for v in _as_list(fm.get("links")):
        rec.links.append(LinkSpec(str(v)))

    # ── body ──────────────────────────────────────────────────────────
    section = ""
    free: list[str] = []
    current_subject: str | None = None
    extra_records: list[ImportRecord] = []
    for line in body.splitlines():
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            if len(heading.group(1)) >= 2:
                section = heading.group(2).strip().lower()
            current_subject = None
            continue
        if not line.strip():
            free.append("")
            continue
        basis = SECTION_BASIS.get(section, "observed")
        if section.startswith("inference"):
            basis = "inferred"
        indent = len(line) - len(line.lstrip())

        inter = _interaction(line, note_day, origin)
        if inter:
            extra_records.append(inter)
            continue
        task = _task(line, default_counterpart or (rec.name if rec.kind == "entity" else None))
        if task:
            rec.commitments.append(task)
            continue
        bare = BARE_LINK_BULLET.match(line)
        if bare and "::" not in line:
            name = bare.group("name").strip()
            q = _quals(bare.group("q"))
            current_subject = name
            ref = _ref(name, "person" if section.startswith("people") else None, "acquaintance")
            if section.startswith("people"):
                rec.mentions.append(ref)
                if rec.kind == "event" and rec.event and bare.group("rest"):
                    spec = ClaimSpec("fact", value=bare.group("rest").strip(), subject=ref,
                                     basis=str(q.get("basis", "told")))
                    rec.claims.append(spec)
            else:
                rec.mentions.append(Ref(name=name, create=False))
            continue
        field_m = INLINE_FIELD.match(line)
        if field_m:
            subj = field_m.group("subj")
            if not subj and indent > 0 and current_subject:
                subj = current_subject
            if rec.kind == "event" and not subj:
                subj = default_counterpart
            spec = _claim_from_field(field_m.group("key"), field_m.group("val"),
                                     _quals(field_m.group("q")), basis, subj)
            if spec:
                if basis == "inferred" and spec.basis == "inferred":
                    spec.confidence = spec.confidence if spec.confidence < 1 else 0.6
                rec.claims.append(spec)
            continue
        url = URL_LINE.match(line)
        if url and (section.startswith("link") or line.strip().startswith(("-", "*"))):
            note = (url.group("note") or "").strip() or None
            names = _links(note or "")
            target = _ref(names[0]) if names else None
            if note:
                note = WIKILINK.sub(lambda mm: mm.group(2) or mm.group(1), note)
            rec.links.append(LinkSpec(url.group("url"), note=note, target=target))
            continue
        for name in _links(line):
            rec.mentions.append(Ref(name=name, create=False))
        free.append(line)

    notes = "\n".join(free).strip()
    rec.notes = notes or None
    parsed.records = [rec] + extra_records
    return parsed


def scan(folder: str | Path, known_gids: dict[str, str] | None = None) -> list[ParsedNote]:
    root = Path(folder).expanduser()
    if not root.is_dir():
        raise ValueError(f"Not a folder: {root}")
    notes = []
    for path in sorted(root.rglob("*.md")):
        rel_parts = path.relative_to(root).parts
        if any(p.startswith(".") or p in ("_graph", "templates", "Templates") for p in rel_parts[:-1]):
            continue
        notes.append(parse_note(path, root, known_gids))
    return notes


def write_gid(path: Path, gid: str) -> bool:
    """Insert `gid: <id>` as the first frontmatter line. Touches nothing else."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return False
    lines = text.split("\n")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None or any(re.match(r"^gid\s*:", ln) for ln in lines[1:end]):
        return False
    lines.insert(1, f"gid: {gid}")
    path.write_text("\n".join(lines), encoding="utf-8")
    return True


def import_folder(vault, folder: str | Path, dry_run: bool = False,
                  write_gids: bool | None = None):
    """Parse a folder and apply it through the importer contract."""
    from . import importer

    if write_gids is None:
        write_gids = vault.get_meta("md_write_gid") != "0"
    known = {r["path"]: r["gid"] for r in vault.all("select path, gid from md_note")}
    notes = scan(folder, known)
    records = [r for n in notes for r in n.records]
    report = importer.apply(vault, "markdown", records, dry_run=dry_run, label=str(folder))
    written = 0
    if not dry_run:
        with vault.tx() as c:
            for n in notes:
                if not n.gid:
                    continue
                eid = report.origin_entities.get(f"md:{n.gid}")
                c.execute("insert into md_note(gid, path, content_hash, note_type, entity_id) "
                          "values(?,?,?,?,?) on conflict(gid) do update set path=excluded.path, "
                          "content_hash=excluded.content_hash, entity_id=excluded.entity_id, "
                          "last_imported=datetime('now')",
                          (n.gid, n.rel, n.content_hash, n.note_type, eid))
        if write_gids:
            for n in notes:
                if n.needs_gid and write_gid(n.path, n.gid):
                    written += 1
        vault.set_meta("md_folder", str(Path(folder).expanduser()))
    report.warnings = list(dict.fromkeys(report.warnings))
    return report, {"notes": len(notes), "gids_written": written,
                    "needs_gid": sum(1 for n in notes if n.needs_gid)}
