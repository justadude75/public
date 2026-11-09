import pytest

from ties import repo
from ties.db import Vault


def test_init_is_idempotent(vault):
    ego = repo.ego_id(vault)
    assert repo.init_vault(vault, "Someone Else") == ego
    assert repo.get_entity(vault, ego)["engagement"] == "ego"


def test_reopen_keeps_data_and_schema_version(tmp_path):
    path = tmp_path / "v.vault"
    v = Vault(path)
    repo.init_vault(v, "Me")
    pid = repo.create_entity(v, "person", "Ana Silva", engagement="contact")
    v.close()
    v2 = Vault(path)
    assert repo.get_entity(v2, pid)["display_name"] == "Ana Silva"
    assert int(v2.get_meta("schema_version")) >= 2


def test_duplicate_detection_reasons(vault):
    repo.create_entity(vault, "person", "Robert Lang", identifiers=[("email", "Rob@Example.com ")])
    repo.create_entity(vault, "person", "Sarah Chen")
    by_nick = repo.duplicate_candidates(vault, "person", "Bob Lang")
    assert [d["display_name"] for d in by_nick] == ["Robert Lang"]
    assert "nickname of the same name" in by_nick[0]["reasons"]
    by_email = repo.duplicate_candidates(vault, "person", "Totally Different",
                                         [("email", "rob@example.com")])
    assert by_email and "same email" in by_email[0]["reasons"]
    assert repo.duplicate_candidates(vault, "person", "Sara Chen")[0]["reasons"] == ["similar spelling"]
    assert repo.duplicate_candidates(vault, "person", "S Chen")[0]["display_name"] == "Sarah Chen"
    assert repo.duplicate_candidates(vault, "person", "Maria Gomez") == []


def test_search_matches_alias_and_nickname(vault):
    pid = repo.create_entity(vault, "person", "Thomas Reyes", aliases="TR")
    assert repo.search_entities(vault, "tom")[0]["id"] == pid
    assert repo.search_entities(vault, "reyes")[0]["id"] == pid
    assert repo.search_entities(vault, "tr")[0]["id"] == pid
    assert repo.search_entities(vault, "Test Owner") == []  # owner excluded


def test_single_valued_fact_supersedes(vault):
    pid = repo.create_entity(vault, "person", "Ana Silva")
    repo.add_claim(vault, pid, "title", value="Engineer")
    repo.add_claim(vault, pid, "title", value="Engineering Manager")
    titles = [f["value_text"] for f in repo.facts(vault, pid) if f["predicate_id"] == "title"]
    assert titles == ["Engineering Manager"]
    statuses = {h["value_text"]: h["status"] for h in repo.claim_history(vault, pid)}
    assert statuses["Engineer"] == "superseded"


def test_multi_valued_facts_accumulate_and_dedupe(vault):
    pid = repo.create_entity(vault, "person", "Ana Silva")
    repo.add_claim(vault, pid, "interest", value="sailing")
    repo.add_claim(vault, pid, "interest", value="sailing")
    repo.add_claim(vault, pid, "interest", value="chess")
    assert sorted(f["value_text"] for f in repo.facts(vault, pid)) == ["chess", "sailing"]


def test_relationships_read_from_both_sides(vault):
    a = repo.create_entity(vault, "person", "Ana Silva")
    b = repo.create_entity(vault, "person", "Ben Ode")
    org = repo.create_entity(vault, "organization", "Acme")
    repo.add_claim(vault, a, "friend_of", object_ref=b)
    repo.add_claim(vault, b, "friend_of", object_ref=a)  # symmetric duplicate is skipped
    repo.add_claim(vault, a, "parent_of", object_ref=f"new:person:Kid Silva")
    repo.add_claim(vault, b, "employed_by", object_ref=org)
    labels_b = sorted(r["label"] for r in repo.relations(vault, b))
    assert labels_b == ["Friend of", "Works at"]
    kid = [r for r in repo.relations(vault, a) if r["label"] == "Parent of"][0]["other_id"]
    assert [r["label"] for r in repo.relations(vault, kid)] == ["Child of"]
    assert [r["label"] for r in repo.relations(vault, org)] == ["Employs"]


def test_end_and_retract_claim(vault):
    pid = repo.create_entity(vault, "person", "Ana Silva")
    org = repo.create_entity(vault, "organization", "Acme")
    cid = repo.add_claim(vault, pid, "employed_by", object_ref=org, valid_from="2020")
    repo.end_claim(vault, cid, "2026-10")
    rel = repo.relations(vault, pid)[0]
    assert (rel["valid_from"], rel["valid_to"]) == ("2020", "2026-10")
    repo.retract_claim(vault, rel["id"])
    assert repo.relations(vault, pid) == []


def test_claim_validation(vault):
    pid = repo.create_entity(vault, "person", "Ana Silva")
    with pytest.raises(ValueError):
        repo.add_claim(vault, pid, "employed_by", value="Acme")  # needs a linked entity
    with pytest.raises(ValueError):
        repo.add_claim(vault, pid, "title", value="  ")
    with pytest.raises(ValueError):
        repo.add_claim(vault, pid, "knows", object_ref=pid)


def test_event_with_new_people_follow_ups_and_tags(vault):
    sarah = repo.create_entity(vault, "person", "Sarah Chen", engagement="contact")
    eid = repo.create_event(
        vault, kind="meeting", status="occurred", start_at="2026-11-09",
        participants=[sarah, "new:person:Priya Nair"], location_ref="new:place:Blue Bottle",
        notes="Talked about Voltgrid.", tags="topic:climate", initiated_by="me",
        follow_ups=[{"text": "Intro to Raj", "due": "2026-11-14", "owner": "me"},
                    {"text": "Send deck", "owner": "them"}])
    ev = repo.get_event(vault, eid)
    names = {p["display_name"] for p in ev["participants"]}
    assert names == {"Test Owner", "Sarah Chen", "Priya Nair"}
    assert ev["location_name"] == "Blue Bottle" and ev["tags"] == ["topic:climate"]
    assert ev["title"] == "Meeting with Sarah Chen, Priya Nair"
    loops = repo.commitments(vault, sarah)
    assert {(l["description"], l["mine"]) for l in loops} == {("Intro to Raj", True),
                                                               ("Send deck", False)}
    priya = repo.search_entities(vault, "priya")[0]
    assert priya["engagement"] == "acquaintance"
    repo.add_event_note(vault, eid, "Follow-up thought.")
    assert repo.get_event(vault, eid)["notes"].endswith("Follow-up thought.")


def test_update_entity_rename_keeps_former_name(vault):
    pid = repo.create_entity(vault, "person", "Ana Silva")
    repo.update_entity(vault, pid, "Ana Costa", "contact", "Annie", "context:work")
    assert repo.search_entities(vault, "silva")[0]["id"] == pid
    assert repo.get_profile(vault, pid) is not None
    assert repo.tags_for(vault, "entity", pid) == ["context:work"]


def test_export_contains_all_tables(vault):
    repo.create_entity(vault, "person", "Ana Silva")
    data = repo.export_all(vault)
    assert data["format"] == "ties-export"
    assert {"entity", "claim", "event_detail", "commitment", "audit_log"} <= set(data["tables"])
    assert any(r["display_name"] == "Ana Silva" for r in data["tables"]["entity"])


def test_follow_up_attaches_to_named_participant(vault):
    tom = repo.create_entity(vault, "person", "Tom Reyes", engagement="contact")
    nora = repo.create_entity(vault, "person", "Nora Quinn", engagement="contact")
    repo.create_event(vault, kind="meeting", status="occurred", start_at="2026-11-09",
                      participants=[tom, nora],
                      follow_ups=[{"text": "Send Nora the deck"},
                                  {"text": "Book the next review"}])
    assert [l["description"] for l in repo.commitments(vault, nora)] == ["Send Nora the deck"]
    assert [l["description"] for l in repo.commitments(vault, tom)] == ["Book the next review"]
