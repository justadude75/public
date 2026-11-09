"""The network as a graph: data for the explorer, warm paths, and network analytics.

Pure Python (no graph library); sized for a personal network of a few thousand nodes.
"""

from __future__ import annotations

import heapq
import math
import random
from collections import defaultdict, deque
from datetime import date

from . import scoring
from .db import Vault

# How strong a tie each relationship type implies, for path finding (0..1).
EDGE_STRENGTH = {
    "partner_of": 0.9, "family_of": 0.8, "parent_of": 0.8, "child_of": 0.8,
    "friend_of": 0.7, "mentor_of": 0.6, "mentee_of": 0.6, "introduced_by": 0.6,
    "colleague_of": 0.5, "knows": 0.5, "shares_connector": 0.2, "member_of": 0.3,
    "employed_by": 0.3, "studied_at": 0.2, "invested_in": 0.4, "related_to": 0.3,
    "together": 0.4,
}
NODE_TYPES = ("person", "organization", "place")


def _load(vault: Vault, include_inferred: bool, include_together: bool, today: date):
    """Nodes and edges of the whole network. Cached until the vault changes."""
    return vault.cached(("graph_load", include_inferred, include_together, today.isoformat()),
                        lambda: _load_uncached(vault, include_inferred, include_together, today))


def _load_uncached(vault: Vault, include_inferred: bool, include_together: bool, today: date):
    ego = vault.get_meta("ego_id")
    nodes = {r["id"]: r for r in vault.all(
        f"select id, display_name, type_id, engagement from entity where status='active' and "
        f"type_id in ({','.join('?' * len(NODE_TYPES))})", NODE_TYPES)}
    statuses = scoring.compute_all(vault, today)
    edges = []
    status_sql = "('accepted','proposed')" if include_inferred else "('accepted')"
    for r in vault.all(
            f"""select cl.id, cl.subject_id, cl.object_id, cl.predicate_id, cl.status, cl.basis,
                       cl.confidence, cl.valid_to, p.label
                from claim cl join predicate p on p.id=cl.predicate_id
                where cl.object_id is not null and cl.status in {status_sql}"""):
        if r["subject_id"] not in nodes or r["object_id"] not in nodes:
            continue
        if not include_inferred and r["basis"] == "inferred":
            continue
        inferred = r["status"] == "proposed" or r["basis"] == "inferred"
        w = EDGE_STRENGTH.get(r["predicate_id"], 0.3) * (r["confidence"] or 1)
        if r["valid_to"]:
            w *= 0.6
        if inferred:
            w *= 0.5
        edges.append({"id": r["id"], "source": r["subject_id"], "target": r["object_id"],
                      "label": r["label"], "kind": r["predicate_id"], "weight": w,
                      "inferred": inferred})
    for eid, st in statuses.items():
        if eid in nodes and ego in nodes:
            edges.append({"id": f"ego-{eid}", "source": ego, "target": eid, "label": "your contact",
                          "kind": "ego", "weight": max(st.strength, 0.05), "inferred": False})
    if include_together:
        pairs: dict[tuple, int] = defaultdict(int)
        for ev in vault.all("select ed.entity_id from event_detail ed join entity e on "
                            "e.id=ed.entity_id where e.status='active' and ed.status='occurred'"):
            ps = [r["entity_id"] for r in vault.all(
                "select entity_id from participation where event_id=?", (ev["entity_id"],))]
            others = sorted(p for p in ps if p != ego and p in nodes)
            if len(ps) <= 8:
                for i, a in enumerate(others):
                    for b in others[i + 1:]:
                        pairs[(a, b)] += 1
        for (a, b), n in pairs.items():
            edges.append({"id": f"tog-{a}-{b}", "source": a, "target": b,
                          "label": f"together ×{n}", "kind": "together",
                          "weight": min(0.3 + 0.1 * n, 0.7), "inferred": False})
    return ego, nodes, edges, statuses


def _adjacency(edges: list[dict]) -> dict[str, list[tuple[str, dict]]]:
    adj: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for e in edges:
        adj[e["source"]].append((e["target"], e))
        adj[e["target"]].append((e["source"], e))
    return adj


def build(vault: Vault, center: str | None = None, depth: int = 2, include_inferred: bool = True,
          include_together: bool = True, include_ego: bool = False, types: tuple = NODE_TYPES,
          today: date | None = None, limit: int = 1500) -> dict:
    today = today or date.today()
    ego, nodes, edges, statuses = _load(vault, include_inferred, include_together, today)
    nodes = {k: v for k, v in nodes.items() if v["type_id"] in types or k == center}
    if not include_ego and center != ego:
        edges = [e for e in edges if e["kind"] != "ego"]
        nodes.pop(ego, None)
    edges = [e for e in edges if e["source"] in nodes and e["target"] in nodes]
    keep = set(nodes)
    if center and center in nodes:
        adj = _adjacency(edges)
        keep, frontier = {center}, deque([(center, 0)])
        while frontier:
            n, d = frontier.popleft()
            if d >= depth:
                continue
            for m, _ in adj[n]:
                if m not in keep:
                    keep.add(m)
                    frontier.append((m, d + 1))
    else:
        connected = {e["source"] for e in edges} | {e["target"] for e in edges}
        keep = {n for n in keep if n in connected or nodes[n]["engagement"] == "contact"}
    total = len(keep)
    if len(keep) > limit:
        # Keep the best-connected nodes (and the center), not an arbitrary subset.
        degree: dict[str, int] = defaultdict(int)
        for e in edges:
            if e["source"] in keep and e["target"] in keep:
                degree[e["source"]] += 1
                degree[e["target"]] += 1
        ranked = sorted(keep, key=lambda n: (n != center, -degree.get(n, 0)))
        keep = set(ranked[:limit])
    out_nodes = []
    for nid in keep:
        n = nodes[nid]
        st = statuses.get(nid)
        out_nodes.append({"id": nid, "label": n["display_name"], "type": n["type_id"],
                          "engagement": n["engagement"] or "", "tier": st.tier if st else "",
                          "strength": round(st.strength, 2) if st else 0,
                          "due": bool(st and st.is_due), "center": nid == center})
    out_edges = [e for e in edges if e["source"] in keep and e["target"] in keep]
    return {"nodes": out_nodes, "edges": out_edges, "total": total}


# ── warm paths ───────────────────────────────────────────────────────────────


def _dijkstra(adj, start: str, goal: str, banned_nodes: set[str],
              allowed: set[str] | None = None) -> list[tuple[str, dict | None]] | None:
    dist = {start: 0.0}
    prev: dict[str, tuple[str, dict]] = {}
    heap = [(0.0, start)]
    while heap:
        d, n = heapq.heappop(heap)
        if n == goal:
            break
        if d > dist.get(n, math.inf):
            continue
        for m, e in adj[n]:
            if m in banned_nodes or (allowed is not None and m not in allowed and m != goal):
                continue
            nd = d - math.log(max(e["weight"], 0.01))
            if nd < dist.get(m, math.inf):
                dist[m], prev[m] = nd, (n, e)
                heapq.heappush(heap, (nd, m))
    if goal not in dist:
        return None
    path, n = [], goal
    while n != start:
        p, e = prev[n]
        path.append((n, e))
        n = p
    path.append((start, None))
    return list(reversed(path))


def warm_paths(vault: Vault, target: str, k: int = 3, today: date | None = None) -> list[dict]:
    """Strongest introduction chains from you to `target`, each through a different first contact.

    Edge cost is -log(strength), so the path with the highest product of strengths wins.
    Only people are used as intermediaries; organizations and places are not someone to ask.
    """
    today = today or date.today()
    ego, nodes, edges, _ = _load(vault, include_inferred=True, include_together=True, today=today)
    adj = vault.cached(("adjacency", today.isoformat()), lambda: _adjacency(edges))
    people = {n for n, v in nodes.items() if v["type_id"] == "person"}
    results, banned = [], set()
    for _ in range(k):
        path = _dijkstra(adj, ego, target, banned, allowed=people)
        if not path or len(path) < 2:
            break
        hops = []
        strength = 1.0
        for nid, e in path[1:]:
            strength *= e["weight"]
            hops.append({"id": nid, "name": nodes[nid]["display_name"], "via": e["label"],
                         "inferred": e["inferred"], "weight": round(e["weight"], 2)})
        results.append({"hops": hops, "strength": round(strength, 3),
                        "ask": hops[0]["name"] if len(hops) > 1 else None})
        if len(path) <= 2:
            break
        banned.add(path[1][0])
    return results


# ── analytics ────────────────────────────────────────────────────────────────


def _people_graph(vault: Vault, today: date):
    """People plus the organizations they share, as one undirected graph.

    Organizations stay as their own nodes (person - org - person) rather than linking
    every pair of colleagues, which keeps the graph small for large employers.
    """
    ego, nodes, edges, statuses = _load(vault, include_inferred=False, include_together=True,
                                        today=today)
    people = {n for n, v in nodes.items() if v["type_id"] == "person" and n != ego}
    adj: dict[str, dict[str, float]] = defaultdict(dict)

    def link(a, b, w):
        adj[a][b] = max(adj[a].get(b, 0), w)
        adj[b][a] = max(adj[b].get(a, 0), w)

    for e in edges:
        if e["kind"] == "ego":
            continue
        s, t = e["source"], e["target"]
        if s in people and t in people:
            link(s, t, e["weight"])
        elif e["kind"] in ("employed_by", "member_of", "studied_at", "affiliated_with"):
            p, o = (s, t) if s in people else (t, s)
            if p in people and o in nodes:
                link(p, o, 0.2)
    return ego, nodes, people, adj, statuses


EXACT_LIMIT = 600      # above this many nodes, betweenness is estimated from a sample
SAMPLE_SOURCES = 120


def betweenness(adj: dict[str, dict[str, float]], nodes: set[str],
                samples: int | None = None, seed: int = 11) -> dict[str, float]:
    """Brandes' algorithm on the unweighted graph (who sits on many shortest paths).

    Exact for small networks. For large ones it runs from a random sample of source
    nodes and scales up (Brandes & Pich 2007), which keeps the ranking of the top
    bridges stable at a fraction of the cost.
    """
    bc = dict.fromkeys(nodes, 0.0)
    sources = sorted(nodes)
    if samples is None:
        if len(nodes) <= EXACT_LIMIT:
            samples = len(nodes)
        else:
            # keep total work near a few million edge visits, whatever the size
            n_edges = sum(len(v) for v in adj.values()) or 1
            samples = max(24, min(SAMPLE_SOURCES, 3_000_000 // n_edges))
    if samples < len(sources):
        sources = random.Random(seed).sample(sources, samples)
    for s in sources:
        stack, preds = [], defaultdict(list)
        sigma = dict.fromkeys(nodes, 0.0)
        sigma[s] = 1.0
        dist = dict.fromkeys(nodes, -1)
        dist[s] = 0
        q = deque([s])
        while q:
            v = q.popleft()
            stack.append(v)
            for w in adj.get(v, {}):
                if w not in dist:
                    continue
                if dist[w] < 0:
                    dist[w] = dist[v] + 1
                    q.append(w)
                if dist[w] == dist[v] + 1:
                    sigma[w] += sigma[v]
                    preds[w].append(v)
        delta = dict.fromkeys(nodes, 0.0)
        while stack:
            w = stack.pop()
            for v in preds[w]:
                delta[v] += sigma[v] / sigma[w] * (1 + delta[w])
            if w != s:
                bc[w] += delta[w]
    n = len(nodes)
    scale = 1 / ((n - 1) * (n - 2)) if n > 2 else 1
    scale *= len(nodes) / max(len(sources), 1)
    return {k: v * scale for k, v in bc.items()}


def communities(adj: dict[str, dict[str, float]], nodes: set[str], seed: int = 7) -> dict[str, int]:
    """Weighted label propagation. Returns node -> community number."""
    rnd = random.Random(seed)
    label = {n: i for i, n in enumerate(sorted(nodes))}
    order = sorted(nodes)
    for _ in range(20):
        changed = False
        rnd.shuffle(order)
        for n in order:
            if not adj.get(n):
                continue
            score: dict[int, float] = defaultdict(float)
            for m, w in adj[n].items():
                score[label[m]] += w
            best = max(score.items(), key=lambda x: (x[1], -x[0]))[0]
            if best != label[n]:
                label[n], changed = best, True
        if not changed:
            break
    remap, out = {}, {}
    for n in sorted(nodes, key=lambda x: label[x]):
        out[n] = remap.setdefault(label[n], len(remap))
    return out


def analytics(vault: Vault, today: date | None = None) -> dict:
    """Bridges and clusters. Cached until the vault changes."""
    today = today or date.today()
    return vault.cached(("analytics", today.isoformat()), lambda: _analytics(vault, today))


def _analytics(vault: Vault, today: date) -> dict:
    ego, nodes, people, adj, statuses = _people_graph(vault, today)
    graph_nodes = set(adj) | people
    bc = betweenness(adj, graph_nodes)
    comm = communities(adj, graph_nodes)
    groups: dict[int, list[str]] = defaultdict(list)
    for n, c in comm.items():
        if n in people:
            groups[c].append(n)
    named_groups = []
    for c, members in groups.items():
        if len(members) < 2:
            continue
        members.sort(key=lambda m: -bc.get(m, 0))
        named_groups.append({"id": c, "size": len(members),
                             "members": [{"id": m, "name": nodes[m]["display_name"]} for m in members[:12]]})
    named_groups.sort(key=lambda g: -g["size"])
    brokers = []
    for n in sorted(people, key=lambda x: -bc.get(x, 0))[:15]:
        if bc.get(n, 0) <= 0:
            break
        touching = {comm[m] for m in adj.get(n, {}) if m in comm} - {comm[n]}
        st = statuses.get(n)
        brokers.append({"id": n, "name": nodes[n]["display_name"], "betweenness": round(bc[n], 3),
                        "bridges": len(touching) + 1, "strength": round(st.strength, 2) if st else None,
                        "dormant": bool(st and st.ratio and st.ratio > 2)})
    ties = sum(1 for a in people for b in adj.get(a, {}) if b in people) // 2
    return {"brokers": brokers, "groups": named_groups, "people": len(people), "ties": ties,
            "estimated": len(graph_nodes) > EXACT_LIMIT}
