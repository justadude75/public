import io
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from ties import gateway, graph, repo, scoring, strategy
from ties.mcp_server import Server, serve_stdio
from ties.web import create_app


def _contact(vault, name, tier="active"):
    pid = repo.create_entity(vault, "person", name, engagement="contact")
    repo.update_profile(vault, pid, tier, None, None, None, None)
    return pid


@pytest.fixture
def network(vault, today):
    org = repo.create_entity(vault, "organization", "Voltgrid")
    repo.add_claim(vault, org, "industry", value="grid storage")
    repo.add_claim(vault, org, "size", value="14")
    people = {}
    for name, tier in (("Sarah Chen", "close"), ("Raj Mehta", "active"), ("Dev Patel", "active"),
                       ("Mia Lopez", "inner"), ("Tom Reyes", "close")):
        people[name] = _contact(vault, name, tier)
    s, r, d, m, t = (people[n] for n in ("Sarah Chen", "Raj Mehta", "Dev Patel", "Mia Lopez", "Tom Reyes"))
    repo.add_claim(vault, s, "employed_by", object_ref=org)
    repo.add_claim(vault, s, "title", value="Head of Partnerships")
    repo.add_claim(vault, s, "offering", value="payments partnerships advice")
    repo.add_claim(vault, t, "seeking", value="advice on payments partnerships")
    repo.add_claim(vault, r, "seeking", value="climate founders raising")
    repo.add_claim(vault, s, "family_detail", value="Daughter at university")
    repo.add_identifier(vault, s, "email", "sarah@example.com")
    repo.add_claim(vault, m, "friend_of", object_ref=s)
    repo.add_claim(vault, s, "knows", object_ref=r)
    repo.add_claim(vault, r, "colleague_of", object_ref=d)
    repo.add_claim(vault, t, "colleague_of", object_ref=m)
    for p in people.values():
        repo.create_event(vault, kind="meal", status="occurred",
                          start_at=(today - timedelta(days=3)).isoformat(), participants=[p])
    return people


def test_goal_raises_priority(vault, today, network):
    statuses = scoring.compute_all(vault, today)
    before = statuses[network["Dev Patel"]].priority
    gid = strategy.create_goal(vault, "Move into climate investing", keywords="climate, grid storage")
    strategy.link_goal(vault, gid, network["Dev Patel"], 1.0, "target")
    after = scoring.compute_all(vault, today)[network["Dev Patel"]]
    assert after.priority > before and "Linked to an active goal" in after.reasons
    detail = strategy.goal_detail(vault, gid, today)
    assert any(s["display_name"] in ("Voltgrid", "Sarah Chen") for s in detail["suggestions"])
    strategy.update_goal(vault, gid, status="done")
    assert scoring.compute_all(vault, today)[network["Dev Patel"]].goal_relevance == 0


def test_intro_suggestions(vault, network):
    intros = strategy.intro_suggestions(vault)
    pairs = {(i["seeker"]["name"], i["offerer"]["name"]) for i in intros}
    assert ("Tom Reyes", "Sarah Chen") in pairs
    top = [i for i in intros if i["seeker"]["name"] == "Tom Reyes"][0]
    assert "partnership" in top["overlap"] and not top["already_connected"]


def test_analytics_finds_bridges(vault, today, network):
    a = graph.analytics(vault, today)
    names = [b["name"] for b in a["brokers"]]
    assert names and names[0] in ("Sarah Chen", "Mia Lopez", "Raj Mehta")
    assert a["people"] == 5


def test_weekly_review(vault, today, network):
    r = strategy.weekly_review(vault, today)
    assert len(r["events"]) == 5 and len(r["people"]) == 5
    assert r["new_facts"]


# ── gateway ─────────────────────────────────────────────────────────────────

def test_gateway_payload_has_no_identifiers(vault, today, network):
    payload, alias_map = gateway.build_payload(vault, "introductions", today=today)
    blob = json.dumps(payload).lower()
    for secret in ("sarah", "chen", "voltgrid", "example.com", "daughter", "raj", "mehta"):
        assert secret not in blob, secret
    sarah_alias = next(a for a, e in alias_map.items() if e == network["Sarah Chen"])
    sarah = next(p for p in payload["people"] if p["id"] == sarah_alias)
    assert sarah["seniority"] == "senior leader"
    assert "payments partnerships advice" in sarah["offering"]
    assert any(t["a"] == "me" for t in payload["ties"])


def test_gateway_scrubs_and_blocks_names_in_free_text(vault, today, network):
    repo.add_claim(vault, network["Raj Mehta"], "seeking", value="an intro to Sarah Chen's boss")
    payload, _ = gateway.build_payload(vault, "introductions", today=today)
    assert "sarah" not in json.dumps(payload).lower()
    assert gateway.leak_check({"x": "please call Raj"}, gateway._names_index(vault))
    with pytest.raises(gateway.GatewayError):
        gateway.send(vault, {"task": "t", "people": [{"note": "Mehta"}]}, {}, client=object())


def test_gateway_send_and_reidentify(vault, today, network):
    payload, alias_map = gateway.build_payload(vault, "introductions", today=today)
    tom = next(a for a, e in alias_map.items() if e == network["Tom Reyes"])
    sarah = next(a for a, e in alias_map.items() if e == network["Sarah Chen"])

    class Fake:
        base_url = "fake"

        def chat_json(self, system, user):
            assert "Sarah" not in user
            return {"suggestions": [{"people": [tom, sarah], "action": f"Introduce {tom} to {sarah}",
                                     "why": f"{tom} needs what {sarah} offers"}]}

    out = gateway.send(vault, payload, alias_map, client=Fake())
    s = out["suggestions"][0]
    assert s["action"] == "Introduce Tom Reyes to Sarah Chen"
    assert [p["name"] for p in s["people"]] == ["Tom Reyes", "Sarah Chen"]
    log = gateway.egress_log(vault)
    assert log and log[0]["action"] == "egress" and log[0]["detail"]["people"] == 5


def test_outside_model_off_by_default(vault):
    with pytest.raises(gateway.GatewayError):
        gateway.outside_client(vault)


# ── MCP server ──────────────────────────────────────────────────────────────

def _rpc(server, method, params=None, mid=1):
    return server.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})


def test_mcp_protocol_and_tools(vault, today, network):
    server = Server(vault, agent="test")
    init = _rpc(server, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "t", "version": "1"}})
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    names = {t["name"] for t in _rpc(server, "tools/list")["result"]["tools"]}
    assert {"search", "get_profile", "propose_fact", "propose_follow_up"} <= names
    res = _rpc(server, "tools/call", {"name": "search", "arguments": {"query": "sarah"}})
    data = json.loads(res["result"]["content"][0]["text"])
    sid = next(e["id"] for e in data["entities"] if e["type_id"] == "person")
    prof = json.loads(_rpc(server, "tools/call", {"name": "get_profile",
                                                  "arguments": {"id": sid}})["result"]["content"][0]["text"])
    assert prof["entity"]["display_name"] == "Sarah Chen"
    res = _rpc(server, "tools/call", {"name": "propose_fact", "arguments": {
        "subject_id": sid, "predicate": "interest", "value": "sailing", "why": "her blog"}})
    assert "waiting in Review" in res["result"]["content"][0]["text"]
    assert repo.proposed_claims(vault)[0]["value_text"] == "sailing"
    assert not any(f["value_text"] == "sailing" for f in repo.facts(vault, sid))
    _rpc(server, "tools/call", {"name": "propose_follow_up",
                                "arguments": {"person_id": sid, "text": "Send article"}})
    assert repo.commitments(vault, status="proposed")[0]["description"] == "Send article"
    bad = _rpc(server, "tools/call", {"name": "get_profile", "arguments": {"id": "nope"}})
    assert bad["result"]["isError"]
    assert _rpc(server, "nope")["error"]["code"] == -32601
    assert vault.scalar("select count(*) from audit_log where actor='agent:test'") >= 4


def test_mcp_stdio_loop(vault):
    stdin = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\nnot json\n")
    stdout = io.StringIO()
    serve_stdio(vault, stdin=stdin, stdout=stdout)
    lines = [json.loads(l) for l in stdout.getvalue().splitlines()]
    assert lines[0]["result"] == {} and lines[1]["error"]["code"] == -32700


def test_phase3_pages(vault, today, network):
    c = TestClient(create_app(vault, today_fn=lambda: today))
    r = c.post("/goals", data={"title": "Climate move", "keywords": "climate"}, follow_redirects=False)
    gid = r.headers["location"].split("/goals/")[1]
    c.post(f"/goals/{gid}", data={"action": "link", "entity": network["Dev Patel"], "role": "target"})
    page = c.get(f"/goals/{gid}").text
    assert "Dev Patel" in page and "target" in page
    ins = c.get("/insights").text
    assert "Introductions worth making" in ins and "Tom Reyes" in ins
    assert "Weekly review" in c.get("/weekly").text
    strat = c.get("/strategy?task=introductions").text
    assert "Exactly what would be sent" in strat and "Sarah" not in strat.split("Exactly what")[1].split("Recent")[0]
    key = f"{network['Tom Reyes']}:{network['Sarah Chen']}"
    c.post(f"/intro/{key}/plan", data={"need": "advice"})
    assert any("Introduce Tom Reyes to Sarah Chen" in l["description"] for l in repo.commitments(vault))
