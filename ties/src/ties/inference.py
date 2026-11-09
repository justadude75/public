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


def _linked_pairs(c) -> set[frozenset]:
    """Every pair already connected (or rejected) by a person-to-person claim, loaded once."""
    marks = ",".join("?" * len(PERSON_LINKS))
    return {frozenset((r["subject_id"], r["object_id"])) for r in c.execute(
        f"""select subject_id, object_id from claim where predicate_id in ({marks})
            and object_id is not null
            and status in ('accepted','proposed','rejected','disputed')""", PERSON_LINKS)}


MAX_NEW_PER_RULE = 200   # per run, so a large import can't flood Review


def _propose(c, src, a: str, b: str, predicate: str, rule: str, confidence: float,
             why: str, linked: set, pending: list) -> bool:
    """Queue a suggestion; run() writes the best ones per rule at the end."""
    pair = frozenset((a, b))
    if a == b or pair in linked:
        return False
    linked.add(pair)
    pending.append((rule, a, b, predicate, confidence, why))
    return True


def run_if_changed(vault: Vault) -> dict[str, int]:
    """Run the rules only if the vault changed since the last run (cheap to call often)."""
    state = vault.__dict__.setdefault("_inference_state", {})
    if state.get("version") == vault.version:
        return {k: 0 for k in INFERENCE_RULES}
    added = run(vault)
    state["version"] = vault.version
    return added


def _year(value: str | None) -> int | None:
    d = parse_day(value) if value else None
    return d.year if d else None


def run(vault: Vault) -> dict[str, int]:
    """Run all enabled rules; returns how many suggestions each rule added."""
    settings = rule_settings(vault)
    ego = vault.get_meta("ego_id")
    added = {k: 0 for k in settings}
    if vault.get_meta("vault_kind") == "research" and vault.get_meta("research_inference") != "1":
        return added            # research vaults: rules are off unless turned on
    with vault.tx() as c:
        src = None
        linked = _linked_pairs(c)
        touched: list = []      # pending suggestions, written at the end

        def source():
            nonlocal src
            if src is None:
                src = _create_source(c, "manual", "rule-based inference run")
            return src

        names = {r["id"]: r["display_name"] for r in c.execute(
            "select id, display_name from entity where status='active'")}
        # Only people you know (tracked contacts and acquaintances) take part in rules;
        # names merely referenced, e.g. paper authors, would produce noise at scale.
        engagement = {r["id"]: r["engagement"] for r in c.execute(
            "select id, engagement from entity where status='active' and type_id='person' "
            "and engagement in ('contact','acquaintance','ego')")}
        people = set(engagement)

        if settings["shared_events"]:
            together: dict[tuple[str, str], list[str]] = defaultdict(list)
            members: dict[str, list[str]] = defaultdict(list)
            titles: dict[str, str] = {}
            for r in c.execute(
                    """select p.event_id, p.entity_id, e.display_name from participation p
                       join event_detail ed on ed.entity_id=p.event_id
                       join entity e on e.id=p.event_id
                       where e.status='active' and ed.status='occurred'"""):
                members[r["event_id"]].append(r["entity_id"])
                titles[r["event_id"]] = r["display_name"]
            for event_id, parts in members.items():
                others = sorted(p for p in parts if p != ego and p in people)
                if len(parts) > SMALL_EVENT_MAX or len(others) < 2:
                    continue
                for a, b in combinations(others, 2):
                    together[(a, b)].append(titles[event_id])
            for (a, b), events in together.items():
                if len(events) >= 2:
                    why = f"Both at {len(events)} small events: " + "; ".join(events[:3])
                    _propose(c, None, a, b, "knows",
                                                       "shared_events", 0.6, why, linked, touched)

        if settings["shared_connector"]:
            intros = defaultdict(list)
            for r in c.execute("select subject_id, object_id from claim where "
                               "predicate_id='introduced_by' and status='accepted'"):
                intros[r["object_id"]].append(r["subject_id"])
            for connector, introduced in intros.items():
                for person in introduced:
                    why = f"{names.get(connector)} introduced you to {names.get(person)}"
                    _propose(c, None, connector, person,
                                                          "knows", "shared_connector", 0.9, why, linked, touched)
                for a, b in combinations(sorted(set(introduced)), 2):
                    why = f"Both introduced to you by {names.get(connector)}"
                    _propose(
                        c, None, a, b, "shares_connector", "shared_connector", 0.5, why,
                        linked, touched)

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
                    _propose(
                        c, None, x["subject_id"], y["subject_id"], "colleague_of",
                        "overlapping_tenure", 0.6, why, linked, touched)

        if settings["same_school_year"]:
            by_school = defaultdict(list)
            for r in c.execute("select subject_id, object_id, valid_to from claim where "
                               "predicate_id='studied_at' and status='accepted'"):
                if r["valid_to"]:
                    by_school[(r["object_id"], _year(r["valid_to"]))].append(r["subject_id"])
            for (school, year), studs in by_school.items():
                for a, b in combinations(sorted(set(studs)), 2):
                    why = f"Both finished at {names.get(school)} in {year}"
                    _propose(c, None, a, b, "knows",
                                                          "same_school_year", 0.4, why, linked, touched)
        # Write the best suggestions per rule: pairs of tracked contacts first.
        rank = {"contact": 2, "acquaintance": 1}
        by_rule: dict[str, list] = defaultdict(list)
        for item in touched:
            by_rule[item[0]].append(item)
        written: set[str] = set()
        for rule, items in by_rule.items():
            items.sort(key=lambda x: -(rank.get(engagement.get(x[1]), 0) +
                                       rank.get(engagement.get(x[2]), 0) + x[4]))
            for rule_, a, b, predicate, confidence, why in items[:MAX_NEW_PER_RULE]:
                _add_claim(c, a, predicate, object_id=b, basis="inferred", confidence=confidence,
                           qualifiers={"why": why, "rule": rule_}, source_id=source(),
                           asserted_by=f"rule:{rule_}", status="proposed", reindex=False)
                written.update((a, b))
            added[rule] = min(len(items), MAX_NEW_PER_RULE)
        from . import textindex
        for eid in written:
            textindex.reindex_entity(c, eid)
    return added
