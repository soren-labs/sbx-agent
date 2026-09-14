# AGENTS.md

本文件是仓库内 Agent 的规范来源。任务拆分与状态仍以 Linear 项目 **sbx-browser**（团队 Sorenforge，Issue 前缀 `SOR-`）为准。执行/评审协议：**P2 任务编排与 SWE 执行计划 v4**（独立评审通过即合并）。

## 0. 开工必读

1. 任务说明书是分配给你的 Linear Issue。开工前完整阅读它、「P2 任务编排与 SWE 执行计划 v4」§0 / §5，以及「设计方案 v2」中被该 Issue 引用的章节。
2. 只修改 Issue 中标明属于你的目录（§1 表）。WP0（`SOR-59`）是唯一一次允许触碰全仓库的工作包。
3. 契约文件合入后冻结（§2）。需要变更时在 **自己的 Issue 评论** 提出「契约变更请求」，**不自行修改契约**；契约技术变更由 SWE 提案、独立 SWE 审阅，编排者只负责顺序与合并。
4. 提 PR 前 `make lint` 与 `make test` 必须全绿，且不依赖任何云凭证。
5. 禁止把任何凭证、token、密码写入代码、fixture、日志、PR 或 Linear 评论。fixture 里的 token 字段一律用 `REDACTED` 占位。
6. v4 职责：作者（SWE）= 实现 + 自测 + PR 准备；独立评审（SWE，干净环境）= 对当前提交 PASS/退回；编排者 = 派发、状态跟踪、Linear 沟通、PASS 后机械合并。**编排者不实现、不评审、不测试**；旧 Cursor/Bugbot 与「编排者技术把关」规则不再适用。
7. 发现超出本包范围的缺陷：在本项目下新建子 Issue（父 = 当前 Issue），不顺手修。
8. 隔离要求：开发/测试使用独立 HOME/XDG 与剥离的凭证环境（见 `tests/conftest.py`）；不读取、不打印真实凭证；不使用其他 worktree。

## 1. 目录所有权（P2 / v4 §5）

| 所有者 | 路径 |
| --- | --- |
| `SOR-59`（WP0） | `docs/contracts/**`、`runtime/runner/adapter.py`（Protocol）、`control/backend.py`、`control/ports.py`、`control/api_v1/` 空壳、`tests/fakes/`、`tests/fixtures/`、`tests/conftest.py`、`AGENTS.md`、必要 CI/开发环境配置 |
| `SOR-60` | `spike/p2/**`；原始敏感样本留本地，脱敏样本交 WP0；不与其他包双写 fixtures |
| `SOR-61` | `runtime/image.py`、`runtime/packages.txt`、`runtime/entrypoint.sh`、`Dockerfile.local`、`Makefile`、README 镜像章节、镜像测试 |
| `SOR-62` / `SOR-72` | `runtime/runner/**`（冻结的 `adapter.py` Protocol 除外）、`tests/unit/runner/**`；同一作者 |
| `SOR-63` | `control/**`（除 `backend.py`、`ports.py`、`api_v1/**`、`auth_bearer.py`）、`tests/unit/control/**`、`tests/integration/control/**` |
| `SOR-64` | `control/api_v1/**`、`control/auth_bearer.py`、`examples/**`、`/v1` 路由与 API 单元测试 |
| `SOR-65` → `SOR-67` | 65 负责 `web/**` 与 `tests/e2e/**`；交接后 67 负责测试，避免双写 |
| `SOR-66` | `tests/integration/cloud_free` / `api_v1` 集成测试及必要接线；产品缺陷交回原包 |
| `SOR-68` | `tests/e2e_modal/**` 与验收产物 |
| `SOR-69` | 发布配置、README 发布说明、部署操作 |

注意：`control/auth_bearer.py` 属于 **D（SOR-64）**，不属于 C（SOR-63）。后续包不得修改冻结路径；假件只扩展场景，不改事件名 / 退出码 / 路径语义。

## 2. 契约冻结

以下文件是跨包接口，`SOR-59` 合入 `main` 后视为冻结：

- `docs/contracts/filesystem.md`
- `docs/contracts/events.md`
- `docs/contracts/runner-cli.md`
- `docs/contracts/api.yaml`（内部 `/api/*`，HTTP Basic）
- `docs/contracts/api-v1.yaml`（公开 `/v1/*`，Bearer `sbx_<key>`）
- `control/backend.py`（`SandboxBackend` / `LocalProcessBackend` / `ModalBackend` 骨架）
- `control/ports.py`（`AccountRegistry` / `Scheduler` / `ApiKeyStore` / `SessionService` Protocol + `Account` / `ApiKey` / `ScheduleDecision`）
- `runtime/runner/adapter.py`（`AgentAdapter` Protocol + `get_adapter` 注册表；`CodexAdapter` 行为不变）

解析这些契约时，provider 集合（`codex` / `antigravity` / `grok` / `opencode` / `devin`）、事件名、路径、退出码、错误码必须一致（见各文件的 `canonical-yaml` / `x-canonical` 块）。测试入口：`tests/unit/test_contract_consistency.py`。

## 3. 测试与本地环境

```bash
make lint      # ruff check + ruff format --check
make test      # pytest tests/unit tests/integration（禁止云凭证）
make test-e2e  # Playwright → mock_api 冒烟
```

本地环境变量：

| 变量 | 含义 |
| --- | --- |
| `SBX_WORK` | 工作目录。生产 `/work`，测试用临时目录 |
| `HOME` | Sandbox 内为 `$SBX_WORK/home`；测试隔离到 `tmp_path/home` |
| `CODEX_HOME` | 默认 `$HOME/.codex`（v1 兼容显式覆盖为 `$SBX_WORK/.codex`） |
| `CODEX_BIN` | 默认 `codex`；测试指向 `tests/fakes/fake_codex.py` |
| `SBX_ACCOUNT_CREDENTIAL` | 账号凭证 blob `{provider, files:{relpath: content}}`，还原到 `$SBX_WORK/home`（权限 600） |
| `SBX_ACCOUNT_ID` | 当前会话账号 id |
| `FAKE_CODEX_SCENARIO` | `success` / `resume` / `nonzero` / `hang` / `badjson` / `slow` / `auth_invalid` |
| `FAKE_AGY_SCENARIO` `FAKE_GROK_SCENARIO` `FAKE_OPENCODE_SCENARIO` `FAKE_DEVIN_SCENARIO` | 同上，作用于各 provider 假件 |
| `FAKE_CODEX_SLOW_SECONDS`（及各 `FAKE_*_SLOW_SECONDS`） | `slow` 场景在首行之后的静默秒数（默认 40） |
| `SBX_BACKEND` | `local` 或 `modal` |

`make test` 不得导入真实 `modal` 客户端并建立连接。`import modal` 仅允许出现在 `ModalBackend` 骨架中，且测试不得触发其方法。`LocalProcessBackend.exec` 不继承 `os.environ`（白名单 `PATH` `HOME` `LANG` + `SandboxSpec.env` + 显式 `env=`）；测试需要的变量一律显式传入。

## 4. 密钥与脱敏

- 禁止提交 `.env`、`auth.json`、`.modal.toml`、真实 token。
- fixture、stub、mock、日志、PR、Linear 评论中的 token / password / secret 字段一律 `REDACTED`。
- HTTP Basic 的 mock 口令（`sbx` / `sbx`）只用于本地假服务，不是生产凭证。
- 凭证 blob 只能经 `SBX_ACCOUNT_CREDENTIAL` 进出 sandbox；不打印、不记录、不写入事件。

## 5. 交付协议（v4）

| 时机 | 执行者 | 动作 |
| --- | --- | --- |
| 开工 | 编排者 | Issue 置 **In Progress** |
| 实现完成 | 作者（SWE） | 提交分支 + 自测（`make lint` / `make test` + 针对性检查）+ 开 PR；向编排者交接：分支、HEAD SHA、PR 链接、测试输出、契约符合性自检、已知限制 |
| 评审 | 独立 SWE | 干净环境对当前提交 PASS / 退回；只有影响既有功能的实际 bug 才退回；优化、风格、假设性风险不阻断 |
| 合并 | 编排者 | SWE PASS + 必需 CI 绿后机械合并；Issue 置 Done；冲突/失败测试由 SWE 修复 |

作者不改 Linear 状态与评论；一切状态流转、沟通、合并由编排者执行。发现契约设计问题：在自己 Issue 评论「契约变更请求」+ 理由，**代码仍按 §1 表格实现**。
