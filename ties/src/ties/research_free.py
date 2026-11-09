"""Free, keyless research sources: Crossref, ROR, GLEIF and Wikidata search.

None of these needs an account or key. Each fetcher is polite: it identifies the
tool in the User-Agent, waits between requests to the same host, and backs off on
429 or 5xx responses (honouring Retry-After up to a limit). Parsing is separate
from fetching so it can be tested on saved responses.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from .importer import ClaimSpec, ImportRecord, LinkSpec, Ref
from .research_import import require_network

USER_AGENT = "ties-research/0.5 (+https://github.com/justadude75/public)"
MIN_INTERVAL = {"api.crossref.org": 0.25, "api.ror.org": 0.3, "api.gleif.org": 0.3,
                "www.wikidata.org": 0.2}
_last_call: dict[str, float] = {}
_lock = threading.Lock()


class FetchError(Exception):
    pass


def get_json(url: str, contact: str | None = None, retries: int = 4, timeout: float = 30,
             opener: Callable | None = None) -> Any:
    """GET a JSON document politely, with per-host spacing and backoff on 429/5xx."""
    host = urllib.parse.urlparse(url).hostname or ""
    ua = USER_AGENT + (f" mailto:{contact}" if contact else "")
    opener = opener or urllib.request.urlopen
    delay = 1.0
    for attempt in range(retries + 1):
        with _lock:
            wait = _last_call.get(host, 0) + MIN_INTERVAL.get(host, 0.2) - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _last_call[host] = time.monotonic()
        req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "application/json"})
        try:
            with opener(req, timeout=timeout) as res:
                return json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    pause = float(retry_after) if retry_after else delay
                except ValueError:
                    pause = delay
                if pause > 120:
                    raise FetchError(f"{host} asked us to wait {int(pause)}s (rate limit). "
                                     "Try again later.") from exc
                time.sleep(pause)
                delay = min(delay * 2, 30)
                continue
            raise FetchError(f"{host} answered {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt < retries:
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            raise FetchError(f"Could not reach {host}: {exc}") from exc
    raise FetchError(f"{host}: too many retries")


def _q(params: dict) -> str:
    return urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})


# ── ROR (research organizations) ────────────────────────────────────────────


def _ror_id(value: str | None) -> str | None:
    return value.rstrip("/").rsplit("/", 1)[-1] if value else None


def _ror_name(org: dict) -> str:
    names = org.get("names", [])
    for n in names:
        if "ror_display" in n.get("types", []):
            return n["value"]
    for n in names:
        if "label" in n.get("types", []):
            return n["value"]
    return names[0]["value"] if names else _ror_id(org.get("id")) or "?"


def parse_ror(orgs: list[dict]) -> list[ImportRecord]:
    out = []
    for org in orgs:
        rid = _ror_id(org.get("id"))
        if not rid:
            continue
        name = _ror_name(org)
        rec = ImportRecord(origin=f"ror:{rid}", entity_type="organization", name=name,
                           engagement="reference")
        rec.identifiers.append(("ror", rid))
        rec.aliases = [n["value"] for n in org.get("names", []) if n["value"] != name]
        for ext in org.get("external_ids", []):
            if ext.get("type") == "wikidata":
                for q in ext.get("all", []):
                    rec.identifiers.append(("wikidata", q))
            elif ext.get("type") == "isni" and ext.get("preferred"):
                rec.identifiers.append(("isni", ext["preferred"].replace(" ", "")))
        for link in org.get("links", []):
            if link.get("type") == "website":
                rec.identifiers.append(("url", link["value"]))
            elif link.get("type") == "wikipedia":
                rec.links.append(LinkSpec(link["value"], note="Wikipedia"))
        for t in org.get("types", []):
            rec.claims.append(ClaimSpec("instance_of", value=t, basis="public"))
        if org.get("established"):
            rec.claims.append(ClaimSpec("founded", value=str(org["established"]), basis="public"))
        for loc in org.get("locations", [])[:1]:
            g = loc.get("geonames_details") or {}
            if g.get("name"):
                city = f"{g['name']}, {g.get('country_name') or g.get('country_code') or ''}".strip(", ")
                rec.claims.append(ClaimSpec("located_in", obj=Ref(
                    name=city, type="place", identifiers=[("geonames", str(loc.get("geonames_id")))]
                    if loc.get("geonames_id") else []), basis="public"))
        for rel in org.get("relationships", []):
            other = Ref(name=rel.get("label"), type="organization",
                        identifiers=[("ror", _ror_id(rel.get("id")))], key=f"ror:{_ror_id(rel.get('id'))}")
            kind = rel.get("type")
            if kind == "parent":
                rec.claims.append(ClaimSpec("part_of", obj=other, basis="public"))
            elif kind == "child":
                rec.claims.append(ClaimSpec("part_of", subject=other, obj=Ref(key=rec.origin),
                                            basis="public"))
            elif kind in ("successor", "predecessor"):
                rec.claims.append(ClaimSpec("related_to", obj=other, basis="public",
                                            qualifiers={"label": kind}))
        if org.get("status") and org["status"] != "active":
            rec.claims.append(ClaimSpec("fact", value=org["status"], qualifiers={"label": "status"},
                                        basis="public"))
        out.append(rec)
    return out


def fetch_ror(query: str | None = None, ror_id: str | None = None, contact=None) -> list[dict]:
    if ror_id:
        doc = get_json(f"https://api.ror.org/v2/organizations/{_ror_id(ror_id)}", contact)
        return [doc] if doc else []
    data = get_json("https://api.ror.org/v2/organizations?" + _q({"query": query}), contact)
    return (data or {}).get("items", [])


_affiliation_cache: dict[str, dict | None] = {}


def match_affiliation(text: str, contact=None) -> dict | None:
    """ROR's affiliation matcher: free text -> the organization it chose, or None."""
    key = text.strip().lower()
    if key in _affiliation_cache:
        return _affiliation_cache[key]
    data = get_json("https://api.ror.org/v2/organizations?" + _q({"affiliation": text}), contact)
    chosen = None
    for item in (data or {}).get("items", []):
        if item.get("chosen"):
            chosen = item["organization"]
            break
    _affiliation_cache[key] = chosen
    return chosen


# ── Crossref (publications) ─────────────────────────────────────────────────


def _date_parts(obj: dict | None) -> str | None:
    try:
        parts = obj["date-parts"][0]
    except (KeyError, IndexError, TypeError):
        return None
    if not parts or parts[0] is None:
        return None
    return "-".join(f"{p:02d}" if i else str(p) for i, p in enumerate(parts))


def _orcid(value: str | None) -> str | None:
    return value.rstrip("/").rsplit("/", 1)[-1] if value else None


def parse_crossref(items: list[dict], affiliation_orgs: dict[str, dict] | None = None
                   ) -> list[ImportRecord]:
    """Works become documents; authors become people (ORCID when given).

    Affiliations are free text in Crossref. When `affiliation_orgs` maps an affiliation
    string to a ROR organization (see match_affiliation), the author is linked to that
    organization; otherwise the text is kept as a qualifier on the authorship.
    """
    affiliation_orgs = affiliation_orgs or {}
    dois = {(w.get("DOI") or "").lower() for w in items}
    records: dict[str, ImportRecord] = {}
    for w in items:
        doi = (w.get("DOI") or "").lower()
        title = (w.get("title") or [None])[0]
        if not doi or not title:
            continue
        origin = f"crossref:{doi}"
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", title)).strip()
        rec = ImportRecord(origin=origin, entity_type="document", name=title, title=title,
                           engagement="reference")
        rec.identifiers.append(("doi", doi))
        issued = _date_parts(w.get("issued"))
        if issued:
            rec.claims.append(ClaimSpec("published", value=issued, basis="public"))
        if w.get("type"):
            rec.claims.append(ClaimSpec("instance_of", value=w["type"], basis="public"))
        venue = (w.get("container-title") or [None])[0]
        if venue:
            rec.claims.append(ClaimSpec("fact", value=venue, qualifiers={"label": "venue"},
                                        basis="public"))
        if w.get("publisher"):
            rec.claims.append(ClaimSpec("fact", value=w["publisher"],
                                        qualifiers={"label": "publisher"}, basis="public"))
        if w.get("is-referenced-by-count") is not None:
            rec.attrs["cited_by_count"] = w["is-referenced-by-count"]
        rec.links.append(LinkSpec(f"https://doi.org/{doi}", note="DOI"))
        for f in w.get("funder", []) or []:
            if not f.get("name"):
                continue
            ids = [("doi", f["DOI"].lower())] if f.get("DOI") else []
            rec.claims.append(ClaimSpec("funded_by", obj=Ref(name=f["name"][:200], type="organization",
                                                             identifiers=ids), basis="public"))
        for ref in w.get("reference", []) or []:
            rdoi = (ref.get("DOI") or "").lower()
            if not rdoi:
                continue
            if rdoi in dois:
                rec.claims.append(ClaimSpec("cites", obj=Ref(key=f"crossref:{rdoi}", type="document"),
                                            basis="public"))
            else:
                rec.claims.append(ClaimSpec("cites", obj=Ref(identifiers=[("doi", rdoi)], create=False),
                                            basis="public"))
        records[origin] = rec
        for a in w.get("author", []) or []:
            name = " ".join(x for x in (a.get("given"), a.get("family")) if x) or a.get("name")
            if not name:
                continue
            orcid = _orcid(a.get("ORCID"))
            akey = f"orcid:{orcid}" if orcid else f"crossref-author:{doi}:{name.lower()}"
            arec = records.get(akey)
            if not arec:
                arec = ImportRecord(origin=akey, entity_type="person", name=name, engagement="reference")
                if orcid:
                    arec.identifiers.append(("orcid", orcid))
                records[akey] = arec
            affs = [x.get("name") for x in a.get("affiliation", []) or [] if x.get("name")]
            quals = {"position": a.get("sequence")}
            if affs:
                quals["affiliation"] = affs[0][:300]
            arec.claims.append(ClaimSpec("authored", obj=Ref(key=origin, name=title, type="document"),
                                         basis="public", qualifiers=quals))
            year = issued[:4] if issued else None
            for aff in affs:
                org = affiliation_orgs.get(aff)
                if org:
                    rid = _ror_id(org.get("id"))
                    arec.claims.append(ClaimSpec(
                        "affiliated_with",
                        obj=Ref(name=_ror_name(org), type="organization", key=f"ror:{rid}",
                                identifiers=[("ror", rid)]),
                        valid_from=year, valid_to=year, basis="public",
                        qualifiers={"as_written": aff[:300], "matched_by": "ror"}))
    return list(records.values())


def fetch_crossref(query: str | None = None, doi: str | None = None, orcid: str | None = None,
                   rows: int = 20, contact=None) -> list[dict]:
    fields = ("DOI,title,author,issued,type,container-title,publisher,reference,funder,"
              "is-referenced-by-count")
    if doi:
        doc = get_json(f"https://api.crossref.org/works/{urllib.parse.quote(doi)}", contact)
        return [doc["message"]] if doc else []
    params = {"rows": min(rows, 1000), "select": fields, "mailto": contact}
    if orcid:
        params["filter"] = f"orcid:{_orcid(orcid)}"
    elif query:
        params["query.bibliographic"] = query
    else:
        raise ValueError("Give a search, a DOI or an ORCID.")
    data = get_json("https://api.crossref.org/works?" + _q(params), contact)
    return (data or {}).get("message", {}).get("items", [])


def affiliation_matches(items: list[dict], contact=None, limit: int = 60) -> dict[str, dict]:
    """Match distinct affiliation strings in Crossref items to ROR organizations."""
    seen: list[str] = []
    for w in items:
        for a in w.get("author", []) or []:
            for x in a.get("affiliation", []) or []:
                if x.get("name") and x["name"] not in seen:
                    seen.append(x["name"])
    out = {}
    for text in seen[:limit]:
        org = match_affiliation(text, contact)
        if org:
            out[text] = org
    return out


# ── GLEIF (legal entities and ownership) ────────────────────────────────────


def _gleif_record(d: dict) -> dict:
    return d.get("attributes", {}) if "attributes" in d else d


def parse_gleif(records: list[dict], parents: dict[str, dict] | None = None,
                children: dict[str, list[dict]] | None = None) -> list[ImportRecord]:
    """Legal entities with LEI codes. `parents` maps LEI -> direct parent record;
    `children` maps LEI -> list of direct child records."""
    parents, children = parents or {}, children or {}
    out: dict[str, ImportRecord] = {}

    def make(d: dict) -> ImportRecord | None:
        a = _gleif_record(d)
        lei = a.get("lei") or d.get("id")
        ent = a.get("entity", {})
        name = (ent.get("legalName") or {}).get("name")
        if not lei or not name:
            return None
        origin = f"gleif:{lei}"
        if origin in out:
            return out[origin]
        rec = ImportRecord(origin=origin, entity_type="organization", name=name, engagement="reference")
        rec.identifiers.append(("lei", lei))
        if ent.get("registeredAs"):
            rec.identifiers.append(("registry", f"{ent.get('jurisdiction', '')}:{ent['registeredAs']}"))
        rec.aliases = [n.get("name") for n in ent.get("otherNames", []) + ent.get(
            "transliteratedOtherNames", []) if n.get("name")]
        addr = ent.get("headquartersAddress") or ent.get("legalAddress") or {}
        if addr.get("city"):
            rec.claims.append(ClaimSpec("located_in", obj=Ref(
                name=f"{addr['city']}, {addr.get('country', '')}".strip(", "), type="place"),
                basis="public"))
        if ent.get("jurisdiction"):
            rec.claims.append(ClaimSpec("fact", value=ent["jurisdiction"],
                                        qualifiers={"label": "jurisdiction"}, basis="public"))
        if ent.get("creationDate"):
            rec.claims.append(ClaimSpec("founded", value=ent["creationDate"][:10], basis="public"))
        if ent.get("status"):
            rec.claims.append(ClaimSpec("fact", value=ent["status"].lower(),
                                        qualifiers={"label": "legal status"}, basis="public"))
        reg = a.get("registration", {})
        if reg.get("corroborationLevel"):
            rec.attrs["gleif_corroboration"] = reg["corroborationLevel"]
        out[origin] = rec
        return rec

    for d in records:
        make(d)
    for lei, parent in parents.items():
        child_rec, parent_rec = out.get(f"gleif:{lei}"), make(parent)
        if child_rec and parent_rec:
            child_rec.claims.append(ClaimSpec("part_of", obj=Ref(key=parent_rec.origin,
                                                                 type="organization"),
                                              basis="public", qualifiers={"relationship": "direct parent"}))
    for lei, kids in children.items():
        parent_rec = out.get(f"gleif:{lei}")
        for kid in kids:
            kid_rec = make(kid)
            if parent_rec and kid_rec:
                kid_rec.claims.append(ClaimSpec("part_of", obj=Ref(key=parent_rec.origin,
                                                                   type="organization"),
                                                basis="public", qualifiers={"relationship": "direct parent"}))
    return list(out.values())


def fetch_gleif(name: str | None = None, lei: str | None = None, with_parent: bool = True,
                with_children: int = 0, contact=None) -> tuple[list[dict], dict, dict]:
    base = "https://api.gleif.org/api/v1/lei-records"
    if lei:
        doc = get_json(f"{base}/{lei.strip()}", contact)
        recs = [doc["data"]] if doc and doc.get("data") else []
    elif name:
        doc = get_json(f"{base}?" + _q({"filter[entity.legalName]": name, "page[size]": 10}), contact)
        recs = (doc or {}).get("data", [])
        if not recs:
            doc = get_json(f"{base}?" + _q({"filter[fulltext]": name, "page[size]": 10}), contact)
            recs = (doc or {}).get("data", [])
    else:
        raise ValueError("Give a company name or an LEI.")
    parents, children = {}, {}
    for r in recs[:10]:
        if with_parent:
            p = get_json(f"{base}/{r['id']}/direct-parent", contact)
            if p and p.get("data"):
                parents[r["id"]] = p["data"]
        if with_children:
            ch = get_json(f"{base}/{r['id']}/direct-children?" + _q({"page[size]": min(with_children, 200)}),
                          contact)
            if ch and ch.get("data"):
                children[r["id"]] = ch["data"]
    return recs, parents, children


# ── Wikidata search ─────────────────────────────────────────────────────────


def search_wikidata(text: str, limit: int = 8, contact=None) -> list[dict]:
    data = get_json("https://www.wikidata.org/w/api.php?" + _q({
        "action": "wbsearchentities", "search": text, "language": "en", "format": "json",
        "limit": limit}), contact)
    return [{"id": s["id"], "label": s.get("label"), "description": s.get("description")}
            for s in (data or {}).get("search", [])]


# ── one entry point for the CLI and the web page ────────────────────────────

SOURCES = {
    "crossref": "Publications and authors (Crossref)",
    "ror": "Research organizations (ROR)",
    "gleif": "Companies and their parents (GLEIF)",
    "wikidata": "General entities (Wikidata)",
}


def fetch_records(vault, source: str, query: str = "", ident: str = "", limit: int = 20,
                  match_affiliations: bool = True, children: int = 0) -> list[ImportRecord]:
    require_network(vault)
    contact = vault.get_meta("research_contact") or None
    if source == "crossref":
        is_doi = ident.startswith("10.")
        items = fetch_crossref(query or None, doi=ident if is_doi else None,
                               orcid=None if is_doi else (ident or None), rows=limit, contact=contact)
        orgs = affiliation_matches(items, contact) if match_affiliations else {}
        records = parse_crossref(items, orgs)
        records += parse_ror(list({o["id"]: o for o in orgs.values()}.values()))
        return records
    if source == "ror":
        return parse_ror(fetch_ror(query or None, ident or None, contact)[:limit])
    if source == "gleif":
        recs, parents, kids = fetch_gleif(query or None, ident or None, True, children, contact)
        return parse_gleif(recs, parents, kids)
    if source == "wikidata":
        from .research_import import fetch_wikidata, parse_wikidata
        qid = ident or (search_wikidata(query, 1, contact) or [{}])[0].get("id")
        if not qid:
            return []
        entity, labels = fetch_wikidata(qid)
        return parse_wikidata(entity, labels)
    raise ValueError(f"Unknown source: {source}")
