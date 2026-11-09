import json
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from ties import (brief, graph, importer, importers_std, repo, resolution, scoring, triggers)
from ties.db import Vault
from ties.web import create_app

FILES = Path(__file__).parent / "fixtures" / "files"


def _contact(vault, name, tier="active"):
    pid = repo.create_entity(vault, "person", name, engagement="contact")
    repo.update_profile(vault, pid, tier, None, None, None, None)
    return pid


# ── importers ───────────────────────────────────────────────────────────────

def test_vcard_import(vault):
    records = importers_std.parse_vcard((FILES / "contacts.vcf").read_text())
    assert [r.name for r in records] == ["Sarah Chen", "Raj Mehta"]
    importer.apply(vault, "vcard", records)
    sarah = repo.search_entities(vault, "sarah")[0]["id"]
    assert {(i["scheme"], i["value"]) for i in repo.identifiers(vault, sarah)} == {
        ("email", "sarah@example.com"), ("phone", "+14155550100")}
    rels = {(r["label"], r["other_name"]) for r in repo.relations(vault, sarah)}
    assert rels == {("Works at", "Paybridge"), ("Lives in", "Oakland")}
    assert repo.search_entities(vault, "sare")[0]["id"] == sarah
    assert repo.tags_for(vault, "entity", sarah) == ["climate", "work"]
    raj = repo.search_entities(vault, "raj")[0]["id"]
    assert any(f["value_text"] == "--07-05" for f in repo.facts(vault, raj))
    again = importer.apply(vault, "vcard", importers_std.parse_vcard((FILES / "contacts.vcf").read_text()))
    assert again.changes == 0


def test_vcard_matches_existing_person_by_email(vault):
    pid = repo.create_entity(vault, "person", "S. Chen", identifiers=[("email", "SARAH@example.com")])
    report = importer.apply(vault, "vcard", importers_std.parse_vcard((FILES / "contacts.vcf").read_text()))
    assert "Sarah Chen (matched existing)" in report.matched
    assert repo.search_entities(vault, "sarah chen")[0]["id"] == pid


def test_ical_import(vault, today):
    repo.add_identifier(vault, repo.ego_id(vault), "email", "me@example.com")
    records = importers_std.parse_ical((FILES / "calendar.ics").read_text(),
                                       importers_std.my_emails(vault), today)
    assert len(records) == 3          # "Focus time" has no one else
    importer.apply(vault, "ical", records)
    sarah = repo.search_entities(vault, "sarah", type_id="person")[0]["id"]
    ev = repo.get_event(vault, repo.list_events(vault, entity_id=sarah)[0]["id"])
    assert ev["status"] == "occurred" and ev["location_name"] == "Blue Bottle Oakland"
    assert {p["display_name"] for p in ev["participants"]} == {"Test Owner", "Sarah Chen"}
    raj = repo.search_entities(vault, "raj", type_id="person")[0]["id"]
    dinner = repo.list_events(vault, entity_id=raj)[0]
    assert dinner["status"] == "planned" and dinner["channel"] == "video"
    dev = repo.search_entities(vault, "dev", type_id="person")[0]["id"]
    assert repo.list_events(vault, entity_id=dev)[0]["status"] == "cancelled"


def test_csv_linkedin_import(vault):
    records, mapping, headers = importers_std.parse_csv((FILES / "linkedin.csv").read_text(),
                                                        as_contacts=True)
    assert mapping["First Name"] == "first_name" and mapping["Company"] == "organization"
    assert [r.name for r in records] == ["Sarah Chen", "Dev Patel"]
    importer.apply(vault, "csv", records)
    sarah = repo.search_entities(vault, "sarah")[0]["id"]
    assert repo.get_profile(vault, sarah)["tier"] == "active"
    assert repo.links(vault, sarah)[0]["uri"] == "https://www.linkedin.com/in/sarahchen"


def test_export_and_restore_round_trip(vault, tmp_path, today):
    s = _contact(vault, "Sarah Chen")
    repo.add_claim(vault, s, "interest", value="sailing")
    repo.create_event(vault, kind="meeting", status="occurred", start_at=today.isoformat(),
                      participants=[s], notes="About sailing")
    data = json.loads(json.dumps(repo.export_all(vault), default=str))
    assert not any("_fts" in t for t in data["tables"])
    fresh = Vault(tmp_path / "restored.vault")
    repo.prepare(fresh)
    counts = importers_std.restore(fresh, data)
    assert counts["entity"] >= 3
    assert repo.search_entities(fresh, "sarah")[0]["display_name"] == "Sarah Chen"
    assert scoring.contact_status(fresh, s, today).days_since == 0
    from ties import textindex
    assert textindex.search(fresh, "sailing")["notes"]


# ── duplicates ──────────────────────────────────────────────────────────────

def test_candidates_merge_and_undo(vault, today):
    a = _contact(vault, "Robert Lang")
    b = repo.create_entity(vault, "person", "Bob Lang", identifiers=[("email", "bob@x.com")])
    org = repo.create_entity(vault, "organization", "Acme")
    repo.add_claim(vault, b, "employed_by", object_ref=org)
    repo.create_event(vault, kind="call", status="occurred", start_at=today.isoformat(),
                      participants=[b])
    repo.add_commitment(vault, b, "Send notes", None)
    cands = resolution.candidates(vault)
    assert cands and {cands[0]["a"]["id"], cands[0]["b"]["id"]} == {a, b}
    assert cands[0]["a"]["id"] == a  # the tracked contact is offered as the one to keep
    did = resolution.merge(vault, a, b)
    assert repo.get_entity(vault, b)["id"] == a              # redirects
    assert repo.relations(vault, a)[0]["other_name"] == "Acme"
    assert scoring.contact_status(vault, a, today).days_since == 0
    assert repo.commitments(vault, a)[0]["description"] == "Send notes"
    assert repo.search_entities(vault, "bob")[0]["id"] == a
    assert resolution.candidates(vault) == []
    resolution.unmerge(vault, did)
    assert repo.get_entity(vault, b)["id"] == b
    assert repo.relations(vault, a) == [] and repo.relations(vault, b)[0]["other_name"] == "Acme"
    assert repo.commitments(vault, b) and not repo.commitments(vault, a)
    assert repo.identifiers(vault, b)[0]["value"] == "bob@x.com"


def test_not_same_is_remembered(vault):
    a = repo.create_entity(vault, "person", "Sam Lee")
    b = repo.create_entity(vault, "person", "Sam Lee")
    assert resolution.candidates(vault)
    resolution.not_same(vault, a, b)
    assert resolution.candidates(vault) == []


# ── triggers and briefs ─────────────────────────────────────────────────────

def test_snooze_hides_due_contact(vault, today):
    a = _contact(vault, "Ana")
    assert [s.name for s in scoring.today_view(vault, today)["due"]] == ["Ana"]
    triggers.set_state(vault, "due", a, "snoozed", (today + timedelta(days=7)).isoformat())
    view = scoring.today_view(vault, today)
    assert view["due"] == [] and view["n_snoozed"] == 1
    later = scoring.today_view(vault, today + timedelta(days=8))
    assert [s.name for s in later["due"]] == ["Ana"]


def test_brief_debrief_and_fact_change_items(vault, today):
    a = _contact(vault, "Ana")
    old = repo.create_entity(vault, "organization", "OldCo")
    new = repo.create_entity(vault, "organization", "NewCo")
    cid = repo.add_claim(vault, a, "employed_by", object_ref=old)
    repo.end_claim(vault, cid, "2026-10")
    repo.add_claim(vault, a, "employed_by", object_ref=new)
    repo.create_event(vault, kind="meal", status="planned",
                      start_at=(today + timedelta(days=2)).isoformat(), participants=[a])
    repo.create_event(vault, kind="call", status="occurred",
                      start_at=(today - timedelta(days=1)).isoformat(), participants=[a])
    view = scoring.today_view(vault, today)
    assert len(view["briefs"]) == 1 and len(view["debriefs"]) == 1
    assert [f["text"] for f in view["fact_changes"]] == ["Ana now works at NewCo"]
    triggers.set_state(vault, "fact_change", view["fact_changes"][0]["id"], "dismissed")
    assert scoring.today_view(vault, today)["fact_changes"] == []
    b = brief.event_brief(vault, view["briefs"][0]["id"], today)
    assert b["people"][0]["entity"]["display_name"] == "Ana"
    assert any(r["other_name"] == "NewCo" for r in b["people"][0]["work"])


# ── graph ───────────────────────────────────────────────────────────────────

def test_graph_and_warm_paths(vault, today):
    mia = _contact(vault, "Mia", "inner")
    sarah = _contact(vault, "Sarah", "close")
    raj = repo.create_entity(vault, "person", "Raj", engagement="acquaintance")
    target = repo.create_entity(vault, "person", "Target Person", engagement="reference")
    for p in (mia, sarah):
        repo.create_event(vault, kind="meal", status="occurred", start_at=today.isoformat(),
                          participants=[p])
    repo.add_claim(vault, mia, "friend_of", object_ref=raj)
    repo.add_claim(vault, raj, "colleague_of", object_ref=target)
    repo.add_claim(vault, sarah, "knows", object_ref=target)
    data = graph.build(vault, today=today)
    ids = {n["id"] for n in data["nodes"]}
    assert {mia, sarah, raj, target} <= ids
    centered = graph.build(vault, center=target, depth=1, today=today)
    assert {n["id"] for n in centered["nodes"]} == {target, raj, sarah}
    paths = graph.warm_paths(vault, target, today=today)
    assert len(paths) == 2
    assert [h["name"] for h in paths[0]["hops"]][-1] == "Target Person"
    assert {p["ask"] for p in paths} == {"Mia", "Sarah"}


def test_phase2_pages(vault, today, tmp_path):
    c = TestClient(create_app(vault, today_fn=lambda: today))
    a = _contact(vault, "Robert Lang")
    b = repo.create_entity(vault, "person", "Bob Lang")
    assert "Possible duplicates" in c.get("/review").text
    assert c.get(f"/merge?a={a}&b={b}").status_code == 200
    r = c.post("/merge", data={"keep": a, "drop": b}, follow_redirects=False)
    assert r.status_code == 303
    assert "Recent merges" in c.get("/review").text
    with open(FILES / "contacts.vcf", "rb") as fh:
        prev = c.post("/import/vcard", files={"file": ("contacts.vcf", fh, "text/vcard")})
    assert "Preview: vcard" in prev.text
    content = (FILES / "contacts.vcf").read_text()
    done = c.post("/import/vcard", data={"content": content, "confirm": "1"}, follow_redirects=False)
    assert "vcard" in done.headers["location"]
    with open(FILES / "linkedin.csv", "rb") as fh:
        prev = c.post("/import/csv", files={"file": ("linkedin.csv", fh, "text/csv")})
    assert "Column matching" in prev.text
    ev = repo.create_event(vault, kind="meal", status="planned",
                           start_at=(today + timedelta(days=1)).isoformat(), participants=[a])
    assert "Brief:" in c.get(f"/event/{ev}/brief").text
    assert c.get("/graph").status_code == 200
    assert c.get("/api/graph").json()["nodes"]
    assert c.get(f"/graph?to={a}").status_code == 200
    c.post(f"/outreach/due/{a}", data={"status": "snoozed", "days": "7"})
    assert vault.scalar("select status from outreach_state where key=?", (a,)) == "snoozed"
