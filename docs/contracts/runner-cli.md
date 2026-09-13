# runner CLI

Sandbox 内驱动 Codex 会话的进程入口。可执行文件名约定为 `runner`（测试假件：`tests/fakes/stub_runner.py`）。工作目录语义见 `filesystem.md`；事件见 `events.md`。行为必须与 Linear `SOR-30` 一致。

环境：`SBX_WORK`（生产 `/work`）、`CODEX_HOME=$SBX_WORK/.codex`、`CODEX_BIN`（默认 `codex`，测试指向 `tests/fakes/fake_codex.py`）。

## 命令

### `runner init --auth auth_json|provider --model M`

1. 创建 `$CODEX_HOME`，写入 `config.toml`（最小内容）：

   ```toml
   model = "<M>"
   approval_policy = "never"
   sandbox_mode = "danger-full-access"

   [shell_environment_policy]
   exclude = ["CODEX_AUTH_JSON"]
   ```

   凭证经环境变量注入后写入 `$CODEX_HOME/auth.json`，随后 **不** 让 Codex 子进程继承 `CODEX_AUTH_JSON`。
2. 按 `--auth` 安装凭证（**不得**把真实 token 写入仓库或日志）：
   - `auth_json`：将 `$SBX_WORK/auth.json`（若存在）复制到 `$CODEX_HOME/auth.json`，或写入占位文件；所有 token 字段必须是 `REDACTED`；文件权限 **600**
   - `provider`：写入 provider 登录占位（token 字段同样 `REDACTED`），权限同样 600，不发起网络登录
3. 写入 `$SBX_WORK/AGENTS.md`（sandbox 内说明，不是仓库根 `AGENTS.md`）
4. 初始化空的 `events.jsonl`、`session.json`（`codex_session_id` 空、`turn` 0）、`inbox/`、`turns/`

### `runner turn --n N --message-file F [--max-seconds S]`

- 默认 `--max-seconds` **900**。
- 将 `F` 复制为 `$SBX_WORK/inbox/<N>.md`。`$PROMPT` 取该文件全文。
- 第 1 轮调用（**stdin 关闭**，prompt 仅为位置参数，不用 `-`）：

  ```
  $CODEX_BIN exec --json --skip-git-repo-check -C $SBX_WORK \
    --dangerously-bypass-approvals-and-sandbox -m <model> \
    "$PROMPT"
  ```

- 第 2 轮及以后：

  ```
  $CODEX_BIN exec resume --json --skip-git-repo-check -C $SBX_WORK \
    --dangerously-bypass-approvals-and-sandbox <session_id> \
    "$PROMPT"
  ```

  `session_id` 来自 `session.json.codex_session_id`（即首轮 `thread.started.thread_id`）。后续轮 `thread.started.thread_id` 与首轮相同。
- Codex stdout **逐行追加**到 `$SBX_WORK/events.jsonl`，同时写 runner 自己的 stdout。以 `\n` 为界切行；无法解析的行计为坏行并继续，最终退出码 4。
- 在 Codex 输出前后插入 `sbx.turn_started` / `sbx.turn_finished{status,exit_code,duration_s,usage}`；异常插入 `sbx.error`。
- 解析 `thread.started`、`turn.completed.usage`、最终 `agent_message`，写入 `turns/<N>.json`，更新 `session.json`（`codex_session_id`、`turn`）。
- 软超时：到达 `S` 秒后对 Codex 进程 **SIGTERM**，再等 **30 s** 收尾；仍未退出则 SIGKILL。超时退出码 3。

### `runner stop`

终止当前正在进行的 `turn`（对 Codex 子进程 SIGTERM → 宽限 → SIGKILL），不删除 `$SBX_WORK`。

## 进程约束

1. **必须关闭 stdin**。Codex 在 stdin 为打开的管道时会等待 EOF 后才继续（help 原文：*If stdin is piped and a prompt is also provided, stdin is appended as a `<stdin>` block*；P0 在 Sandbox 内 25 s 超时复现挂死）。runner 启动 Codex 时：
   - Python：`stdin=subprocess.DEVNULL`
   - shell：`</dev/null`
2. **prompt 只通过位置参数传递，不使用 `-`。**
3. **stdout 按行消费**。一个 chunk 可能含多行 JSON；以 `\n` 切分后再 `json.loads`。控制面子进程见 `control/backend.py`（`Process.stdout` 保证按行产出；Modal 实现必须 `sb.exec(..., bufsize=1)`）。

## 退出码

| 码 | 含义 |
| --- | --- |
| 0 | 成功 |
| 2 | Codex 进程非 0 |
| 3 | 超时 |
| 4 | 事件流中出现无法解析的非 JSON 行（坏 JSON） |

（无退出码 1 的契约语义；假件内部错误可用 1，但不作为跨包约定。）

```canonical-yaml
commands:
  - init
  - turn
  - stop
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
keepalives_s: 15
sse:
  id: events.jsonl line number
  event: type
  data: json
  keepalive: ": keepalive"
```
