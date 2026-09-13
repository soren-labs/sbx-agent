# 事件流

`$SBX_WORK/events.jsonl`：**每行一个 JSON 对象**（JSONL）。runner 把 Codex 0.153.0 `codex exec --json` 的原生事件**原样透传**，并在每轮前后插入 runner 自身事件。

控制面 SSE（`GET /api/sessions/{id}/events`）按行号重放同一流，见 `api.yaml`。

消费者以 `\n` 为界解析；一个读缓冲可能含多行 JSON（Modal `sb.exec` 默认 `bufsize=-1` 按 chunk 产出）。遇到无法解析的行计为坏行并继续处理后续行。

## Codex 0.153.0 原生事件（透传）

| `type` | 形状 | 说明 |
| --- | --- | --- |
| `thread.started` | `{thread_id}` | 线程/会话 id，供 `codex exec resume`。`thread_id` 为 UUIDv7。同一会话后续轮次的 `thread.started.thread_id` **与首轮完全相同**。 |
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
| `sbx.turn_started` | `{n}` | 第 n 轮开始，写在 Codex 输出之前 |
| `sbx.turn_finished` | `{n,status,exit_code,duration_s,usage}` | 第 n 轮结束（成功或失败）。`n` 与对应的 `sbx.turn_started` 相同，供 SSE 对账 |
| `sbx.error` | `{message}` | runner 层异常（坏 JSON、超时收尾失败等） |

`sbx.turn_finished.status`：`success` / `codex_error` / `timeout` / `bad_json`，与 CLI 退出码 0 / 2 / 3 / 4 对应。

## 来源

本文件事件形状以 **Codex CLI 0.153.0**（npm 包 `@openai/codex@0.153.0`，`codex-cli 0.153.0`）为准。

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
usage_fields:
  - input_tokens
  - cached_input_tokens
  - output_tokens
usage_fields_optional:
  - cache_write_input_tokens
  - reasoning_output_tokens
exit_codes:
  "0": success
  "2": codex_nonzero
  "3": timeout
  "4": bad_json
error_codes:
  - 401
  - 404
  - 409
  - 429
paths:
  - inbox/<n>.md
  - turns/<n>.json
  - events.jsonl
  - session.json
```
