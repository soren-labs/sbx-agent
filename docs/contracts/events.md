# 事件流

`$SBX_WORK/events.jsonl`：**每行一个 JSON 对象**（JSONL）。runner 把 Codex 0.153.0 `codex exec --json` 的原生事件**原样透传**，并在每轮前后插入 runner 自身事件。

控制面 SSE（`GET /api/sessions/{id}/events`）按行号重放同一流，见 `api.yaml`。

## Codex 0.153.0 原生事件（透传）

| `type` | 形状 | 说明 |
| --- | --- | --- |
| `thread.started` | `{thread_id}` | 线程/会话 id，供 `codex exec resume` |
| `turn.started` | `{}` | 一轮推理开始 |
| `item.started` | `{item}` | item 进入 in-progress |
| `item.updated` | `{item}` | item 增量更新 |
| `item.completed` | `{item}` | item 到达终态 |
| `turn.completed` | `{usage:{input_tokens,cached_input_tokens,output_tokens}}` | 成功结束；usage 三字段为契约必选 |
| `error` | `{message}` | 流上的不可恢复错误 |

`item` 使用 Codex 0.153.0 扁平标签：`{id, type, ...payload}`。契约要求覆盖的 `item.type`：

- `agent_message`（`text`）
- `command_execution`（`command`, `aggregated_output`, `status`, 可选 `exit_code`）
- `file_change`（`changes[{path,kind}]`, `status`）
- `reasoning`（`text`）

其它 item 类型（如 `mcp_tool_call`）若出现则原样透传，解析器必须忽略未知字段。

0.153.0 源码另有 `turn.failed`。§0 表格未列入；runner **透传但不作为必选集合**（见文末契约变更请求说明）。`usage` 在 0.153.0 还可能带 `cache_write_input_tokens` / `reasoning_output_tokens`，同样透传。

## runner 自身事件

| `type` | 形状 | 说明 |
| --- | --- | --- |
| `sbx.turn_started` | `{n}` | 第 n 轮开始，写在 Codex 输出之前 |
| `sbx.turn_finished` | `{status,exit_code,duration_s,usage}` | 第 n 轮结束（成功或失败） |
| `sbx.error` | `{message}` | runner 层异常（坏 JSON、超时收尾失败等） |

`sbx.turn_finished.status`：`success` / `codex_error` / `timeout` / `bad_json`，与 CLI 退出码 0 / 2 / 3 / 4 对应。

## 来源

本文件事件形状以 **Codex CLI 0.153.0**（npm 包 `@openai/codex@0.153.0`，`codex-cli 0.153.0`）为准，不登录、不调用会触发鉴权的子命令。

1. **本机 CLI help**（WP0 环境安装 `npm i -g @openai/codex@0.153.0` 后执行，未登录）：
   - `codex exec --help`：`Run Codex non-interactively`；`--json` 释义为 `Print events to stdout as JSONL`；全局/exec 选项含 `--skip-git-repo-check`、`-C/--cd`、`--dangerously-bypass-approvals-and-sandbox`、`-m/--model`；子命令 `resume` / `fork` / `review`。
   - `codex exec resume --help`：`Usage: codex exec resume [OPTIONS] [SESSION_ID] [PROMPT]`；`[PROMPT]` 为 `-` 时从 stdin 读；`--json` 同样可用。
2. **官方文档** [Command line options – Codex CLI](https://developers.openai.com/codex/cli/reference)：`codex exec` 将 `--json` / `--experimental-json` 定义为 newline-delimited JSON events（one per state change）；`resume` 为 `codex exec resume [SESSION_ID]`。文档**没有**列出各事件的 `type` 字段名。
3. **0.153.0 源码（事件形状的权威）**：
   - [`codex-rs/exec/src/exec_events.rs` @ `rust-v0.153.0`](https://github.com/openai/codex/blob/rust-v0.153.0/codex-rs/exec/src/exec_events.rs)：`ThreadEvent` 的 serde tag 为 `thread.started` / `turn.started` / `turn.completed` / `turn.failed` / `item.started` / `item.updated` / `item.completed` / `error`；`TurnCompletedEvent.usage` 含 `input_tokens` / `cached_input_tokens` / `output_tokens`。
   - [`sdk/typescript/src/events.ts` 与 `items.ts` @ `rust-v0.153.0`](https://github.com/openai/codex/blob/rust-v0.153.0/sdk/typescript/src/events.ts)：item 判别字段为 `type`（`agent_message` / `command_execution` / `file_change` / `reasoning` 等），**不是**早期 `--experimental-json` 示例里的 `item_type` / `assistant_message`。

未登录故无法对真实模型跑 `codex exec --json` 采样；假件 `tests/fakes/fake_codex.py` 与 `tests/fixtures/events/*.jsonl` 按上述源码形状生成。

```canonical-yaml
codex_events:
  - thread.started
  - turn.started
  - item.started
  - item.updated
  - item.completed
  - turn.completed
  - error
item_types:
  - agent_message
  - command_execution
  - file_change
  - reasoning
runner_events:
  - sbx.turn_started
  - sbx.turn_finished
  - sbx.error
usage_fields:
  - input_tokens
  - cached_input_tokens
  - output_tokens
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
