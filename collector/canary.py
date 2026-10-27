#!/usr/bin/env python3
"""Fail if Claude Code renames the span types this collector maps.

Tested with Claude Code 2.1.177 and otelcol-contrib 0.163.2.

    python3 collector/canary.py --capture DIR
    python3 collector/canary.py --live

--capture reads DIR/traces.json, the concatenated OTLP JSON the file
exporter writes. --live starts that exporter on 127.0.0.1:4318, runs one
trivial `claude -p` prompt, and checks the capture. It does not send
anything to Sentry. Port 4318 must be free. The binary is $OTELCOL, or
/tmp/df-collector/otelcol-contrib (then bin/otelcol-contrib) when unset.

A passing run has at least one span.type=interaction and one
span.type=llm_request, and that llm_request carries input_tokens,
output_tokens, cache_read_tokens, and cache_creation_tokens. Cache counts
may be 0. A trivial prompt has no tool spans, so those are not required.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = Path(__file__).resolve().parent / "baseline.yaml"
DEFAULT_OTELCOLS = (
    Path("/tmp/df-collector/otelcol-contrib"),
    Path("/tmp/df-collector/bin/otelcol-contrib"),
)
PROMPT = "Reply with the single word pong."
TOKEN_ATTRS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
)


def fail(message):
    print("canary FAIL:", message)
    raise SystemExit(1)


def attr_value(raw):
    if not isinstance(raw, dict):
        return None
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in raw:
            return raw[key]
    return None


def iter_spans(path):
    text = Path(path).read_text()
    decoder = json.JSONDecoder()
    index = 0
    while True:
        rest = text[index:].lstrip()
        if not rest:
            return
        index = len(text) - len(rest)
        try:
            obj, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError as exc:
            fail(f"traces.json is not concatenated OTLP JSON ({exc})")
        index += end
        for resource in obj.get("resourceSpans") or []:
            for scope in resource.get("scopeSpans") or []:
                for span in scope.get("spans") or []:
                    attrs = {}
                    for item in span.get("attributes") or []:
                        attrs[item.get("key")] = attr_value(item.get("value"))
                    yield attrs


def check(path):
    if not Path(path).is_file():
        fail(f"no traces.json in the capture")
    types = {}
    llm = []
    for attrs in iter_spans(path):
        kind = attrs.get("span.type")
        types[kind] = types.get(kind, 0) + 1
        if kind == "llm_request":
            llm.append(attrs)
    print("span.type", " ".join(f"{k}={v}" for k, v in sorted(types.items(), key=lambda item: str(item[0]))))
    if types.get("interaction", 0) < 1:
        fail("no span.type=interaction (Claude Code may have renamed it)")
    if not llm:
        fail("no span.type=llm_request (Claude Code may have renamed it)")
    missing = [name for name in TOKEN_ATTRS if not any(name in span for span in llm)]
    if missing:
        fail("llm_request is missing " + ", ".join(missing))
    print("canary ok", "interaction", types["interaction"], "llm_request", len(llm))


def claude_version():
    try:
        proc = subprocess.run(
            ["claude", "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        fail(f"claude --version failed: {exc}")
    text = (proc.stdout or proc.stderr or "").strip().splitlines()
    print("claude", text[0] if text else f"exit {proc.returncode}")


def run_live():
    chosen = os.environ.get("OTELCOL")
    otelcol = Path(chosen) if chosen else next((p for p in DEFAULT_OTELCOLS if p.is_file()), None)
    if otelcol is None or not otelcol.is_file():
        fail("otelcol-contrib not found; set OTELCOL")
    if not (ROOT / "agent-home").is_dir():
        fail("agent-home/ is missing; log in with CLAUDE_CONFIG_DIR=./agent-home")
    capture = Path(tempfile.mkdtemp(prefix="df-canary-"))
    env = os.environ.copy()
    env["NATIVE_CAPTURE_DIR"] = str(capture)
    proc = subprocess.Popen(
        [str(otelcol), "--config", str(BASELINE)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            if proc.poll() is not None:
                out = proc.stdout.read() if proc.stdout else ""
                fail(f"collector exited {proc.returncode}: {out[-400:]}")
            try:
                import socket

                with socket.create_connection(("127.0.0.1", 4318), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.2)
        else:
            fail("collector did not listen on 4318")
        run_env = os.environ.copy()
        run_env["CLAUDE_CONFIG_DIR"] = str(ROOT / "agent-home")
        run_env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
        run_env["CLAUDE_CODE_ENABLE_TELEMETRY"] = "1"
        run_env["CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"] = "1"
        run_env["OTEL_TRACES_EXPORTER"] = "otlp"
        run_env["OTEL_LOGS_EXPORTER"] = "otlp"
        run_env["OTEL_METRICS_EXPORTER"] = "none"
        run_env["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/protobuf"
        run_env["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://127.0.0.1:4318"
        run_env["OTEL_TRACES_EXPORT_INTERVAL"] = "1000"
        run_env["OTEL_LOGS_EXPORT_INTERVAL"] = "1000"
        run_env["OTEL_SERVICE_NAME"] = "dictation-fixer-canary"
        for key in (
            "TRACEPARENT",
            "TRACESTATE",
            "OTEL_LOG_USER_PROMPTS",
            "OTEL_LOG_TOOL_DETAILS",
            "OTEL_LOG_TOOL_CONTENT",
        ):
            run_env.pop(key, None)
        mcp = capture / "mcp.json"
        mcp.write_text('{"mcpServers":{}}\n')
        claude = subprocess.run(
            [
                "claude",
                "-p",
                PROMPT,
                "--max-turns",
                "1",
                "--permission-mode",
                "bypassPermissions",
                "--tools",
                "",
                "--mcp-config",
                str(mcp),
                "--strict-mcp-config",
                "--setting-sources",
                "",
                "--no-session-persistence",
            ],
            cwd=str(ROOT),
            env=run_env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        print("claude_exit", claude.returncode, "stderr_bytes", len(claude.stderr or ""))
        if claude.returncode != 0:
            fail("claude -p failed")
        time.sleep(3)
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    check(capture / "traces.json")
    shutil.rmtree(capture, ignore_errors=True)


def main():
    if "tracing" in sys.modules:
        fail("tracing.py was imported")
    claude_version()
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == "--capture":
        check(Path(args[1]) / "traces.json")
        return
    if args == ["--live"]:
        run_live()
        return
    fail("usage: collector/canary.py --capture DIR | --live")


if __name__ == "__main__":
    main()
