import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ties import crossvault, exports, importer, repo, research, research_import, resolution
from ties.db import Vault
from ties.web import create_app

FILES = Path(__file__).parent / "fixtures" / "files"


@pytest.fixture
def rvault(tmp_path):
    v = Vault(tmp_path / "research.vault")
    repo.init_vault(v, "Researcher")
    research.make_research(v)
    yield v
    v.close()


def test_sources_grades_and_corroboration(rvault):
    pid = research.create_project(rvault, "Grid storage players")
    wire = research.add_source(rvault, title="Wire story", uri="https://news.example/a",
                               source_type="article", reliability="B", credibility=2,
                               project_id=pid)
    copy = research.add_source(rvault, title="Reprint", source_type="article", reliability="C",
                               credibility=3, independent_of=wire)
    filing = research.add_source(rvault, title="Company filing", source_type="filing",
                                 reliability="A", credibility=1)
    c1 = research.add_evidence_claim(rvault, wire, "new:person:Jane Smith", "employed_by",
                                     object_ref="new:organization:Voltgrid", valid_from="2020",
                                     quote="Smith joined Voltgrid in 2020")
    jane = repo.search_entities(rvault, "jane")[0]["id"]
    org = repo.search_entities(rvault, "voltgrid")[0]["id"]
    c2 = research.add_evidence_claim(rvault, copy, jane, "employed_by", object_ref=org,
                                     valid_from="2020")
    assert c1 == c2                                    # same claim, extra source
    sup = research.support(rvault)[c1]
    assert sup["sources"] == 2 and sup["independent"] == 1 and sup["best_grade"] == "B2"
    research.add_evidence_claim(rvault, filing, jane, "employed_by", object_ref=org, valid_from="2020")
    sup = research.support(rvault)[c1]
    assert sup["independent"] == 2 and sup["best_grade"] == "A1" and sup["confidence"] > 0.95
    detail = research.source_detail(rvault, wire)
    assert detail["claims"][0]["quote"] == "Smith joined Voltgrid in 2020"
    assert detail["copies"][0]["title"] == "Reprint"


def test_contradictions_kept_side_by_side(rvault):
    a = research.add_source(rvault, title="Profile A", reliability="B", credibility=2)
    b = research.add_source(rvault, title="Profile B", reliability="D", credibility=4)
    research.add_evidence_claim(rvault, a, "new:person:Jane Smith", "born", value="1970-03-04")
    jane = repo.search_entities(rvault, "jane")[0]["id"]
    research.add_evidence_claim(rvault, b, jane, "born", value="1971-03-04")
    births = [f for f in repo.facts(rvault, jane) if f["predicate_id"] == "born"]
    assert len(births) == 2                            # research keeps both
    found = research.contradictions(rvault)
    assert len(found) == 1 and found[0]["predicate"] == "Born"
    grades = {found[0]["a"]["support"]["best_grade"], found[0]["b"]["support"]["best_grade"]}
    assert grades == {"B2", "D4"}


def test_as_of_views(rvault):
    s = research.add_source(rvault, title="Registry", reliability="A", credibility=1)
    research.add_evidence_claim(rvault, s, "new:person:Jane Smith", "employed_by",
                                object_ref="new:organization:OldCo", valid_from="2010", valid_to="2015")
    jane = repo.search_entities(rvault, "jane")[0]["id"]
    research.add_evidence_claim(rvault, s, jane, "employed_by", object_ref="new:organization:NewCo",
                                valid_from="2016")
    in_2012 = {r["other_name"] for r in research.entity_as_of(rvault, jane, valid_at="2012-06-01")}
    assert in_2012 == {"OldCo"}
    in_2020 = {r["other_name"] for r in research.entity_as_of(rvault, jane, valid_at="2020-01-01")}
    assert in_2020 == {"NewCo"}
    assert research.entity_as_of(rvault, jane, known_at="2000-01-01") == []
    assert len(research.entity_as_of(rvault, jane, known_at="2999-01-01")) == 2


def test_ach_matrix(rvault):
    pid = research.create_project(rvault, "Who funded Voltgrid?")
    h1 = research.add_hypothesis(rvault, pid, "Northfield led the round")
    h2 = research.add_hypothesis(rvault, pid, "A strategic investor led")
    e1 = research.add_evidence_item(rvault, pid, "Northfield partner joined the board")
    e2 = research.add_evidence_item(rvault, pid, "Press release names no lead")
    research.rate(rvault, h1, e1, "CC")
    research.rate(rvault, h2, e1, "I")
    research.rate(rvault, h1, e2, "N")
    research.rate(rvault, h2, e2, "N")
    m = research.ach_matrix(rvault, pid)
    assert [h["title"] for h in m["ranked"]] == ["Northfield led the round", "A strategic investor led"]
    diag = {i["label"]: i["diagnostic"] for i in m["evidence"]}
    assert diag == {"Northfield partner joined the board": True, "Press release names no lead": False}
    with pytest.raises(ValueError):
        research.rate(rvault, h1, e1, "maybe")


def test_openalex_import_and_batch_resolution(rvault):
    data = json.loads((FILES / "openalex_works.json").read_text())
    records = research_import.parse_openalex(data)
    report = importer.apply(rvault, "openalex", records)
    research_import.grade_import_sources(rvault, "openalex:", "B", 2)
    assert "Grid-scale storage economics (document)" in report.created
    paper = repo.search_entities(rvault, "grid scale storage")[0]["id"]
    rels = {(r["label"], r["other_name"]) for r in repo.relations(rvault, paper)}
    assert ("Author", "Jane Smith") in rels and ("Author", "Raj Mehta") in rels
    assert ("Cites", "Battery degradation in practice") in rels
    raj = [e for e in repo.search_entities(rvault, "raj mehta") if e["type_id"] == "person"]
    assert len(raj) == 1                                  # same OpenAlex author id → one entity
    jane = repo.search_entities(rvault, "jane smith")[0]["id"]
    assert ("orcid", "0000-0001-0000-0001") in {(i["scheme"], i["value"]) for i in repo.identifiers(rvault, jane)}
    # "J. Smith" shares an institution and a co-author with Jane Smith
    result = resolution.batch(rvault)
    top = result["candidates"][0]
    assert {top["a"]["display_name"], top["b"]["display_name"]} == {"Jane Smith", "J. Smith"}
    assert any("shared connections" in r for r in top["reasons"])
    auto = resolution.batch(rvault, auto_threshold=0.6)
    assert auto["merged"] and repo.search_entities(rvault, "j smith")[0]["id"] == jane
    again = importer.apply(rvault, "openalex", research_import.parse_openalex(data))
    assert again.changes == 0


def test_batch_never_merges_conflicting_strong_ids(rvault):
    a = repo.create_entity(rvault, "person", "Sam Lee", identifiers=[("orcid", "1")])
    b = repo.create_entity(rvault, "person", "Sam Lee", identifiers=[("orcid", "2")])
    assert resolution.batch(rvault, auto_threshold=0.5)["merged"] == []


def test_wikidata_parse(rvault):
    entity = json.loads((FILES / "wikidata_Q100.json").read_text())
    labels = {"Q5": "human", "Q200": "Acme Research", "Q300": "State University", "Q400": "Holding Co"}
    records = research_import.parse_wikidata(entity, labels)
    importer.apply(rvault, "wikidata", records)
    ada = repo.search_entities(rvault, "ada")[0]["id"]
    assert repo.get_entity(rvault, ada)["type_id"] == "person"
    facts = {(f["predicate_id"], f["value_text"]) for f in repo.facts(rvault, ada)}
    assert ("born", "1970-03-04") in facts and ("instance_of", "human") in facts
    rels = {(r["label"], r["other_name"], r["valid_from"], r["valid_to"]) for r in repo.relations(rvault, ada)}
    assert ("Works at", "Acme Research", "2010", "2015-06") in rels
    assert ("Owned by", "Holding Co", None, None) in rels
    assert ("wikidata", "Q100") in {(i["scheme"], i["value"]) for i in repo.identifiers(rvault, ada)}


def test_exports(rvault):
    s = research.add_source(rvault, title="Registry", reliability="A", credibility=1)
    research.add_evidence_claim(rvault, s, "new:person:Jane Smith", "employed_by",
                                object_ref="new:organization:Acme & Sons", valid_from="2010")
    gml = exports.graphml(rvault)
    assert "<graphml" in gml and "Acme &amp; Sons" in gml
    gx = exports.gexf(rvault)
    assert "<gexf" in gx and "employed_by" in gx
    lines = [json.loads(l) for l in exports.ftm(rvault).splitlines()]
    assert {"Person", "Organization", "Employment"} <= {l["schema"] for l in lines}
    emp = next(l for l in lines if l["schema"] == "Employment")
    assert emp["properties"]["startDate"] == ["2010"]
    import io, zipfile
    z = zipfile.ZipFile(io.BytesIO(exports.csv_zip(rvault)))
    assert set(z.namelist()) == {"nodes.csv", "edges.csv"}


def test_snapshot_needs_permission(rvault):
    s = research.add_source(rvault, title="Page", uri="https://example.com")
    with pytest.raises(ValueError):
        research.snapshot(rvault, s)
    with pytest.raises(ValueError):
        research_import.require_network(rvault)


def test_cross_vault_link(vault, rvault, tmp_path):
    p = repo.create_entity(vault, "person", "Jane Smith", engagement="contact",
                           identifiers=[("email", "jane@uni.edu")])
    r = repo.create_entity(rvault, "person", "Jane Smith", identifiers=[("email", "jane@uni.edu")])
    repo.add_claim(rvault, r, "title", value="Professor")
    cands = crossvault.candidates(vault, rvault)
    assert cands[0]["other_id"] == r and cands[0]["why"] == "same email"
    crossvault.confirm(vault, str(rvault.path), p, r, "Jane Smith")
    view = crossvault.linked_view(vault, p)
    assert view[0]["available"] and view[0]["facts"][0]["value_text"] == "Professor"
    assert crossvault.candidates(vault, rvault) == []
    # nothing was copied into the personal vault
    assert all(f["value_text"] != "Professor" for f in repo.facts(vault, p))


def test_research_pages(tmp_path, today):
    from ties.demo import build_research_demo
    v = Vault(tmp_path / "demo-research.vault")
    build_research_demo(v)
    c = TestClient(create_app(v, today_fn=lambda: today))
    r = c.get("/", follow_redirects=False)
    assert r.headers["location"] == "/projects"
    page = c.get("/projects").text
    assert "Who is behind grid-storage research?" in page and "Contradictions" in page
    pid = research.projects(v)[0]["id"]
    proj = c.get(f"/projects/{pid}").text
    assert "Jane Smith" in proj and "contradiction" in proj
    ach = c.get(f"/projects/{pid}/ach").text
    assert "Competing hypotheses" in ach and "diagnostic" in ach
    hyp = research.ach_matrix(v, pid)["hypotheses"][0]
    item = research.ach_matrix(v, pid)["evidence"][1]
    c.post(f"/projects/{pid}/ach", data={f"r-{hyp['id']}-{item['id']}": "II"})
    assert research.ach_matrix(v, pid)["ratings"][(hyp["id"], item["id"])] == "II"
    assert "Trade press profile" in c.get("/sources").text
    sid = research.sources(v, "registry")[0]["id"]
    src = c.get(f"/sources/{sid}").text
    assert "Date of birth 1970-03-04" in src and "Saved copy" in src
    assert "Born" in c.get("/contradictions").text
    jane = repo.search_entities(v, "jane smith")[0]["id"]
    prof = c.get(f"/e/{jane}?valid_at=2017-01-01").text
    assert "As of" in prof and "Acme Research" in prof and "1× A2" in prof
    for fmt in ("graphml", "gexf", "csv", "ftm"):
        r = c.get(f"/export/graph?format={fmt}&project={pid}")
        assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert "Resolve duplicates" in c.get("/resolve").text
    assert "Network access is off" in c.get("/research/import").text
    r = c.post("/research/import", data={"kind": "wikidata", "qid": "Q1"}, follow_redirects=False)
    assert "error=" in r.headers["location"]
    r = c.post("/sources", data={"title": "New report", "reliability": "B", "credibility": "2",
                                 "project": pid}, follow_redirects=False)
    assert r.status_code == 303
