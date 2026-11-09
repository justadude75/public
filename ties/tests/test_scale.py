from datetime import timedelta

from fastapi.testclient import TestClient

from ties import bench, graph, inference, repo, scoring, textindex, warm
from ties.db import Vault
from ties.web import create_app


def test_version_and_cache_invalidation(vault):
    calls = []

    def compute():
        calls.append(1)
        return len(calls)

    assert vault.cached(("k",), compute) == 1
    assert vault.cached(("k",), compute) == 1          # cached
    repo.create_entity(vault, "person", "Ana")         # any write invalidates
    assert vault.cached(("k",), compute) == 2


def test_writes_from_another_connection_invalidate(tmp_path):
    a = Vault(tmp_path / "v.vault")
    repo.init_vault(a, "Me")
    b = Vault(tmp_path / "v.vault")                    # e.g. the MCP server process
    before = a.version
    repo.create_entity(b, "person", "From elsewhere")
    assert a.version != before


def test_startup_does_not_invalidate_caches(vault):
    repo.prepare(vault)
    v = vault.version
    repo.prepare(vault)                                # unchanged registry: no writes
    assert vault.version == v


def test_inference_caps_new_suggestions(vault, monkeypatch, today):
    monkeypatch.setattr(inference, "MAX_NEW_PER_RULE", 3)
    org = repo.create_entity(vault, "organization", "Tiny")
    repo.add_claim(vault, org, "size", value="10")
    people = [repo.create_entity(vault, "person", f"P{i}", engagement="acquaintance")
              for i in range(6)]
    for p in people:
        repo.add_claim(vault, p, "employed_by", object_ref=org, valid_from="2020")
    added = inference.run(vault)
    assert added["overlapping_tenure"] == 3
    assert repo.proposal_counts(vault)["rule"] == 3
    assert inference.run(vault)["overlapping_tenure"] == 3    # next batch on the next run
    n = repo.reject_group(vault, "rule", "overlapping_tenure")
    assert n == 6 and repo.proposal_counts(vault)["rule"] == 0


def test_references_do_not_trigger_rules(vault):
    org = repo.create_entity(vault, "organization", "Tiny")
    repo.add_claim(vault, org, "size", value="10")
    for i in range(3):
        p = repo.create_entity(vault, "person", f"Author {i}", engagement="reference")
        repo.add_claim(vault, p, "employed_by", object_ref=org, valid_from="2020")
    assert inference.run(vault)["overlapping_tenure"] == 0


def test_index_rebuild_keeps_search_working(vault, today):
    p = repo.create_entity(vault, "person", "Sarah Chen", engagement="contact")
    repo.add_claim(vault, p, "interest", value="sailing")
    repo.create_event(vault, kind="meeting", status="occurred", start_at=today.isoformat(),
                      participants=[p], notes="We talked about grid storage.")
    textindex.rebuild(vault)
    assert textindex.search(vault, "sailing")["entities"][0]["id"] == p
    assert textindex.search(vault, "grid")["notes"]
    repo.update_entity(vault, p, "Sarah Lin", "contact", "", "")      # reindex one entry
    assert textindex.search(vault, "lin")["entities"][0]["id"] == p
    assert vault.scalar("select count(*) from entity_fts where entity_id=?", (p,)) == 1


def test_synthetic_vault_and_warm_pages(tmp_path, today):
    v = Vault(tmp_path / "bench.vault")
    info = bench.build(v, people=300, today=today)
    assert info["contacts"] > 0 and info["events"] > 0
    assert warm.warm(v, today) is True
    assert warm.warm(v, today) is False                # nothing changed since
    statuses = scoring.compute_all(v, today)
    assert statuses
    a = graph.analytics(v, today)
    assert a["people"] > 0
    c = TestClient(create_app(v, today_fn=lambda: today))
    for url in ("/", "/people", "/people?page=2", "/timeline", "/review", "/insights", "/weekly",
                "/api/graph"):
        assert c.get(url).status_code == 200, url
    data = c.get("/api/graph").json()
    assert data["total"] >= len(data["nodes"])


def test_sampled_betweenness_finds_the_bridge():
    # two dense groups joined through one person
    adj = {}

    def link(a, b):
        adj.setdefault(a, {})[b] = 1
        adj.setdefault(b, {})[a] = 1

    left = [f"L{i}" for i in range(400)]
    right = [f"R{i}" for i in range(400)]
    for group in (left, right):
        for i, a in enumerate(group):
            link(a, group[(i + 1) % len(group)])
            link(a, group[(i + 7) % len(group)])
    link(left[0], "bridge")
    link("bridge", right[0])
    nodes = set(adj)
    bc = graph.betweenness(adj, nodes)                 # sampled: more than 600 nodes
    assert max(bc, key=bc.get) in ("bridge", "L0", "R0")
