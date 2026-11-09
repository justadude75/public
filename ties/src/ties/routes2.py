"""Phase 2 routes: standard importers, duplicate merging, snoozing, briefs, graph explorer."""

from __future__ import annotations

import json
from datetime import timedelta

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import brief, graph, importer, importers_std, repo, resolution, triggers


async def _upload_text(form) -> str:
    if form.get("content"):
        return form["content"]
    up = form.get("file")
    if up is None or not hasattr(up, "read"):
        return ""
    data = await up.read()
    for enc in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return ""


def register(app, vault, render, back, today_fn, entity_or_404) -> None:

    # ── standard importers ────────────────────────────────────────────────

    async def _run(request, kind: str, build):
        form = await request.form()
        text = await _upload_text(form)
        if not text.strip():
            return back("/import", error="Choose a file first.")
        try:
            records, extra = build(form, text)
        except ValueError as exc:
            return back("/import", error=str(exc))
        if not records:
            return back("/import", error="Nothing recognisable was found in that file.")
        if form.get("confirm"):
            report = importer.apply(vault, kind, records, label=form.get("filename") or kind)
            from .routes import _summary_line
            return back("/import", flash=f"{kind}: {_summary_line(report)}")
        report = importer.apply(vault, kind, records, dry_run=True)
        hidden = {"content": text, "filename": getattr(form.get("file"), "filename", "") or
                  form.get("filename", ""), **{k: v for k, v in form.items()
                                                if k.startswith(("map-", "opt-"))}}
        return render(request, "import_preview.html", report=report, info=None, nav="import",
                      action=f"/import/{kind}", hidden=hidden, mapping=extra,
                      title=f"Preview: {kind} ({len(records)} records)")

    @app.post("/import/vcard", response_class=HTMLResponse)
    async def import_vcard(request: Request):
        return await _run(request, "vcard", lambda form, text: (
            importers_std.parse_vcard(text, as_contacts=bool(form.get("opt-contacts"))), None))

    @app.post("/import/ical", response_class=HTMLResponse)
    async def import_ical(request: Request):
        return await _run(request, "ical", lambda form, text: (
            importers_std.parse_ical(text, importers_std.my_emails(vault), today_fn(),
                                     only_with_others=not form.get("opt-all")), None))

    @app.post("/import/csv", response_class=HTMLResponse)
    async def import_csv(request: Request):
        def build(form, text):
            mapping = {k[4:]: v for k, v in form.items() if k.startswith("map-")} or None
            records, used, headers = importers_std.parse_csv(
                text, mapping, as_contacts=bool(form.get("opt-contacts")))
            return records, {"used": used, "headers": headers, "fields": importers_std.CSV_FIELDS}
        return await _run(request, "csv", build)

    # ── duplicates ────────────────────────────────────────────────────────

    @app.get("/merge", response_class=HTMLResponse)
    def merge_page(request: Request, a: str, b: str):
        ents = []
        for eid in (a, b):
            e = entity_or_404(eid)
            ents.append({"e": e, "facts": repo.facts(vault, eid), "rels": repo.relations(vault, eid),
                         "idents": repo.identifiers(vault, eid),
                         "events": len(repo.list_events(vault, entity_id=eid, limit=1000)),
                         "names": [r["name"] for r in vault.all(
                             "select name from entity_name where entity_id=?", (eid,))]})
        return render(request, "merge.html", ents=ents, nav="review",
                      history=resolution.merges(vault))

    @app.post("/merge")
    async def merge_do(request: Request):
        form = await request.form()
        try:
            did = resolution.merge(vault, form["keep"], form["drop"])
        except ValueError as exc:
            return back("/review", error=str(exc))
        return back(f"/e/{form['keep']}", flash="Merged. Undo it from Review → recent merges.",
                    undo=did)

    @app.post("/merge/{decision_id}/undo")
    def merge_undo(decision_id: str):
        try:
            resolution.unmerge(vault, decision_id)
        except ValueError as exc:
            return back("/review", error=str(exc))
        return back("/review", flash="Merge undone")

    @app.post("/not-same")
    async def not_same(request: Request):
        form = await request.form()
        resolution.not_same(vault, form["a"], form["b"])
        return back("/review", flash="Noted. That pair won't be suggested again.")

    @app.get("/merges", response_class=HTMLResponse)
    def merges_page(request: Request):
        return render(request, "merges.html", rows=resolution.merges(vault), nav="review")

    # ── snooze / dismiss ──────────────────────────────────────────────────

    @app.post("/outreach/{kind}/{key}")
    async def outreach(request: Request, kind: str, key: str):
        form = await request.form()
        status = form.get("status", "snoozed")
        until = None
        if status == "snoozed":
            until = (today_fn() + timedelta(days=int(form.get("days") or 7))).isoformat()
        if status == "clear":
            triggers.clear_state(vault, kind, key)
        else:
            triggers.set_state(vault, kind, key, status, until, form.get("reason"))
        return back(form.get("back") or "/")

    @app.post("/settings/triggers")
    async def trigger_settings(request: Request):
        form = await request.form()
        triggers.save_settings(vault, {k: form.get(k) or v for k, v in triggers.DEFAULTS.items()})
        return back("/settings", flash="Saved")

    # ── briefs ────────────────────────────────────────────────────────────

    @app.get("/event/{event_id}/brief", response_class=HTMLResponse)
    def event_brief(request: Request, event_id: str):
        try:
            b = brief.event_brief(vault, event_id, today_fn())
        except ValueError:
            raise HTTPException(404)
        return render(request, "brief.html", b=b, nav="timeline")

    # ── graph ─────────────────────────────────────────────────────────────

    @app.get("/graph", response_class=HTMLResponse)
    def graph_page(request: Request, center: str = "", to: str = ""):
        center_ent = repo.get_entity(vault, center) if center else None
        to_ent = repo.get_entity(vault, to) if to else None
        paths = graph.warm_paths(vault, to, today=today_fn()) if to_ent else None
        return render(request, "graph.html", center=center_ent, to=to_ent, paths=paths,
                      nav="graph")

    @app.get("/api/graph")
    def api_graph(center: str = "", depth: int = 2, inferred: int = 1, together: int = 1,
                  me: int = 0, types: str = "person,organization,place"):
        data = graph.build(vault, center=center or None, depth=max(1, min(depth, 4)),
                           include_inferred=bool(inferred), include_together=bool(together),
                           include_ego=bool(me), types=tuple(t for t in types.split(",") if t),
                           today=today_fn())
        return JSONResponse(data)

    @app.get("/api/path")
    def api_path(to: str):
        return JSONResponse(graph.warm_paths(vault, to, today=today_fn()))

    try:
        from . import routes3
        routes3.register(app, vault, render, back, today_fn, entity_or_404)
    except ImportError as exc:
        if "routes3" not in str(exc):
            raise
