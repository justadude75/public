"""Rule-based inferences. Each rule proposes claims (status 'proposed', basis 'inferred').

Suggestions never become facts on their own. Confirming one turns it into a
fact; rejecting one stores it as rejected, and a rejected or existing claim for
the same pair blocks the rule from proposing it again.
"""

from __future__ import annotations

import json
from collections import defaultdict
from itertools import combinations

from .db import Vault
from .registry import INFERENCE_RULES, SMALL_EVENT_MAX, SMALL_ORG_MAX
from .repo import _add_claim, _create_source, parse_day

PERSON_LINKS = ("knows", "friend_of", "colleague_of", "family_of", "partner_of", "parent_of",
                "child_of", "mentor_of", "mentee_of", "shares_connector")

RULE_LABELS = {
    "shared_events": "Shared small events",
    "shared_connector": "Shared connector",
    "overlapping_tenure": "Overlapping time at a small organization",
    "same_school_year": "Same school and year",
}


def rule_settings(vault: Vault) -> dict[str, bool]:
    raw = vault.get_meta("inference_rules")
    settings = dict(INFERENCE_RULES)
    if raw:
        settings.update({k: bool(v) for k, v in json.loads(raw).items() if k in settings})
    return settings


def save_rule_settings(vault: Vault, settings: dict[str, bool]) -> None:
    vault.set_meta("inference_rules", json.dumps({k: bool(settings.get(k)) for k in INFERENCE_RULES}))


def _pair_blocked(c, a: str, b: str, predicates: tuple[str, ...]) -> bool:
    marks = ",".join("?" * len(predicates))
    row = c.execute(
        f"""select 1 from claim where predicate_id in ({marks})
            and ((subject_id=? and object_id=?) or (subject_id=? and object_id=?))
            and status in ('accepted','proposed','rejected','disputed') limit 1""",
        (*predicates, a, b, b, a)).fetchone()
    return row is not None


def _propose(c, src: str, a: str, b: str, predicate: str, rule: str, confidence: float,
             why: str, block_on: tuple[str, ...] = PERSON_LINKS) -> bool:
    if a == b or _pair_blocked(c, a, b, block_on + (predicate,)):
        return False
    _add_claim(c, a, predicate, object_id=b, basis="inferred", confidence=confidence,
               qualifiers={"why": why, "rule": rule}, source_id=src,
               asserted_by=f"rule:{rule}", status="proposed")
    return True


def _year(value: str | None) -> int | None:
    d = parse_day(value) if value else None
    return d.year if d else None


def run(vault: Vault) -> dict[str, int]:
    """Run all enabled rules; returns how many suggestions each rule added."""
    settings = rule_settings(vault)
    ego = vault.get_meta("ego_id")
    added = {k: 0 for k in settings}
    with vault.tx() as c:
        src = None

        def source():
            nonlocal src
            if src is None:
                src = _create_source(c, "manual", "rule-based inference run")
            return src

        names = {r["id"]: r["display_name"] for r in c.execute(
            "select id, display_name from entity where status='active'")}
        people = {r["id"] for r in c.execute(
            "select id from entity where status='active' and type_id='person'")}

        if settings["shared_events"]:
            together: dict[tuple[str, str], list[str]] = defaultdict(list)
            for ev in c.execute(
                    """select ed.entity_id, e.display_name from event_detail ed
                       join entity e on e.id=ed.entity_id
                       where e.status='active' and ed.status='occurred'"""):
                parts = [r["entity_id"] for r in c.execute(
                    "select entity_id from participation where event_id=?", (ev["entity_id"],))]
                others = sorted(p for p in parts if p != ego and p in people)
                if len(parts) > SMALL_EVENT_MAX or len(others) < 2:
                    continue
                for a, b in combinations(others, 2):
                    together[(a, b)].append(ev["display_name"])
            for (a, b), events in together.items():
                if len(events) >= 2:
                    why = f"Both at {len(events)} small events: " + "; ".join(events[:3])
                    added["shared_events"] += _propose(c, source(), a, b, "knows",
                                                       "shared_events", 0.6, why)

        if settings["shared_connector"]:
            intros = defaultdict(list)
            for r in c.execute("select subject_id, object_id from claim where "
                               "predicate_id='introduced_by' and status='accepted'"):
                intros[r["object_id"]].append(r["subject_id"])
            for connector, introduced in intros.items():
                for person in introduced:
                    why = f"{names.get(connector)} introduced you to {names.get(person)}"
                    added["shared_connector"] += _propose(c, source(), connector, person,
                                                          "knows", "shared_connector", 0.9, why)
                for a, b in combinations(sorted(set(introduced)), 2):
                    why = f"Both introduced to you by {names.get(connector)}"
                    added["shared_connector"] += _propose(
                        c, source(), a, b, "shares_connector", "shared_connector", 0.5, why)

        if settings["overlapping_tenure"]:
            sizes = {}
            for r in c.execute("select subject_id, value from claim where predicate_id='size' "
                               "and status='accepted'"):
                try:
                    sizes[r["subject_id"]] = float(json.loads(r["value"]))
                except (TypeError, ValueError):
                    pass
            by_org = defaultdict(list)
            for r in c.execute(
                    """select subject_id, object_id, valid_from, valid_to, qualifiers from claim
                       where predicate_id='employed_by' and status='accepted'"""):
                if r["subject_id"] in people:
                    by_org[r["object_id"]].append(r)
            for org, rows in by_org.items():
                small = sizes.get(org) is not None and sizes[org] <= SMALL_ORG_MAX
                for x, y in combinations(rows, 2):
                    if x["subject_id"] == y["subject_id"]:
                        continue
                    team_x = json.loads(x["qualifiers"] or "{}").get("team")
                    team_y = json.loads(y["qualifiers"] or "{}").get("team")
                    same_team = bool(team_x) and team_x == team_y
                    if not (small or same_team):
                        continue
                    xs, xe = _year(x["valid_from"]) or 0, _year(x["valid_to"]) or 9999
                    ys, ye = _year(y["valid_from"]) or 0, _year(y["valid_to"]) or 9999
                    if max(xs, ys) > min(xe, ye):
                        continue
                    where = f"team {team_x}" if same_team else f"{names.get(org)} (small team)"
                    why = f"Both at {where} at the same time"
                    added["overlapping_tenure"] += _propose(
                        c, source(), x["subject_id"], y["subject_id"], "colleague_of",
                        "overlapping_tenure", 0.6, why)

        if settings["same_school_year"]:
            by_school = defaultdict(list)
            for r in c.execute("select subject_id, object_id, valid_to from claim where "
                               "predicate_id='studied_at' and status='accepted'"):
                if r["valid_to"]:
                    by_school[(r["object_id"], _year(r["valid_to"]))].append(r["subject_id"])
            for (school, year), studs in by_school.items():
                for a, b in combinations(sorted(set(studs)), 2):
                    why = f"Both finished at {names.get(school)} in {year}"
                    added["same_school_year"] += _propose(c, source(), a, b, "knows",
                                                          "same_school_year", 0.4, why)
    return added
