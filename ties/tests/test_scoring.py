from datetime import timedelta

from ties import repo, scoring


def _contact(vault, name, tier="active", cadence=None):
    pid = repo.create_entity(vault, "person", name, engagement="contact")
    repo.update_profile(vault, pid, tier, cadence, None, None, None)
    return pid


def _log(vault, pid, day, status="occurred", **kw):
    return repo.create_event(vault, kind=kw.pop("kind", "meeting"), status=status,
                             start_at=day.isoformat(), participants=[pid], **kw)


def test_never_contacted_is_due(vault, today):
    pid = _contact(vault, "Ana")
    st = scoring.contact_status(vault, pid, today)
    assert st.is_due and st.last_contact is None
    assert "No contact logged yet" in st.reasons


def test_days_since_and_due(vault, today):
    pid = _contact(vault, "Ana", tier="close")  # every 30 days
    _log(vault, pid, today - timedelta(days=41))
    _log(vault, pid, today - timedelta(days=100))
    st = scoring.contact_status(vault, pid, today)
    assert st.days_since == 41 and st.cadence == 30
    assert st.days_to_due == -11 and st.is_due
    assert st.reasons[0].startswith("Overdue by 11 days")


def test_not_due_before_threshold(vault, today):
    pid = _contact(vault, "Ana", cadence=30)
    _log(vault, pid, today - timedelta(days=20))
    st = scoring.contact_status(vault, pid, today)
    assert st.cadence_source == "custom" and not st.is_due and st.days_to_due == 10


def test_non_contact_events_and_absent_me_do_not_count(vault, today):
    pid = _contact(vault, "Ana")
    _log(vault, pid, today - timedelta(days=3), counts_as_contact=False)
    _log(vault, pid, today - timedelta(days=2), include_me=False)
    _log(vault, pid, today - timedelta(days=1), status="cancelled")
    assert scoring.contact_status(vault, pid, today).last_contact is None


def test_planned_event_covers_outreach(vault, today):
    pid = _contact(vault, "Ana", tier="active")  # 60 days
    _log(vault, pid, today - timedelta(days=70))
    _log(vault, pid, today + timedelta(days=4), status="planned")
    st = scoring.contact_status(vault, pid, today)
    assert st.covered and not st.is_due
    assert any(r.startswith("Covered by") for r in st.reasons)


def test_far_future_plan_does_not_cover(vault, today):
    pid = _contact(vault, "Ana", tier="close")
    _log(vault, pid, today - timedelta(days=40))
    _log(vault, pid, today + timedelta(days=60), status="planned")
    st = scoring.contact_status(vault, pid, today)
    assert not st.covered and st.is_due


def test_learned_cadence_and_drift_reason(vault, today):
    pid = _contact(vault, "Ana", tier="inner")  # 14 days
    for n in (5, 65, 125, 185):
        _log(vault, pid, today - timedelta(days=n))
    st = scoring.contact_status(vault, pid, today)
    assert st.learned_cadence == 60
    assert any("actual rhythm" in r for r in st.reasons)


def test_reciprocity_flag(vault, today):
    pid = _contact(vault, "Ana")
    for n in (10, 40, 80, 120):
        _log(vault, pid, today - timedelta(days=n), initiated_by="me")
    st = scoring.contact_status(vault, pid, today)
    assert st.share_initiated_by_them == 0
    assert "You start most of the contact" in st.reasons


def test_strength_rises_with_recent_in_person_contact(vault, today):
    a = _contact(vault, "Ana")
    b = _contact(vault, "Ben")
    _log(vault, a, today - timedelta(days=2))
    _log(vault, b, today - timedelta(days=300), kind="message", channel="message")
    assert scoring.contact_status(vault, a, today).strength > \
        scoring.contact_status(vault, b, today).strength


def test_today_view_orders_and_collects(vault, today):
    close = _contact(vault, "Close Friend", tier="close")
    wide = _contact(vault, "Old Colleague", tier="wider")
    _log(vault, close, today - timedelta(days=45))
    _log(vault, wide, today - timedelta(days=400))
    repo.add_claim(vault, wide, "birthday", value="1988-" + (today + timedelta(days=3)).strftime("%m-%d"))
    repo.add_commitment(vault, close, "Send the article", (today - timedelta(days=1)).isoformat())
    _log(vault, close, today - timedelta(days=1), status="planned", title="Lunch")
    view = scoring.today_view(vault, today)
    assert [s.name for s in view["due"]] == ["Close Friend", "Old Colleague"]
    assert view["dates"][0]["in_days"] == 3
    assert view["loops"][0]["due_in"] == -1
    assert [e["title"] for e in view["unlogged"]] == ["Lunch"]


def test_leap_day_birthday(vault, today):
    pid = _contact(vault, "Leap")
    repo.add_claim(vault, pid, "birthday", value="2000-02-29")
    nxt = scoring._next_anniversary("2000-02-29", today)
    assert (nxt.month, nxt.day) in ((2, 28), (2, 29))
