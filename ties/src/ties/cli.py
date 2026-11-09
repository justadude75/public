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
    vault.close()
    print(f"Created {'encrypted ' if passphrase else ''}vault at {path}")
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
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("serve", help="run the app in your browser (localhost only)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--open", action="store_true", help="open the browser")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("demo", help="create a vault filled with sample data to try things out")
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

    for name in ("serve", "demo", "export", "backup", "init", "restore", "import", "mcp"):
        sub.choices[name].add_argument("--vault", default=argparse.SUPPRESS,
                                       help="vault file")
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
