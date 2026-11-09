"""Bulk research importers: OpenAlex (publications) and Wikidata (general entities).

Parsing is separate from fetching. Fetching needs network access, which is off
until you allow it (Settings, or `--allow-network` on the command line).
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

from .importer import ClaimSpec, ImportRecord, Ref

USER_AGENT = "ties-research/0.2 (personal research tool)"


def _get_json(url: str, timeout: float = 30) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))


def require_network(vault) -> None:
    if vault.get_meta("allow_network") != "1":
        raise ValueError("Network access is off for this vault. Allow it in Settings first.")


# ── OpenAlex ────────────────────────────────────────────────────────────────


def _short(openalex_id: str | None) -> str | None:
    return openalex_id.rsplit("/", 1)[-1] if openalex_id else None


def _doi(doi: str | None) -> str | None:
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", doi).lower() if doi else None


def _orcid(orcid: str | None) -> str | None:
    return orcid.rsplit("/", 1)[-1] if orcid else None


def parse_openalex(data: dict) -> list[ImportRecord]:
    """Turn an OpenAlex /works response (results list) into records.

    Works become documents; authors become people with ORCID/OpenAlex ids;
    institutions become organizations with ROR ids. Edges: authored, affiliated_with,
    cites (only between works in this batch or already in the vault).
    """
    works = data.get("results", data if isinstance(data, list) else [])
    work_keys = {_short(w.get("id")) for w in works}
    records: dict[str, ImportRecord] = {}
    for w in works:
        wid = _short(w.get("id"))
        title = w.get("display_name") or w.get("title")
        if not wid or not title:
            continue
        origin = f"openalex:{wid}"
        rec = ImportRecord(origin=origin, entity_type="document", name=title.strip(),
                           engagement="reference", title=title)
        rec.identifiers.append(("openalex", wid))
        if _doi(w.get("doi")):
            rec.identifiers.append(("doi", _doi(w["doi"])))
        if w.get("publication_date") or w.get("publication_year"):
            rec.claims.append(ClaimSpec("published", value=w.get("publication_date") or
                                        str(w["publication_year"]), basis="public"))
        if w.get("type"):
            rec.claims.append(ClaimSpec("instance_of", value=w["type"], basis="public"))
        venue = ((w.get("primary_location") or {}).get("source") or {}).get("display_name")
        if venue:
            rec.claims.append(ClaimSpec("fact", value=venue, qualifiers={"label": "venue"},
                                        basis="public"))
        if w.get("cited_by_count") is not None:
            rec.attrs["cited_by_count"] = w["cited_by_count"]
        year = str(w.get("publication_year") or "") or None
        for a in w.get("authorships", []):
            au = a.get("author") or {}
            aid = _short(au.get("id"))
            if not aid or not au.get("display_name"):
                continue
            akey = f"openalex:{aid}"
            arec = records.get(akey)
            if not arec:
                arec = ImportRecord(origin=akey, entity_type="person", name=au["display_name"],
                                    engagement="reference")
                arec.identifiers.append(("openalex", aid))
                if _orcid(au.get("orcid")):
                    arec.identifiers.append(("orcid", _orcid(au["orcid"])))
                records[akey] = arec
            arec.claims.append(ClaimSpec("authored", obj=Ref(key=origin, name=title,
                                                             type="document"),
                                         basis="public", qualifiers={"position": a.get("author_position")}))
            for inst in a.get("institutions", []) or []:
                if not inst.get("display_name"):
                    continue
                ids = [("openalex", _short(inst.get("id")))] if inst.get("id") else []
                if inst.get("ror"):
                    ids.append(("ror", inst["ror"].rsplit("/", 1)[-1]))
                arec.claims.append(ClaimSpec(
                    "affiliated_with", obj=Ref(name=inst["display_name"], type="organization",
                                               identifiers=ids),
                    valid_from=year, valid_to=year, basis="public",
                    qualifiers={"as_of_work": wid}))
        for ref in w.get("referenced_works", []) or []:
            rid = _short(ref)
            if rid in work_keys:
                rec.claims.append(ClaimSpec("cites", obj=Ref(key=f"openalex:{rid}", type="document"),
                                            basis="public"))
            else:
                rec.claims.append(ClaimSpec("cites", obj=Ref(identifiers=[("openalex", rid)],
                                                             create=False), basis="public"))
        records[origin] = rec
    return list(records.values())


def fetch_openalex(query: str | None = None, orcid: str | None = None, per_page: int = 50) -> dict:
    params = {"per-page": str(min(per_page, 200))}
    if orcid:
        params["filter"] = f"author.orcid:{_orcid(orcid)}"
    elif query:
        params["search"] = query
    else:
        raise ValueError("Give a search query or an ORCID.")
    return _get_json("https://api.openalex.org/works?" + urllib.parse.urlencode(params))


# ── Wikidata ────────────────────────────────────────────────────────────────

PERSON_TYPES = {"Q5"}
ORG_TYPES = {"Q43229", "Q4830453", "Q783794", "Q891723", "Q3918", "Q31855", "Q163740",
             "Q327333", "Q7210356", "Q2659904", "Q15911314", "Q6881511", "Q7278"}
PLACE_TYPES = {"Q515", "Q6256", "Q1549591", "Q486972", "Q35657", "Q5119", "Q3624078"}

# property -> (predicate, direction, object type); direction "out" = item is subject
WD_PROPS = {
    "P108": ("employed_by", "out", "organization"),
    "P69": ("studied_at", "out", "organization"),
    "P463": ("member_of", "out", "organization"),
    "P39": ("position_held", "out", "topic"),
    "P26": ("partner_of", "out", "person"),
    "P22": ("child_of", "out", "person"),
    "P25": ("child_of", "out", "person"),
    "P40": ("parent_of", "out", "person"),
    "P159": ("located_in", "out", "place"),
    "P131": ("located_in", "out", "place"),
    "P1830": ("owns", "out", None),
    "P127": ("owns", "in", None),
    "P112": ("related_to", "out", "person"),
    "P169": ("related_to", "out", "person"),
    "P488": ("related_to", "out", "person"),
    "P749": ("part_of", "out", "organization"),
    "P27": ("related_to", "out", "place"),
}
WD_LABELS = {"P112": "founded_by", "P169": "chief_executive", "P488": "chairperson",
             "P27": "citizenship"}
WD_DATES = {"P569": "born", "P570": "died", "P571": "founded"}


def _wd_time(snak: dict) -> str | None:
    try:
        v = snak["datavalue"]["value"]
        t, precision = v["time"], v.get("precision", 11)
    except (KeyError, TypeError):
        return None
    m = re.match(r"^[+-](\d{4,})-(\d{2})-(\d{2})", t)
    if not m:
        return None
    y, mo, d = m.groups()
    if precision >= 11 and d != "00":
        return f"{y}-{mo}-{d}"
    if precision >= 10 and mo != "00":
        return f"{y}-{mo}"
    return y


def _wd_item(snak: dict) -> str | None:
    try:
        return snak["datavalue"]["value"]["id"]
    except (KeyError, TypeError):
        return None


def wd_type(types: set[str]) -> str:
    if types & PERSON_TYPES:
        return "person"
    if types & PLACE_TYPES:
        return "place"
    if types & ORG_TYPES:
        return "organization"
    return "organization" if types else "topic"


def parse_wikidata(entity: dict, labels: dict[str, str], lang: str = "en") -> list[ImportRecord]:
    qid = entity["id"]
    name = (entity.get("labels", {}).get(lang) or {}).get("value") or qid
    claims = entity.get("claims", {})
    types = {_wd_item(c["mainsnak"]) for c in claims.get("P31", [])} - {None}
    rec = ImportRecord(origin=f"wikidata:{qid}", entity_type=wd_type(types), name=name,
                       engagement="reference")
    rec.identifiers.append(("wikidata", qid))
    rec.aliases = [a["value"] for a in entity.get("aliases", {}).get(lang, [])]
    desc = (entity.get("descriptions", {}).get(lang) or {}).get("value")
    if desc:
        rec.claims.append(ClaimSpec("description", value=desc, basis="public"))
    for t in sorted(types):
        rec.claims.append(ClaimSpec("instance_of", value=labels.get(t, t), basis="public"))
    for pid, field in (("P496", "orcid"), ("P6782", "ror"), ("P856", "url")):
        for c in claims.get(pid, []):
            try:
                rec.identifiers.append((field, c["mainsnak"]["datavalue"]["value"]))
            except (KeyError, TypeError):
                pass
    for pid, pred in WD_DATES.items():
        for c in claims.get(pid, [])[:1]:
            d = _wd_time(c["mainsnak"])
            if d:
                rec.claims.append(ClaimSpec(pred, value=d, basis="public"))
    for pid, (pred, direction, otype) in WD_PROPS.items():
        for c in claims.get(pid, []):
            target = _wd_item(c["mainsnak"])
            if not target:
                continue
            quals = c.get("qualifiers", {})
            start = _wd_time(quals["P580"][0]) if quals.get("P580") else None
            end = _wd_time(quals["P582"][0]) if quals.get("P582") else None
            ref = Ref(name=labels.get(target, target), type=otype,
                      identifiers=[("wikidata", target)], key=f"wikidata:{target}")
            q = {"wikidata_property": pid}
            if pid in WD_LABELS:
                q["label"] = WD_LABELS[pid]
            if direction == "out":
                rec.claims.append(ClaimSpec(pred, obj=ref, valid_from=start, valid_to=end,
                                            basis="public", qualifiers=q))
            else:
                rec.claims.append(ClaimSpec(pred, subject=ref, obj=Ref(key=rec.origin),
                                            valid_from=start, valid_to=end, basis="public",
                                            qualifiers=q))
    return [rec]


def fetch_wikidata(qid: str, lang: str = "en") -> tuple[dict, dict[str, str]]:
    qid = qid.strip().upper()
    if not re.match(r"^Q\d+$", qid):
        raise ValueError("Give a Wikidata id like Q42.")
    data = _get_json(f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json")
    entity = data["entities"][qid]
    refs = set()
    for pid, statements in entity.get("claims", {}).items():
        for c in statements:
            t = _wd_item(c.get("mainsnak", {}))
            if t:
                refs.add(t)
    labels: dict[str, str] = {}
    refs = sorted(refs)
    for i in range(0, len(refs), 50):
        chunk = refs[i:i + 50]
        res = _get_json("https://www.wikidata.org/w/api.php?" + urllib.parse.urlencode({
            "action": "wbgetentities", "ids": "|".join(chunk), "props": "labels",
            "languages": lang, "format": "json"}))
        for k, v in res.get("entities", {}).items():
            lab = v.get("labels", {}).get(lang, {}).get("value")
            if lab:
                labels[k] = lab
    return entity, labels


def grade_import_sources(vault, prefix: str, reliability: str, credibility: int) -> None:
    """Give imported metadata sources a grade so they count as evidence."""
    with vault.tx() as c:
        c.execute("update source set reliability=coalesce(reliability, ?), "
                  "credibility=coalesce(credibility, ?), source_type=coalesce(source_type, 'dataset') "
                  "where kind='import' and external_key like ?", (reliability, credibility, prefix + "%"))
