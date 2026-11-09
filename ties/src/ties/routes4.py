"""Phase 4 routes: research vault pages, exports, research importers, batch ER, vault links."""

from __future__ import annotations

import json

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from . import (crossvault, exports, importer, repo, research, research_import, resolution)


def register(app, vault, render, back, today_fn, entity_or_404) -> None:
    # ── projects ──────────────────────────────────────────────────────────

    @app.get("/projects", response_class=HTMLResponse)
    def projects_page(request: Request):
        return render(request, "projects.html", projects=research.projects(vault), nav="projects")

    @app.post("/projects")
    async def project_create(request: Request):
        form = await request.form()
        if not (form.get("title") or "").strip():
            return back("/projects", error="A project needs a title.")
        pid = research.create_project(vault, form["title"], form.get("description", ""))
        return back(f"/projects/{pid}")

    @app.get("/projects/{project_id}", response_class=HTMLResponse)
    def project_page(request: Request, project_id: str):
        try:
            d = research.project_detail(vault, project_id)
        except ValueError:
            raise HTTPException(404)
        ids = {e["id"] for e in d["entities"]}
        return render(request, "project.html", d=d, nav="projects",
                      contradictions=research.contradictions(vault, ids) if ids else [])

    @app.post("/projects/{project_id}/add")
    async def project_add(request: Request, project_id: str):
        form = await request.form()
        if form.get("entity"):
            with vault.tx() as c:
                eid = repo.resolve_ref(c, vault, form["entity"], "person", "reference")
            research.add_to_project(vault, project_id, "entity", eid)
        if form.get("source"):
            research.add_to_project(vault, project_id, "source", form["source"])
        if form.get("remove_kind"):
            research.remove_from_project(vault, project_id, form["remove_kind"], form["remove_id"])
        return back(form.get("back") or f"/projects/{project_id}")

    # ── hypotheses matrix ─────────────────────────────────────────────────

    @app.get("/projects/{project_id}/ach", response_class=HTMLResponse)
    def ach_page(request: Request, project_id: str):
        try:
            d = research.project_detail(vault, project_id)
        except ValueError:
            raise HTTPException(404)
        return render(request, "ach.html", p=d["project"], m=research.ach_matrix(vault, project_id),
                      stances=list(research.STANCES), nav="projects")

    @app.post("/projects/{project_id}/ach")
    async def ach_save(request: Request, project_id: str):
        form = await request.form()
        if form.get("hypothesis"):
            research.add_hypothesis(vault, project_id, form["hypothesis"], form.get("description", ""))
        if form.get("evidence"):
            research.add_evidence_item(vault, project_id, form["evidence"],
                                       form.get("evidence_kind") or "note",
                                       form.get("evidence_target") or None)
        for key, value in form.items():
            if key.startswith("r-"):
                _, hid, eid = key.split("-", 2)
                research.rate(vault, hid, eid, value)
        return back(f"/projects/{project_id}/ach", flash="Saved")

    # ── sources ───────────────────────────────────────────────────────────

    @app.get("/sources", response_class=HTMLResponse)
    def sources_page(request: Request, q: str = "", project: str = ""):
        return render(request, "sources.html", rows=research.sources(vault, q, project or None),
                      q=q, projects=research.projects(vault), project=project, nav="sources")

    @app.post("/sources")
    async def source_create(request: Request):
        form = await request.form()
        if not (form.get("title") or "").strip():
            return back("/sources", error="A source needs a title.")
        sid = research.add_source(
            vault, title=form["title"], uri=form.get("uri"), author=form.get("author"),
            published_at=form.get("published_at"), source_type=form.get("source_type") or "other",
            reliability=form.get("reliability") or None,
            credibility=int(form["credibility"]) if form.get("credibility") else None,
            excerpt=form.get("excerpt", ""), project_id=form.get("project") or None)
        return back(f"/sources/{sid}")

    @app.get("/sources/{source_id}", response_class=HTMLResponse)
    def source_page(request: Request, source_id: str):
        try:
            d = research.source_detail(vault, source_id)
        except ValueError:
            raise HTTPException(404)
        return render(request, "source.html", d=d, preds=repo.predicates(vault),
                      projects=research.projects(vault), nav="sources",
                      all_sources=research.sources(vault)[:200],
                      network=vault.get_meta("allow_network") == "1")

    @app.post("/sources/{source_id}")
    async def source_update(request: Request, source_id: str):
        form = await request.form()
        action = form.get("action")
        try:
            if action == "grade":
                research.update_source_grade(
                    vault, source_id, form.get("reliability") or None,
                    int(form["credibility"]) if form.get("credibility") else None,
                    form.get("independent_of") or None)
            elif action == "claim":
                research.add_evidence_claim(
                    vault, source_id, form.get("subject", ""), form.get("predicate_id", ""),
                    value=form.get("value") or None, object_ref=form.get("object") or None,
                    valid_from=form.get("valid_from") or None, valid_to=form.get("valid_to") or None,
                    quote=form.get("quote"))
            elif action == "snapshot":
                info = research.snapshot(vault, source_id)
                return back(f"/sources/{source_id}", flash=f"Saved a copy ({info['size']} bytes)")
            elif action == "project" and form.get("project"):
                research.add_to_project(vault, form["project"], "source", source_id)
        except (ValueError, OSError) as exc:
            return back(f"/sources/{source_id}", error=str(exc))
        return back(f"/sources/{source_id}", flash="Saved")

    @app.get("/sources/{source_id}/copy")
    def source_copy(source_id: str):
        blob = vault.one("select content_type, data from source_blob where source_id=?", (source_id,))
        if not blob:
            raise HTTPException(404)
        # Served as a download, never rendered in the app's origin.
        return Response(blob["data"], media_type="application/octet-stream",
                        headers={"Content-Disposition": f'attachment; filename="source-{source_id}"',
                                 "X-Content-Type-Options": "nosniff"})

    # ── contradictions ────────────────────────────────────────────────────

    @app.get("/contradictions", response_class=HTMLResponse)
    def contradictions_page(request: Request):
        return render(request, "contradictions.html", rows=research.contradictions(vault),
                      nav="contradictions")

    # ── exports ───────────────────────────────────────────────────────────

    @app.get("/export/graph")
    def export_graph(format: str = "graphml", project: str = "", valid_at: str = ""):
        ids = research.project_entity_ids(vault, project) if project else None
        valid = valid_at or None
        stamp = today_fn().isoformat()
        if format == "graphml":
            body, mt, ext = exports.graphml(vault, ids, valid), "application/xml", "graphml"
        elif format == "gexf":
            body, mt, ext = exports.gexf(vault, ids, valid), "application/xml", "gexf"
        elif format == "csv":
            body, mt, ext = exports.csv_zip(vault, ids, valid), "application/zip", "zip"
        elif format == "ftm":
            body, mt, ext = exports.ftm(vault, ids, valid), "application/x-ndjson", "ftm.json"
        else:
            raise HTTPException(400, "Unknown format")
        return Response(body, media_type=mt, headers={
            "Content-Disposition": f'attachment; filename="ties-{stamp}.{ext}"'})

    # ── research importers (network) ──────────────────────────────────────

    @app.get("/research/import", response_class=HTMLResponse)
    def research_import_page(request: Request):
        from .research_free import SOURCES
        return render(request, "research_import.html", nav="import", sources=SOURCES,
                      network=vault.get_meta("allow_network") == "1",
                      contact=vault.get_meta("research_contact") or "")

    fetched: dict[str, tuple[float, list]] = {}

    @app.post("/research/import", response_class=HTMLResponse)
    async def research_import_run(request: Request):
        import hashlib
        import time

        from .research_free import FetchError, fetch_records, search_wikidata
        form = await request.form()
        kind = form.get("kind")
        if kind == "contact":
            vault.set_meta("research_contact", (form.get("contact") or "").strip())
            return back("/research/import", flash="Saved")
        if kind == "wikidata-search":
            try:
                research_import.require_network(vault)
                hits = search_wikidata(form.get("query", ""))
            except (ValueError, FetchError) as exc:
                return back("/research/import", error=str(exc))
            from .research_free import SOURCES
            return render(request, "research_import.html", nav="import", sources=SOURCES,
                          network=True, hits=hits, q=form.get("query", ""),
                          contact=vault.get_meta("research_contact") or "")
        key = hashlib.sha1(json.dumps(sorted((k, v) for k, v in form.items()
                                             if k != "confirm")).encode()).hexdigest()
        try:
            cached = fetched.get(key)
            if cached and time.time() - cached[0] < 900:
                records = cached[1]
            elif kind == "openalex":
                research_import.require_network(vault)
                data = research_import.fetch_openalex(form.get("query") or None,
                                                      form.get("ident") or None,
                                                      int(form.get("limit") or 50))
                records = research_import.parse_openalex(data)
            else:
                records = fetch_records(vault, kind, query=form.get("query", "").strip(),
                                        ident=form.get("ident", "").strip(),
                                        limit=int(form.get("limit") or 20),
                                        match_affiliations=bool(form.get("match_affiliations")),
                                        children=int(form.get("children") or 0))
            fetched[key] = (time.time(), records)
        except (ValueError, OSError, FetchError) as exc:
            return back("/research/import", error=str(exc))
        if not records:
            return back("/research/import", error="Nothing found.")
        if form.get("confirm"):
            report = importer.apply(vault, kind, records, label=form.get("query") or form.get("ident"))
            research_import.grade_import_sources(vault, f"{kind}:", "B", 2)
            fetched.pop(key, None)
            from .routes import _summary_line
            return back("/research/import", flash=f"{kind}: {_summary_line(report)}")
        report = importer.apply(vault, kind, records, dry_run=True)
        return render(request, "import_preview.html", report=report, info=None, nav="import",
                      action="/research/import", title=f"Preview: {kind} ({len(records)} records)",
                      hidden={k: v for k, v in form.items() if k != "confirm"})

    # ── batch resolution ──────────────────────────────────────────────────

    @app.get("/resolve", response_class=HTMLResponse)
    def resolve_page(request: Request):
        return render(request, "resolve.html", r=resolution.batch(vault), nav="review",
                      merges=resolution.merges(vault, 10))

    @app.post("/resolve")
    async def resolve_auto(request: Request):
        form = await request.form()
        threshold = float(form.get("threshold") or 0.95)
        r = resolution.batch(vault, auto_threshold=threshold)
        return back("/resolve", flash=f"Merged {len(r['merged'])} pairs at score ≥ {threshold}. "
                                      "Each can be undone.")

    # ── links to another vault ────────────────────────────────────────────

    @app.get("/links", response_class=HTMLResponse)
    def links_page(request: Request):
        path = vault.get_meta("other_vault_path") or ""
        other = crossvault.open_other(path) if path else None
        cands = crossvault.candidates(vault, other) if other else []
        return render(request, "links.html", path=path, available=other is not None,
                      cands=cands, nav="settings")

    @app.post("/links")
    async def links_save(request: Request):
        form = await request.form()
        if form.get("path") is not None and form.get("action") == "path":
            vault.set_meta("other_vault_path", form["path"].strip())
        elif form.get("action") == "confirm":
            crossvault.confirm(vault, vault.get_meta("other_vault_path"), form["local"],
                               form["other"], form.get("name", ""))
        return back("/links")

    # ── settings: network permission ──────────────────────────────────────

    @app.post("/settings/network")
    async def network_setting(request: Request):
        form = await request.form()
        vault.set_meta("allow_network", "1" if form.get("allow_network") else "0")
        return back("/settings", flash="Saved")
