"""Routes added in Phase 1 and later: review, search, capture, import, debrief, settings."""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import capture, importer, inference, markdown, memory, repo, textindex
from .ai import AIClient, AIError, is_local_url


def form_rows(form, prefix: str) -> list[dict]:
    """Collect fields named `<prefix>-<i>-<field>` into a list of dicts, in index order."""
    rows: dict[int, dict] = {}
    pat = re.compile(rf"^{re.escape(prefix)}-(\d+)-(\w+)$")
    for key, value in form.multi_items():
        m = pat.match(key)
        if m and value not in (None, ""):
            rows.setdefault(int(m.group(1)), {})[m.group(2)] = value
    return [rows[i] for i in sorted(rows)]


def register(app, vault, render, back, today_fn, entity_or_404) -> None:

    def ai():
        return AIClient.from_vault(vault)

    # ── review inbox ──────────────────────────────────────────────────────

    @app.get("/review", response_class=HTMLResponse)
    def review(request: Request):
        added = inference.run(vault)
        proposed = repo.proposed_claims(vault)
        groups = {"rule": [], "model": [], "other": []}
        for p in proposed:
            src = p["asserted_by"]
            key = "rule" if src.startswith("rule:") else (
                "model" if p["qual"].get("source") == "model" or src.startswith("model:") else "other")
            groups[key].append(p)
        from . import resolution
        extra = {"dups": resolution.candidates(vault), "recent_merges": resolution.merges(vault, 5)}
        return render(request, "review.html", groups=groups,
                      disputed=repo.proposed_claims(vault, "disputed"),
                      added=sum(added.values()), rule_labels=inference.RULE_LABELS,
                      proposed_loops=repo.commitments(vault, status="proposed"),
                      nav="review", **extra)

    @app.post("/claim/{claim_id}/confirm")
    async def claim_confirm(request: Request, claim_id: str):
        form = await request.form()
        repo.confirm_claim(vault, claim_id, form.get("basis") or "observed")
        return back(form.get("back") or "/review")

    @app.post("/claim/{claim_id}/reject")
    async def claim_reject(request: Request, claim_id: str):
        form = await request.form()
        repo.reject_claim(vault, claim_id)
        return back(form.get("back") or "/review")

    @app.post("/claim/{claim_id}/dispute")
    async def claim_dispute(request: Request, claim_id: str):
        form = await request.form()
        repo.resolve_dispute(vault, claim_id, keep_new=form.get("keep") == "new")
        return back(form.get("back") or "/review")

    @app.post("/review/bulk")
    async def review_bulk(request: Request):
        form = await request.form()
        ids = form.getlist("ids")
        for cid in ids:
            if form.get("action") == "confirm":
                repo.confirm_claim(vault, cid, form.get("basis") or "observed")
            else:
                repo.reject_claim(vault, cid)
        return back("/review", flash=f"{len(ids)} updated")

    # ── search ────────────────────────────────────────────────────────────

    @app.get("/search", response_class=HTMLResponse)
    def search_page(request: Request, q: str = ""):
        res = textindex.search(vault, q, ai=ai()) if q.strip() else None
        return render(request, "search.html", q=q, res=res, nav="search")

    # ── quick capture ─────────────────────────────────────────────────────

    def _capture_records(form, model_data=None):
        with_ids = [w for w in form.getlist("participants") if w]
        preds = repo.predicates(vault)
        return capture.build_records(
            form.get("text", ""), with_ids=with_ids, day=form.get("date") or today_fn().isoformat(),
            kind=form.get("kind") or "meeting", channel=form.get("channel") or None,
            model_data=model_data, predicate_ids={p["id"] for p in preds},
            origin=form.get("origin") or None)

    @app.get("/capture", response_class=HTMLResponse)
    def capture_form(request: Request):
        return render(request, "capture.html", nav="capture", default_day=today_fn().isoformat(),
                      ai_ready=ai() is not None, v={})

    @app.post("/capture", response_class=HTMLResponse)
    async def capture_submit(request: Request):
        form = await request.form()
        if not (form.get("text") or "").strip():
            return render(request, "capture.html", nav="capture", error="Write something first.",
                          default_day=today_fn().isoformat(), ai_ready=ai() is not None, v=form)
        model_data = None
        if form.get("model_data"):
            model_data = json.loads(form["model_data"])
        elif form.get("use_model") and ai():
            try:
                model_data = capture.extract_with_model(ai(), form["text"], repo.predicates(vault),
                                                        today_fn())
            except AIError as exc:
                return render(request, "capture.html", nav="capture", error=str(exc), v=form,
                              default_day=today_fn().isoformat(), ai_ready=True)
        origin = form.get("origin") or f"capture:{repo.new_id()}"
        records = _capture_records(_FormProxy(form, origin), model_data)
        if form.get("confirm"):
            report = importer.apply(vault, "capture", records)
            return back("/", flash=f"Saved: {_summary_line(report)}")
        report = importer.apply(vault, "capture", records, dry_run=True)
        return render(request, "capture_preview.html", report=report, form=form, origin=origin,
                      model_data=json.dumps(model_data) if model_data else "", nav="capture")

    # ── imports ───────────────────────────────────────────────────────────

    @app.get("/import", response_class=HTMLResponse)
    def import_page(request: Request):
        return render(request, "import.html", nav="import", batches=importer.batches(vault),
                      md_folder=vault.get_meta("md_folder") or "",
                      write_gid=vault.get_meta("md_write_gid") != "0")

    @app.post("/import/markdown", response_class=HTMLResponse)
    async def import_markdown(request: Request):
        form = await request.form()
        folder = (form.get("folder") or "").strip()
        try:
            report, info = markdown.import_folder(vault, folder, dry_run=not form.get("confirm"))
        except ValueError as exc:
            return back("/import", error=str(exc))
        if form.get("confirm"):
            msg = f"Imported {info['notes']} notes: {_summary_line(report)}"
            if info["gids_written"]:
                msg += f" · ID line added to {info['gids_written']} notes"
            return back("/import", flash=msg)
        return render(request, "import_preview.html", report=report, info=info, nav="import",
                      action="/import/markdown", hidden={"folder": folder},
                      title=f"Preview: Markdown from {folder}")

    @app.post("/import/rollback/{batch_id}")
    def import_rollback(batch_id: str):
        n = importer.rollback(vault, batch_id)
        return back("/import", flash=f"Rolled back {n} items")

    # ── post-meeting debrief ──────────────────────────────────────────────

    @app.get("/event/{event_id}/debrief", response_class=HTMLResponse)
    def debrief_form(request: Request, event_id: str):
        ev = repo.get_event(vault, event_id)
        if not ev:
            raise HTTPException(404)
        ego = vault.get_meta("ego_id")
        attendees = [p for p in ev["participants"] if p["id"] != ego]
        open_loops = []
        for a in attendees:
            open_loops += repo.commitments(vault, a["id"])
        cadence_next = None
        from . import scoring
        for a in attendees:
            st = scoring.contact_status(vault, a["id"], today_fn())
            if st:
                from datetime import timedelta
                day = repo.parse_day(ev["start_at"]) or today_fn()
                nd = day + timedelta(days=st.cadence)
                cadence_next = nd if cadence_next is None or nd < cadence_next else cadence_next
        return render(request, "debrief.html", ev=ev, attendees=attendees, loops=open_loops,
                      value_preds=repo.predicates(vault, "value", "person"),
                      edge_preds=repo.predicates(vault, "entity", "person"),
                      all_preds=repo.predicates(vault),
                      next_default=cadence_next.isoformat() if cadence_next else "",
                      nav="timeline")

    @app.post("/event/{event_id}/debrief")
    async def debrief_submit(request: Request, event_id: str):
        form = await request.form()
        learned = form_rows(form, "learned")
        infs = form_rows(form, "inf")
        try:
            counts = repo.debrief_event(
                vault, event_id, summary=form.get("summary", ""),
                learned=learned, inferences_=infs, follow_ups=form_rows(form, "fu"),
                mentioned=form_rows(form, "ppl"), links_=form_rows(form, "link"),
                next_contact=form.get("next_contact") or None)
        except ValueError as exc:
            return back(f"/event/{event_id}/debrief", error=str(exc))
        for loop_id in form.getlist("done_loops"):
            repo.set_commitment_status(vault, loop_id, "done")
        parts = [f"{v} {k.replace('_', '-')}" for k, v in counts.items() if v]
        return back(f"/event/{event_id}", flash="Debrief saved" + (": " + ", ".join(parts) if parts else ""))

    # ── links and summaries on profiles ───────────────────────────────────

    @app.post("/e/{entity_id}/link")
    async def add_link(request: Request, entity_id: str):
        form = await request.form()
        if form.get("remove"):
            repo.remove_link(vault, entity_id, form["remove"])
        elif form.get("url"):
            repo.add_link(vault, entity_id, form["url"], form.get("note"))
        return back(f"/e/{entity_id}")

    @app.post("/e/{entity_id}/summary")
    async def regen_summary(request: Request, entity_id: str):
        form = await request.form()
        if form.get("clear"):
            memory.clear_model_summary(vault, entity_id)
            return back(f"/e/{entity_id}")
        client = ai()
        if not client:
            return back(f"/e/{entity_id}", error="No local model is configured (Settings).")
        try:
            memory.regenerate(vault, entity_id, client, today_fn())
        except AIError as exc:
            return back(f"/e/{entity_id}", error=str(exc))
        return back(f"/e/{entity_id}", flash="Summary updated")

    # ── settings ──────────────────────────────────────────────────────────

    @app.get("/settings", response_class=HTMLResponse)
    def settings(request: Request):
        meta = {k: vault.get_meta(k) or "" for k in (
            "ai_base_url", "ai_chat_model", "ai_embed_model", "md_folder", "md_write_gid",
            "gateway_base_url", "gateway_model", "gateway_enabled")}
        counts = {"chunks": vault.scalar("select count(*) from chunk"),
                  "embedded": vault.scalar("select count(*) from embedding")}
        from . import triggers
        return render(request, "settings.html", meta=meta, rules=inference.rule_settings(vault),
                      trig=triggers.settings(vault),
                      rule_labels=inference.RULE_LABELS, counts=counts, nav="settings")

    @app.post("/settings")
    async def settings_save(request: Request):
        form = await request.form()
        section = form.get("section")
        if section == "ai":
            url = (form.get("ai_base_url") or "").strip()
            if url and not is_local_url(url):
                return back("/settings", error="The local model address must be on this machine "
                                               "(localhost or 127.0.0.1).")
            for k in ("ai_base_url", "ai_chat_model", "ai_embed_model"):
                vault.set_meta(k, (form.get(k) or "").strip())
        elif section == "markdown":
            vault.set_meta("md_folder", (form.get("md_folder") or "").strip())
            vault.set_meta("md_write_gid", "1" if form.get("md_write_gid") else "0")
        elif section == "rules":
            inference.save_rule_settings(vault, {k: bool(form.get(k)) for k in inference.RULE_LABELS})
        elif section == "gateway":
            vault.set_meta("gateway_enabled", "1" if form.get("gateway_enabled") else "0")
            for k in ("gateway_base_url", "gateway_model"):
                vault.set_meta(k, (form.get(k) or "").strip())
        return back("/settings", flash="Saved")

    @app.post("/settings/ai-test")
    def settings_ai_test():
        client = ai()
        if not client:
            return back("/settings", error="Set the local model address first.")
        if client.ping():
            return back("/settings", flash="The model answered.")
        return back("/settings", error="No answer from the model at that address.")

    @app.post("/settings/reindex")
    def settings_reindex():
        stats = textindex.rebuild(vault)
        embedded = 0
        client = ai()
        if client and client.embed_model:
            try:
                while True:
                    n = textindex.embed_pending(vault, client)
                    embedded += n
                    if n == 0:
                        break
            except AIError as exc:
                return back("/settings", error=f"Index rebuilt; embedding failed: {exc}")
        return back("/settings", flash=f"Indexed {stats['entities']} entries and "
                                       f"{stats['sources']} notes; embedded {embedded}.")

    @app.get("/api/inference/run")
    def api_inference():
        return JSONResponse(inference.run(vault))

    from . import routes2
    routes2.register(app, vault, render, back, today_fn, entity_or_404)


class _FormProxy:
    """Lets capture reuse a submitted form with a fixed origin."""

    def __init__(self, form, origin):
        self.form, self.origin = form, origin

    def get(self, key, default=None):
        if key == "origin":
            return self.origin
        return self.form.get(key, default)

    def getlist(self, key):
        return self.form.getlist(key)


def _summary_line(report) -> str:
    bits = []
    for label, items in (("new", report.created), ("facts", report.claims_added),
                         ("events", report.events_added), ("follow-ups", report.follow_ups_added),
                         ("links", report.links_added), ("conflicts", report.conflicts),
                         ("removed", report.claims_retired)):
        if items:
            bits.append(f"{len(items)} {label}")
    return ", ".join(bits) or "no changes"
