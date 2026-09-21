"""Offline agent eval (#35): fixed questions -> tool-call distribution + checks.
Infra-free: the agent is a fake that returns canned LangGraph messages."""
import asyncio
import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from tpk import evals


def _run(*tools, answer="Final answer about InterpreterInsertQuery."):
    """A LangGraph result: one AI turn per tool call, then the final answer."""
    msgs = [HumanMessage("q")]
    for i, (name, args) in enumerate(tools):
        msgs.append(AIMessage("", tool_calls=[{"name": name, "args": args, "id": f"c{i}"}]))
        msgs.append(ToolMessage("result", tool_call_id=f"c{i}", name=name))
    msgs.append(AIMessage(answer))
    return {"messages": msgs}


class FakeAgent:
    def __init__(self, results):
        self.results, self.seen = results, []

    async def ainvoke(self, payload, config=None):
        self.seen.append(payload["messages"][-1]["content"])
        r = self.results[len(self.seen) - 1]
        if isinstance(r, Exception):
            raise r
        return r


def test_extract_run_reads_tool_sequence_and_final_answer():
    run = evals.extract_run(_run(("search_entities", {"query": "x"}), ("neighbors", {"entity_id": "a"})))
    assert run["tools"] == ["search_entities", "neighbors"]
    assert run["answer"].startswith("Final answer")


def test_extract_run_handles_block_content_and_parallel_tool_calls():
    msgs = [HumanMessage("q"),
            AIMessage([{"type": "text", "text": "thinking"}],
                      tool_calls=[{"name": "search_entities", "args": {}, "id": "1"},
                                  {"name": "get_entity", "args": {}, "id": "2"}]),
            ToolMessage("r", tool_call_id="1"), ToolMessage("r", tool_call_id="2"),
            AIMessage([{"type": "text", "text": "Part one. "}, {"type": "text", "text": "Part two."}])]
    run = evals.extract_run({"messages": msgs})
    assert run["tools"] == ["search_entities", "get_entity"]
    assert run["answer"] == "Part one. Part two."


def test_score_checks_graph_tool_use_keywords_and_budget():
    q = evals.Question(id="t1", category="trace", question="?", expect_tools_any=["neighbors", "path_between"],
                       expect_answer_any=["InterpreterInsertQuery"], max_tool_calls=3)
    good = evals.score(q, evals.extract_run(_run(("search_entities", {}), ("path_between", {}))))
    assert good["passed"] and good["used_graph_tool"] and good["checks"] == {
        "tools": True, "answer": True, "budget": True}
    # keyword search only, wrong answer, over budget
    bad = evals.score(q, evals.extract_run(_run(*[("search_entities", {})] * 4, answer="dunno")))
    assert not bad["passed"] and not bad["used_graph_tool"]
    assert bad["checks"] == {"tools": False, "answer": False, "budget": False}


def test_score_without_expectations_only_needs_an_answer():
    q = evals.Question(id="d1", category="docs", question="?")
    assert evals.score(q, evals.extract_run(_run(("search_entities", {}))))["passed"]
    assert not evals.score(q, evals.extract_run(_run(("search_entities", {}), answer="")))["passed"]


def test_run_eval_records_each_question_and_survives_an_agent_error():
    qs = [evals.Question(id="a", category="trace", question="trace it", expect_tools_any=["path_between"]),
          evals.Question(id="b", category="docs", question="explain it")]
    agent = FakeAgent([_run(("path_between", {})), RuntimeError("gateway 500")])
    records = asyncio.run(evals.run_eval(agent, qs))
    assert agent.seen == ["trace it", "explain it"]
    assert [r["id"] for r in records] == ["a", "b"]
    assert records[0]["passed"] and records[0]["tools"] == ["path_between"]
    assert not records[1]["passed"] and "gateway 500" in records[1]["error"]
    assert all(r["latency_s"] >= 0 for r in records)


def test_summarize_by_category_and_overall():
    recs = [
        {"id": "a", "category": "trace", "tools": ["search_entities", "neighbors"], "used_graph_tool": True,
         "passed": True, "error": ""},
        {"id": "b", "category": "trace", "tools": ["search_entities"] * 3, "used_graph_tool": False,
         "passed": False, "error": ""},
        {"id": "c", "category": "docs", "tools": ["search_entities", "read_source"], "used_graph_tool": False,
         "passed": True, "error": ""},
    ]
    s = evals.summarize(recs)
    assert s["overall"] == {"questions": 3, "passed": 2, "graph_tool_rate": 0.33, "avg_tool_calls": 2.33,
                            "errors": 0}
    assert s["by_category"]["trace"] == {"questions": 2, "passed": 1, "graph_tool_rate": 0.5,
                                         "avg_tool_calls": 2.5, "errors": 0}
    assert s["tool_counts"] == {"search_entities": 5, "neighbors": 1, "read_source": 1}


def test_compare_reports_metric_deltas_and_flips():
    old = {"summary": {"overall": {"questions": 2, "passed": 1, "graph_tool_rate": 0.0, "avg_tool_calls": 9.0, "errors": 0}},
           "records": [{"id": "a", "passed": False}, {"id": "b", "passed": True}]}
    new = {"summary": {"overall": {"questions": 2, "passed": 1, "graph_tool_rate": 0.5, "avg_tool_calls": 6.0, "errors": 0}},
           "records": [{"id": "a", "passed": True}, {"id": "b", "passed": False}]}
    diff = evals.compare(old, new)
    assert diff["graph_tool_rate"] == (0.0, 0.5) and diff["avg_tool_calls"] == (9.0, 6.0)
    assert diff["fixed"] == ["a"] and diff["regressed"] == ["b"]


def test_bundled_question_set_is_well_formed():
    qs = evals.load_questions()
    assert len(qs) >= 10 and len({q.id for q in qs}) == len(qs)
    cats = {q.category for q in qs}
    assert {"trace", "relationship", "architecture", "docs"} <= cats
    # every trace / relationship question must demand a graph tool
    for q in qs:
        if q.category in ("trace", "relationship"):
            assert set(q.expect_tools_any) & set(evals.GRAPH_TOOLS), q.id


def test_report_round_trips_as_json(tmp_path):
    q = evals.Question(id="a", category="trace", question="?")
    rec = evals.score(q, evals.extract_run(_run(("neighbors", {}))))
    out = tmp_path / "r.json"
    evals.write_report(out, [dict(rec, id="a", category="trace", question="?", latency_s=0.1, error="")],
                       meta={"model": "m"})
    data = json.loads(out.read_text())
    assert data["meta"]["model"] == "m" and data["summary"]["overall"]["questions"] == 1


def test_cli_eval_runs_selected_questions_and_compares(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    import tpk.agent as agent_mod
    import tpk.server as server_mod
    from tpk.cli import app

    agent = FakeAgent([_run(("search_entities", {}), ("neighbors", {}), answer="calls buildChainImpl"),
                       _run(("search_entities", {}), answer="it is called by doProcessRequest")])
    monkeypatch.setattr(server_mod, "_build_kg_and_repos", lambda prefix="": (object(), {}))
    monkeypatch.setattr(agent_mod, "build_agent", lambda kg, cfg, repos: agent)
    monkeypatch.setenv("TPK_AGENT_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")

    old = tmp_path / "old.json"
    old.write_text(json.dumps({
        "summary": {"overall": {"questions": 2, "passed": 0, "graph_tool_rate": 0.0,
                                "avg_tool_calls": 9.0, "errors": 0}},
        "records": [{"id": "rel-execute-callees", "passed": False},
                    {"id": "rel-produce-callers", "passed": True}]}))
    out = tmp_path / "new.json"
    result = CliRunner().invoke(app, ["eval", "--only", "relationship", "--limit", "2", "--yes",
                                      "--out", str(out), "--compare", str(old)])
    assert result.exit_code == 0, result.output
    assert agent.seen == ["What does InterpreterInsertQuery::execute call?",
                          "What calls NativeLog::processProduceRequest?"]
    report = json.loads(out.read_text())
    assert [r["passed"] for r in report["records"]] == [True, False]   # 2nd never touched a graph tool
    assert "| relationship | 2 | 1 | 50% |" in result.output
    assert "fixed: ['rel-execute-callees']" in result.output
    assert "regressed: ['rel-produce-callers']" in result.output
