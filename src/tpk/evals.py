"""Offline agent eval (#35): run a fixed question set through the real agent
and measure HOW it answers -- which tools it called, whether it touched the
graph's edges, how many calls it spent -- plus cheap sanity checks on the
answer. It exists to replace eyeballing chat traces: change the prompt, the
tools or the extraction, run it again, `--compare` the two reports.

It is not a correctness benchmark: `expect_answer_any` only catches an answer
that never mentions the thing asked about.
"""

import json
import time
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

# The tools that read graph EDGES (as opposed to keyword search / raw source).
GRAPH_TOOLS = ("neighbors", "path_between")
DEFAULT_MAX_TOOL_CALLS = 20


@dataclass
class Question:
    id: str
    category: str
    question: str
    # pass if ANY of these tools was called (empty = no requirement)
    expect_tools_any: list[str] = field(default_factory=list)
    # pass if the answer mentions ANY of these, case-insensitively (empty = any non-empty answer)
    expect_answer_any: list[str] = field(default_factory=list)
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS


def load_questions(path: Path | None = None) -> list[Question]:
    """The bundled set (tpk/eval_questions.toml), or a custom TOML file with the
    same `[[question]]` tables."""
    if path is None:
        raw = resources.files("tpk").joinpath("eval_questions.toml").read_text()
    else:
        raw = Path(path).read_text()
    return [Question(**q) for q in tomllib.loads(raw)["question"]]


def _text(content) -> str:
    # OpenAI yields str content; Anthropic may yield a list of content blocks.
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content
                       if isinstance(b, dict) and b.get("type") == "text")
    return ""


def extract_run(result: dict) -> dict:
    """Tool-call sequence and final answer from a LangGraph agent result."""
    tools: list[str] = []
    answer = ""
    for msg in result.get("messages", []):
        calls = getattr(msg, "tool_calls", None)
        if calls:
            tools.extend(c["name"] for c in calls)
        elif getattr(msg, "type", "") == "ai":
            answer = _text(msg.content)   # the last AI message without tool calls
    return {"tools": tools, "answer": answer.strip()}


def score(q: Question, run: dict) -> dict:
    tools, answer = run["tools"], run["answer"]
    checks = {
        "tools": not q.expect_tools_any or any(t in tools for t in q.expect_tools_any),
        "answer": bool(answer) and (not q.expect_answer_any or any(
            k.lower() in answer.lower() for k in q.expect_answer_any)),
        "budget": len(tools) <= q.max_tool_calls,
    }
    return {**run, "checks": checks, "passed": all(checks.values()),
            "used_graph_tool": any(t in GRAPH_TOOLS for t in tools)}


async def run_eval(agent, questions: list[Question], recursion_limit: int = 40,
                   on_result=None) -> list[dict]:
    """One fresh conversation per question. An agent/gateway error fails that
    question and is recorded -- it must not abort the run."""
    records = []
    for q in questions:
        started = time.monotonic()
        try:
            result = await agent.ainvoke(
                {"messages": [{"role": "user", "content": q.question}]},
                config={"recursion_limit": recursion_limit},
            )
            rec = {**score(q, extract_run(result)), "error": ""}
        except Exception as exc:  # noqa: BLE001 -- recorded, not swallowed
            rec = {"tools": [], "answer": "", "checks": {}, "passed": False,
                   "used_graph_tool": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
        rec = {"id": q.id, "category": q.category, "question": q.question,
               "latency_s": round(time.monotonic() - started, 1), **rec}
        records.append(rec)
        if on_result:
            on_result(rec)
    return records


def _stats(records: list[dict]) -> dict:
    n = len(records)
    return {
        "questions": n,
        "passed": sum(1 for r in records if r["passed"]),
        "graph_tool_rate": round(sum(1 for r in records if r["used_graph_tool"]) / n, 2) if n else 0.0,
        "avg_tool_calls": round(sum(len(r["tools"]) for r in records) / n, 2) if n else 0.0,
        "errors": sum(1 for r in records if r.get("error")),
    }


def summarize(records: list[dict]) -> dict:
    by_cat: dict[str, list[dict]] = {}
    tool_counts: dict[str, int] = {}
    for r in records:
        by_cat.setdefault(r["category"], []).append(r)
        for t in r["tools"]:
            tool_counts[t] = tool_counts.get(t, 0) + 1
    return {
        "overall": _stats(records),
        "by_category": {c: _stats(rs) for c, rs in sorted(by_cat.items())},
        "tool_counts": dict(sorted(tool_counts.items(), key=lambda kv: -kv[1])),
    }


def compare(old: dict, new: dict) -> dict:
    """(old, new) for the headline metrics, plus which questions flipped."""
    o, n = old["summary"]["overall"], new["summary"]["overall"]
    was = {r["id"]: r["passed"] for r in old["records"]}
    now = {r["id"]: r["passed"] for r in new["records"]}
    shared = sorted(set(was) & set(now))
    return {
        **{k: (o[k], n[k]) for k in ("passed", "graph_tool_rate", "avg_tool_calls", "errors")},
        "fixed": [i for i in shared if now[i] and not was[i]],
        "regressed": [i for i in shared if was[i] and not now[i]],
    }


def write_report(path: Path, records: list[dict], meta: dict) -> dict:
    report = {"meta": meta, "summary": summarize(records), "records": records}
    Path(path).write_text(json.dumps(report, indent=2))
    return report


def render_markdown(report: dict) -> str:
    s = report["summary"]
    lines = ["| category | questions | passed | used a graph tool | avg tool calls | errors |",
             "|---|---|---|---|---|---|"]
    for name, st in [*s["by_category"].items(), ("**overall**", s["overall"])]:
        lines.append(f"| {name} | {st['questions']} | {st['passed']} | "
                     f"{round(st['graph_tool_rate'] * 100)}% | {st['avg_tool_calls']} | {st['errors']} |")
    lines += ["", "tool calls: " + ", ".join(f"{t} {c}" for t, c in s["tool_counts"].items())]
    failed = [r for r in report["records"] if not r["passed"]]
    if failed:
        lines += ["", "failed:"] + [
            f"- {r['id']}: " + (r["error"] or "failed checks: " + ", ".join(
                k for k, ok in r["checks"].items() if not ok)) for r in failed]
    return "\n".join(lines)
