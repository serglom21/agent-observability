# Collector experiment

Can an OpenTelemetry Collector turn Claude Code's native telemetry into spans that Sentry's Agents > Conversations view renders, with no wrapper code in the agent loop?

**Verdict: works with caveats.** A clean live run, exported on the first try, renders in Conversations: 7 LLM calls, 0 errors, 113K tokens, $0.159, and "2 traces". The CLI `result.total_cost_usd` for that same process is $0.2151071. The page is using Sentry's 5-minute cache-write price. The CLI and the `cost_usd` values on the `claude_code.api_request` logs are the 1-hour cache-write price. The gap is only that rate. Haiku is still an extra LLM call on a second trace. Turning the content flags on stores the prompt and the tool arguments, and the page still does not show the prompt, the tool arguments, the tool results, or the assistant text.

Claude Code 2.1.177. `otelcol-contrib` 0.163.2, checksum matched the release `.sha256`. `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1`. Content flags unset on the run in the screenshot. `tracing.py` was not imported.

The shape section is an earlier no-transform capture of the same prompt. The screenshot and the clean-live section are the later run.

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

## Clean live run

One diagnosis. The collector was started with the header `sentry sentry_key=${env:SENTRY_OTLP_KEY}` and the env file holds the public DSN key in single quotes (32 characters, no space). Content flags were unset.

The page for that run: 7 LLM calls, 0 errors, 113K tokens, $0.159, and "2 traces". The banner is still "This conversation's inputs and outputs weren't captured". The transcript shows `<not reported>` and the tool rows show `0 B`.

The CLI for that process: exit 0, 10 turns, 67,434 ms, `result.total_cost_usd` 0.2151071. `result.usage`: uncached input 12, output 2,603, cache read 85,127, cache write 24,969. Haiku 579 in / 19 out / $0.000674. Six distinct assistant response ids, plus the Haiku chat, which is the seventh LLM call. The invoke span duration is 67,438.8 ms.

## Cost

Sentry's stored `gen_ai.cost.total_tokens` on the seven chats sums to 0.1589268. None is missing, zero, or negative. The page's $0.159 is that sum.

Pricing each chat from the span token attributes:

| Rate | Cache write | Sum |
|---|---|---|
| 5-minute | $3.75 / M | 0.1589268 |
| 1-hour | $6 / M | 0.2151071 |

Sonnet input $3/M, output $15/M, cache read $0.30/M. Haiku input $1/M, output $5/M. Haiku has no cache write, so both rates give $0.000674, which matches the CLI.

`cost_usd` on the seven `claude_code.api_request` logs also sums to 0.2151071, and each log matches that span's 1-hour price. The 5-minute rate is what Sentry stored and what the page shows. The 1-hour rate is what the CLI and the logs show. The difference is 24,969 cache-write tokens times $2.25/M, which is 0.0561802. No other rate was needed to explain the gap. The invoke span still has no cost attribute, and the transform still does not copy `cost_usd` onto a span.

## Content flags

One diagnosis with `OTEL_LOG_USER_PROMPTS=1`, `OTEL_LOG_TOOL_DETAILS=1`, and `OTEL_LOG_TOOL_CONTENT=1`. The transform copies `user_prompt` into `gen_ai.input.messages` as a one-element JSON array, role `user`, and skips the value when it is `<REDACTED>`.

What was exported, and what Sentry stored:

- The interaction span's `user_prompt` is the prompt text, not `<REDACTED>`. `gen_ai.input.messages` is the JSON array, and its content equals that prompt, including the newlines. Sentry stored that attribute.
- `claude_code.user_prompt` log attribute `prompt` is the same text. Sentry stored it.
- Tool spans have the tool name and no argument or result attribute. They have no span events.
- Each `llm_request` span has one span event, `gen_ai.request.attempt`, with `attempt` and `client_request_id` only. No message text. Sentry's OTLP intake drops span events.
- `claude_code.tool_decision` logs carry `tool_parameters` (JSON). `claude_code.tool_result` logs carry `tool_parameters`, `tool_input` (JSON arguments), and `tool_result_size_bytes`. There is no tool-result body. Sentry stored those log attributes.
- No span and no log carries assistant text.

The conversation page for that run: heading "Investigate dictation failure rate alert", 6 LLM calls, 0 errors, 60K, $0.101, "2 traces". The transcript banner still says the inputs and outputs weren't captured. Message bubbles are `<not reported>`. Tool rows are `0 B`. The chat drawer's Input tab says "No input for this span" and its Output tab says "No output for this span". The tool drawer's Input and Output tabs say the same. The heading is not the prompt. The page does not show the prompt, the tool arguments, the tool results, or the assistant text.

## Customer config

`collector/otelcol.customer.yaml` has no file exporter. It drops `tool.execution` and `tool.blocked_on_user` before the mapping. Missing `cache_read_tokens` and `cache_creation_tokens` are set to 0 before the sum. The agent name is `${env:AGENT_NAME}`. `AGENT_NAME` is required. If it is unset the span name is the message `AGENT_NAME is unset`. The user and organization deletes stay, in a processor after the mapping. `error_mode: ignore` stays, with a comment that a failing statement is skipped and the span is still exported. Cache counts are written only as `gen_ai.usage.cache_read.input_tokens` and `gen_ai.usage.cache_creation.input_tokens`.

A local fixture with the cache attributes absent produced `gen_ai.usage.input_tokens` equal to the uncached input and cache counts of 0. The two child spans were not exported. A live diagnosis through this file, content flags unset, exported. Sentry indexed 22 spans for that conversation: 1 `gen_ai.invoke_agent`, 9 `gen_ai.chat`, 12 `gen_ai.execute_tool`, and zero `span.op=default`. Every span's agent name is `dictation-fixer`. No `user.email` came back.

## What the page shows

The screenshot is the clean live run above. Header: 7 LLM calls, 0 errors, 113K tokens, $0.159. The row under the date says 2 traces. The conversation is in Agents > Conversations.

- 7 LLM calls against 6 assistant response ids. The seventh is Haiku (`chat claude-haiku-4-5-20251001`), on its own trace, pulled into the conversation because `session.id` was copied to `gen_ai.conversation.id`. The page's "2 traces" is that split.
- The header's 113K is input plus output on those chats. The page's $0.159 is the 5-minute price in the cost section, not the CLI total.
- Tool rows are the `claude_code.tool` spans, with names and the CLI's own durations. `tool.execution` and `tool.blocked_on_user` are dropped by `otelcol.customer.yaml`. The experiment config leaves them as `span.op=default`, and they are not extra tool rows.
- Sonnet `api_request`, tool, and prompt logs share the interaction trace. The Haiku `api_request` log and the MCP connection logs do not.
- Content flags were unset. The page shows the banner "This conversation's inputs and outputs weren't captured", `<not reported>` on the prompt and assistant rows, and `0 B` on tool rows. `user_prompt` on the span was the literal `<REDACTED>`.

## Wrapper vs collector

| | Wrapper | Collector |
|---|---|---|
| What the customer maintains | `tracing.py` (471 lines) and the runner that parses `stream-json` | `collector/otelcol.yaml` (99 lines) or `collector/otelcol.customer.yaml` (99 lines) plus a running collector. The agent loop only gains env vars |
| Change in the loop | The process must be started by the runner so it can read the stream | None, if `claude` is already started with the OTEL env vars in the config |
| Timing | Gaps between `stream-json` events, tagged approximate. The first chat includes CLI and MCP startup | The CLI's own span timestamps. Tool rows above match those timestamps |
| Tokens | Per assistant message from the stream. Output there is often a handful of tokens. Exact `result.usage` is stored on `invoke_agent` | Per native `llm_request`. `input_tokens` on the span is uncached; the transform adds cache read and cache write before sending, which is what kept the cost positive |
| Cost | `result.total_cost_usd` on `invoke_agent`. The conversation page still prices the chat spans and under-counts output | No cost attribute exists on the spans. On the clean live run the page shows $0.159, which is the 5-minute cache-write price of the span tokens (0.1589268). The CLI and the logs' `cost_usd` are $0.2151071, the 1-hour price. The gap is $2.25/M on 24,969 cache-write tokens. `cost_usd` was not copied onto spans |
| Haiku | Attribute on `invoke_agent`, not a chat span, so the page does not count it | Own trace, mapped to a `chat` span, same conversation id, so the page counts it. Its log is not on that trace |
| Log correlation | Wrapper logs are emitted inside the invoke span, so they share its trace | Sonnet `api_request`, tool, and prompt logs share the interaction trace. Haiku's `api_request` and the MCP connection logs do not |
| Process dies mid-run | Not re-tested here. The wrapper sends a span when it closes; a hard kill drops whatever it has not flushed, and the open invoke span is one of those | Not re-tested by killing `claude`. Export interval is 1s, so a finished span can already have left. The interaction span stays open until the end, so a kill loses it and any unflushed batch. This config does not retry a batch Sentry rejects |
| What Sentry cannot ingest | OTLP metrics. This run set `OTEL_METRICS_EXPORTER=none` and did not send a metrics payload | Same. Direct OTLP docs cover traces and logs. Span events are dropped, which does not matter here: the native spans put tokens on attributes, not span events |
| Fragile | The stream-json event shape | Span names and `span.type` come from Claude Code 2.1.177 with the enhanced telemetry beta flag. A later CLI can rename `claude_code.llm_request` or stop putting `input_tokens` on the span. The beta flag is still named beta |
