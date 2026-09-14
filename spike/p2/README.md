# P2.1-S0 Spike 结论（SOR-73）— 2026-09-14 实测

Devin CLI 独立认证 / 并发 + Modal 干净室凭证可移植性验证。
环境：`devin 3000.10.21`（static-pie ELF，版本目录 `~/.local/share/devin/cli/_versions/3000.10.21`）、
Modal SDK 1.5.5、模型 `swe-2-high`（Free 档）。凭证值全程未打印；仅以 sha256 前 16 位比对。
所有 Sandbox 已 terminate，`Sandbox.list` 无残留。

## 结果表

| # | 验证项 | 结果 | 实测数据 |
| -- | -- | -- | -- |
| 1 | 全新 HOME/XDG 最小凭证集 | **PASS** | 仅 `$XDG_DATA_HOME/devin/credentials.toml`（未设 XDG 时为 `~/.local/share/devin/credentials.toml`）**一个文件**，env 全剥离（无 `DEVIN_*`/`WINDSURF_*`/`ACP_BACKEND`）即 `auth status` = "Logged in (via Devin)"，Tier=Devin Pro。**不需要** `config.json`/`installation_id`/`sessions.db` |
| 2 | `ACP_BACKEND=windsurf` 泄漏 | **危险确认** | 同一 credentials.toml、同一 HOME，仅多设 `ACP_BACKEND=windsurf` → `auth status` 报 **"Not logged in"**。本机与 Modal Sandbox 内均复现。**runner 必须从 devin 子进程 env 剔除 `ACP_BACKEND`**（及 `WINDSURF_API_KEY`、`DEVIN_API_KEY` 等认证变量，避免意外走 env 通道而非凭证文件） |
| 3 | XDG 语义 | **确认** | `XDG_DATA_HOME` 被尊重：设了读 `$XDG_DATA_HOME/devin/credentials.toml`，否则回落 `$HOME/.local/share/devin/`。契约/实现应统一按 XDG 规则写文件 |
| 4 | Modal 干净室（Secret→文件注入） | **PASS** | `Secret.from_dict({"SBX_CRED_B64": base64})` → sandbox 内 `base64 -d` 写 `$HOME/.local/share/devin/credentials.toml`（chmod 600）。sandbox env 探针确认 `ACP_BACKEND`/`DEVIN_API_KEY`/`WINDSURF_API_KEY` 均 `<unset>`；`auth status` Devin Pro；`devin -p` rc=0 输出 PONG |
| 5 | 并发（同一账号、独立 Sandbox） | **PASS** | 2 并发 wall 34–52 s；4 并发 wall 102 s（单 sandbox smoke 8.7–77 s）；**8 并发 wall 108 s**（smoke 12–96 s），全部 `auth_logged_in + smoke_pong + rc=0`，无限流/冲突。本地同账号 2/4/8/16 进程此前亦全过（8 ≈ 28 s、16 ≈ 60 s） |
| 6 | 凭证是否被 CLI 重写/刷新 | **未观察到重写** | 所有 sandbox 与本地 fresh-HOME 探针 `sha256(credentials.toml)` 前后一致（16 位前缀 `c1adb8b29542606f`）。短期会话内 CLI 不回写凭证文件 → `runner export-credentials` 对 devin 的默认行为可为「hash 相同则输出空」 |
| 7 | CLI 副作用文件 | 记录 | 首次运行自动创建 `.config/devin/config.json`（org/theme，非认证必需）、`.local/share/devin/cli/{installation_id,sessions.db,logs/,plugins/,session_locks/}`、`.cache/devin/cli/*`；managed plugins 会联网拉取（首次 smoke 较慢的主因之一） |

## 对契约 / runner 的变更请求（冻结文件，未自行修改）

1. **`docs/contracts/filesystem.md` §布局**：devin provider 凭证路径由
   `home/.config/devin/` 更正为 **`home/.local/share/devin/credentials.toml`**
   （遵循 XDG：设 `XDG_DATA_HOME` 时为 `$XDG_DATA_HOME/devin/credentials.toml`）。
   `.config/devin/config.json` 由 CLI 自建、非认证必需；`export-credentials`
   的 devin `credential_files` 建议只含 credentials.toml（必要时含 config.json）。
2. **`docs/contracts/runner-cli.md` §进程约束**：devin provider 启动子进程前必须
   **从 env 剔除 `ACP_BACKEND`**（实测 `ACP_BACKEND=windsurf` 使合法 credentials.toml
   被判 "Not logged in"，本机 + Modal 均复现）；同批剔除 `WINDSURF_API_KEY`、
   `DEVIN_API_KEY`/`DEVIN_V3_API_KEY`/`DEVIN_LEGACY_API_KEY`/`DEVIN_ORG_ID`，
   保证认证只走凭证文件通道。
3. **devin argv**（adapter 依据）：`devin -p "<PROMPT>" --model <M>
   --respect-workspace-trust false </dev/null`。注意 `-p` 的可选参数必须紧跟 flag
   （或 `devin -p -- <PROMPT>`）；把 prompt 放在其它 flag 之后会被当 `[PATH]` 解析并报错。
   stdin 关闭要求与 codex 一致。Devin CLI 无文档化 JSON 事件流，`fake_devin.py` 的
   NDJSON 仍是 WP0 占位，adapter 事件翻译需另行实测（建议列为后续子 Issue）。

## 复现

```bash
bash spike/p2/fresh_home_probe.sh                      # 本地 fresh-HOME 探针
uv run python spike/p2/modal_devin_spike.py \
    --concurrency 2,4,8 --model swe-2-high             # Modal 干净室 + 并发
```

- `fresh_home_probe.sh`：mktemp 全新 HOME → 只拷 credentials.toml → `env -i`
  跑 auth status / ACP_BACKEND 负例 / `-p` smoke → hash 前后比对 →
  `out/fresh_home.json`。
- `modal_devin_spike.py`：镜像 `debian_slim + add_local_dir(…/_versions/3000.10.21,
  copy=True)`（版本钉死，~174 MB 一次性构建缓存）；每 sandbox `Secret.from_dict`
  注入 b64 凭证 → 物化 chmod 600 → auth status / ACP 负例（idx 0）/ `-p` smoke
  （`env -u SBX_CRED_B64` 剔除注入变量，呼应 runner 契约）→ hash 比对 →
  `out/modal_devin.json`（多次运行合并）。身份字段（email/user/org id）落盘前已脱敏。

## 已知限制

- 凭证刷新窗口未覆盖：会话短（≤ 2 min），未观察到 token refresh 回写；
  长会话（> token TTL）是否回写待 SOR-62/72 实现期再验。
- Modal 镜像体积 174 MB（整目录拷贝）。生产可裁剪为 `bin/devin` + 按需 `share/`；
  spike 选择整目录以保证与本机行为一致。
- `swe-2-high` 为 Free 档、session credit=0/ACU=0.0（与既有发现一致）；并发上限
  仅验证到 8，未触碰账号侧限流阈值。
