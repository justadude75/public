"""Command-line entry point: ties init | serve | demo | export | backup."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import webbrowser
from pathlib import Path

from . import repo
from .db import Vault, VaultError, is_encrypted

DEFAULT_VAULT = Path(os.environ.get("TIES_VAULT", Path.home() / ".ties" / "personal.vault"))


def _passphrase(path: Path, confirm: bool = False) -> str | None:
    env = os.environ.get("TIES_PASSPHRASE")
    if env:
        return env
    if confirm:
        first = getpass.getpass("New passphrase: ")
        if not first:
            sys.exit("A passphrase is required for an encrypted vault.")
        if getpass.getpass("Repeat passphrase: ") != first:
            sys.exit("Passphrases did not match.")
        return first
    if is_encrypted(path):
        return getpass.getpass(f"Passphrase for {path.name}: ")
    return None


def open_vault(path: Path) -> Vault:
    if not path.exists():
        sys.exit(f"No vault at {path}. Create one with: ties init")
    try:
        vault = Vault(path, _passphrase(path))
    except VaultError as exc:
        sys.exit(str(exc))
    repo.prepare(vault)
    return vault


def cmd_init(args) -> None:
    path = Path(args.vault).expanduser()
    if path.exists():
        sys.exit(f"{path} already exists.")
    path.parent.mkdir(parents=True, exist_ok=True)
    passphrase = _passphrase(path, confirm=True) if args.encrypt else None
    name = args.name or input("Your name: ").strip() or "Me"
    try:
        vault = Vault(path, passphrase)
    except VaultError as exc:
        sys.exit(str(exc))
    repo.init_vault(vault, name)
    if args.research:
        vault.set_meta("vault_kind", "research")
    vault.close()
    print(f"Created {'encrypted ' if passphrase else ''}{'research ' if args.research else ''}"
          f"vault at {path}")
    print(f"Start it with: ties serve --vault {path}")


def cmd_serve(args) -> None:
    import uvicorn

    from .web import create_app

    vault = open_vault(Path(args.vault).expanduser())
    app = create_app(vault)
    _start_background(vault)
    url = f"http://127.0.0.1:{args.port}"
    print(f"ties is running at {url}  (Ctrl+C to stop)")
    if args.open:
        webbrowser.open(url)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


def _start_background(vault: Vault) -> None:
    """Keep embeddings up to date while the app runs (only when a local model is set)."""
    import threading
    import time

    from . import textindex
    from .ai import AIClient

    def loop():
        while True:
            client = AIClient.from_vault(vault)
            if client and client.embed_model:
                try:
                    while textindex.embed_pending(vault, client):
                        pass
                except Exception:
                    pass
            time.sleep(60)

    threading.Thread(target=loop, daemon=True, name="ties-embed").start()


def cmd_demo(args) -> None:
    from .demo import build_demo

    path = Path(args.vault).expanduser()
    if path.exists():
        sys.exit(f"{path} already exists. Choose another path with --vault.")
    path.parent.mkdir(parents=True, exist_ok=True)
    vault = Vault(path)
    if args.research:
        from .demo import build_research_demo
        build_research_demo(vault)
    else:
        build_demo(vault)
    vault.close()
    print(f"Demo vault with sample people and events created at {path}")
    print(f"Open it with: ties serve --vault {path}")


def cmd_export(args) -> None:
    vault = open_vault(Path(args.vault).expanduser())
    data = json.dumps(repo.export_all(vault), indent=1, default=str)
    if args.out:
        Path(args.out).write_text(data, encoding="utf-8")
        print(f"Exported to {args.out} (not encrypted; store it somewhere safe)")
    else:
        print(data)


def cmd_restore(args) -> None:
    from .importers_std import restore

    path = Path(args.vault).expanduser()
    if path.exists():
        sys.exit(f"{path} already exists. Restore into a new vault path.")
    path.parent.mkdir(parents=True, exist_ok=True)
    passphrase = _passphrase(path, confirm=True) if args.encrypt else None
    vault = Vault(path, passphrase)
    repo.prepare(vault)
    data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    try:
        counts = restore(vault, data)
    except ValueError as exc:
        vault.close()
        path.unlink(missing_ok=True)
        sys.exit(str(exc))
    vault.close()
    print(f"Restored {sum(counts.values())} rows into {path}")


def cmd_import(args) -> None:
    from . import importer, importers_std, markdown

    vault = open_vault(Path(args.vault).expanduser())
    if args.kind == "markdown":
        report, info = markdown.import_folder(vault, args.source, dry_run=not args.apply)
    else:
        text = Path(args.source).read_text(encoding="utf-8", errors="replace")
        if args.kind == "vcard":
            records = importers_std.parse_vcard(text, as_contacts=args.contacts)
        elif args.kind == "ical":
            records = importers_std.parse_ical(text, importers_std.my_emails(vault))
        else:
            records, mapping, _ = importers_std.parse_csv(text, as_contacts=args.contacts)
            print("Column matching:", json.dumps(mapping))
        report = importer.apply(vault, args.kind, records, dry_run=not args.apply,
                                label=args.source)
    print(f"{'Applied' if args.apply else 'Preview (nothing saved; add --apply)'}: "
          f"{len(report.created)} new, {len(report.matched)} matched, "
          f"{len(report.claims_added)} facts, {len(report.events_added)} events, "
          f"{len(report.follow_ups_added)} follow-ups, {len(report.conflicts)} conflicts, "
          f"{len(report.claims_retired)} removed")
    for w in report.warnings[:20]:
        print("  note:", w)


def cmd_mcp(args) -> None:
    from .mcp_server import serve_stdio

    path = Path(args.vault).expanduser()
    if is_encrypted(path) and not os.environ.get("TIES_PASSPHRASE"):
        sys.exit("This vault is encrypted: set TIES_PASSPHRASE in the MCP client's environment.")
    vault = open_vault(path)
    serve_stdio(vault, agent=args.agent)


def cmd_research(args) -> None:
    from . import importer, research_import
    from .research_free import FetchError, fetch_records

    vault = open_vault(Path(args.vault).expanduser())
    if args.allow_network:
        vault.set_meta("allow_network", "1")
    try:
        if args.source == "openalex":
            research_import.require_network(vault)
            data = research_import.fetch_openalex(args.query if not args.id else None, args.id,
                                                  args.limit)
            records = research_import.parse_openalex(data)
        else:
            records = fetch_records(vault, args.source, query=args.query, ident=args.id or "",
                                    limit=args.limit, children=args.children,
                                    match_affiliations=not args.no_affiliations)
    except (ValueError, OSError, FetchError) as exc:
        sys.exit(str(exc))
    report = importer.apply(vault, args.source, records, dry_run=not args.apply,
                            label=args.query or args.id)
    if args.apply:
        research_import.grade_import_sources(vault, f"{args.source}:", "B", 2)
    print(f"{'Applied' if args.apply else 'Preview (add --apply to save)'}: {len(records)} records, "
          f"{len(report.created)} new, {len(report.matched)} matched, {len(report.claims_added)} facts")


def cmd_resolve(args) -> None:
    from . import resolution

    vault = open_vault(Path(args.vault).expanduser())
    result = resolution.batch(vault, auto_threshold=args.auto)
    for m in result["merged"]:
        print(f"merged  {m['drop']['display_name']} -> {m['keep']['display_name']}  ({m['score']})")
    for c in result["candidates"][:args.show]:
        print(f"{c['score']:.2f}  {c['a']['display_name']}  ~  {c['b']['display_name']}  "
              f"[{', '.join(c['reasons'])}]")
    print(f"{len(result['merged'])} merged, {len(result['candidates'])} candidates left for review")


def cmd_link(args) -> None:
    from . import crossvault

    vault = open_vault(Path(args.vault).expanduser())
    other_path = Path(args.other).expanduser()
    if is_encrypted(other_path) and not os.environ.get("TIES_RESEARCH_PASSPHRASE"):
        os.environ["TIES_RESEARCH_PASSPHRASE"] = getpass.getpass(f"Passphrase for {other_path.name}: ")
    other = crossvault.open_other(str(other_path))
    if other is None:
        sys.exit(f"Could not open {other_path}")
    vault.set_meta("other_vault_path", str(other_path))
    n = 0
    for c in crossvault.candidates(vault, other):
        answer = "y" if args.yes else input(
            f"Link {c['local']['display_name']} <-> {c['other_name']} ({c['why']})? [y/N] ")
        if answer.strip().lower() == "y":
            crossvault.confirm(vault, str(other_path), c["local"]["id"], c["other_id"], c["other_name"])
            n += 1
    print(f"{n} links saved. Nothing was copied between vaults.")


def cmd_backup(args) -> None:
    vault = open_vault(Path(args.vault).expanduser())
    dest = vault.backup(Path(args.dest).expanduser())
    print(f"Backed up to {dest}{' (encrypted)' if vault.encrypted else ''}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ties", description="Local personal relationship graph")
    parser.add_argument("--vault", default=str(DEFAULT_VAULT),
                        help=f"vault file (default: {DEFAULT_VAULT}, or $TIES_VAULT)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="create a new vault")
    p.add_argument("--encrypt", action="store_true", help="encrypt the vault with a passphrase")
    p.add_argument("--name", help="your name")
    p.add_argument("--research", action="store_true", help="create a research vault")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("serve", help="run the app in your browser (localhost only)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--open", action="store_true", help="open the browser")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("demo", help="create a vault filled with sample data to try things out")
    p.add_argument("--research", action="store_true", help="a sample research vault instead")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("export", help="export everything as JSON")
    p.add_argument("--out", help="file to write (default: print)")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("backup", help="copy the vault file (stays encrypted)")
    p.add_argument("dest", help="file or folder to copy to")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("restore", help="restore a JSON export into a new vault")
    p.add_argument("file")
    p.add_argument("--encrypt", action="store_true")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("import", help="import notes, contacts, a calendar or a CSV")
    p.add_argument("kind", choices=["markdown", "vcard", "ical", "csv"])
    p.add_argument("source", help="folder (markdown) or file")
    p.add_argument("--apply", action="store_true", help="save (default is a preview)")
    p.add_argument("--contacts", action="store_true", help="track imported people as contacts")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("mcp", help="run a local MCP server on stdin/stdout for agents")
    p.add_argument("--agent", default="mcp", help="name recorded in the audit log")
    p.set_defaults(func=cmd_mcp)

    p = sub.add_parser("research", help="fetch public records (Crossref, ROR, GLEIF, Wikidata)")
    p.add_argument("source", choices=["crossref", "ror", "gleif", "wikidata", "openalex"])
    p.add_argument("query", nargs="?", default="", help="search text")
    p.add_argument("--id", help="DOI or ORCID (crossref), ROR id, LEI (gleif), Q-id (wikidata)")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--children", type=int, default=0, help="gleif: also import subsidiaries")
    p.add_argument("--no-affiliations", action="store_true",
                   help="crossref: don't match affiliations to institutions")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--allow-network", action="store_true", help="turn on network access for this vault")
    p.set_defaults(func=cmd_research)

    p = sub.add_parser("resolve", help="score likely duplicates; optionally merge the surest")
    p.add_argument("--auto", type=float, help="merge pairs scoring at least this (e.g. 0.95)")
    p.add_argument("--show", type=int, default=30)
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("link", help="link entries to the same people in another vault")
    p.add_argument("other", help="path to the other vault")
    p.add_argument("--yes", action="store_true", help="accept every suggested link")
    p.set_defaults(func=cmd_link)

    for name in ("serve", "demo", "export", "backup", "init", "restore", "import", "mcp",
                 "research", "resolve", "link"):
        sub.choices[name].add_argument("--vault", default=argparse.SUPPRESS,
                                       help="vault file")
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
