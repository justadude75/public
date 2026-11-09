"""Parsers for the free sources, tested on real responses saved from the live services."""
import io
import json
import urllib.error
from pathlib import Path

import pytest

from ties import importer, repo, research, research_free
from ties.db import Vault

FILES = Path(__file__).parent / "fixtures" / "files"


def _load(name):
    return json.loads((FILES / name).read_text())


@pytest.fixture
def rvault(tmp_path):
    v = Vault(tmp_path / "r.vault")
    repo.init_vault(v, "Researcher")
    research.make_research(v)
    yield v
    v.close()


def test_crossref_live_response(rvault):
    items = _load("crossref_live.json")["message"]["items"]
    aff = _load("ror_affiliation_live.json")["items"][0]["organization"]
    text = items[0]["author"][0]["affiliation"][0]["name"]
    records = research_free.parse_crossref(items, {text: aff})
    report = importer.apply(rvault, "crossref", records)
    assert report.created
    paper = repo.search_entities(rvault, "lithium battery", type_id="document") or \
        [e for e in rvault.all("select id from entity where type_id='document'")]
    doc_count = rvault.scalar("select count(*) from entity where type_id='document'")
    assert doc_count >= 5
    hesse = [e for e in repo.search_entities(rvault, "hesse") if e["type_id"] == "person"][0]["id"]
    ids = {(i["scheme"], i["value"]) for i in repo.identifiers(rvault, hesse)}
    assert ("orcid", "0000-0002-6670-2684") in ids
    rels = {(r["label"], r["other_name"]) for r in repo.relations(rvault, hesse)}
    assert ("Affiliated with", "Technical University of Munich") in rels
    assert any(l == "Author of" for l, _ in rels)
    funded = rvault.scalar("select count(*) from claim where predicate_id='funded_by'")
    assert funded >= 1
    again = importer.apply(rvault, "crossref", research_free.parse_crossref(items, {text: aff}))
    assert again.changes == 0


def test_ror_live_response(rvault):
    orgs = _load("ror_live.json")["items"]
    importer.apply(rvault, "ror", research_free.parse_ror(orgs))
    um = repo.search_entities(rvault, "University of Michigan", type_id="organization")[0]
    assert um["display_name"] == "University of Michigan"
    ids = {i["scheme"] for i in repo.identifiers(rvault, um["id"])}
    assert {"ror", "wikidata"} <= ids
    rels = {(r["label"], r["other_name"]) for r in repo.relations(rvault, um["id"])}
    assert ("Located in", "Ann Arbor, United States") in rels
    assert any(l == "Includes" for l, _ in rels)
    assert repo.search_entities(rvault, "umich")[0]["id"] == um["id"]   # alias


def test_gleif_live_response(rvault):
    parent = _load("gleif_siemens_live.json")["data"]
    kids = _load("gleif_children_live.json")["data"]
    energy = _load("gleif_live.json")["data"]
    records = research_free.parse_gleif([parent] + energy, children={parent["id"]: kids})
    importer.apply(rvault, "gleif", records)
    ag = repo.search_entities(rvault, "Siemens Aktiengesellschaft")[0]["id"]
    assert ("lei", "W38RGI023J3WT1HWRP32") in {(i["scheme"], i["value"]) for i in repo.identifiers(rvault, ag)}
    subs = [r["other_name"] for r in repo.relations(rvault, ag) if r["label"] == "Includes"]
    assert len(subs) == len(kids)


class _Res(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_polite_fetch_retries_and_limits(monkeypatch):
    monkeypatch.setattr(research_free.time, "sleep", lambda s: None)
    calls = []

    def opener(req, timeout):
        calls.append(req.full_url)
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "slow down", {"Retry-After": "2"}, None)
        assert "ties-research" in req.get_header("User-agent")
        return _Res(b'{"ok": true}')

    assert research_free.get_json("https://api.ror.org/x", opener=opener) == {"ok": True}
    assert len(calls) == 2

    def too_long(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 429, "x", {"Retry-After": "70000"}, None)

    with pytest.raises(research_free.FetchError):
        research_free.get_json("https://api.ror.org/y", opener=too_long)

    def missing(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 404, "x", {}, None)

    assert research_free.get_json("https://api.ror.org/z", opener=missing) is None


def test_fetch_requires_permission(rvault):
    with pytest.raises(ValueError):
        research_free.fetch_records(rvault, "crossref", query="anything")
