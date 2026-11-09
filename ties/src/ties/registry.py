"""Built-in entity types and predicates. Seeded into every vault at startup.

Adding a predicate here makes it available in all vaults on next start;
it never removes or changes data already recorded.
"""

from __future__ import annotations

import json

ENTITY_TYPES = [
    ("person", "Person"),
    ("organization", "Organization"),
    ("place", "Place"),
    ("event", "Event"),
    ("topic", "Topic"),
    ("document", "Document"),
]

P, O, PL, E, ANY = "person", "organization", "place", "event", "*"

# id, label, reverse_label, domain, range_kind, range_types, cardinality,
# temporal, symmetric, inverse, sensitivity, sort
PREDICATES = [
    # attributes
    ("title", "Title / role", None, [P], "string", None, "one", 1, 0, None, "normal", 10),
    ("birthday", "Birthday", None, [P], "date", None, "one", 0, 0, None, "normal", 11),
    ("interest", "Interest", None, [P], "string", None, "many", 1, 0, None, "normal", 12),
    ("preference", "Preference", None, [P], "string", None, "many", 1, 0, None, "normal", 13),
    ("family_detail", "Family detail", None, [P], "string", None, "many", 1, 0, None, "sensitive", 14),
    ("industry", "Industry", None, [O], "string", None, "many", 1, 0, None, "normal", 15),
    ("description", "Description", None, [ANY], "string", None, "one", 0, 0, None, "normal", 16),
    ("fact", "Other fact", None, [ANY], "string", None, "many", 1, 0, None, "normal", 99),
    # edges: person → organization / place
    ("employed_by", "Works at", "Employs", [P], "entity", [O], "many", 1, 0, None, "normal", 20),
    ("member_of", "Member of", "Has member", [P, O], "entity", [O], "many", 1, 0, None, "normal", 21),
    ("studied_at", "Studied at", "Alumni", [P], "entity", [O], "many", 1, 0, None, "normal", 22),
    ("lives_in", "Lives in", "Residents", [P], "entity", [PL], "one", 1, 0, None, "normal", 23),
    ("located_in", "Located in", "Location of", [O, PL, E], "entity", [PL], "many", 1, 0, None, "normal", 24),
    ("invested_in", "Invested in", "Investor", [P, O], "entity", [O], "many", 1, 0, None, "normal", 25),
    ("part_of", "Part of", "Includes", [O, PL], "entity", [O, PL], "many", 1, 0, None, "normal", 26),
    # edges: person ↔ person
    ("knows", "Knows", None, [P], "entity", [P], "many", 1, 1, None, "normal", 30),
    ("friend_of", "Friend of", None, [P], "entity", [P], "many", 1, 1, None, "normal", 31),
    ("colleague_of", "Colleague of", None, [P], "entity", [P], "many", 1, 1, None, "normal", 32),
    ("family_of", "Family of", None, [P], "entity", [P], "many", 1, 1, None, "sensitive", 33),
    ("partner_of", "Partner of", None, [P], "entity", [P], "many", 1, 1, None, "sensitive", 34),
    ("parent_of", "Parent of", None, [P], "entity", [P], "many", 0, 0, "child_of", "sensitive", 35),
    ("child_of", "Child of", None, [P], "entity", [P], "many", 0, 0, "parent_of", "sensitive", 36),
    ("mentor_of", "Mentor of", None, [P], "entity", [P], "many", 1, 0, "mentee_of", "normal", 37),
    ("mentee_of", "Mentee of", None, [P], "entity", [P], "many", 1, 0, "mentor_of", "normal", 38),
    ("introduced_by", "Introduced to me by", "Introduced me to", [P], "entity", [P], "one", 0, 0, None, "normal", 39),
    # edges: anything
    ("attended", "Attended", "Attendee", [P, O], "entity", [E], "many", 0, 0, None, "normal", 40),
    ("related_to", "Related to", None, [ANY], "entity", [ANY], "many", 1, 1, None, "normal", 98),
]

BASES = ["observed", "told", "heard", "public", "inferred"]
TIERS = ["inner", "close", "active", "extended", "wider"]
TIER_CADENCE = {"inner": 14, "close": 30, "active": 60, "extended": 120, "wider": 365}
TIER_WEIGHT = {"inner": 1.0, "close": 0.8, "active": 0.6, "extended": 0.4, "wider": 0.2}
ENGAGEMENTS = ["contact", "acquaintance", "reference"]
EVENT_KINDS = ["meeting", "meal", "call", "video", "message", "email", "event", "life_event", "other"]
CHANNELS = ["in_person", "video", "call", "message", "email", "social"]
EVENT_STATUSES = ["planned", "occurred", "cancelled", "missed"]
CHANNEL_WEIGHT = {"in_person": 1.0, "video": 0.8, "call": 0.7, "message": 0.3,
                  "email": 0.3, "social": 0.05}


def seed(vault) -> None:
    with vault.tx() as c:
        for tid, label in ENTITY_TYPES:
            c.execute(
                "insert into entity_type(id, label) values(?,?) "
                "on conflict(id) do update set label=excluded.label",
                (tid, label),
            )
        # insert without inverse first, then set inverses (FK order)
        for (pid, label, rev, dom, rk, rt, card, temporal, sym, _inv, sens, sort) in PREDICATES:
            c.execute(
                """insert into predicate(id, label, reverse_label, domain_types, range_kind,
                       range_types, cardinality, temporal, symmetric, sensitivity, sort)
                   values(?,?,?,?,?,?,?,?,?,?,?)
                   on conflict(id) do update set label=excluded.label,
                       reverse_label=excluded.reverse_label, domain_types=excluded.domain_types,
                       range_kind=excluded.range_kind, range_types=excluded.range_types,
                       cardinality=excluded.cardinality, temporal=excluded.temporal,
                       symmetric=excluded.symmetric, sensitivity=excluded.sensitivity,
                       sort=excluded.sort""",
                (pid, label, rev, json.dumps(dom), rk, json.dumps(rt) if rt else None,
                 card, temporal, sym, sens, sort),
            )
        for p in PREDICATES:
            c.execute("update predicate set inverse_id=? where id=?", (p[9], p[0]))
