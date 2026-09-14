# 文件系统布局

生产环境工作根目录为 `/work`（`SBX_WORK=/work`）。测试使用临时目录，并由 `LocalProcessBackend.create` 或测试夹具注入 `SBX_WORK`。

Sandbox 内 `HOME` 固定为 `$SBX_WORK/home`（凭证 blob 还原的目标根）。`CODEX_HOME` 默认为 `$HOME/.codex`（runner `init` 写入 `config.toml` 与脱敏后的 `auth.json`）；`CODEX_HOME` 环境变量可显式覆盖——v1 控制面继续传 `$SBX_WORK/.codex` 时仍然有效。

## 布局

```
$SBX_WORK/
  inbox/<n>.md          # 第 n 轮用户消息（runner turn 从 --message-file 复制）
  turns/<n>.json        # 第 n 轮结果（session id、usage、最终消息、退出码）
  events.jsonl          # 追加写入的 JSONL 规范事件流（canonical，Codex 形状）
  events.raw.jsonl      # CLI 原生 stdout 行原样追加（排障用；codex 下与 events.jsonl 同形）
  session.json          # native_session_id、provider、account_id、turn 计数
  AGENTS.md             # runner init 生成的 sandbox 内说明
  home/                 # $HOME：凭证 blob 还原到此目录下（权限 600）
    .codex/             # CODEX_HOME（codex provider）
      config.toml
      auth.json         # token 字段必须为 REDACTED（测试 / fixture）；权限 600
      sessions/YYYY/MM/DD/rollout-<ts>-<thread_id>.jsonl
                          # Codex 自身会话落盘；thread_id = session.json.native_session_id
    .grok/              # grok provider 凭证 / 会话目录（按需）
    .local/share/opencode/auth.json
                        # opencode provider 凭证（按需）
    .gemini/antigravity-cli/  # antigravity provider 凭证（按需）
    .config/devin/      # devin provider 凭证（按需）
```

`events.jsonl` 的行号从 1 起算，与会话 API SSE 的 `id:` 帧字段一致。

`session.json.native_session_id` 即首轮原生会话标记（Codex：`thread.started.thread_id`，UUIDv7）。后续 resume 与 Codex rollout 文件名都用这个 id。`codex_session_id` 保留为 `native_session_id` 的兼容别名（同值）。

`session.json` 字段：`turn`、`native_session_id`、`provider`、`account_id`（+ 别名 `codex_session_id`、runner 内部字段如 `pid` `model` `auth`）。

### `$CODEX_HOME/config.toml`

由 `runner init` 生成（codex provider），最小内容：

```toml
model = "<M>"
approval_policy = "never"
sandbox_mode = "danger-full-access"

[shell_environment_policy]
exclude = ["CODEX_AUTH_JSON", "SBX_PROVIDER_API_KEY", "SBX_ACCOUNT_CREDENTIAL"]
```

凭证通过环境变量注入后写入 `auth.json`，随后不让 Codex 子进程继承 `CODEX_AUTH_JSON` / `SBX_ACCOUNT_CREDENTIAL`。`auth.json` 权限 **600**。

```canonical-yaml
paths:
  - inbox/<n>.md
  - turns/<n>.json
  - events.jsonl
  - events.raw.jsonl
  - session.json
home: $SBX_WORK/home
codex_home: $HOME/.codex
codex_home_v1: $SBX_WORK/.codex
work_env: SBX_WORK
production_work: /work
credential_env: SBX_ACCOUNT_CREDENTIAL
account_id_env: SBX_ACCOUNT_ID
session_json_fields:
  - turn
  - native_session_id
  - provider
  - account_id
session_json_aliases:
  codex_session_id: native_session_id
codex_rollout: $CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<thread_id>.jsonl
auth_json_mode: "600"
config_toml:
  approval_policy: never
  sandbox_mode: danger-full-access
shell_environment_policy_exclude:
  - CODEX_AUTH_JSON
  - SBX_PROVIDER_API_KEY
  - SBX_ACCOUNT_CREDENTIAL
```
