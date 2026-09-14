# 事件流

`$SBX_WORK/events.jsonl`：**每行一个 JSON 对象**（JSONL），内容是 canonical 事件（Codex 形状）。`$SBX_WORK/events.raw.jsonl`：CLI 原生 stdout 行原样追加（排障用，不重放给 SSE）。

对 `codex` provider，runner 把 Codex 0.153.0 `codex exec --json` 的原生事件**原样透传**（translate = 恒等），并在每轮前后插入 runner 自身事件。对其他 provider，原生行先写 `events.raw.jsonl`，再由 adapter `translate` 归一化为下列 canonical 事件写入 `events.jsonl`。

控制面 SSE（`GET /api/sessions/{id}/events`）按行号重放 `events.jsonl`，见 `api.yaml`。

消费者以 `\n` 为界解析；一个读缓冲可能含多行 JSON（Modal `sb.exec` 默认 `bufsize=-1` 按 chunk 产出）。遇到无法解析的行计为坏行并继续处理后续行。

## canonical 事件（Codex 0.153.0 原生形状；所有 provider 归一化到此集合）

| `type` | 形状 | 说明 |
| --- | --- | --- |
| `thread.started` | `{thread_id}` | 线程/会话 id，供 resume。Codex 为 UUIDv7；其他 provider 翻译出自己的原生 session id。同一会话后续轮次的 `thread_id` **与首轮完全相同**。 |
| `turn.started` | `{}` | 一轮推理开始 |
| `item.started` | `{item}` | item 进入 in-progress。P0 实测：`command_execution` / `file_change` 有 started+completed；`agent_message` **只有** `item.completed` |
| `item.updated` | `{item}` | item 增量更新。P0 实测未观察到；契约保留为可选，消费者不得依赖 |
| `item.completed` | `{item}` | item 到达终态 |
| `turn.completed` | `{usage}` | 成功结束。`usage` 必选三字段见下；真实输出还带两个可选字段 |
| `turn.failed` | `{error:{message}}` | 本轮失败（与顶层 `error` 事件不同） |
| `error` | `{message}` | 流上的不可恢复错误 |

`item` 使用 Codex 0.153.0 扁平标签：`{id, type, ...payload}`。契约要求覆盖的 `item.type`：

- `agent_message`（`text`）——只有 `item.completed`，没有 `item.started`
- `command_execution`（`command`, `aggregated_output`, `status`, `exit_code`）——`item.started` + `item.completed`；started 里 `exit_code` 为 `null`；`command` 形如 `/bin/bash -lc "..."`
- `file_change`（`changes[{path,kind}]`, `status`）——`item.started` + `item.completed`（源码注释曾写「只发 completed」，P0 实测有 started，假件以实测为准）
- `reasoning`（`text`）
- `error`（`message`）——非致命错误以 **item** 出现，形状 `{"type":"item.completed","item":{"id":"item_0","type":"error","message":"..."}}`，与顶层 `error` 事件不同

其它 item 类型（如 `mcp_tool_call`）若出现则原样透传，解析器必须忽略未知字段。

### `usage` 字段

必选（canonical `usage_fields`）：

- `input_tokens`
- `cached_input_tokens`（真实输出通常很大，来自 prompt cache）
- `output_tokens`

可选（canonical `usage_fields_optional`；真实 `turn.completed` 一定带这两个字段）：

- `cache_write_input_tokens`
- `reasoning_output_tokens`

## runner 自身事件

| `type` | 形状 | 说明 |
| --- | --- | --- |
| `sbx.session_meta` | `{provider, model, account_id}` | 首轮开头一次（turn 1 的 `sbx.turn_started` 之前）；不含凭证 |
| `sbx.turn_started` | `{n}` | 第 n 轮开始，写在 CLI 输出之前 |
| `sbx.turn_finished` | `{status,exit_code,duration_s,usage}` | 第 n 轮结束（成功或失败） |
| `sbx.error` | `{message}` | runner 层异常（坏 JSON、超时收尾失败等） |

`sbx.turn_finished.status`：`success` / `codex_error` / `timeout` / `bad_json` / `auth_invalid`，与 CLI 退出码 0 / 2 / 3 / 4 / 5 对应（`codex_error` 是 v1 沿用名，含义为「provider CLI 非 0 退出」）。

## 非 Codex provider 的翻译规则

adapter `translate(raw_line) -> list[dict]` 把一行原生输出映射为 0..n 条 canonical 事件：

1. 首个原生会话标记（agy `init`/`step_update.conversation_id`、grok `end.sessionId`、opencode 每行 `sessionID`、devin 会话标记）→ 首个 `thread.started{thread_id}`。
2. 原生 assistant 文本 → `item.completed{type: agent_message, text}`；思考 → `item.completed{type: reasoning, text}`；命令/工具调用 → `command_execution`（`command`/`aggregated_output`/`exit_code`）；文件写 → `file_change`。
3. 原生终态/结果事件 → `turn.completed{usage}`（成功）或 `turn.failed{error:{message}}`；原生致命错误 → `error{message}`。
4. usage 字段映射：`cache_read_tokens → cached_input_tokens`、`thinking_tokens → reasoning_output_tokens`、`cache_write_tokens → cache_write_input_tokens`；`input_tokens`/`output_tokens` 同名；缺失字段填 **0**。
5. `translate` 不得吞掉无法识别的行：返回 `[]` 由 runner 计坏行；原生行始终先进 `events.raw.jsonl`。

## 来源

本文件事件形状以 **Codex CLI 0.153.0**（npm 包 `@openai/codex@0.153.0`，`codex-cli 0.153.0`）为准；其他 provider 的原生形状以 `tests/fixtures/events/<provider>/*.jsonl` 为准（WP0 手写，SOR-60 Spike 录制脱敏样本合入后以录制为准）。

1. **本机 CLI help**（WP0 环境安装 `npm i -g @openai/codex@0.153.0` 后执行，未登录）：
   - `codex exec --help`：`Run Codex non-interactively`；`--json` 释义为 `Print events to stdout as JSONL`；**If stdin is piped and a prompt is also provided, stdin is appended as a `<stdin>` block**（stdin 为打开管道时会等 EOF，见 `runner-cli.md` 进程约束）。
   - `codex exec resume --help`：`Usage: codex exec resume [OPTIONS] [SESSION_ID] [PROMPT]`。
2. **官方文档** [Command line options – Codex CLI](https://developers.openai.com/codex/cli/reference)。
3. **0.153.0 源码**：
   - [`codex-rs/exec/src/exec_events.rs` @ `rust-v0.153.0`](https://github.com/openai/codex/blob/rust-v0.153.0/codex-rs/exec/src/exec_events.rs)：`ThreadEvent` 含 `turn.failed`；`Usage` 含五字段；`ThreadItemDetails` 含 `Error`（item 级非致命错误）。
4. **P0 Spike 实测**（Codex 0.153.0，`gpt-5.6-luna`，Modal Sandbox，脱敏样本）：`tests/fixtures/events/real_multiturn.jsonl`。观测到的时序、UUIDv7 `thread_id`、resume 同 id、`/bin/bash -lc`、usage 五字段、item 级 `error` 以该录制为准。

```canonical-yaml
codex_events:
  - thread.started
  - turn.started
  - item.started
  - item.updated
  - item.completed
  - turn.completed
  - turn.failed
  - error
item_types:
  - agent_message
  - command_execution
  - file_change
  - reasoning
  - error
runner_events:
  - sbx.turn_started
  - sbx.turn_finished
  - sbx.error
  - sbx.session_meta
usage_fields:
  - input_tokens
  - cached_input_tokens
  - output_tokens
usage_fields_optional:
  - cache_write_input_tokens
  - reasoning_output_tokens
usage_mapping:
  cache_read_tokens: cached_input_tokens
  thinking_tokens: reasoning_output_tokens
  cache_write_tokens: cache_write_input_tokens
providers:
  - codex
  - antigravity
  - grok
  - opencode
  - devin
exit_codes:
  "0": success
  "2": cli_nonzero
  "3": timeout
  "4": bad_json
  "5": auth_invalid
error_codes:
  - 400
  - 401
  - 404
  - 409
  - 429
error_subcodes:
  - unauthorized
  - not_found
  - invalid_provider
  - turn_in_progress
  - session_not_runnable
  - account_busy
  - account_unavailable
  - provider_exhausted
  - concurrency_limit
paths:
  - inbox/<n>.md
  - turns/<n>.json
  - events.jsonl
  - events.raw.jsonl
  - session.json
```
