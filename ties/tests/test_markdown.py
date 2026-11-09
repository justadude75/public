import shutil
from pathlib import Path

import pytest

from ties import importer, markdown, repo, scoring

FIXTURES = Path(__file__).parent / "fixtures" / "notes"


@pytest.fixture
def notes(tmp_path):
    dest = tmp_path / "notes"
    shutil.copytree(FIXTURES, dest)
    return dest


def _person(vault, name):
    return repo.search_entities(vault, name)[0]["id"]


def test_dry_run_changes_nothing(vault, notes):
    report, info = markdown.import_folder(vault, notes, dry_run=True)
    assert report.dry_run and report.changes > 0
    assert repo.search_entities(vault, "sarah") == []
    assert "gid:" not in (notes / "People" / "Sarah Chen.md").read_text()


def test_person_note(vault, notes, today):
    report, info = markdown.import_folder(vault, notes)
    assert info["gids_written"] == 2
    assert (notes / "People" / "Sarah Chen.md").read_text().startswith("---\ngid: ")
    sarah = _person(vault, "Sarah Chen")
    ent = repo.get_entity(vault, sarah)
    assert ent["engagement"] == "contact"
    prof = repo.get_profile(vault, sarah)
    assert (prof["tier"], prof["cadence_days"]) == ("close", 30)
    assert prof["introduced_by_name"] == "Mia Lopez"
    facts = {(f["predicate_id"], f["value_text"]) for f in repo.facts(vault, sarah)}
    assert ("title", "Head of Payments Partnerships") in facts
    assert ("birthday", "1990-04-12") in facts
    assert ("interest", "grid-scale storage") in facts
    rels = {(r["label"], r["other_name"]) for r in repo.relations(vault, sarah)}
    assert ("Works at", "Stripe") in rels and ("Lives in", "Oakland") in rels
    assert ("Knows", "Raj Mehta") in rels and ("Colleague of", "Dev Patel") in rels
    assert ("Family of", "University of Michigan") not in rels
    infs = repo.inferences(vault, sarah)
    assert any(i["value_text"] == "advisory roles" and i["basis"] == "inferred" for i in infs)
    assert [l["uri"] for l in repo.links(vault, sarah)] == ["https://example.com/sarah-talk"]
    assert repo.identifiers(vault, sarah)[0]["value"] == "sarah@example.com"
    assert any("shoe_size" in w for w in report.warnings)
    assert "context:stripe" in repo.tags_for(vault, "entity", sarah)


def test_meeting_note(vault, notes, today):
    markdown.import_folder(vault, notes)
    sarah = _person(vault, "Sarah Chen")
    ev = [e for e in repo.list_events(vault, entity_id=sarah) if e["title"] == "Coffee with Sarah Chen"][0]
    full = repo.get_event(vault, ev["id"])
    assert full["start_at"] == "2026-11-09T09:30" and full["location_name"] == "Blue Bottle Oakland"
    assert "nervous" in full["notes"]
    loops = {(l["description"], l["mine"], l["due_at"]) for l in repo.commitments(vault, sarah)}
    assert ("Intro Sarah Chen to Raj Mehta", True, "2026-11-14") in loops
    assert ("Send her the grid storage report", True, "2026-11-12") in loops
    assert ("send me the Voltgrid deck for feedback", False, "2026-11-20") in loops
    st = scoring.contact_status(vault, sarah, today)
    assert st.next_due.isoformat() == "2026-11-30"
    priya = _person(vault, "Priya Nair")
    assert any(r["other_name"] == "Sarah Chen" for r in repo.relations(vault, priya))
    voltgrid = repo.search_entities(vault, "voltgrid")[0]
    assert voltgrid["type_id"] == "organization"
    assert any(i["value_text"] == "partnerships roles" for i in repo.inferences(vault, voltgrid["id"]))


def test_daily_note_lines(vault, notes):
    markdown.import_folder(vault, notes)
    dev = _person(vault, "Dev Patel")
    evs = repo.list_events(vault, entity_id=dev)
    assert evs[0]["start_at"] == "2026-11-08T14:00" and evs[0]["kind"] == "call"
    raj = _person(vault, "Raj Mehta")
    dinner = repo.get_event(vault, repo.list_events(vault, entity_id=raj)[0]["id"])
    assert dinner["location_name"] == "Nopa"
    assert {p["display_name"] for p in dinner["participants"]} >= {"Raj Mehta", "Mia Lopez"}
    assert [l["description"] for l in repo.commitments(vault, dev)] == ["Send Dev Patel the Denver intro list"]


def test_reimport_is_idempotent_and_tracks_edits(vault, notes):
    markdown.import_folder(vault, notes)
    again, _ = markdown.import_folder(vault, notes)
    assert again.changes == 0, again
    path = notes / "People" / "Sarah Chen.md"
    text = path.read_text()
    path.write_text(text.replace("- interest:: grid-scale storage {basis: told}\n",
                                 "- interest:: sailing\n").replace("Prefers morning", "Prefers evening"))
    shutil.move(str(path), str(notes / "People" / "Sarah C.md"))  # renamed in Obsidian
    report, _ = markdown.import_folder(vault, notes)
    sarah = _person(vault, "Sarah Chen")
    interests = [f["value_text"] for f in repo.facts(vault, sarah) if f["predicate_id"] == "interest"]
    assert interests == ["sailing"]
    assert len(repo.search_entities(vault, "sarah", type_id="person")) == 1  # the rename did not create a duplicate
    assert report.claims_retired


def test_hand_entered_fact_is_not_overwritten(vault, notes):
    sarah = repo.create_entity(vault, "person", "Sarah Chen", engagement="contact")
    repo.add_claim(vault, sarah, "title", value="VP Partnerships")
    report, _ = markdown.import_folder(vault, notes)
    assert any("title" in c for c in report.conflicts)
    titles = [f["value_text"] for f in repo.facts(vault, sarah) if f["predicate_id"] == "title"]
    assert titles == ["VP Partnerships"]
    disputed = repo.proposed_claims(vault, "disputed")
    assert disputed and disputed[0]["current"]["value_text"] == "VP Partnerships"
    repo.resolve_dispute(vault, disputed[0]["id"], keep_new=True)
    titles = [f["value_text"] for f in repo.facts(vault, sarah) if f["predicate_id"] == "title"]
    assert titles == ["Head of Payments Partnerships"]


def test_rollback_batch(vault, notes):
    report, _ = markdown.import_folder(vault, notes)
    importer.rollback(vault, report.batch_id)
    assert repo.search_entities(vault, "priya") == []
    assert repo.commitments(vault) == []
    # importing again after rollback recreates everything
    again, _ = markdown.import_folder(vault, notes)
    assert repo.search_entities(vault, "priya")
