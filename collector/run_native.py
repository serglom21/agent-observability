#!/usr/bin/env python3
"""One headless Claude diagnosis with native OpenTelemetry on.

Same isolation and flags as run.claude_command(). tracing.py is not imported,
so the wrapper does not open spans for this process.
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from run import alert_prompt, claude_command, claude_env, load_env_file  # noqa: E402

if "tracing" in sys.modules:
    raise SystemExit("tracing.py was imported; native run aborted")


def native_env():
    env = claude_env()
    env["CLAUDE_CODE_ENABLE_TELEMETRY"] = "1"
    env["CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"] = "1"
    env["OTEL_TRACES_EXPORTER"] = "otlp"
    env["OTEL_LOGS_EXPORTER"] = "otlp"
    env["OTEL_METRICS_EXPORTER"] = "none"
    env["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/protobuf"
    env["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://127.0.0.1:4318"
    env["OTEL_TRACES_EXPORT_INTERVAL"] = "1000"
    env["OTEL_LOGS_EXPORT_INTERVAL"] = "1000"
    env["OTEL_SERVICE_NAME"] = "dictation-fixer-native"
    for key in (
        "OTEL_LOG_USER_PROMPTS",
        "OTEL_LOG_TOOL_DETAILS",
        "OTEL_LOG_TOOL_CONTENT",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
        "OTEL_EXPORTER_OTLP_LOGS_HEADERS",
    ):
        env.pop(key, None)
    return env


def main():
    load_env_file()
    prompt = alert_prompt()
    env = native_env()
    proc = subprocess.Popen(
        claude_command(prompt, max_turns=20),
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
    lines = []
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            lines.append(line)
        code = proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    stderr = stderr_box[0] if stderr_box else ""
    out_dir = ROOT / "runs"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    raw_path = out_dir / f"native-{stamp}.jsonl"
    raw_path.write_text("".join(lines))
    result = {}
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result":
            result = event
    usage = result.get("usage") or {}
    model_usage = result.get("modelUsage") or {}
    print("exit", code, "stderr_bytes", len(stderr))
    print(
        "turns",
        result.get("num_turns"),
        "duration_ms",
        result.get("duration_ms"),
        "cost",
        result.get("total_cost_usd"),
        "is_error",
        result.get("is_error"),
        "stop",
        result.get("stop_reason") or result.get("subtype"),
    )
    print(
        "usage_input",
        usage.get("input_tokens"),
        "usage_output",
        usage.get("output_tokens"),
        "cache_read",
        usage.get("cache_read_input_tokens"),
        "cache_write",
        usage.get("cache_creation_input_tokens"),
    )
    for name, raw in model_usage.items():
        if isinstance(raw, dict):
            print(
                "model",
                name,
                "in",
                raw.get("inputTokens"),
                "out",
                raw.get("outputTokens"),
                "cache_read",
                raw.get("cacheReadInputTokens"),
                "cache_write",
                raw.get("cacheCreationInputTokens"),
                "cost",
                raw.get("costUSD"),
            )
    print("stream_lines", len(lines), "saved_bytes", raw_path.stat().st_size)


if __name__ == "__main__":
    main()
