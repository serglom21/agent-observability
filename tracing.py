#!/usr/bin/env python3
"""Map Claude stream-json events onto Sentry gen_ai spans.

stream-json event                         gen_ai span
----------------------------------------  ------------------------------------------
(run start, before the process)           open the first gen_ai.chat
system / init  session_id                 gen_ai.conversation.id on every span
assistant message, one response id        one gen_ai.chat, including later chunks
assistant tool_use                        open gen_ai.execute_tool
user tool_result                          close that tool; the chat stays open
assistant message, a different id         close the previous chat, open the next
result                                    close the open chat and send it
result.usage + result.total_cost_usd      exact totals on gen_ai.invoke_agent
result.modelUsage for other models        attributes on invoke_agent, no span

One gen_ai.chat per assistant response id. A tool_result does not close it,
so a later chunk with the same id stays on that span. The chat closes, and
is sent, when a different id arrives or at result. The first chat opens at
process start, so it includes CLI and MCP startup. Later chats open at the
previous event. Durations are tagged timing=approximate_stream_arrival.

The stream's per-message output is often 1 token. That count is what the
chat span stores. The exact output total stays on invoke_agent, from
result.usage. A telemetry failure is logged and does not stop the agent.

Sentry does not list a trace until some segment span is closed. A segment
named dictation-fixer is ended immediately so later spans can show up while
invoke_agent is still open. That parent is a fraction of a millisecond. It
is not a model call.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone

log = logging.getLogger("dictation_fixer")

CONTENT_LIMIT = 4000


def usage_attributes(raw):
    """Normalize Anthropic usage so cache tokens are a subset of input_tokens.

    The CLI's input_tokens field is the uncached count. Sentry prices
    (input - cache read - cache write) at the input rate, then adds cache
    read and cache write at their own rates. Leaving input uncached while
    also sending the cache fields makes that cost negative. The alias fields
    (input_tokens.cached, input_tokens.cache_write) are the same counts as
    cache_read.input_tokens and cache_creation.input_tokens. A one-trace
    variant check priced both layouts the same.
    """
    uncached = int(raw.get("input_tokens") or raw.get("inputTokens") or 0)
    output = int(raw.get("output_tokens") or raw.get("outputTokens") or 0)
    cached = int(raw.get("cache_read_input_tokens") or raw.get("cacheReadInputTokens") or 0)
    written = int(
        raw.get("cache_creation_input_tokens") or raw.get("cacheCreationInputTokens") or 0
    )
    total_in = uncached + cached + written
    return {
        "gen_ai.usage.input_tokens": total_in,
        "gen_ai.usage.input_tokens.cached": cached,
        "gen_ai.usage.input_tokens.cache_write": written,
        "gen_ai.usage.cache_read.input_tokens": cached,
        "gen_ai.usage.cache_creation.input_tokens": written,
        "gen_ai.usage.output_tokens": output,
        "gen_ai.usage.total_tokens": total_in + output,
    }


def content_enabled():
    return os.environ.get("DICTATION_FIXER_CAPTURE_CONTENT", "").strip() in ("1", "true", "yes")


def clip(value):
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, default=str)
    if len(text) > CONTENT_LIMIT:
        return text[:CONTENT_LIMIT] + "…[truncated]"
    return text


def flush_telemetry():
    import sentry_sdk

    client = sentry_sdk.get_client()
    batcher = getattr(client, "span_batcher", None)
    if batcher is not None:
        batcher.flush()
    logs = getattr(client, "log_batcher", None)
    if logs is not None:
        logs.flush()
    sentry_sdk.flush(timeout=10)


def init_sdk():
    import sentry_sdk

    sentry_sdk.init(
        dsn=os.environ["SENTRY_DSN"],
        traces_sample_rate=1.0,
        trace_lifecycle="stream",
        enable_logs=True,
        # The stdlib integration would turn this process's own Sentry queries
        # into http.client spans on the same trace.
        default_integrations=False,
        send_default_pii=content_enabled(),
        environment="demo",
        release="dictation-fixer-demo@0.1.0",
    )


class LiveRun:
    """One invoke_agent span, plus a chat or tool span per stream event."""

    def __init__(self, prompt, model, max_turns):
        import sentry_sdk
        import sentry_sdk.logger as sentry_logger
        from sentry_sdk.traces import start_span

        self.sdk = sentry_sdk
        self.log = sentry_logger
        self.start_span = start_span
        self.prompt = prompt
        self.model = model
        self.max_turns = max_turns
        self.capture = content_enabled()
        self.session_id = None
        self.init = None
        self.result = None
        self.chat = None
        self.chat_id = None
        self.chat_text = []
        self.chat_opened = None
        self.chat_last = None
        self.pending_boundary = None
        self.messages = {}
        self.bound_ids = []
        self.tools = {}
        self.open_tools = {}
        self.tool_failures = 0
        segment = start_span(
            name="dictation-fixer",
            attributes={
                "sentry.op": "gen_ai.invoke_agent",
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.system": "anthropic",
                "gen_ai.request.model": model,
                "gen_ai.agent.name": "dictation-fixer",
                "timing": "segment_closed_so_children_can_list",
            },
        )
        segment.end()
        self.root = start_span(
            name="invoke_agent dictation-fixer",
            parent_span=segment,
            active=True,
            attributes={
                "sentry.op": "gen_ai.invoke_agent",
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.system": "anthropic",
                "gen_ai.request.model": model,
                "gen_ai.agent.name": "dictation-fixer",
                "gen_ai.request.max_turns": max_turns,
                "timing": "approximate_stream_arrival",
            },
        )
        if type(self.root).__name__ != "StreamedSpan":
            self.warn(f"span streaming did not start: {type(self.root).__name__}")
        if self.capture:
            self.root.set_attribute(
                "gen_ai.input.messages",
                json.dumps([{"role": "user", "content": prompt}]),
            )
        # First chat covers process start through the first assistant message.
        # Flush after it is open so the closed segment is sent without a hole
        # between that segment and this chat.
        self.begin_chat_window()
        flush_telemetry()
        self.log.info(
            "dictation fixer run started",
            attributes={
                "gen_ai.agent.name": "dictation-fixer",
                "gen_ai.request.model": model,
                "gen_ai.request.max_turns": max_turns,
            },
        )

    def warn(self, message):
        """Telemetry problems are logged. They do not stop the agent."""
        text = str(message)
        print("WARNING", text, flush=True)
        log.warning("%s", text)
        try:
            self.log.warning(text, attributes={"gen_ai.agent.name": "dictation-fixer"})
        except Exception:
            pass

    def trace_id(self):
        return self.root.trace_id

    def tag(self, span):
        if self.session_id:
            span.set_attribute("gen_ai.conversation.id", self.session_id)
        span.set_attribute("gen_ai.agent.name", "dictation-fixer")

    def begin_chat_window(self, at=None):
        """Open a chat at this instant, or at an earlier event's arrival."""
        if self.chat is not None:
            return
        opened = time.time() if at is None else at
        self.chat_opened = opened
        self.chat_last = opened
        self.chat_id = None
        self.chat_text = []
        span = self.start_span(
            name=f"chat {self.model}",
            active=False,
            attributes={
                "sentry.op": "gen_ai.chat",
                "gen_ai.operation.name": "chat",
                "gen_ai.system": "anthropic",
                "gen_ai.request.model": self.model,
                "timing": "approximate_stream_arrival",
            },
        )
        if at is not None:
            # sentry-sdk 2.68.1 traces.start_span() takes name, attributes,
            # parent_span, and active. It has no start_timestamp argument.
            # StreamedSpan.start_timestamp is a read-only property. The older
            # Span constructor does accept start_timestamp, but that API does
            # not emit streamed spans when trace_lifecycle="stream".
            span._start_timestamp = datetime.fromtimestamp(opened, timezone.utc)
        self.chat = span

    def bind_chat(self, message):
        mid = message.get("id")
        self.bound_ids.append(mid)
        if mid in self.messages:
            self.warn(f"duplicate gen_ai.response.id {mid}")
            return False
        model = message.get("model") or self.model
        self.chat_id = mid
        self.chat.name = f"chat {model}"
        self.chat.set_attribute("gen_ai.request.model", model)
        self.chat.set_attribute("gen_ai.response.model", model)
        self.chat.set_attribute("gen_ai.response.id", mid)
        self.chat.set_attribute("gen_ai.usage.source", "assistant_message")
        self.messages[mid] = {"id": mid, "model": model, "text": self.chat_text, "usage": None}
        return True

    def close_chat(self):
        """End the open chat at its last assistant event, not at 'now'."""
        span = self.chat
        if span is None:
            return
        if self.chat_id is None:
            # A later tool_result can arrive before the next assistant message.
            # Drop that empty window instead of emitting a 0ms chat.
            self.chat = None
            return
        self.tag(span)
        if self.capture and self.chat_text:
            span.set_attribute(
                "gen_ai.output.messages",
                json.dumps([{"role": "assistant", "content": clip("".join(self.chat_text))}]),
            )
        end = self.chat_last if self.chat_last is not None else time.time()
        if self.chat_opened is not None and end < self.chat_opened:
            end = self.chat_opened
        span.end(end)
        self.chat = None
        self.chat_id = None

    def observe_message(self, message):
        """Record this chunk on the chat already bound to its response id."""
        mid = message.get("id")
        self.chat_last = time.time()
        usage = message.get("usage") or {}
        if usage:
            for key, value in usage_attributes(usage).items():
                self.chat.set_attribute(key, value)
            self.messages[mid]["usage"] = usage
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text"):
                self.chat_text.append(block["text"])
            elif block.get("type") == "tool_use" and block.get("id") not in self.tools:
                self.open_tool(block)

    def on_assistant(self, event):
        message = event.get("message") or {}
        mid = message.get("id")
        if not mid:
            return
        if self.chat is not None and self.chat_id == mid:
            self.pending_boundary = None
            self.observe_message(message)
            return
        if self.chat is not None and self.chat_id is not None:
            started = self.pending_boundary if self.pending_boundary is not None else self.chat_last
            self.close_chat()
            self.begin_chat_window(at=started)
            self.pending_boundary = None
        if self.chat is None:
            self.begin_chat_window()
        if not self.bind_chat(message):
            return
        self.observe_message(message)

    def open_tool(self, block):
        name = block.get("name") or "tool"
        attrs = {
            "sentry.op": "gen_ai.execute_tool",
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": name,
            "timing": "approximate_stream_arrival",
        }
        if self.capture:
            attrs["gen_ai.tool.input"] = clip(block.get("input"))
        span = self.start_span(name=f"execute_tool {name}", active=False, attributes=attrs)
        self.open_tools[block["id"]] = span
        self.tools[block["id"]] = {
            "id": block["id"],
            "name": name,
            "is_error": None,
            "start": time.time(),
            "end": None,
        }

    def on_user(self, event):
        # Same response id can continue after this tool_result. Leave the chat open.
        content = (event.get("message") or {}).get("content")
        if not isinstance(content, list):
            return
        closed_tool = False
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_id = block.get("tool_use_id")
            span = self.open_tools.pop(tool_id, None)
            tool = self.tools.get(tool_id)
            if span is None or tool is None:
                continue
            failed = bool(block.get("is_error"))
            tool["is_error"] = failed
            tool["end"] = time.time()
            span.status = "error" if failed else "ok"
            if self.capture and block.get("content") is not None:
                span.set_attribute("gen_ai.tool.output", clip(block.get("content")))
            self.tag(span)
            if failed:
                self.tool_failures += 1
                self.log.error(
                    "dictation fixer tool failed",
                    attributes={"gen_ai.tool.name": tool["name"]},
                )
            span.end()
            closed_tool = True
        if closed_tool:
            self.pending_boundary = time.time()

    def on_result(self, event):
        self.close_chat()
        self.result = event
        usage = event.get("usage") or {}
        if usage:
            for key, value in usage_attributes(usage).items():
                self.root.set_attribute(key, value)
            self.root.set_attribute("gen_ai.usage.source", "claude_result_event")
        if event.get("total_cost_usd") is not None:
            self.root.set_attribute("gen_ai.usage.total_cost_usd", float(event["total_cost_usd"]))
        if event.get("num_turns") is not None:
            self.root.set_attribute("gen_ai.response.num_turns", int(event["num_turns"]))
        if self.capture and event.get("result"):
            self.root.set_attribute(
                "gen_ai.output.messages",
                json.dumps([{"role": "assistant", "content": clip(event.get("result"))}]),
            )
        if event.get("is_error"):
            self.root.status = "error"
        self.add_side_models(event)

    def add_side_models(self, event):
        """Haiku is not in the stream. Record its usage without a 0ms chat span."""
        names = []
        for name, raw in (event.get("modelUsage") or {}).items():
            if self.model and name == self.model:
                continue
            names.append(str(name))
            slug = "".join(ch if ch.isalnum() else "_" for ch in str(name))[:48]
            prefix = f"gen_ai.side_model.{slug}"
            self.root.set_attribute(f"{prefix}.timing", "not_in_stream")
            for key, value in usage_attributes(raw).items():
                leaf = key.split("gen_ai.usage.", 1)[-1]
                self.root.set_attribute(f"{prefix}.{leaf}", value)
            if raw.get("costUSD") is not None:
                self.root.set_attribute(f"{prefix}.total_cost_usd", float(raw["costUSD"]))
        if names:
            self.root.set_attribute("gen_ai.side_model.names", ",".join(names)[:200])

    def finish(self, code):
        for span in list(self.open_tools.values()):
            span.status = "error"
            self.tag(span)
            span.end()
        self.open_tools.clear()
        self.close_chat()
        result = self.result or {}
        self.log.info(
            "dictation fixer run finished",
            attributes={
                "exit_code": code,
                "gen_ai.response.num_turns": int(result.get("num_turns") or 0),
                "tool_count": len(self.tools),
                "tool_failures": self.tool_failures,
                "total_cost_usd": float(result.get("total_cost_usd") or 0),
                "duration_ms": int(result.get("duration_ms") or 0),
            },
        )
        self.tag(self.root)
        self.root.end()
        flush_telemetry()
        if len(self.bound_ids) != len(set(self.bound_ids)):
            self.warn("duplicate gen_ai.response.id")

    def note_model(self, model):
        self.model = model
        self.root.set_attribute("gen_ai.request.model", model)
        self.root.set_attribute("gen_ai.response.model", model)
        if self.chat is not None and self.chat_id is None:
            self.chat.name = f"chat {model}"
            self.chat.set_attribute("gen_ai.request.model", model)


def handle_line(live, line):
    try:
        _handle_line(live, line)
    except Exception as exc:
        live.warn(f"telemetry dropped an event: {exc}")


def _handle_line(live, line):
    line = line.strip()
    if not line.startswith("{"):
        return
    event = json.loads(line)
    kind = event.get("type")
    if kind == "system" and event.get("subtype") == "init":
        live.init = event
        live.session_id = event.get("session_id")
        if live.session_id:
            live.sdk.get_current_scope().set_conversation_id(live.session_id)
            live.root.set_attribute("gen_ai.conversation.id", live.session_id)
            live.root.set_attribute("gen_ai.response.id", live.session_id)
            print("session", live.session_id, flush=True)
        if event.get("model"):
            live.note_model(event["model"])
    elif kind == "assistant":
        live.on_assistant(event)
    elif kind == "user":
        live.on_user(event)
    elif kind == "result":
        if not live.session_id and event.get("session_id"):
            live.session_id = event["session_id"]
            live.sdk.get_current_scope().set_conversation_id(live.session_id)
        live.on_result(event)
