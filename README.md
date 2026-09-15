# sbx-browser

Cursor Cloud Agent 式的 Codex 云端会话：网页新建会话 → 一台按会话创建的 Modal Sandbox 内运行 Codex CLI，多轮对话 → 空闲自动回收。浏览器 + noVNC 实时画面作为 P2 叠加。

## 任务与设计的唯一来源

所有需求、设计、任务拆分与状态都在 Linear 项目 **sbx-browser**（团队 Sorenforge，Issue 前缀 `SOR-`）：

- 项目概述与「已决定」表：Linear 项目描述
- 设计方案 v1（顶部有 v1.1 范围收敛说明）：Linear 项目文档
- 开发任务分配与并发计划 v1：Linear 项目文档
- 当前起点：P1 `SOR-39` WP0 已合入。P2 进行中：`SOR-59` WP0 冻结 v2 契约（五 provider 多账号）与跨包 Protocol 壳；P0 spike（`SOR-28`）结论见 `spike/README.md`。

仓库内的 `AGENTS.md`、`docs/contracts/` 是代码层面的规范来源。

## P2 概览（多 provider / 多账号）

P2 把单 Codex 会话扩展为 Cursor Cloud Agent 式的多 provider 平台：

- **Provider 集合**：`codex` / `antigravity` / `grok` / `opencode` / `devin`，由 `runtime/runner/adapter.py` 的 `AgentAdapter` Protocol + 注册表统一驱动；Codex 行为保持不变，其余 provider 的 Adapter 在 `SOR-62` / `SOR-72` 实现。
- **多账号凭证**：每个 provider 可挂多个账号；凭证以 blob `{provider, files:{relpath: content}}` 经 `SBX_ACCOUNT_CREDENTIAL` 注入，还原到沙箱 `HOME=$SBX_WORK/home`（权限 600）。`runner export-credentials` 把可能刷新的凭证以同一 blob 形式写回控制面。
- **账号注册与调度**：`control/ports.py` 定义 `AccountRegistry` / `Scheduler` / `ApiKeyStore` / `SessionService` Protocol 与 `Account` / `ApiKey` / `ScheduleDecision` 数据类；`SOR-63` 实现（`modal.Dict` + 每账号 Secret），支持 `account_id` 指定或 `"auto"` LRU 选择。
- **API 两层**：内部 `/api/*`（HTTP Basic，`docs/contracts/api.yaml`）供 web 看板；公开 `/v1/*`（Bearer `sbx_<key>`，`docs/contracts/api-v1.yaml`，`agent ≙ session`、`run ≙ turn`）由 `control/api_v1/`（`SOR-64`）实现。
- **事件**：CLI 原生行原样落 `events.raw.jsonl`，Adapter 翻译为 canonical 事件写 `events.jsonl`；新增 `sbx.session_meta{provider, model, account_id}`，非 Codex usage 映射见 `docs/contracts/events.md`。
- **测试隔离**：`tests/conftest.py` 剥离宿主凭证并隔离 HOME/XDG；`LocalProcessBackend.exec` 只继承白名单（`PATH` `HOME` `LANG`）+ `SandboxSpec.env` + 显式 `env=`；`tests/fakes/` 提供五个 provider 的可执行假件与 `tests/fakes/fake_ports.py` 内存版端口实现。

## 架构（P1 MVP）

```
 你（浏览器）                              Modal
 ┌──────────────┐  HTTPS + Basic Auth   ┌──────────────────────────────────────────┐
 │ web/ 聊天页  │ ────────────────────▶ │ control/ sbx-control（FastAPI，可缩容到 0） │
 │  会话列表    │                       │  会话 API · modal.Dict 状态机 · SSE        │
 │  流式事件    │                       │  reaper 只做 Dict 对账                      │
 └──────────────┘                       └────────────┬─────────────────────────────┘
                                                     │ Sandbox.create(
                                                     │   image=sbx-runtime,
                                                     │   idle_timeout, timeout,
                                                     │   cpu, memory, workdir,
                                                     │   tags, secrets)
                                                     ▼
                                        ┌── Sandbox（一会话一台，常驻多轮）─────────┐
                                        │ entrypoint.sh                            │
                                        │   $SBX_WORK/{inbox,turns,.codex}         │
                                        │   exec sleep infinity（或控制面传入的命令）│
                                        │ 控制面 sb.exec → runner（WP1-B）         │
                                        │   codex exec --json / resume …           │
                                        │ Secret: CODEX_AUTH_JSON（写入 .codex/）  │
                                        └──────────────────────────────────────────┘
```

核心不变量：

1. **一台会话 = 一台 Sandbox**；空闲回收用 `Sandbox.create(idle_timeout=…)` 原生能力（P0 实测 90 s 窗口约 96 s 后自行结束），`timeout` 作硬兜底。
2. **Sandbox 是唯一安全边界**：容器内没有 Modal token，也没有 GitHub / 云厂商凭证。
3. **镜像不含 Chrome**（P2 再加 `sbx-runtime-browser`）。硬件与生命周期参数不写进镜像，由控制面传入。

## 镜像 `sbx-runtime`

配方与 P0 实测同源（构建约 52 s，冷启动约 3.5 s）：`Image.debian_slim(python_version="3.12")` + apt（`curl git ca-certificates ripgrep jq procps build-essential python3-pip`）+ NodeSource Node 22 + `npm i -g @openai/codex@0.153.0`。

包列表的唯一来源是 `runtime/packages.txt`。`runtime/image.py` 读取它构造 Modal Image；`Dockerfile.local` 由同一文件生成，供无云 `docker build` 验证。

```bash
make image        # python -m runtime.image → 构建并 publish 命名镜像 sbx-runtime（需要 Modal 凭证）
make image-devin  # python -m runtime.image --devin → publish sbx-runtime-devin（SOR-74）
make deploy       # 占位调用 control 的 deploy；WP1-C 未合入时打印提示
make test         # pytest unit + integration；禁止连 Modal、禁止云凭证
make lint
```

无云验证：`docker build -f Dockerfile.local` 后断言 `codex --version` 为 `codex-cli 0.153.0`、`node --version` 为 v22、entrypoint 收到 SIGTERM 后 5 s 内退出（见 `tests/integration/runtime/test_image_local.py`）。真实 `modal run` / `sb.exec("codex --version")` 由编排者在 WSL 执行。

### Devin-only 快速通道（SOR-74）

命名镜像 **`sbx-runtime-devin`**：`sbx-runtime` 之上叠加 pin 的 Devin CLI standalone bundle（`3000.10.21`，sha256 校验，见 `runtime/install-devin.sh` 与 `packages.txt` 的 `devin_*` 键），镜像 env 把 `HOME`/XDG 固定到 `$SBX_WORK/home`。`Dockerfile.devin.local` 是同一配方的本地生成物。

- `provider=devin` 的 `SandboxSpec`（`tags["provider"]="devin"` + `secrets=["sbx-acct-<id>"]`）让 `ModalBackend` 选择该镜像与每账号凭证 Secret；Codex 默认路径不变。
- `runner init --provider devin` 把 `SBX_ACCOUNT_CREDENTIAL` blob 还原到 `$SBX_WORK/home` 下（权限 600），如 `home/.local/share/devin/credentials.toml`；`provider` 不匹配则 init 失败。
- 子进程环境剔除 `ACP_BACKEND` 与 `DEVIN_API_KEY` / `DEVIN_V3_API_KEY` / `DEVIN_LEGACY_API_KEY` / `DEVIN_ORG_ID`（`runtime/runner/credentials.py` + `entrypoint.sh`），auth 只来自凭证 blob，不依赖 Devin Desktop。

## 为什么用 `--dangerously-bypass-approvals-and-sandbox`

官方把该开关标注为「只在外部已经加固的环境使用」。本项目的安全边界是 **Modal Sandbox**，不是 Codex 自带的 Landlock / seccomp 沙箱：后者在 gVisor 下往往不可用。Sandbox 内只有会话工作目录和注入的 Codex 凭证，没有平台密钥；审批策略由 runner 写成 `approval_policy = "never"` + `sandbox_mode = "danger-full-access"`（见 `docs/contracts/filesystem.md`）。因此 CLI 使用该 flag（P0 已在 0.153.0 上验证 `codex exec` / `exec resume`）。

配套约束（P0 实测，runner / 控制面必须遵守，镜像不处理）：

- 调用 `codex exec` 必须关闭 stdin（`</dev/null` 或 `stdin=DEVNULL`），否则会等到 EOF 挂死。
- 控制面 `sb.exec(..., bufsize=1)`，按行消费 JSONL。

## Modal Secret 初始化

在**本机可信环境**创建 Secret，不要把凭证写入仓库、fixture、日志、PR 或 Linear。

### `CODEX_AUTH_JSON`（ChatGPT 订阅 `auth.json`）

1. 在可信机器上执行 `codex login`（ChatGPT 账号），得到 `~/.codex/auth.json`。
2. 把 JSON **作为 Secret 的值**交给 Modal（不要提交该文件）：

   ```bash
   modal secret create sbx-codex-auth \
     CODEX_AUTH_JSON="$(cat ~/.codex/auth.json)"
   ```

   控制面创建 Sandbox 时传入 `secrets=[…]`，runner 写入 `$CODEX_HOME/auth.json`（权限 600）。镜像与 entrypoint **不**内置任何 auth。
3. 同一份 `auth.json` 可被少量 Sandbox 并发使用（P0：2 台同时 rc=0）；MVP 并发上限为 2。token 刷新后若文件被回写，按「有则回写」处理（WP1-B / WP1-C）。

MVP 默认认证模式是 `auth_json`。第三方 Responses 网关（DisTokens 等）记为 P3，不在本镜像接入。

### HTTP Basic Auth（控制面看板）

```bash
modal secret create sbx-basic-auth \
  SBX_BASIC_USER='<username>' \
  SBX_BASIC_PASSWORD='<long-random-password>'
```

所有 `/api/sessions*` 端点走 HTTP Basic（见 `docs/contracts/api.yaml`）。本地假服务口令只用于 mock，不是生产凭证。

## 部署 bootstrap（SOR-98 / Release 0.1）

`sbx` CLI 把干净检出带到可调用的 `/v1`：单一配置源（`config.toml` + env 覆盖）管理 Modal profile / app / durable Dict / Secret / 镜像 pin / base URL；bootstrap `sbx_` key 明文只落本地 0600 文件，控制面仅存 sha256。

```bash
uv run sbx init --profile <modal-profile>   # 检查工具链，写 config
uv run sbx deploy                            # 幂等：Secrets → Dicts → 镜像 → app → /v1 探活
uv run sbx doctor                            # 全链路验证（绝不打印 secret 值）
uv run sbx smoke                             # 最小 agent → terminal → 清理
uv run sbx upgrade                           # 重部署，durable stores 不丢
uv run sbx uninstall                         # 停 app + 清空 sbx sandbox（默认保留凭证/数据）
```

详见 `docs/bootstrap.md`；命令输出可直接给 `examples/sbx_client.py` 用（`SBX_BASE_URL` + `SBX_API_KEY`）。

## 分层（P1 MVP）

```
web/      单页聊天看板（无构建步骤）
control/  sbx-control：FastAPI on Modal，会话 API / modal.Dict 状态机 / SSE / reaper
runtime/  Sandbox 内：镜像定义、entrypoint、runner（Codex 会话驱动）
sbx/      部署 bootstrap CLI（init/config/status/deploy/doctor/smoke/upgrade/uninstall）
tests/    unit / integration / e2e / fakes / fixtures —— 全部不依赖云凭证
spike/    P0 验证脚本（WSL 本机执行）
```
