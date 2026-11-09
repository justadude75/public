"""Contact tracking: days since contact, cadence, next due, strength and the Today list.

The formulas follow section 6 of docs/personal-graph-plan.md and are kept
deliberately simple so every number can be explained on screen.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta

from .db import Vault
from .registry import CHANNEL_WEIGHT, TIER_CADENCE, TIER_WEIGHT
from .repo import commitments, list_events, parse_day

HALF_LIFE_DAYS = 90
DUE_RATIO = 0.9          # surface a contact once 90% of the cadence has passed
COVER_WINDOW_DAYS = 7    # a planned event this close to the due date counts as covering it
UPCOMING_DAYS = 14

KIND_TO_CHANNEL = {"call": "call", "video": "video", "message": "message", "email": "email"}


@dataclass
class ContactStatus:
    entity_id: str
    name: str
    tier: str
    cadence: int
    cadence_source: str                 # "custom" | "tier"
    last_contact: date | None = None
    last_event_title: str | None = None
    days_since: int | None = None
    next_due: date | None = None
    days_to_due: int | None = None
    next_planned: date | None = None
    next_planned_title: str | None = None
    next_planned_id: str | None = None
    covered: bool = False
    learned_cadence: int | None = None
    n_contacts: int = 0
    strength: float = 0.0
    strength_parts: dict = field(default_factory=dict)
    share_initiated_by_them: float | None = None
    ratio: float | None = None
    priority: float = 0.0
    is_due: bool = False
    reasons: list[str] = field(default_factory=list)


def _interactions(vault: Vault, ego: str) -> list[dict]:
    return vault.all(
        """select p.entity_id, ed.entity_id as event_id, e.display_name as title, ed.start_at,
                  ed.status, ed.channel, ed.kind, ed.initiated_by, ed.counts_as_contact
           from participation p
           join event_detail ed on ed.entity_id = p.event_id
           join entity e on e.id = ed.entity_id
           where e.status='active' and p.entity_id <> ?
             and exists(select 1 from participation pe where pe.event_id = p.event_id
                        and pe.entity_id = ?)""", (ego, ego))


def _context_tag_count(vault: Vault, entity_id: str) -> int:
    return vault.scalar(
        "select count(*) from tagging g join tag t on t.id=g.tag_id where g.target_kind='entity' "
        "and g.target_id=? and t.namespace='context'", (entity_id,)) or 0


def compute_all(vault: Vault, today: date | None = None,
                only: str | None = None) -> dict[str, ContactStatus]:
    """Status for every tracked contact (or only one entity)."""
    today = today or date.today()
    ego = vault.get_meta("ego_id")
    params: tuple = ()
    extra = ""
    if only:
        extra, params = " and e.id=?", (only,)
    contacts = vault.all(
        f"""select e.id, e.display_name, rp.tier, rp.cadence_days, rp.closeness_self
            from relationship_profile rp join entity e on e.id = rp.entity_id
            where e.status='active' and e.engagement='contact'{extra}""", params)
    by_entity: dict[str, list[dict]] = {}
    for row in _interactions(vault, ego):
        by_entity.setdefault(row["entity_id"], []).append(row)

    out: dict[str, ContactStatus] = {}
    for c in contacts:
        tier = c["tier"] if c["tier"] in TIER_CADENCE else "active"
        cadence = c["cadence_days"] or TIER_CADENCE[tier]
        st = ContactStatus(entity_id=c["id"], name=c["display_name"], tier=tier, cadence=cadence,
                           cadence_source="custom" if c["cadence_days"] else "tier")
        events = by_entity.get(c["id"], [])

        occurred = []
        for ev in events:
            day = parse_day(ev["start_at"])
            if day is None:
                continue
            if ev["status"] == "occurred" and ev["counts_as_contact"] and day <= today:
                occurred.append((day, ev))
            elif ev["status"] == "planned" and day >= today:
                if st.next_planned is None or day < st.next_planned:
                    st.next_planned, st.next_planned_title = day, ev["title"]
                    st.next_planned_id = ev["event_id"]
        occurred.sort(key=lambda x: x[0])
        st.n_contacts = len(occurred)

        if occurred:
            st.last_contact, last_ev = occurred[-1]
            st.last_event_title = last_ev["title"]
            st.days_since = (today - st.last_contact).days
            st.next_due = st.last_contact + timedelta(days=cadence)
            st.days_to_due = (st.next_due - today).days
            st.ratio = st.days_since / cadence
        days = sorted({d for d, _ in occurred})
        if len(days) >= 3:
            gaps = [(b - a).days for a, b in zip(days, days[1:])]
            st.learned_cadence = int(round(statistics.median(gaps)))

        # A planned event covers outreach if it lands by the due date (or, when already
        # overdue, within the next week), give or take the cover window.
        if st.next_planned:
            anchor = max(st.next_due, today) if st.next_due else today
            if st.next_due is None or st.next_planned <= anchor + timedelta(days=COVER_WINDOW_DAYS):
                st.covered = True

        # strength (section 6.2)
        score = 0.0
        for day, ev in occurred:
            channel = ev["channel"] or KIND_TO_CHANNEL.get(ev["kind"], "in_person")
            weight = CHANNEL_WEIGHT.get(channel, 0.3)
            score += weight * 2 ** (-(today - day).days / HALF_LIFE_DAYS)
        interaction = score / (1 + score)
        closeness = (c["closeness_self"] / 5) if c["closeness_self"] else 0.5
        multiplexity = min(_context_tag_count(vault, c["id"]), 3) / 3
        recent = [ev for day, ev in occurred if (today - day).days <= 365 and ev["initiated_by"]]
        if recent:
            them = sum(1 for ev in recent if ev["initiated_by"] != ego)
            st.share_initiated_by_them = them / len(recent)
            reciprocity = 1 - abs(0.5 - st.share_initiated_by_them) * 2
        else:
            reciprocity = 0.5
        st.strength_parts = {"interactions": interaction, "closeness": closeness,
                             "contexts": multiplexity, "reciprocity": reciprocity}
        st.strength = 0.5 * interaction + 0.3 * closeness + 0.1 * multiplexity + 0.1 * reciprocity

        # due and priority (section 6.4)
        if st.last_contact is None:
            urgency = 1.0
            st.reasons.append("No contact logged yet")
        else:
            urgency = max(0.0, min(st.ratio, 3.0))
            if st.days_to_due < 0:
                st.reasons.append(f"Overdue by {-st.days_to_due} days "
                                  f"({st.days_since} since last contact, every {cadence})")
            else:
                st.reasons.append(f"{st.days_since} days since last contact, every {cadence}")
        importance = 0.4 * TIER_WEIGHT[tier] + 0.3 * st.strength
        st.priority = urgency * importance
        st.is_due = (st.last_contact is None or st.ratio >= DUE_RATIO) and not st.covered
        if st.covered and st.next_planned:
            st.reasons.append(f"Covered by {st.next_planned_title} on {st.next_planned.isoformat()}")
        if st.share_initiated_by_them is not None and len(recent) >= 4:
            if st.share_initiated_by_them < 0.2:
                st.reasons.append("You start most of the contact")
            elif st.share_initiated_by_them > 0.8:
                st.reasons.append("They start most of the contact")
        if st.learned_cadence and abs(st.learned_cadence - cadence) / cadence > 0.5:
            st.reasons.append(f"Your actual rhythm is about every {st.learned_cadence} days")
        out[c["id"]] = st
    return out


def contact_status(vault: Vault, entity_id: str, today: date | None = None) -> ContactStatus | None:
    return compute_all(vault, today, only=entity_id).get(entity_id)


def _next_anniversary(value: str, today: date) -> date | None:
    """Next occurrence of a yearly date given as YYYY-MM-DD, MM-DD or --MM-DD."""
    v = value.strip().lstrip("-")
    try:
        month, day = (int(v[5:7]), int(v[8:10])) if len(v) >= 10 else (int(v[:2]), int(v[3:5]))
    except ValueError:
        return None
    for year in (today.year, today.year + 1):
        try:
            d = date(year, month, day)
        except ValueError:  # 29 February in a non-leap year
            d = date(year, month, 28)
        if d >= today:
            return d
    return None


def upcoming_dates(vault: Vault, today: date, days: int = UPCOMING_DAYS) -> list[dict]:
    rows = vault.all(
        "select cl.subject_id, cl.value, e.display_name from claim cl join entity e "
        "on e.id=cl.subject_id where cl.predicate_id='birthday' and cl.status='accepted' "
        "and e.status='active'")
    out = []
    for r in rows:
        nxt = _next_anniversary(json.loads(r["value"]), today)
        if nxt and (nxt - today).days <= days:
            out.append({"entity_id": r["subject_id"], "name": r["display_name"],
                        "what": "Birthday", "date": nxt, "in_days": (nxt - today).days})
    return sorted(out, key=lambda x: x["date"])


def today_view(vault: Vault, today: date | None = None) -> dict:
    today = today or date.today()
    statuses = compute_all(vault, today)
    due = sorted((s for s in statuses.values() if s.is_due), key=lambda s: -s.priority)
    soon = sorted((s for s in statuses.values()
                   if not s.is_due and not s.covered and s.days_to_due is not None
                   and 0 <= s.days_to_due <= 7), key=lambda s: s.days_to_due)
    horizon = (today + timedelta(days=UPCOMING_DAYS)).isoformat()
    upcoming = list_events(vault, status="planned", start=today.isoformat(), end=horizon,
                           descending=False)
    overdue_planned = [e for e in list_events(vault, status="planned", end=(
        today - timedelta(days=1)).isoformat(), descending=True, limit=20)]
    loops = commitments(vault)
    for loop in loops:
        d = parse_day(loop["due_at"])
        loop["due_in"] = (d - today).days if d else None
    recent = list_events(vault, status="occurred",
                         start=(today - timedelta(days=7)).isoformat(),
                         end=today.isoformat(), limit=20)
    return {
        "today": today,
        "due": due,
        "soon": soon,
        "upcoming": upcoming,
        "unlogged": overdue_planned,
        "dates": upcoming_dates(vault, today),
        "loops": loops,
        "recent": recent,
        "n_contacts": len(statuses),
        "n_due": len(due),
    }
