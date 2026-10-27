#!/usr/bin/env python3
"""Start one real Claude Code session and let tracing.py record it.

`python run.py` is the one-shot. It does not query Sentry back. Span checks
live in dev/verify.py, which is not part of the shareable tree.
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "runs"
AGENT_HOME = ROOT / "agent-home"
MCP = ROOT / "mcp.json"
LABEL_LIMIT = 200


def load_env_file():
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, value = s.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'").strip('"'))


def bounded(value, limit=LABEL_LIMIT):
    """One short label. Payload bodies never go into the prompt."""
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text[:limit]


def alert_prompt(project=None, alert_name=None):
    org = bounded(os.environ.get("SENTRY_ORG", ""), 100)
    project = bounded(project or os.environ.get("SENTRY_PROJECT", ""), 100)
    if not org or not project:
        raise SystemExit("SENTRY_ORG and SENTRY_PROJECT are required")
    rule = bounded(alert_name, 200) or "dictation failure rate"
    return (
        "A metric alert just fired.\n"
        f"Alert rule: {rule}\n"
        f"Organization: {org}\n"
        f"Project: {project}\n\n"
        "Did a recent release cause this error?\n\n"
        "Investigate with Sentry. In the final answer, say whether a recent "
        "release caused it, name the issue or issues, and cite the stack frame "
        "or culprit you actually saw."
    )


def claude_env():
    if not os.environ.get("SENTRY_ACCESS_TOKEN"):
        raise SystemExit("SENTRY_ACCESS_TOKEN is required")
    if not os.environ.get("SENTRY_DSN"):
        raise SystemExit("SENTRY_DSN is required")
    AGENT_HOME.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["CLAUDE_CONFIG_DIR"] = str(AGENT_HOME)
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    env["CLAUDE_CODE_ENABLE_TELEMETRY"] = "0"
    env["OTEL_TRACES_EXPORTER"] = "none"
    env["OTEL_LOGS_EXPORTER"] = "none"
    env["OTEL_METRICS_EXPORTER"] = "none"
    for key in (
        "TRACEPARENT",
        "TRACESTATE",
        "OTEL_LOG_USER_PROMPTS",
        "OTEL_LOG_TOOL_DETAILS",
        "OTEL_LOG_TOOL_CONTENT",
    ):
        env.pop(key, None)
    return env


def claude_command(prompt, max_turns):
    return [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        os.environ.get("CLAUDE_MODEL", "").strip() or "sonnet",
        "--max-turns",
        str(max_turns),
        "--mcp-config",
        str(MCP),
        "--strict-mcp-config",
        "--setting-sources",
        "",
        "--disable-slash-commands",
        "--permission-mode",
        "bypassPermissions",
        "--tools",
        "ToolSearch",
        "--allowedTools",
        "ToolSearch,mcp__sentry__*",
        "--no-session-persistence",
    ]


def run_once(max_turns=20, project=None, alert_name=None):
    import sentry_sdk
    from tracing import LiveRun, handle_line, init_sdk

    prompt = alert_prompt(project=project, alert_name=alert_name)
    model = os.environ.get("CLAUDE_MODEL", "").strip() or "sonnet"
    init_sdk()
    live = LiveRun(prompt, model, max_turns)
    print("trace", live.trace_id(), "span", type(live.root).__name__, flush=True)
    scope_trace = sentry_sdk.get_current_scope().get_trace_context().get("trace_id")
    print("scope_trace_matches", scope_trace == live.trace_id(), flush=True)
    env = claude_env()
    proc = subprocess.Popen(
        claude_command(prompt, max_turns),
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stderr_box = []

    def drain():
        stderr_box.append(proc.stderr.read() if proc.stderr else "")

    threading.Thread(target=drain, name="claude-stderr", daemon=True).start()
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            handle_line(live, line)
        code = proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    stderr = stderr_box[0] if stderr_box else ""
    live.finish(code)
    summary = summarize(code, stderr, live)
    RUNS.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = RUNS / f"{stamp}.json"
    path.write_text(json.dumps(summary, indent=2, default=str))
    print_summary(summary, path)
    return summary


def summarize(code, stderr, live):
    result = live.result or {}
    usage = result.get("usage") or {}
    tool_rows = []
    for tool in live.tools.values():
        duration = None
        if tool["end"] and tool["start"]:
            duration = round((tool["end"] - tool["start"]) * 1000, 1)
        tool_rows.append(
            {"name": tool["name"], "approximate_ms": duration, "is_error": tool["is_error"]}
        )
    init = live.init or {}
    return {
        "exit_code": code,
        "stderr_bytes": len(stderr or ""),
        "stderr_tail": (stderr or "")[-400:],
        "session_id": live.session_id,
        "trace_id": live.trace_id(),
        "model": init.get("model") or live.model,
        "apiKeySource": init.get("apiKeySource"),
        "cwd": init.get("cwd"),
        "tools": init.get("tools"),
        "mcp_servers": init.get("mcp_servers"),
        "skills": init.get("skills"),
        "plugins": init.get("plugins"),
        "slash_commands": init.get("slash_commands"),
        "memory_paths": init.get("memory_paths"),
        "max_turns_flag": live.max_turns,
        "duration_ms": result.get("duration_ms"),
        "num_turns": result.get("num_turns"),
        "stop_reason": result.get("stop_reason") or result.get("subtype"),
        "is_error": result.get("is_error"),
        "total_cost_usd": result.get("total_cost_usd"),
        "usage": {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
        },
        "modelUsage": result.get("modelUsage"),
        "chat_count": len(live.messages),
        "chats": [
            {"id": row["id"], "model": row["model"], "usage": row.get("usage")}
            for row in live.messages.values()
        ],
        "tool_calls": tool_rows,
        "tool_failures": live.tool_failures,
        "result": result.get("result"),
        "content": live.capture,
    }


def print_summary(summary, path):
    print("exit", summary["exit_code"], "stderr_bytes", summary["stderr_bytes"])
    if summary["stderr_bytes"]:
        print("stderr_tail", summary["stderr_tail"].replace("\n", " ")[:300])
    print("session", summary["session_id"])
    print("trace", summary["trace_id"])
    print("model", summary["model"], "apiKeySource", summary["apiKeySource"])
    print("mcp", summary["mcp_servers"])
    print("skills", summary["skills"])
    print("memory_paths", summary["memory_paths"])
    print("builtin_tools", summary["tools"])
    print(
        "max_turns_flag",
        summary["max_turns_flag"],
        "turns",
        summary["num_turns"],
        "stop",
        summary["stop_reason"],
        "duration_ms",
        summary["duration_ms"],
        "cost",
        summary["total_cost_usd"],
        "is_error",
        summary["is_error"],
    )
    for tool in summary["tool_calls"]:
        print(("FAIL" if tool["is_error"] else "ok"), tool["name"], tool["approximate_ms"])
    head = (summary["result"] or "").replace("\n", " ")[:400]
    print("RESULT_HEAD", head)
    print_reconciliation(summary)
    print("saved", path)


def _add_usage(total, raw):
    if not raw:
        return
    from tracing import usage_attributes

    for key, value in usage_attributes(raw).items():
        total[key] = total.get(key, 0) + value


def _blank_usage():
    from tracing import usage_attributes

    return usage_attributes({})


def print_reconciliation(summary):
    chats = summary.get("chats") or []
    ids = [row["id"] for row in chats]
    print("RESPONSE_IDS", len(ids), "unique", len(set(ids)))
    if len(ids) != len(set(ids)):
        print("DUPLICATE_RESPONSE_IDS", [item for item in ids if ids.count(item) > 1])
    chat_sum = _blank_usage()
    for row in chats:
        _add_usage(chat_sum, row.get("usage"))
    haiku_sum = _blank_usage()
    haiku_models = []
    main = summary.get("model")
    for name, raw in (summary.get("modelUsage") or {}).items():
        if main and name == main:
            continue
        haiku_models.append(name)
        _add_usage(haiku_sum, raw)
    combined = {key: chat_sum[key] + haiku_sum[key] for key in chat_sum}
    from tracing import usage_attributes

    result_usage = usage_attributes(
        {
            "input_tokens": (summary.get("usage") or {}).get("input_tokens"),
            "output_tokens": (summary.get("usage") or {}).get("output_tokens"),
            "cache_read_input_tokens": (summary.get("usage") or {}).get("cache_read_input_tokens"),
            "cache_creation_input_tokens": (summary.get("usage") or {}).get("cache_creation_input_tokens"),
        }
    )
    print("HAIKU_MODELS", haiku_models)
    print("CHAT_SUM", chat_sum)
    print("HAIKU_SUM", haiku_sum)
    print("CHAT_PLUS_HAIKU", combined)
    print("RESULT_USAGE", result_usage)
    gaps = {key: result_usage[key] - combined[key] for key in result_usage}
    print("GAP_RESULT_MINUS_CHATS_AND_HAIKU", gaps)
    for name, raw in (summary.get("modelUsage") or {}).items():
        print("MODEL_USAGE", name, usage_attributes(raw))


def parse_args(argv):
    max_turns = 20
    repeat = 1
    index = 0
    while index < len(argv):
        if argv[index] == "--max-turns":
            max_turns = int(argv[index + 1])
            index += 2
        elif argv[index] == "--repeat":
            repeat = int(argv[index + 1])
            index += 2
        else:
            raise SystemExit(f"unknown argument {argv[index]}")
    if max_turns < 1:
        raise SystemExit("--max-turns must be >= 1")
    return max_turns, repeat


def main():
    load_env_file()
    venv_python = ROOT / ".venv" / "bin" / "python"
    if venv_python.exists() and Path(sys.prefix) != ROOT / ".venv":
        os.execv(str(venv_python), [str(venv_python), __file__, *sys.argv[1:]])
    max_turns, repeat = parse_args(sys.argv[1:])
    for index in range(1, repeat + 1):
        from tracing import content_enabled

        print(
            f"\n=== run {index}/{repeat} content={content_enabled()} max_turns={max_turns} ===",
            flush=True,
        )
        run_once(max_turns=max_turns)


if __name__ == "__main__":
    main()
