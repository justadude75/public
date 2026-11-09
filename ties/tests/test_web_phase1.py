import shutil
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ties import repo
from ties.web import create_app

FIXTURES = Path(__file__).parent / "fixtures" / "notes"


@pytest.fixture
def client(vault, today):
    return TestClient(create_app(vault, today_fn=lambda: today))


def test_markdown_import_flow(client, vault, tmp_path):
    notes = tmp_path / "notes"
    shutil.copytree(FIXTURES, notes)
    preview = client.post("/import/markdown", data={"folder": str(notes)})
    assert preview.status_code == 200 and "Nothing has been saved yet" in preview.text
    assert "Sarah Chen (person)" in preview.text
    done = client.post("/import/markdown", data={"folder": str(notes), "confirm": "1"},
                       follow_redirects=False)
    assert "ID+line+added+to+2+notes" in done.headers["location"]
    page = client.get("/import").text
    assert "markdown" in page and "Undo" in page
    bad = client.post("/import/markdown", data={"folder": str(tmp_path / "nope")},
                      follow_redirects=False)
    assert "error=" in bad.headers["location"]


def test_capture_preview_then_save(client, vault):
    sid = repo.create_entity(vault, "person", "Sarah Chen", engagement="contact")
    data = {"participants": [sid], "date": "2026-11-09", "kind": "meeting",
            "channel": "in_person", "text": "Lunch.\n- [ ] Send her the report due:: 2026-11-12"}
    preview = client.post("/capture", data=data)
    assert "Check before saving" in preview.text and "Send her the report" in preview.text
    assert repo.commitments(vault) == []
    saved = client.post("/capture", data={**data, "confirm": "1", "origin": "capture:x"},
                        follow_redirects=False)
    assert saved.status_code == 303
    assert repo.commitments(vault)[0]["description"] == "Send her the report"


def test_debrief_page_and_submit(client, vault, today):
    sid = repo.create_entity(vault, "person", "Sarah Chen", engagement="contact")
    ev = repo.create_event(vault, kind="meeting", status="planned",
                           start_at=(today - timedelta(days=1)).isoformat(), participants=[sid])
    page = client.get(f"/event/{ev}/debrief")
    assert page.status_code == 200 and "What you learned" in page.text
    r = client.post(f"/event/{ev}/debrief", data={
        "summary": "Good chat.", "learned-0-subject": sid, "learned-0-predicate": "interest",
        "learned-0-value": "sailing", "learned-0-basis": "told",
        "fu-0-owner": "me", "fu-0-who": sid, "fu-0-text": "Send the article",
        "link-0-url": "https://example.com/a", "link-0-target": sid,
        "inf-0-subject": sid, "inf-0-predicate": "fact", "inf-0-value": "thinking of moving",
        "inf-0-confidence": "0.6", "next_contact": "2026-12-01"}, follow_redirects=False)
    assert r.status_code == 303 and "Debrief+saved" in r.headers["location"]
    profile = client.get(f"/e/{sid}").text
    assert "sailing" in profile and "thinking of moving" in profile and "example.com/a" in profile
    assert "Memory" in profile


def test_review_confirm_and_reject(client, vault, today):
    a = repo.create_entity(vault, "person", "Ana", engagement="contact")
    b = repo.create_entity(vault, "person", "Ben", engagement="contact")
    for n in (1, 2):
        repo.create_event(vault, kind="meal", status="occurred",
                          start_at=(today - timedelta(days=n)).isoformat(), participants=[a, b])
    page = client.get("/review").text
    assert "Suggested by rules" in page and "2 small events" in page
    cid = repo.proposed_claims(vault)[0]["id"]
    client.post(f"/claim/{cid}/confirm", data={"basis": "observed"})
    assert any(r["label"] == "Knows" for r in repo.relations(vault, a))
    assert "All clear" in client.get("/review").text


def test_search_and_settings(client, vault):
    sid = repo.create_entity(vault, "person", "Sarah Chen")
    repo.add_claim(vault, sid, "interest", value="sailing")
    assert "Sarah Chen" in client.get("/search", params={"q": "sailing"}).text
    assert client.get("/settings").status_code == 200
    r = client.post("/settings", data={"section": "ai", "ai_base_url": "https://api.example.com/v1"},
                    follow_redirects=False)
    assert "error=" in r.headers["location"]
    client.post("/settings", data={"section": "ai", "ai_base_url": "http://127.0.0.1:11434/v1",
                                   "ai_chat_model": "m"})
    assert vault.get_meta("ai_base_url") == "http://127.0.0.1:11434/v1"
    client.post("/settings", data={"section": "rules", "shared_events": "1"})
    from ties import inference
    assert inference.rule_settings(vault)["shared_connector"] is False
    r = client.post("/settings/reindex", follow_redirects=False)
    assert "Indexed" in r.headers["location"]
