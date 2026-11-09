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
    url = f"http://127.0.0.1:{args.port}"
    print(f"ties is running at {url}  (Ctrl+C to stop)")
    if args.open:
        webbrowser.open(url)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


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

    for name in ("serve", "demo", "export", "backup", "init"):
        sub.choices[name].add_argument("--vault", default=argparse.SUPPRESS,
                                       help="vault file")
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
