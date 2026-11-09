"""Phase 3 routes: goals, insights, introductions, weekly review, strategy via the gateway."""

from __future__ import annotations

import json

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from . import gateway, graph, repo, strategy
from .ai import AIClient, AIError


def register(app, vault, render, back, today_fn, entity_or_404) -> None:

    def ai():
        return AIClient.from_vault(vault)

    # ── goals ─────────────────────────────────────────────────────────────

    @app.get("/goals", response_class=HTMLResponse)
    def goals_page(request: Request):
        return render(request, "goals.html", goals=strategy.goals(vault, None), nav="insights")

    @app.post("/goals")
    async def goal_create(request: Request):
        form = await request.form()
        if not (form.get("title") or "").strip():
            return back("/goals", error="A goal needs a title.")
        gid = strategy.create_goal(vault, form["title"], form.get("description", ""),
                                   form.get("keywords", ""), form.get("horizon") or None)
        return back(f"/goals/{gid}")

    @app.get("/goals/{goal_id}", response_class=HTMLResponse)
    def goal_page(request: Request, goal_id: str):
        try:
            d = strategy.goal_detail(vault, goal_id, today_fn(), ai=ai())
        except ValueError:
            raise HTTPException(404)
        return render(request, "goal.html", d=d, nav="insights")

    @app.post("/goals/{goal_id}")
    async def goal_update(request: Request, goal_id: str):
        form = await request.form()
        action = form.get("action")
        if action == "link" and form.get("entity"):
            with vault.tx() as c:
                eid = repo.resolve_ref(c, vault, form["entity"], "person", "acquaintance")
            strategy.link_goal(vault, goal_id, eid, float(form.get("relevance") or 1.0),
                               form.get("role") or None)
        elif action == "unlink":
            strategy.unlink_goal(vault, goal_id, form["entity"])
        elif action in ("done", "dropped", "active"):
            strategy.update_goal(vault, goal_id, status=action)
        elif action == "edit":
            strategy.update_goal(vault, goal_id, title=form.get("title"),
                                 description=form.get("description"), keywords=form.get("keywords"),
                                 horizon=form.get("horizon") or None)
        return back(form.get("back") or f"/goals/{goal_id}")

    # ── insights ──────────────────────────────────────────────────────────

    @app.get("/insights", response_class=HTMLResponse)
    def insights(request: Request):
        a = graph.analytics(vault, today_fn())
        intros = strategy.intro_suggestions(vault, ai=ai())
        return render(request, "insights.html", a=a, intros=intros,
                      goals=strategy.goals(vault), nav="insights")

    @app.post("/intro/{key}/dismiss")
    def intro_dismiss(key: str):
        from . import triggers
        triggers.set_state(vault, "intro", key, "dismissed")
        return back("/insights")

    @app.post("/intro/{key}/plan")
    async def intro_plan(request: Request, key: str):
        form = await request.form()
        seeker, offerer = key.split(":")
        names = {e: repo.get_entity(vault, e)["display_name"] for e in (seeker, offerer)}
        repo.add_commitment(vault, seeker, f"Introduce {names[seeker]} to {names[offerer]}"
                            + (f" ({form.get('need')})" if form.get("need") else ""), None)
        from . import triggers
        triggers.set_state(vault, "intro", key, "done")
        return back("/insights", flash="Added to your follow-ups")

    # ── weekly review ─────────────────────────────────────────────────────

    @app.get("/weekly", response_class=HTMLResponse)
    def weekly(request: Request):
        return render(request, "weekly.html", r=strategy.weekly_review(vault, today_fn()),
                      ai_ready=ai() is not None, narrative=None, nav="insights")

    @app.post("/weekly", response_class=HTMLResponse)
    def weekly_ai(request: Request):
        r = strategy.weekly_review(vault, today_fn())
        client = ai()
        narrative, error = None, None
        if client:
            try:
                narrative = strategy.weekly_narrative(client, r)
            except AIError as exc:
                error = str(exc)
        return render(request, "weekly.html", r=r, ai_ready=client is not None,
                      narrative=narrative, error=error, nav="insights")

    # ── strategy through the privacy gateway ──────────────────────────────

    @app.get("/strategy", response_class=HTMLResponse)
    def strategy_page(request: Request, task: str = "introductions", goal: str = ""):
        error, payload = None, None
        try:
            payload, _ = gateway.build_payload(vault, task, goal or None, today_fn())
        except gateway.GatewayError as exc:
            error = str(exc)
        return render(request, "strategy.html", task=task, goal=goal, tasks=gateway.TASKS,
                      goals=strategy.goals(vault), payload=payload,
                      payload_json=json.dumps(payload, indent=1, ensure_ascii=False) if payload else "",
                      phash=gateway.payload_hash(payload) if payload else "",
                      outside_on=vault.get_meta("gateway_enabled") == "1",
                      local_on=ai() is not None, result=None, error=error,
                      log=gateway.egress_log(vault), nav="insights")

    @app.post("/strategy", response_class=HTMLResponse)
    async def strategy_send(request: Request):
        form = await request.form()
        task, goal, where = form.get("task", "introductions"), form.get("goal") or None, form.get("where")
        result, error, payload = None, None, None
        try:
            payload, alias_map = gateway.build_payload(vault, task, goal, today_fn())
            if form.get("hash") != gateway.payload_hash(payload):
                raise gateway.GatewayError("Your data changed since the preview. Review it again.")
            result = gateway.send(vault, payload, alias_map, where=where)
        except gateway.GatewayError as exc:
            error = str(exc)
        return render(request, "strategy.html", task=task, goal=goal or "", tasks=gateway.TASKS,
                      goals=strategy.goals(vault), payload=payload,
                      payload_json=json.dumps(payload, indent=1, ensure_ascii=False) if payload else "",
                      phash=gateway.payload_hash(payload) if payload else "",
                      outside_on=vault.get_meta("gateway_enabled") == "1",
                      local_on=ai() is not None, result=result, error=error,
                      log=gateway.egress_log(vault), nav="insights")

    # ── proposed follow-ups (from agents) ─────────────────────────────────

    @app.post("/loop/{loop_id}/accept")
    def loop_accept(loop_id: str):
        repo.set_commitment_status(vault, loop_id, "open")
        return back("/review")

    try:
        from . import routes4
        routes4.register(app, vault, render, back, today_fn, entity_or_404)
    except ImportError as exc:
        if "routes4" not in str(exc):
            raise
