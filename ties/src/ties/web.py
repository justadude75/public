"""Local web app (served on 127.0.0.1 only). Server-rendered pages, no build step."""

from __future__ import annotations

import json
from datetime import date, timedelta
from importlib import resources
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import memory, repo, scoring
from .ai import AIClient
from .db import Vault
from .registry import (BASES, CHANNELS, ENGAGEMENTS, EVENT_KINDS, EVENT_STATUSES, TIER_CADENCE,
                       TIERS)

TYPE_LABELS = {"person": "Person", "organization": "Organization", "place": "Place"}


def _form_list(form, key: str) -> list[str]:
    return [v for v in form.getlist(key) if v]


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


def _relative(d: date | None, today: date) -> str:
    if d is None:
        return ""
    n = (d - today).days
    if n == 0:
        return "today"
    if n == 1:
        return "tomorrow"
    if n == -1:
        return "yesterday"
    return f"in {n} days" if n > 0 else f"{-n} days ago"


def create_app(vault: Vault, today_fn=date.today) -> FastAPI:
    repo.prepare(vault)
    app = FastAPI(title="ties", docs_url=None, redoc_url=None)
    static_dir = resources.files("ties") / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    templates = Jinja2Templates(directory=str(resources.files("ties") / "templates"))
    env = templates.env
    env.globals.update(TIERS=TIERS, TIER_CADENCE=TIER_CADENCE, BASES=BASES, CHANNELS=CHANNELS,
                       EVENT_KINDS=EVENT_KINDS, EVENT_STATUSES=EVENT_STATUSES,
                       ENGAGEMENTS=ENGAGEMENTS, TYPE_LABELS=TYPE_LABELS,
                       encrypted=vault.encrypted, vault_name=vault.path.name)
    from .registry import CREDIBILITY, RELIABILITY, SOURCE_TYPES
    env.globals.update(RELIABILITY=RELIABILITY, CREDIBILITY=CREDIBILITY, SOURCE_TYPES=SOURCE_TYPES,
                       research_mode=lambda: vault.get_meta("vault_kind") == "research")
    env.filters["rel"] = lambda d: _relative(d if isinstance(d, date) else repo.parse_day(d),
                                             today_fn())
    env.filters["day"] = lambda s: (s or "")[:10]
    env.filters["pct"] = lambda x: f"{round((x or 0) * 100)}"
    env.globals["review_count"] = lambda: vault.scalar(
        "select count(*) from claim where status in ('proposed','disputed')") or 0

    def render(request: Request, template: str, **ctx) -> HTMLResponse:
        ctx.setdefault("error", request.query_params.get("error"))
        ctx.setdefault("flash", request.query_params.get("flash"))
        return templates.TemplateResponse(request, template, ctx)

    def back(url: str, **params) -> RedirectResponse:
        q = {k: v for k, v in params.items() if v}
        return RedirectResponse(url + (("?" + urlencode(q)) if q else ""), status_code=303)

    def _asof(request: Request, entity_id: str):
        known = request.query_params.get("known_at") or None
        valid = request.query_params.get("valid_at") or None
        if not (known or valid):
            return None
        from . import research
        return {"known_at": known, "valid_at": valid,
                "rows": research.entity_as_of(vault, entity_id, known, valid)}

    def entity_or_404(entity_id: str) -> dict:
        ent = repo.get_entity(vault, entity_id)
        if not ent:
            raise HTTPException(404, "Not found")
        return ent

    # ── pages ─────────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    def today(request: Request):
        if vault.get_meta("vault_kind") == "research":
            return RedirectResponse("/projects", status_code=303)
        return render(request, "today.html", t=scoring.today_view(vault, today_fn()), nav="today")

    @app.get("/people", response_class=HTMLResponse)
    def people(request: Request, type: str = "", engagement: str = "", tag: str = "",
               q: str = "", sort: str = "name"):
        rows = repo.list_entities(vault, type_id=type or None, engagement=engagement or None,
                                  tag=tag or None, q=q or None)
        statuses = scoring.compute_all(vault, today_fn())
        for r in rows:
            r["status"] = statuses.get(r["id"])
        if sort == "due":
            rows.sort(key=lambda r: (r["status"] is None,
                                     r["status"].days_to_due if r["status"] and
                                     r["status"].days_to_due is not None else -10**6))
        elif sort == "recent":
            rows.sort(key=lambda r: (r["status"] is None or r["status"].days_since is None,
                                     r["status"].days_since if r["status"] and
                                     r["status"].days_since is not None else 0))
        return render(request, "people.html", rows=rows, tags=repo.all_tags(vault), nav="people",
                      f={"type": type, "engagement": engagement, "tag": tag, "q": q, "sort": sort})

    @app.get("/new", response_class=HTMLResponse)
    def new_entity_form(request: Request, type: str = "person", name: str = ""):
        if type not in TYPE_LABELS:
            type = "person"
        return render(request, "entity_new.html", type_id=type, name=name, nav="new",
                      v={"engagement": "contact" if type == "person" else "reference"})

    @app.post("/new")
    async def new_entity(request: Request):
        form = await request.form()
        type_id = form.get("type_id", "person")
        name = (form.get("name") or "").strip()
        idents = [("email", form.get("email", "")), ("phone", form.get("phone", "")),
                  ("url", form.get("url", ""))]
        if not name:
            return render(request, "entity_new.html", type_id=type_id, name=name, v=form,
                          error="A name is required.", nav="new")
        if not form.get("force"):
            dups = repo.duplicate_candidates(vault, type_id, name, idents)
            if dups:
                return render(request, "duplicates.html", dups=dups, form=form,
                              type_id=type_id, name=name, nav="new")
        try:
            eid = repo.create_entity(
                vault, type_id, name, engagement=form.get("engagement") or None,
                aliases=form.get("aliases", ""), tags=form.get("tags", ""),
                identifiers=idents, description=form.get("description") or None)
            if type_id == "person" and form.get("engagement") == "contact":
                tier = form.get("tier") or "active"
                repo.update_profile(vault, eid, tier, _int_or_none(form.get("cadence_days")),
                                    _int_or_none(form.get("closeness_self")),
                                    form.get("how_met"), form.get("introduced_by") or None)
            for pid in ("employed_by", "lives_in", "located_in"):
                ref = form.get(pid)
                if ref:
                    repo.add_claim(vault, eid, pid, object_ref=ref)
            if form.get("title"):
                repo.add_claim(vault, eid, "title", value=form.get("title"))
            if form.get("birthday"):
                repo.add_claim(vault, eid, "birthday", value=form.get("birthday"))
        except ValueError as exc:
            return render(request, "entity_new.html", type_id=type_id, name=name, v=form,
                          error=str(exc), nav="new")
        return back(f"/e/{eid}", flash=f"Added {name}")

    @app.get("/e/{entity_id}", response_class=HTMLResponse)
    def entity_page(request: Request, entity_id: str, history: int = 0):
        ent = entity_or_404(entity_id)
        if ent["id"] != entity_id:
            return back(f"/e/{ent['id']}")
        if ent["type_id"] == "event":
            return back(f"/event/{entity_id}")
        names = vault.all("select name, kind from entity_name where entity_id=? order by kind",
                          (entity_id,))
        ctx = dict(
            e=ent,
            aliases=[n["name"] for n in names if n["kind"] == "alias"],
            former=[n["name"] for n in names if n["kind"] == "former"],
            tags=repo.tags_for(vault, "entity", entity_id),
            idents=repo.identifiers(vault, entity_id),
            facts=repo.facts(vault, entity_id),
            relations=repo.relations(vault, entity_id),
            events=repo.list_events(vault, entity_id=entity_id, limit=100),
            loops=repo.commitments(vault, entity_id),
            done_loops=repo.commitments(vault, entity_id, status="done")[:10],
            profile=repo.get_profile(vault, entity_id),
            status=scoring.contact_status(vault, entity_id, today_fn()),
            value_preds=repo.predicates(vault, "value", ent["type_id"]),
            edge_preds=repo.predicates(vault, "entity", ent["type_id"]),
            history=repo.claim_history(vault, entity_id) if history else None,
            is_ego=ent["engagement"] == "ego",
            nav="people",
            links=repo.links(vault, entity_id),
            infer=repo.inferences(vault, entity_id),
            memory=memory.get_summary(vault, entity_id, today_fn()),
            changes=memory.changed_since_last_contact(vault, entity_id, today_fn()),
            ai_ready=AIClient.from_vault(vault) is not None,
            all_goals=__import__("ties.strategy", fromlist=["goals"]).goals(vault),
            support=__import__("ties.research", fromlist=["support"]).support(vault),
            asof=_asof(request, entity_id),
            linked=__import__("ties.crossvault", fromlist=["linked_view"]).linked_view(vault, entity_id),
            projects=__import__("ties.research", fromlist=["projects"]).projects(vault)
            if vault.get_meta("vault_kind") == "research" else [],
            entity_goals=vault.all("select g.id, g.title, gl.role from goal_link gl join goal g "
                                   "on g.id=gl.goal_id where gl.target_id=? and g.status='active'",
                                   (entity_id,)),
        )
        return render(request, "entity.html", **ctx)

    @app.post("/e/{entity_id}/edit")
    async def entity_edit(request: Request, entity_id: str):
        form = await request.form()
        try:
            repo.update_entity(vault, entity_id, form.get("name", ""),
                               form.get("engagement") or None, form.get("aliases", ""),
                               form.get("tags", ""))
        except ValueError as exc:
            return back(f"/e/{entity_id}", error=str(exc))
        return back(f"/e/{entity_id}", flash="Saved")

    @app.post("/e/{entity_id}/profile")
    async def entity_profile(request: Request, entity_id: str):
        form = await request.form()
        try:
            repo.update_profile(vault, entity_id, form.get("tier", "active"),
                                _int_or_none(form.get("cadence_days")),
                                _int_or_none(form.get("closeness_self")),
                                form.get("how_met"), form.get("introduced_by") or None)
        except ValueError as exc:
            return back(f"/e/{entity_id}", error=str(exc))
        return back(f"/e/{entity_id}", flash="Relationship settings saved")

    @app.post("/e/{entity_id}/fact")
    async def entity_fact(request: Request, entity_id: str):
        form = await request.form()
        try:
            repo.add_claim(
                vault, entity_id, form.get("predicate_id", ""),
                value=form.get("value") or None, object_ref=form.get("object") or None,
                basis=form.get("basis", "observed"),
                confidence=float(form.get("confidence") or 1.0),
                valid_from=form.get("valid_from") or None, valid_to=form.get("valid_to") or None,
                note=form.get("note"))
        except ValueError as exc:
            return back(f"/e/{entity_id}", error=str(exc))
        anchor = "#relations" if form.get("object") else "#facts"
        return RedirectResponse(f"/e/{entity_id}{anchor}", status_code=303)

    @app.post("/e/{entity_id}/identifier")
    async def entity_identifier(request: Request, entity_id: str):
        form = await request.form()
        if form.get("remove"):
            repo.remove_identifier(vault, entity_id, form.get("scheme", ""), form.get("value", ""))
        elif form.get("value"):
            repo.add_identifier(vault, entity_id, form.get("scheme", "other"), form.get("value"))
        return back(f"/e/{entity_id}")

    @app.post("/e/{entity_id}/archive")
    def entity_archive(entity_id: str):
        repo.archive_entity(vault, entity_id)
        return back("/people", flash="Archived")

    @app.post("/claim/{claim_id}/end")
    async def claim_end(request: Request, claim_id: str):
        form = await request.form()
        repo.end_claim(vault, claim_id, form.get("valid_to") or today_fn().isoformat())
        return back(form.get("back", "/"))

    @app.post("/claim/{claim_id}/retract")
    async def claim_retract(request: Request, claim_id: str):
        form = await request.form()
        repo.retract_claim(vault, claim_id)
        return back(form.get("back", "/"))

    @app.post("/e/{entity_id}/loop")
    async def entity_loop(request: Request, entity_id: str):
        form = await request.form()
        if form.get("text"):
            repo.add_commitment(vault, entity_id, form.get("text"), form.get("due") or None,
                                form.get("owner", "me"))
        return RedirectResponse(f"/e/{entity_id}#loops", status_code=303)

    @app.post("/loop/{loop_id}")
    async def loop_status(request: Request, loop_id: str):
        form = await request.form()
        repo.set_commitment_status(vault, loop_id, form.get("status", "done"))
        return back(form.get("back", "/"))

    # ── events ────────────────────────────────────────────────────────────

    @app.get("/log", response_class=HTMLResponse)
    def log_form(request: Request, status: str = "occurred"):
        with_ids = [w for w in request.query_params.getlist("with") if w]
        preset = []
        for wid in with_ids:
            ent = repo.get_entity(vault, wid)
            if ent:
                preset.append({"id": ent["id"], "name": ent["display_name"]})
        today = today_fn()
        default_day = today if status == "occurred" else today + timedelta(days=7)
        return render(request, "log.html", status=status, preset=preset,
                      default_day=default_day.isoformat(), nav="log")

    @app.post("/log")
    async def log_event(request: Request):
        form = await request.form()
        day = form.get("date") or today_fn().isoformat()
        start = f"{day}T{form.get('time')}" if form.get("time") else day
        follow_ups = []
        for text, due, owner in zip(form.getlist("fu_text"), form.getlist("fu_due"),
                                    form.getlist("fu_owner")):
            if text.strip():
                follow_ups.append({"text": text, "due": due or None, "owner": owner})
        try:
            eid = repo.create_event(
                vault, kind=form.get("kind", "meeting"), status=form.get("status", "occurred"),
                start_at=start, participants=_form_list(form, "participants"),
                title=form.get("title") or None, channel=form.get("channel") or None,
                location_ref=form.get("location") or None,
                initiated_by=form.get("initiated_by") or None,
                counts_as_contact=bool(form.get("counts_as_contact")),
                include_me=not form.get("not_me"), notes=form.get("notes"),
                tags=form.get("tags", ""), follow_ups=follow_ups)
        except ValueError as exc:
            return render(request, "log.html", status=form.get("status", "occurred"), preset=[],
                          default_day=day, error=str(exc), nav="log")
        people = _form_list(form, "participants")
        if len(people) == 1 and not people[0].startswith("new:"):
            return back(f"/e/{people[0]}", flash="Logged")
        return back(f"/event/{eid}", flash="Saved")

    @app.get("/event/{event_id}", response_class=HTMLResponse)
    def event_page(request: Request, event_id: str):
        ev = repo.get_event(vault, event_id)
        if not ev:
            raise HTTPException(404, "Not found")
        loops = vault.all("select c.*, f.display_name as from_name, t.display_name as to_name "
                          "from commitment c join entity f on f.id=c.from_id left join entity t "
                          "on t.id=c.to_id where c.source_event_id=?", (event_id,))
        return render(request, "event.html", ev=ev, loops=loops, nav="timeline",
                      ego=vault.get_meta("ego_id"))

    @app.post("/event/{event_id}/status")
    async def event_status(request: Request, event_id: str):
        form = await request.form()
        status = form.get("status", "occurred")
        if status in EVENT_STATUSES:
            repo.set_event_status(vault, event_id, status)
        return back(form.get("back") or f"/event/{event_id}")

    @app.post("/event/{event_id}/note")
    async def event_note(request: Request, event_id: str):
        form = await request.form()
        repo.add_event_note(vault, event_id, form.get("text", ""))
        return back(f"/event/{event_id}")

    @app.get("/timeline", response_class=HTMLResponse)
    def timeline(request: Request, kind: str = "", status: str = "", tag: str = ""):
        rows = repo.list_events(vault, kind=kind or None, status=status or None, tag=tag or None,
                                limit=500)
        today = today_fn().isoformat()
        future = sorted([r for r in rows if r["start_at"][:10] >= today and
                         r["status"] == "planned"], key=lambda r: r["start_at"])
        past = [r for r in rows if r not in future]
        return render(request, "timeline.html", future=future, past=past, nav="timeline",
                      f={"kind": kind, "status": status, "tag": tag})

    # ── api ───────────────────────────────────────────────────────────────

    @app.get("/api/search")
    def api_search(q: str = "", type: str = ""):
        results = repo.search_entities(vault, q, type_id=type or None)
        return JSONResponse([{"id": r["id"], "name": r["display_name"], "type": r["type_id"],
                              "engagement": r["engagement"], "context": r["context"]}
                             for r in results])

    @app.get("/export")
    def export():
        payload = json.dumps(repo.export_all(vault), indent=1, default=str)
        fname = f"ties-export-{today_fn().isoformat()}.json"
        return Response(payload, media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    from .routes import register

    register(app, vault, render, back, today_fn, entity_or_404)
    return app
