"""Synthetic large vaults and timings for the main pages and operations.

    ties bench --people 20000            # build a synthetic vault and time everything

Data is inserted with bulk SQL (fast), then indexed. All names are generated.
"""

from __future__ import annotations

import json
import random
import time
from contextlib import contextmanager
from datetime import date, timedelta

from . import repo, textindex
from .db import Vault
from .repo import new_id, normalize_name

FIRST = ("Ana Ben Cara Dev Eli Fay Gus Hana Ivo Jon Kai Lea Mia Nik Oli Pia Raj Sam Tia Uma "
         "Vic Wen Xia Yan Zoe Ada Bo Cy Di Ed").split()
LAST = ("Smith Chen Patel Garcia Kim Novak Silva Okafor Rossi Muller Haddad Tanaka Lopez Ivanov "
        "Nair Costa Berg Dubois Kowal Moreau Sato Weber Ali Park Reyes Quinn Lang Ford Hale Voss").split()
TOPICS = ("climate grid storage payments ai policy health biotech design music sailing chess "
          "running cooking venture hiring robotics education energy finance law media").split()


def build(vault: Vault, people: int = 5000, orgs: int | None = None, events_per_contact: int = 6,
          contact_share: float = 0.25, seed: int = 7, today: date | None = None) -> dict:
    rnd = random.Random(seed)
    today = today or date.today()
    orgs = orgs or max(20, people // 25)
    places = max(10, people // 200)
    repo.init_vault(vault, "Bench Owner")
    ego = vault.get_meta("ego_id")
    t0 = time.perf_counter()
    with vault.tx() as c:
        src = new_id()
        c.execute("insert into source(id, kind, content, content_hash) values(?,?,?,?)",
                  (src, "manual", "bench", "x"))
        ent_rows, name_rows = [], []

        def ent(type_id, name, engagement):
            eid = new_id() + f"{len(ent_rows):06d}"
            ent_rows.append((eid, type_id, name, engagement))
            name_rows.append((eid, name, normalize_name(name), "primary"))
            return eid

        place_ids = [ent("place", f"City {i}", "reference") for i in range(places)]
        org_ids = [ent("organization", f"{rnd.choice(LAST)} {rnd.choice(['Labs', 'Group', 'Capital', 'Energy', 'Health'])} {i}", "reference")
                   for i in range(orgs)]
        person_ids, contacts = [], []
        for i in range(people):
            is_contact = rnd.random() < contact_share
            pid = ent("person", f"{rnd.choice(FIRST)} {rnd.choice(LAST)} {i}",
                      "contact" if is_contact else "acquaintance")
            person_ids.append(pid)
            if is_contact:
                contacts.append(pid)
        c.executemany("insert into entity(id, type_id, display_name, engagement) values(?,?,?,?)",
                      ent_rows)
        c.executemany("insert into entity_name(entity_id, name, name_norm, kind) values(?,?,?,?)",
                      name_rows)
        tiers = ["inner", "close", "active", "active", "extended", "wider", "wider"]
        c.executemany("insert into relationship_profile(entity_id, tier, closeness_self) values(?,?,?)",
                      [(p, rnd.choice(tiers), rnd.randint(1, 5)) for p in contacts])

        claims, evidence = [], []

        def claim(s, p, o=None, v=None, vf=None):
            cid = new_id() + f"{len(claims):07d}"
            claims.append((cid, s, p, o, json.dumps(v) if v is not None else None, vf))
            evidence.append((cid, src))

        for p in person_ids:
            claim(p, "employed_by", o=rnd.choice(org_ids), vf=str(rnd.randint(2005, 2024)))
            claim(p, "lives_in", o=rnd.choice(place_ids))
            claim(p, "interest", v=rnd.choice(TOPICS))
            if rnd.random() < 0.3:
                claim(p, "seeking", v=f"{rnd.choice(TOPICS)} {rnd.choice(TOPICS)} advice")
            if rnd.random() < 0.3:
                claim(p, "offering", v=f"{rnd.choice(TOPICS)} expertise")
            for _ in range(rnd.randint(1, 3)):
                other = rnd.choice(person_ids)
                if other != p:
                    claim(p, rnd.choice(["knows", "knows", "colleague_of", "friend_of"]), o=other)
        for o in org_ids:
            claim(o, "located_in", o=rnd.choice(place_ids))
            claim(o, "size", v=str(rnd.choice([8, 30, 120, 600, 5000])))
        c.executemany("insert into claim(id, subject_id, predicate_id, object_id, value, valid_from) "
                      "values(?,?,?,?,?,?)", claims)
        c.executemany("insert into claim_evidence(claim_id, source_id) values(?,?)", evidence)

        ev_rows, part_rows, src_rows = [], [], []
        kinds = [("meeting", "in_person"), ("call", "call"), ("message", "message"),
                 ("meal", "in_person"), ("video", "video")]
        for p in contacts:
            for _ in range(rnd.randint(1, events_per_contact * 2 - 1)):
                eid = new_id() + f"e{len(ev_rows):07d}"
                day = today - timedelta(days=rnd.randint(-20, 900))
                kind, channel = rnd.choice(kinds)
                status = "planned" if day > today else "occurred"
                note_id = None
                if rnd.random() < 0.4:
                    note_id = new_id() + f"n{len(src_rows):07d}"
                    text = (f"Talked about {rnd.choice(TOPICS)} and {rnd.choice(TOPICS)}. "
                            f"Follow up on {rnd.choice(TOPICS)}.")
                    src_rows.append((note_id, "note", text, "h"))
                ent_rows2 = (eid, "event", f"{kind.title()} {len(ev_rows)}", None)
                c.execute("insert into entity(id, type_id, display_name, engagement) values(?,?,?,?)",
                          ent_rows2)
                ev_rows.append((eid, kind, status, day.isoformat(), channel,
                                rnd.choice([ego, p]), note_id))
                part_rows.append((eid, ego, "self"))
                part_rows.append((eid, p, "attendee"))
                if rnd.random() < 0.2:
                    part_rows.append((eid, rnd.choice(person_ids), "attendee"))
        c.executemany("insert into source(id, kind, content, content_hash) values(?,?,?,?)", src_rows)
        c.executemany("insert into event_detail(entity_id, kind, status, start_at, channel, "
                      "initiated_by, notes_source_id) values(?,?,?,?,?,?,?)", ev_rows)
        c.executemany("insert or ignore into participation(event_id, entity_id, role) values(?,?,?)",
                      part_rows)
        loops = [(new_id() + f"c{i:06d}", ego, rnd.choice(contacts), "promise",
                  f"Send {rnd.choice(TOPICS)} notes",
                  (today + timedelta(days=rnd.randint(-10, 30))).isoformat())
                 for i in range(len(contacts) // 4)]
        c.executemany("insert into commitment(id, from_id, to_id, kind, description, due_at) "
                      "values(?,?,?,?,?,?)", loops)
    t_insert = time.perf_counter() - t0
    t0 = time.perf_counter()
    textindex.rebuild(vault)
    t_index = time.perf_counter() - t0
    return {"people": people, "contacts": len(contacts), "orgs": orgs, "events": len(ev_rows),
            "claims": len(claims), "notes": len(src_rows), "insert_s": round(t_insert, 2),
            "index_s": round(t_index, 2)}


@contextmanager
def _timer(results: dict, name: str):
    t0 = time.perf_counter()
    try:
        yield
    finally:
        results[name] = round((time.perf_counter() - t0) * 1000)


def run(vault: Vault, today: date | None = None, heavy: bool = True) -> dict:
    """Time the main operations directly and through the web app. Values are milliseconds."""
    from fastapi.testclient import TestClient

    from . import graph, inference, resolution, scoring, strategy
    from .web import create_app

    today = today or date.today()
    r: dict[str, int] = {}
    contact = vault.scalar("select entity_id from relationship_profile limit 1")
    target = vault.scalar("select id from entity where engagement='acquaintance' limit 1")
    with _timer(r, "scoring.compute_all"):
        scoring.compute_all(vault, today)
    with _timer(r, "scoring.compute_all (again)"):
        scoring.compute_all(vault, today)
    with _timer(r, "search (keyword)"):
        textindex.search(vault, "climate grid")
    with _timer(r, "graph.build (centered, depth 2)"):
        graph.build(vault, center=contact, depth=2, today=today)
    with _timer(r, "graph.warm_paths"):
        graph.warm_paths(vault, target, today=today)
    with _timer(r, "intro suggestions"):
        strategy.intro_suggestions(vault)
    with _timer(r, "inference.run"):
        inference.run(vault)
    with _timer(r, "resolution.candidates"):
        resolution.candidates(vault)
    if heavy:
        with _timer(r, "graph.analytics"):
            graph.analytics(vault, today)
    from . import warm
    repo.add_claim(vault, contact, "interest", value="benchmarking")   # a change, so caches go cold
    with _timer(r, "background warm-up (after a change)"):
        warm.warm(vault, today)
    client = TestClient(create_app(vault, today_fn=lambda: today))
    for name, url in [("GET /", "/"), ("GET /people", "/people"), ("GET /timeline", "/timeline"),
                      (f"GET /e/<contact>", f"/e/{contact}"), ("GET /search", "/search?q=climate"),
                      ("GET /api/graph (centered)", f"/api/graph?center={contact}"),
                      ("GET /review", "/review"), ("GET /weekly", "/weekly")] + (
            [("GET /insights", "/insights")] if heavy else []):
        with _timer(r, name):
            res = client.get(url)
            assert res.status_code == 200, (url, res.status_code)
    return r
