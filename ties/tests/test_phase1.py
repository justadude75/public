from datetime import timedelta

from ties import capture, importer, inference, memory, repo, scoring, textindex


class FakeAI:
    """Stands in for a local model in tests."""
    chat_model = "fake-chat"
    embed_model = "fake-embed"
    name = "model:fake-chat"

    def __init__(self, extraction=None):
        self.extraction = extraction or {}

    def chat_json(self, system, user):
        return self.extraction

    def chat(self, system, user, json_mode=False, temperature=0.2):
        return "Summary from the model [F1]."

    def embed(self, texts):
        vocab = ["climate", "storage", "payments", "sailing", "denver", "grid"]
        return [[float(t.lower().count(w)) + 0.01 for w in vocab] for t in texts]


def _contact(vault, name, tier="active"):
    pid = repo.create_entity(vault, "person", name, engagement="contact")
    repo.update_profile(vault, pid, tier, None, None, None, None)
    return pid


# ── inference rules ─────────────────────────────────────────────────────────

def test_shared_small_events_rule(vault, today):
    a, b, c = (_contact(vault, n) for n in ("Ana", "Ben", "Cy"))
    for n in (3, 10):
        repo.create_event(vault, kind="meal", status="occurred",
                          start_at=(today - timedelta(days=n)).isoformat(), participants=[a, b])
    repo.create_event(vault, kind="meal", status="occurred", start_at=today.isoformat(),
                      participants=[a, c])
    added = inference.run(vault)
    assert added["shared_events"] == 1
    sugg = repo.proposed_claims(vault)
    assert len(sugg) == 1 and sugg[0]["predicate_id"] == "knows"
    assert "2 small events" in sugg[0]["qual"]["why"]
    assert inference.run(vault)["shared_events"] == 0          # not proposed twice
    repo.reject_claim(vault, sugg[0]["id"])
    assert inference.run(vault)["shared_events"] == 0          # rejection is remembered


def test_shared_connector_and_confirm(vault):
    mia = _contact(vault, "Mia")
    s, r = _contact(vault, "Sarah"), _contact(vault, "Raj")
    repo.update_profile(vault, s, "close", None, None, None, mia)
    repo.update_profile(vault, r, "active", None, None, None, mia)
    added = inference.run(vault)
    assert added["shared_connector"] == 3   # Mia knows Sarah, Mia knows Raj, Sarah~Raj share Mia
    knows = [p for p in repo.proposed_claims(vault) if p["predicate_id"] == "knows"][0]
    repo.confirm_claim(vault, knows["id"], basis="told")
    rels = repo.relations(vault, mia)
    assert any(x["label"] == "Knows" and x["basis"] == "told" for x in rels)


def test_overlapping_tenure_needs_small_org(vault):
    a, b = _contact(vault, "Ana"), _contact(vault, "Ben")
    big = repo.create_entity(vault, "organization", "BigCo")
    small = repo.create_entity(vault, "organization", "TinyCo")
    repo.add_claim(vault, small, "size", value="12")
    for org in (big, small):
        repo.add_claim(vault, a, "employed_by", object_ref=org, valid_from="2018", valid_to="2021")
        repo.add_claim(vault, b, "employed_by", object_ref=org, valid_from="2020")
    added = inference.run(vault)
    assert added["overlapping_tenure"] == 1
    assert "TinyCo" in repo.proposed_claims(vault)[0]["qual"]["why"]


def test_same_school_rule_off_by_default(vault):
    a, b = _contact(vault, "Ana"), _contact(vault, "Ben")
    school = repo.create_entity(vault, "organization", "State U")
    for p in (a, b):
        repo.add_claim(vault, p, "studied_at", object_ref=school, valid_to="2012")
    assert inference.run(vault)["same_school_year"] == 0
    inference.save_rule_settings(vault, {**inference.rule_settings(vault), "same_school_year": True})
    assert inference.run(vault)["same_school_year"] == 1


def test_inferences_are_kept_apart_from_facts(vault):
    a = _contact(vault, "Ana")
    repo.add_claim(vault, a, "fact", value="open to advisory roles", basis="inferred",
                   confidence=0.5)
    assert repo.facts(vault, a) == []
    assert repo.inferences(vault, a)[0]["value_text"] == "open to advisory roles"


# ── debrief ─────────────────────────────────────────────────────────────────

def test_debrief_records_everything(vault, today):
    s = _contact(vault, "Sarah Chen", "close")
    ev = repo.create_event(vault, kind="meeting", status="planned",
                           start_at=(today - timedelta(days=1)).isoformat(), participants=[s])
    counts = repo.debrief_event(
        vault, ev, summary="Talked about her move.",
        learned=[{"subject": s, "predicate": "employed_by", "object": "new:organization:Voltgrid"},
                 {"subject": s, "predicate": "interest", "value": "grid storage"}],
        inferences_=[{"subject": s, "predicate": "fact", "value": "open to advising",
                      "confidence": "0.5", "why": "asked about boards"}],
        follow_ups=[{"owner": "me", "text": "Intro to Raj", "due": "2026-11-14"},
                    {"owner": "them", "text": "Send deck"}],
        mentioned=[{"ref": "new:person:Priya Nair", "predicate": "colleague_of", "to": s}],
        links_=[{"url": "example.com/report", "note": "her recommendation", "target": s}],
        next_contact="2026-11-30")
    assert counts == {"facts": 2, "inferences": 1, "follow_ups": 2, "people": 1, "links": 1}
    e = repo.get_event(vault, ev)
    assert e["status"] == "occurred" and "her move" in e["notes"]
    assert [l["uri"] for l in repo.links(vault, s)] == ["https://example.com/report"]
    assert {r["other_name"] for r in repo.relations(vault, s)} == {"Voltgrid", "Priya Nair"}
    st = scoring.contact_status(vault, s, today)
    assert st.next_due.isoformat() == "2026-11-30" and not st.is_due


# ── capture ─────────────────────────────────────────────────────────────────

def test_capture_structured_lines_without_model(vault, today):
    s = _contact(vault, "Sarah Chen")
    text = ("Coffee this morning, she is leaving Paybridge.\n"
            "- [[Sarah Chen]] interest:: grid storage\n"
            "- [ ] Send her the report due:: 2026-11-12\n")
    recs = capture.build_records(text, with_ids=[s], day=today.isoformat(), kind="meeting")
    preview = importer.apply(vault, "capture", recs, dry_run=True)
    assert preview.events_added and preview.follow_ups_added == ["Send her the report"]
    importer.apply(vault, "capture", recs)
    assert scoring.contact_status(vault, s, today).days_since == 0
    assert any(f["value_text"] == "grid storage" for f in repo.facts(vault, s))
    assert repo.commitments(vault, s)[0]["due_at"] == "2026-11-12"


def test_capture_with_model_proposes_facts(vault, today):
    s = _contact(vault, "Sarah Chen")
    ai = FakeAI({"facts": [{"subject": "Sarah Chen", "predicate": "employed_by", "value": None,
                            "object": "Voltgrid"}],
                 "follow_ups": [{"owner": "them", "person": "Sarah Chen", "text": "Send the deck",
                                 "due": None}],
                 "people": [{"name": "Priya Nair", "type": "person"}]})
    preds = repo.predicates(vault)
    data = capture.extract_with_model(ai, "She is joining Voltgrid", preds, today)
    recs = capture.build_records("She is joining Voltgrid", with_ids=[s], day=today.isoformat(),
                                 model_data=data, predicate_ids={p["id"] for p in preds})
    importer.apply(vault, "capture", recs)
    proposed = repo.proposed_claims(vault)
    assert proposed[0]["object_name"] == "Voltgrid" and proposed[0]["status"] == "proposed"
    assert repo.relations(vault, s) == []   # not a fact until confirmed
    assert repo.commitments(vault, s)[0]["mine"] is False
    assert repo.search_entities(vault, "priya")


# ── memory and search ───────────────────────────────────────────────────────

def test_rules_summary_and_changes_since_contact(vault, today):
    s = _contact(vault, "Sarah Chen")
    org = repo.create_entity(vault, "organization", "Paybridge")
    repo.add_claim(vault, s, "title", value="Head of Partnerships")
    repo.add_claim(vault, s, "employed_by", object_ref=org)
    repo.create_event(vault, kind="meeting", status="occurred",
                      start_at=(today - timedelta(days=5)).isoformat(), participants=[s],
                      notes="Talked about grid storage.")
    text = memory.get_summary(vault, s, today)["text"]
    assert text.startswith("Head of Partnerships at Paybridge.")
    assert "grid storage" in text
    changes = memory.changed_since_last_contact(vault, s, today)
    assert {c["predicate_id"] for c in changes} >= {"title", "employed_by"}
    ai = FakeAI()
    assert memory.regenerate(vault, s, ai, today)["generator"] == "model:fake-chat"


def test_hybrid_search_keyword_and_semantic(vault, today):
    s = _contact(vault, "Sarah Chen")
    repo.add_claim(vault, s, "interest", value="sailing")
    repo.create_event(vault, kind="meeting", status="occurred", start_at=today.isoformat(),
                      participants=[s], notes="We discussed grid storage and climate policy.")
    res = textindex.search(vault, "sailing")
    assert res["entities"][0]["display_name"] == "Sarah Chen"
    res = textindex.search(vault, "storage")
    assert res["notes"] and res["notes"][0]["owner"]["type_id"] == "event"
    ai = FakeAI()
    assert textindex.embed_pending(vault, ai) > 0
    res = textindex.search(vault, "climate", ai=ai)
    assert res["semantic"] and res["notes"]
    assert textindex.rebuild(vault)["entities"] >= 2
