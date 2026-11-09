"""Links between a personal vault and a research vault.

Nothing is copied between vaults. A confirmed link lets the personal profile show
the research vault's facts read-only, if that vault can be opened.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import repo
from .db import Vault, VaultError, is_encrypted
from .repo import normalize_name

_cache: dict[str, Vault] = {}


def open_other(path: str) -> Vault | None:
    p = Path(path).expanduser()
    if not p.exists():
        return None
    key = str(p.resolve())
    if key in _cache:
        return _cache[key]
    passphrase = os.environ.get("TIES_RESEARCH_PASSPHRASE") if is_encrypted(p) else None
    if is_encrypted(p) and not passphrase:
        return None
    try:
        v = Vault(p, passphrase)
    except VaultError:
        return None
    _cache[key] = v
    return v


def candidates(local: Vault, other: Vault) -> list[dict]:
    """Personal entries that look like entries in the other vault (identifier or exact name)."""
    out = []
    linked = {(r["local_entity_id"], r["other_entity_id"]) for r in local.all(
        "select local_entity_id, other_entity_id from cross_vault_link")}
    other_ids: dict[tuple, str] = {}
    for r in other.all("select i.entity_id, i.scheme, i.value from identifier i join entity e on "
                       "e.id=i.entity_id where e.status='active'"):
        other_ids[(r["scheme"], r["value"])] = r["entity_id"]
    other_names: dict[tuple, list[str]] = {}
    for r in other.all("select n.entity_id, n.name_norm, e.type_id from entity_name n join entity e "
                       "on e.id=n.entity_id where e.status='active' and e.type_id in "
                       "('person','organization')"):
        other_names.setdefault((r["type_id"], r["name_norm"]), []).append(r["entity_id"])
    for e in local.all("select id, display_name, type_id from entity where status='active' and "
                       "type_id in ('person','organization') and coalesce(engagement,'')<>'ego'"):
        found: dict[str, str] = {}
        for i in local.all("select scheme, value from identifier where entity_id=?", (e["id"],)):
            oid = other_ids.get((i["scheme"], i["value"]))
            if oid:
                found[oid] = f"same {i['scheme']}"
        matches = other_names.get((e["type_id"], normalize_name(e["display_name"])), [])
        if len(matches) == 1 and matches[0] not in found:
            found[matches[0]] = "same name"
        for oid, why in found.items():
            if (e["id"], oid) in linked:
                continue
            o = other.one("select display_name from entity where id=?", (oid,))
            out.append({"local": e, "other_id": oid, "other_name": o["display_name"], "why": why})
    return out


def confirm(local: Vault, other_path: str, local_id: str, other_id: str, other_name: str) -> None:
    with local.tx() as c:
        c.execute("insert or ignore into cross_vault_link(local_entity_id, other_vault, "
                  "other_entity_id, other_name) values(?,?,?,?)",
                  (local_id, str(Path(other_path).expanduser()), other_id, other_name))
        local.audit(c, "write", local_id, {"op": "cross_vault_link", "other": other_id})


def linked_view(local: Vault, entity_id: str) -> list[dict]:
    out = []
    for link in local.all("select * from cross_vault_link where local_entity_id=?", (entity_id,)):
        other = open_other(link["other_vault"])
        item = {"link": link, "available": other is not None, "facts": [], "relations": []}
        if other:
            ent = repo.get_entity(other, link["other_entity_id"])
            if ent:
                item["facts"] = repo.facts(other, ent["id"])[:12]
                item["relations"] = repo.relations(other, ent["id"])[:20]
        out.append(item)
    return out
