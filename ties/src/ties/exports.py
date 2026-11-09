"""Graph exports: GraphML and GEXF (Gephi), CSV, and Follow the Money (FtM) JSON lines."""

from __future__ import annotations

import csv
import io
import json
import zipfile
from xml.sax.saxutils import escape

from .db import Vault

FTM_SCHEMA = {"person": "Person", "organization": "Organization", "place": "Address",
              "document": "Document", "event": "Event", "topic": "Thing"}
# predicate -> (FtM interval schema, source property, target property)
FTM_LINKS = {
    "employed_by": ("Employment", "employee", "employer"),
    "member_of": ("Membership", "member", "organization"),
    "director_of": ("Directorship", "director", "organization"),
    "owns": ("Ownership", "owner", "asset"),
    "family_of": ("Family", "person", "relative"),
    "parent_of": ("Family", "person", "relative"),
    "child_of": ("Family", "person", "relative"),
    "partner_of": ("Family", "person", "relative"),
    "authored": ("Documentation", "entity", "document"),
}


def _data(vault: Vault, entity_ids: set[str] | None = None, valid_at: str | None = None):
    from .research import _overlap

    nodes = vault.all("select id, type_id, display_name, engagement from entity where status='active'")
    if entity_ids is not None:
        nodes = [n for n in nodes if n["id"] in entity_ids]
    ids = {n["id"] for n in nodes}
    edges = []
    for r in vault.all(
            """select cl.id, cl.subject_id, cl.object_id, cl.predicate_id, p.label, cl.valid_from,
                      cl.valid_to, cl.confidence, cl.basis
               from claim cl join predicate p on p.id=cl.predicate_id
               where cl.object_id is not null and cl.status='accepted'"""):
        if r["subject_id"] in ids and r["object_id"] in ids:
            if valid_at and not _overlap(r["valid_from"], r["valid_to"], valid_at, valid_at):
                continue
            edges.append(r)
    return nodes, edges


def graphml(vault: Vault, entity_ids=None, valid_at=None) -> str:
    nodes, edges = _data(vault, entity_ids, valid_at)
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">',
           '<key id="label" for="node" attr.name="label" attr.type="string"/>',
           '<key id="type" for="node" attr.name="type" attr.type="string"/>',
           '<key id="rel" for="edge" attr.name="relation" attr.type="string"/>',
           '<key id="from" for="edge" attr.name="valid_from" attr.type="string"/>',
           '<key id="to" for="edge" attr.name="valid_to" attr.type="string"/>',
           '<key id="conf" for="edge" attr.name="confidence" attr.type="double"/>',
           '<graph id="ties" edgedefault="directed">']
    for n in nodes:
        out.append(f'<node id="{n["id"]}"><data key="label">{escape(n["display_name"])}</data>'
                   f'<data key="type">{n["type_id"]}</data></node>')
    for e in edges:
        out.append(f'<edge id="{e["id"]}" source="{e["subject_id"]}" target="{e["object_id"]}">'
                   f'<data key="rel">{escape(e["predicate_id"])}</data>'
                   f'<data key="from">{escape(e["valid_from"] or "")}</data>'
                   f'<data key="to">{escape(e["valid_to"] or "")}</data>'
                   f'<data key="conf">{e["confidence"]}</data></edge>')
    out += ["</graph>", "</graphml>"]
    return "\n".join(out)


def gexf(vault: Vault, entity_ids=None, valid_at=None) -> str:
    nodes, edges = _data(vault, entity_ids, valid_at)
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<gexf xmlns="http://gexf.net/1.3" version="1.3">',
           '<graph defaultedgetype="directed" mode="static">',
           '<attributes class="node"><attribute id="0" title="type" type="string"/></attributes>',
           '<attributes class="edge"><attribute id="0" title="relation" type="string"/></attributes>',
           "<nodes>"]
    for n in nodes:
        out.append(f'<node id="{n["id"]}" label="{escape(n["display_name"], {chr(34): "&quot;"})}">'
                   f'<attvalues><attvalue for="0" value="{n["type_id"]}"/></attvalues></node>')
    out.append("</nodes><edges>")
    for e in edges:
        out.append(f'<edge id="{e["id"]}" source="{e["subject_id"]}" target="{e["object_id"]}" '
                   f'label="{escape(e["label"], {chr(34): "&quot;"})}" weight="{e["confidence"]}">'
                   f'<attvalues><attvalue for="0" value="{e["predicate_id"]}"/></attvalues></edge>')
    out += ["</edges>", "</graph>", "</gexf>"]
    return "\n".join(out)


def csv_zip(vault: Vault, entity_ids=None, valid_at=None) -> bytes:
    nodes, edges = _data(vault, entity_ids, valid_at)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        s = io.StringIO()
        w = csv.writer(s)
        w.writerow(["id", "label", "type"])
        for n in nodes:
            w.writerow([n["id"], n["display_name"], n["type_id"]])
        z.writestr("nodes.csv", s.getvalue())
        s = io.StringIO()
        w = csv.writer(s)
        w.writerow(["source", "target", "relation", "valid_from", "valid_to", "confidence", "basis"])
        for e in edges:
            w.writerow([e["subject_id"], e["object_id"], e["predicate_id"], e["valid_from"] or "",
                        e["valid_to"] or "", e["confidence"], e["basis"]])
        z.writestr("edges.csv", s.getvalue())
    return buf.getvalue()


def ftm(vault: Vault, entity_ids=None, valid_at=None) -> str:
    """Follow the Money entities, one JSON object per line."""
    nodes, edges = _data(vault, entity_ids, valid_at)
    lines = []
    for n in nodes:
        props: dict = {"name": [n["display_name"]]}
        aliases = [r["name"] for r in vault.all(
            "select name from entity_name where entity_id=? and kind<>'primary'", (n["id"],))]
        if aliases:
            props["alias"] = aliases
        for i in vault.all("select scheme, value from identifier where entity_id=?", (n["id"],)):
            key = {"email": "email", "phone": "phone", "url": "website", "wikidata": "wikidataId"}.get(
                i["scheme"])
            if key:
                props.setdefault(key, []).append(i["value"])
        lines.append(json.dumps({"id": n["id"], "schema": FTM_SCHEMA.get(n["type_id"], "Thing"),
                                 "properties": props}))
    for e in edges:
        schema, src, dst = FTM_LINKS.get(e["predicate_id"], ("UnknownLink", "subject", "object"))
        props = {src: [e["subject_id"]], dst: [e["object_id"]],
                 "role" if schema != "UnknownLink" else "role": [e["label"]]}
        if e["valid_from"]:
            props["startDate"] = [e["valid_from"]]
        if e["valid_to"]:
            props["endDate"] = [e["valid_to"]]
        lines.append(json.dumps({"id": e["id"], "schema": schema, "properties": props}))
    return "\n".join(lines) + "\n"
