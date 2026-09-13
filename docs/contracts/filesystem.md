# 文件系统布局

生产环境工作根目录为 `/work`（`SBX_WORK=/work`）。测试使用临时目录，并由 `LocalProcessBackend.create` 或测试夹具注入 `SBX_WORK`。

`CODEX_HOME` 固定为 `$SBX_WORK/.codex`（runner `init` 写入 `config.toml` 与脱敏后的 `auth.json`）。

## 布局

```
$SBX_WORK/
  inbox/<n>.md          # 第 n 轮用户消息（runner turn 从 --message-file 复制）
  turns/<n>.json        # 第 n 轮结果（键见下方；message 允许空串）
  events.jsonl          # 追加写入的 JSONL 事件流（Codex 原生 + sbx.*）
  session.json          # 至少 codex_session_id、turn；允许额外字段
  runner.pid            # 当前 turn 的 runner PID；无运行中的轮则不存在
  AGENTS.md             # runner init 生成的 sandbox 内说明
  .codex/               # CODEX_HOME
    config.toml
    auth.json           # token 字段必须为 REDACTED（测试 / fixture）；权限 600
    sessions/YYYY/MM/DD/rollout-<ts>-<thread_id>.jsonl
                        # Codex 自身会话落盘；thread_id = session.json.codex_session_id
```

`events.jsonl` 的行号从 1 起算，与会话 API SSE 的 `id:` 帧字段一致。

`session.json` **至少**含 `codex_session_id`、`turn`，允许扩展字段。`codex_session_id` 即首轮 `thread.started.thread_id`（UUIDv7）。后续 `codex exec resume` 与 Codex rollout 文件名都用这个 id。

`turns/<n>.json` 键冻结：`n`、`codex_session_id`、`status`、`usage`、`message`、`exit_code`。无最终 `agent_message` 时 `message` 为 `""`。

`runner.pid` 供独立进程 `runner stop` 定位当前 turn；不是 `session.json` 的字段。

### `$CODEX_HOME/config.toml`

由 `runner init` 生成，最小内容：

```toml
model = "<M>"
approval_policy = "never"
sandbox_mode = "danger-full-access"

[shell_environment_policy]
exclude = ["CODEX_AUTH_JSON"]
```

凭证通过环境变量注入后写入 `auth.json`，随后不让 Codex 子进程继承 `CODEX_AUTH_JSON`。`auth.json` 权限 **600**。

```canonical-yaml
paths:
  - inbox/<n>.md
  - turns/<n>.json
  - events.jsonl
  - session.json
codex_home: $SBX_WORK/.codex
work_env: SBX_WORK
production_work: /work
session_json_fields:
  - codex_session_id
  - turn
session_json_additional_keys_allowed: true
pid_file: runner.pid
turn_json_fields:
  - n
  - codex_session_id
  - status
  - usage
  - message
  - exit_code
codex_rollout: $CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<thread_id>.jsonl
auth_json_mode: "600"
config_toml:
  approval_policy: never
  sandbox_mode: danger-full-access
shell_environment_policy_exclude:
  - CODEX_AUTH_JSON
```
