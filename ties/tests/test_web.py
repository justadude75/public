import importlib.util

import pytest
from fastapi.testclient import TestClient

from ties import repo
from ties.db import Vault, VaultError
from ties.demo import build_demo
from ties.web import create_app


@pytest.fixture
def client(vault, today):
    return TestClient(create_app(vault, today_fn=lambda: today))


def test_pages_render_with_demo_data(tmp_path, today):
    v = Vault(tmp_path / "demo.vault")
    build_demo(v, today)
    c = TestClient(create_app(v, today_fn=lambda: today))
    home = c.get("/")
    assert home.status_code == 200 and "Sarah Chen" in home.text and "Did these happen?" in home.text
    for url in ("/people", "/people?sort=due", "/timeline", "/new", "/new?type=organization",
                "/log", "/log?status=planned"):
        assert c.get(url).status_code == 200, url
    sarah = repo.search_entities(v, "sarah")[0]["id"]
    page = c.get(f"/e/{sarah}?history=1")
    assert page.status_code == 200
    assert "Head of Partnerships" in page.text and "Intro Sarah to Raj" in page.text
    ev = repo.list_events(v, entity_id=sarah)[0]["id"]
    assert c.get(f"/event/{ev}").status_code == 200


def test_create_person_flow_with_duplicate_check(client, vault):
    r = client.post("/new", data={"type_id": "person", "name": "Sarah Chen",
                                  "engagement": "contact", "tier": "close",
                                  "employed_by": "new:organization:Paybridge",
                                  "email": "sarah@example.com", "tags": "context:work"},
                    follow_redirects=False)
    assert r.status_code == 303
    sid = r.headers["location"].split("/e/")[1].split("?")[0]
    assert repo.get_profile(vault, sid)["tier"] == "close"
    assert repo.relations(vault, sid)[0]["other_name"] == "Paybridge"

    dup = client.post("/new", data={"type_id": "person", "name": "Sara Chen",
                                    "engagement": "contact"})
    assert "Is this someone you already have?" in dup.text
    forced = client.post("/new", data={"type_id": "person", "name": "Sara Chen",
                                       "engagement": "contact", "force": "1"},
                         follow_redirects=False)
    assert forced.status_code == 303


def test_log_interaction_updates_contact_and_today(client, vault, today):
    pid = repo.create_entity(vault, "person", "Ana Silva", engagement="contact")
    assert "Ana Silva" in client.get("/").text  # never contacted, so due
    r = client.post("/log", data={
        "status": "occurred", "participants": [pid], "date": today.isoformat(),
        "kind": "meeting", "channel": "in_person", "notes": "Coffee", "counts_as_contact": "1",
        "fu_text": ["Send article", ""], "fu_due": ["", ""], "fu_owner": ["me", "me"]},
        follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/e/{pid}")
    page = client.get(f"/e/{pid}").text
    assert "0 days ago" in page and "Send article" in page
    assert "Nobody is due" in client.get("/").text


def test_fact_and_relationship_forms(client, vault):
    pid = repo.create_entity(vault, "person", "Ana Silva")
    client.post(f"/e/{pid}/fact", data={"predicate_id": "interest", "value": "sailing",
                                        "basis": "told", "confidence": "0.8"})
    client.post(f"/e/{pid}/fact", data={"predicate_id": "knows",
                                        "object": "new:person:Ben Ode", "basis": "heard"})
    bad = client.post(f"/e/{pid}/fact", data={"predicate_id": "knows"}, follow_redirects=False)
    assert "error=" in bad.headers["location"]
    page = client.get(f"/e/{pid}").text
    assert "sailing" in page and "Ben Ode" in page and "heard" in page


def test_api_search_and_export(client, vault):
    repo.create_entity(vault, "organization", "Paybridge")
    res = client.get("/api/search", params={"q": "payb"}).json()
    assert res[0]["name"] == "Paybridge" and res[0]["type"] == "organization"
    assert client.get("/api/search", params={"q": "payb", "type": "person"}).json() == []
    exp = client.get("/export")
    assert exp.status_code == 200 and exp.json()["format"] == "ties-export"


@pytest.mark.skipif(importlib.util.find_spec("sqlcipher3") is None,
                    reason="sqlcipher3 not installed")
def test_encrypted_vault_round_trip(tmp_path):
    path = tmp_path / "secret.vault"
    v = Vault(path, "correct horse")
    repo.init_vault(v, "Me")
    repo.create_entity(v, "person", "Hidden Person")
    v.close()
    assert b"Hidden Person" not in path.read_bytes()
    with pytest.raises(VaultError):
        Vault(path)
    with pytest.raises(VaultError):
        Vault(path, "wrong")
    v2 = Vault(path, "correct horse")
    assert repo.search_entities(v2, "hidden")[0]["display_name"] == "Hidden Person"
