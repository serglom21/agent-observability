# Collector experiment

Can an OpenTelemetry Collector turn Claude Code's native telemetry into spans that Sentry's Agents > Conversations view renders, with no wrapper code in the agent loop?

**Verdict: works with caveats.** The conversation renders. Tool names and durations are the native ones. Tokens are real. The page cost is positive and is not `result.total_cost_usd`. Haiku becomes an extra LLM call on a second trace. A stock transform processor cannot copy the log's cost onto the span.

Claude Code 2.1.177. `otelcol-contrib` 0.163.2, checksum matched the release `.sha256`. `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1`. Content flags unset. `tracing.py` was not imported.

Two diagnoses. The shape section is the no-transform capture. The live section is a later diagnosis of the same prompt, the one in the screenshot. They are not the same run.

## What Sentry reads

From the docs and from a wrapper span that already renders:

- Conversations groups spans that share `gen_ai.conversation.id`. https://docs.sentry.io/product/agents/conversations/
- The span op, or the `sentry.op` attribute in stream mode, must be `gen_ai.{gen_ai.operation.name}`. `gen_ai.operation.name` must be `invoke_agent`, `chat`, or `execute_tool`. Span names should be `invoke_agent {agent}`, `chat {model}`, and `execute_tool {tool}`. https://docs.sentry.io/platforms/python/agent-tracing/manual-instrumentation/
- Eve's OTLP-shaped spans: Sentry derives the span op from `gen_ai.operation.name`. https://docs.sentry.io/platforms/javascript/guides/node/agent-tracing/eve/
- A stored wrapper span comes back as `span.op` `gen_ai.invoke_agent` / `gen_ai.chat` / `gen_ai.execute_tool`, with the same value in `sentry.op`, `gen_ai.operation.name` set to `invoke_agent` / `chat` / `execute_tool`, `gen_ai.agent.name` `dictation-fixer`, `gen_ai.conversation.id` set, and descriptions `invoke_agent dictation-fixer`, `chat claude-sonnet-4-6`, `execute_tool {tool name}`.
- `gen_ai.usage.input_tokens` includes cache read and cache write. `gen_ai.usage.cache_read.input_tokens` (alias `gen_ai.usage.input_tokens.cached`) is a subset. https://getsentry.github.io/sentry-conventions/attributes/gen_ai/
- Direct OTLP accepts traces and logs. Span events are dropped. https://docs.sentry.io/concepts/otlp/direct/traces/

This transform sets both `gen_ai.operation.name` and `sentry.op`, because the stored wrapper span has both and the docs require the op. It was not re-run with only one of them, so this experiment does not say which one Sentry actually used.

## Native shape

No transform. One diagnosis. 31 spans, 2 traces, scope `com.anthropic.claude_code.tracing`.

| Span | Parent | Count | Attributes actually present |
|---|---|---|---|
| `claude_code.interaction` | none (root of the main trace) | 1 | `session.id`, `span.type=interaction`, `interaction.duration_ms`, `interaction.sequence`, `user_prompt` (the literal `<REDACTED>`), `user_prompt_length`, `terminal.type`, plus account and organization fields that the collector config deletes before export |
| `claude_code.llm_request` | the interaction, except the Haiku span | 6 | `session.id`, `span.type=llm_request`, `model`, `gen_ai.request.model`, `gen_ai.system=anthropic`, `gen_ai.response.id`, `gen_ai.response.finish_reasons`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `duration_ms`, `ttft_ms`, `attempt`, `stop_reason`, `success`, `speed`, `llm_request.context`, `request_id`. No cost attribute |
| `claude_code.tool` | the interaction | 8 | `session.id`, `span.type=tool`, `tool_name`, `gen_ai.tool.call.id`, `tool_use_id`, `duration_ms` |
| `claude_code.tool.execution` | that tool span | 8 | `session.id`, `span.type=tool.execution`, `success`, `duration_ms`, `gen_ai.tool.call.id`. No `tool_name` |
| `claude_code.tool.blocked_on_user` | that tool span | 8 | `session.id`, `span.type=tool.blocked_on_user`, `decision=unknown`, `source=unknown`, `duration_ms` (about 0–1 ms) |

Answers:

- Token counts are on the `llm_request` spans. `input_tokens` is the uncached count. Cache read is `cache_read_tokens`. Cache write is `cache_creation_tokens`. Output is `output_tokens`. On the no-transform run those Sonnet spans summed to the CLI `result.usage` (uncached 11, cache read 51,995, cache write 24,262, output 2,594).
- There is no cost attribute on any span.
- The tool name is `tool_name` on `claude_code.tool`. It is not on `claude_code.tool.execution`.
- `session.id` is on every span. The Haiku span carries the same `session.id` as the interaction.
- Haiku is its own trace. The span is a root `claude_code.llm_request` with `llm_request.context=standalone`, model `claude-haiku-4-5-20251001`, `input_tokens` 579, `output_tokens` 15, both cache counts 0. It does carry `session.id`.

Logs from that capture: `claude_code.user_prompt`, `claude_code.api_request` (6), `claude_code.tool_decision` (8), `claude_code.tool_result` (8), `claude_code.mcp_server_connection` (2). `claude_code.api_request` carries `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `cost_usd`, `cost_usd_micros`, `model`, `request_id`, and `duration_ms`. Five of those logs share the interaction trace. The Haiku `api_request` log has no trace id. The two MCP connection logs have no trace id either.

A `transform` processor cannot read a log while it is editing a span. The Sonnet logs and spans both have `request_id`, so a custom processor could join `cost_usd` onto the matching span. The stock collector config in this repo does not. The Haiku log would have to be joined on `request_id` too, because it has no trace id. This experiment does not set `gen_ai.usage.total_cost_usd`. Sentry prices the token attributes itself.

## Attempts

1. Live diagnosis, transform on, export to Sentry. The file exporter showed the mapping had already been applied (names `invoke_agent dictation-fixer`, `chat {model}`, `execute_tool {tool}`, usage filled, no email attribute). Sentry responded 401, `missing authorization information`, and the exporter dropped the batches. Cause: the auth value was `sentry sentry_key=...` and the shell truncated it to the word `sentry` when the env file was sourced unquoted. The endpoint substitution had worked. This attempt did not land a conversation.
2. The same batches were posted again to a collector whose header is `sentry sentry_key=${env:SENTRY_OTLP_KEY}` (the key is only the public DSN key). No 401. The spans indexed. The conversation rendered. Stopped here. The mapping was not changed between the attempts. The transform is idempotent on these spans, so sending the already-mapped batch through it again did not add a second copy. Sentry shows one of each span.

## What the page shows

The screenshot is this second diagnosis. Header: 6 LLM calls, 0 errors, 64K tokens, $0.123. The row under the date says 2 traces.

The CLI for that process: 9 turns, about 45s, `result.total_cost_usd` $0.135, of which Haiku is $0.000674. `result.usage` output 2,372, uncached input 11, cache read 47,724, cache write 14,141. Five distinct assistant response ids.

- The conversation is in Agents > Conversations.
- Tool rows are the eight `claude_code.tool` spans, with names and durations. Sentry's `span.duration` matches the native span timestamp to the millisecond: ToolSearch 3.11 ms, `search_sentry_tools` 10.43 ms, `search_issues` 1,006.67 / 1,907.18 / 1,240.96 ms, `execute_sentry_tool` 289.72 / 649.42 ms, `get_sentry_resource` 2,278.66 ms. Those are the CLI's own durations, not the wrapper's stream-arrival gaps. The wrapper clock was not measured on this same process, so there is no call-by-call comparison. `tool.execution` and `tool.blocked_on_user` were left as `claude_code.*` and landed with `span.op=default`. They are not extra tool rows.
- 6 LLM calls against 5 assistant response ids. The sixth is Haiku (`chat claude-haiku-4-5-20251001`), on its own trace, pulled into the conversation because `session.id` was copied to `gen_ai.conversation.id`. The page's "2 traces" is that split. No duplicate Sonnet response id.
- Chat attributes sum to 62,455 input tokens and 2,391 output tokens (64,846). The header reads 64K. `gen_ai.cost.total_tokens` on the six chats sums to $0.123. None of those costs is negative or zero. The smallest is the Haiku chat at $0.000741, against the CLI's $0.000674 for Haiku. The invoke span has no cost attribute. The page $0.123 is not `result.total_cost_usd` ($0.135).
- Of 25 `claude_code.*` logs, 22 share a trace with a chat span. All five Sonnet `api_request` logs share the interaction trace. The Haiku `api_request` log does not share the Haiku span's trace. The two `mcp_server_connection` logs have no span trace.
- Content flags were unset. The page shows the banner "This conversation's inputs and outputs weren't captured", `<not reported>` on the prompt and assistant rows, and `0 B` on tool rows. `user_prompt` on the span was the literal `<REDACTED>`.

## Wrapper vs collector

| | Wrapper | Collector |
|---|---|---|
| What the customer maintains | `tracing.py` (471 lines) and the runner that parses `stream-json` | `collector/otelcol.yaml` (91 lines) plus a running collector. The agent loop only gains env vars |
| Change in the loop | The process must be started by the runner so it can read the stream | None, if `claude` is already started with the OTEL env vars in the config |
| Timing | Gaps between `stream-json` events, tagged approximate. The first chat includes CLI and MCP startup | The CLI's own span timestamps. Tool rows above match those timestamps |
| Tokens | Per assistant message from the stream. Output there is often a handful of tokens. Exact `result.usage` is stored on `invoke_agent` | Per native `llm_request`. `input_tokens` on the span is uncached; the transform adds cache read and cache write before sending, which is what kept the cost positive |
| Cost | `result.total_cost_usd` on `invoke_agent`. The conversation page still prices the chat spans and under-counts output | No cost attribute exists on the spans. The page shows Sentry's price of the token attributes ($0.123 here) against the CLI's $0.135. `cost_usd` exists on `claude_code.api_request` logs and was not copied onto spans |
| Haiku | Attribute on `invoke_agent`, not a chat span, so the page does not count it | Own trace, mapped to a `chat` span, same conversation id, so the page counts it. Its log is not on that trace |
| Log correlation | Wrapper logs are emitted inside the invoke span, so they share its trace | Sonnet `api_request`, tool, and prompt logs share the interaction trace. Haiku's `api_request` and the MCP connection logs do not |
| Process dies mid-run | Not re-tested here. The wrapper sends a span when it closes; a hard kill drops whatever it has not flushed, and the open invoke span is one of those | Not re-tested by killing `claude`. Export interval is 1s, so a finished span can already have left. The interaction span stays open until the end, so a kill loses it and any unflushed batch. A 401 from Sentry is permanent: the exporter dropped those batches |
| What Sentry cannot ingest | OTLP metrics. This run set `OTEL_METRICS_EXPORTER=none` and did not send a metrics payload | Same. Direct OTLP docs cover traces and logs. Span events are dropped, which does not matter here: the native spans put tokens on attributes, not span events |
| Fragile | The stream-json event shape | Span names and `span.type` come from Claude Code 2.1.177 with the enhanced telemetry beta flag. A later CLI can rename `claude_code.llm_request` or stop putting `input_tokens` on the span. The beta flag is still named beta |
