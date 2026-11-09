"""Sample data for trying the app. All people and organizations are fictional."""

from __future__ import annotations

from datetime import date, timedelta

from . import repo
from .db import Vault


def build_demo(vault: Vault, today: date | None = None) -> None:
    today = today or date.today()

    def ago(n: int) -> str:
        return (today - timedelta(days=n)).isoformat()

    def ahead(n: int) -> str:
        return (today + timedelta(days=n)).isoformat()

    repo.init_vault(vault, "Jordan Avery")

    oak = repo.create_entity(vault, "place", "Oakland")
    sf = repo.create_entity(vault, "place", "San Francisco")
    denver = repo.create_entity(vault, "place", "Denver")
    paybridge = repo.create_entity(vault, "organization", "Paybridge", tags="topic:payments")
    voltgrid = repo.create_entity(vault, "organization", "Voltgrid",
                                  description="Early-stage grid storage startup")
    northfield = repo.create_entity(vault, "organization", "Northfield Ventures",
                                    tags="topic:climate")
    repo.add_claim(vault, voltgrid, "located_in", object_ref=oak)
    repo.add_claim(vault, northfield, "located_in", object_ref=sf)

    def person(name, tier, *, cadence=None, closeness=None, employer=None, city=None,
               title=None, birthday=None, tags="", email=None, aliases="", engagement="contact"):
        pid = repo.create_entity(vault, "person", name, engagement=engagement, tags=tags,
                                 aliases=aliases, identifiers=[("email", email or "")])
        if engagement == "contact":
            repo.update_profile(vault, pid, tier, cadence, closeness, None, None)
        if employer:
            repo.add_claim(vault, pid, "employed_by", object_ref=employer, basis="told")
        if city:
            repo.add_claim(vault, pid, "lives_in", object_ref=city, basis="told")
        if title:
            repo.add_claim(vault, pid, "title", value=title, basis="told")
        if birthday:
            repo.add_claim(vault, pid, "birthday", value=birthday, basis="told")
        return pid

    bday_soon = (today + timedelta(days=5)).strftime("1990-%m-%d")
    sarah = person("Sarah Chen", "close", closeness=4, employer=paybridge, city=oak,
                   title="Head of Partnerships", birthday="1990-04-12",
                   tags="context:work, topic:payments, topic:climate", email="sarah@example.com")
    raj = person("Raj Mehta", "active", employer=northfield, city=sf, title="Principal",
                 tags="topic:climate, context:investors", email="raj@example.com")
    dev = person("Dev Patel", "extended", city=denver, tags="context:college, context:work")
    mia = person("Mia Lopez", "inner", closeness=5, city=oak, birthday=bday_soon,
                 tags="context:college, context:friends")
    alex = person("Alex Kim", "wider", tags="context:conference")
    priya = person("Priya Nair", "active", employer=voltgrid, title="Co-founder & CEO",
                   tags="topic:climate", engagement="acquaintance")
    tom = person("Tom Reyes", "close", cadence=21, employer=paybridge, aliases="Tommy",
                 tags="context:work")

    repo.update_profile(vault, sarah, "close", None, 4, "Fintech summit 2019", mia)
    repo.add_claim(vault, sarah, "interest", value="grid-scale storage", basis="told")
    repo.add_claim(vault, sarah, "family_detail",
                   value="Daughter starting university next fall", basis="told")
    repo.add_claim(vault, sarah, "knows", object_ref=raj, basis="heard")
    repo.add_claim(vault, dev, "colleague_of", object_ref=sarah, valid_from="2018",
                   valid_to="2021", note="Both at Paybridge")
    repo.add_claim(vault, priya, "colleague_of", object_ref=sarah, basis="told",
                   note="Sarah joining Voltgrid in January")
    repo.add_claim(vault, mia, "friend_of", object_ref=sarah)
    repo.add_claim(vault, raj, "invested_in", object_ref=voltgrid, basis="heard",
                   confidence=0.5, note="Rumoured lead for the Series A")

    def log(people, days_ago, kind="meeting", channel="in_person", notes=None, by="me",
            follow_ups=(), tags="", title=None, location=None):
        return repo.create_event(vault, kind=kind, status="occurred", start_at=ago(days_ago),
                                 participants=people, channel=channel, notes=notes,
                                 initiated_by=by, follow_ups=follow_ups, tags=tags,
                                 title=title, location_ref=location)

    log([sarah], 160, "meal", notes="Dinner after the payments meetup.", by="them")
    log([sarah], 95, "call", "call", by="me")
    log([sarah], 41, "meeting", title="Coffee with Sarah", location=oak, tags="topic:climate",
        notes="She's leaving Paybridge in January to join Voltgrid (Series A). "
              "Excited but nervous about early-stage life. Wants intros to climate investors.",
        follow_ups=[{"text": "Intro Sarah to Raj", "due": ahead(3), "owner": "me"},
                    {"text": "Send Voltgrid deck for feedback", "due": ago(2), "owner": "them"}])
    log([raj], 70, "video", "video", by="them", tags="topic:climate")
    log([dev], 200, "call", "call", by="me")
    log([mia], 9, "meal", notes="Birthday plans; she's thinking about moving to Denver.", by="them")
    log([mia, sarah], 120, "event", title="Mia's housewarming", by="them")
    log([tom], 25, "meeting", notes="Quarterly catch-up.", by="me",
        follow_ups=[{"text": "Share the partnerships playbook", "due": ahead(10), "owner": "me"}])
    log([alex], 400, "event", title="Conference panel", by="them")

    log([raj, dev], 150, "meal", title="Climate founders dinner", by="them")
    log([raj, dev], 60, "event", title="Grid storage roundtable", by="them")
    repo.add_claim(vault, voltgrid, "size", value="14")
    repo.add_claim(vault, priya, "employed_by", object_ref=voltgrid, valid_from="2025")
    repo.add_claim(vault, raj, "seeking", value="climate founders raising a Series A")
    repo.add_claim(vault, priya, "seeking", value="Series A investors for Voltgrid")
    repo.add_claim(vault, sarah, "offering", value="payments partnerships advice")
    repo.add_claim(vault, tom, "seeking", value="partnerships advice for a new payments product")

    repo.create_event(vault, kind="meal", status="planned", start_at=ahead(4) + "T19:00",
                      participants=[raj, mia], title="Dinner with Raj & Mia")
    repo.create_event(vault, kind="call", status="planned", start_at=ago(1), channel="call",
                      participants=[tom], title="Call with Tom")
    repo.add_commitment(vault, dev, "Ask Dev about Denver neighborhoods for Mia", None)
