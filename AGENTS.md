# AGENTS.md

本文件是仓库内 Agent 的规范来源。任务拆分与状态仍以 Linear 项目 **sbx-browser**（团队 Sorenforge，Issue 前缀 `SOR-`）为准。

## 0. 开工必读

1. 任务说明书是分配给你的 Linear Issue。开工前完整阅读它、项目文档「开发任务分配与并发计划 v1」§0 / §3 / §5，以及「设计方案 v1」中被该 Issue 引用的章节。
2. 只修改 Issue 中标明属于你的目录。
3. 契约文件合入后冻结（见下）。需要变更时在 **自己的 Issue 评论** 提出「契约变更请求」，**不自行修改契约**。
4. 提 PR 前 `make lint` 与 `make test` 必须全绿，且不依赖任何云凭证。
5. 禁止把任何凭证、token、密码写入代码、fixture、日志、PR 或 Linear 评论。fixture 里的 token 字段一律用 `REDACTED` 占位。
6. Linear 状态：开工改 **In Progress**；PR 开且 CI 绿改 **In Review**，并在 Issue 评论 PR 链接 + 测试输出/截图 + 契约符合性自检。只改自己 Issue 的状态与评论。
7. 发现超出本包范围的缺陷：在本项目下新建子 Issue（父 = 当前 Issue），不顺手修。

## 1. 目录所有权

来源：Linear 文档「开发任务分配与并发计划 v1」§1。WP0（`SOR-39`）是唯一一次允许触碰全仓库的工作包，合入后按下表执行。

| 路径 | 工作包 | 说明 |
| --- | --- | --- |
| `docs/contracts/` | **冻结**（WP0 合入后） | 文件系统 / 事件 / runner CLI / 会话 API。变更走 Issue 评论「契约变更请求」 |
| `control/backend.py` | **冻结** | `SandboxBackend` + `LocalProcessBackend` + `ModalBackend` 骨架 |
| `runtime/image.py` `runtime/entrypoint.sh` `Dockerfile.local` `Makefile` `README.md` | WP1-A / `SOR-29` | 镜像与入口；`image` / `deploy` target 由该包实现 |
| `runtime/runner/**` `tests/unit/runner/**` | WP1-B / `SOR-30` | Codex 会话驱动 |
| `control/**`（除冻结的 `backend.py`）`tests/unit/control/**` `tests/integration/control/**` | WP1-C / `SOR-31` | 会话 API、状态机、SSE、reaper；可新增 `control/backends/modal.py` |
| `web/**` | WP1-D / `SOR-32` | 聊天页；Playwright 对 mock_api |
| `tests/e2e/**` | WP1-D / `SOR-32` 起步；**WP2-G / `SOR-41`** 对真控制面 | G 可修 `web/**` 缺陷；截图 `tests/e2e/artifacts/` |
| `tests/integration/**` | WP2-F / `SOR-40` | 无云集成（真 runner + LocalProcessBackend + fake_codex） |
| `tests/e2e_modal/**` | WP2-H / `SOR-42` | 编排者在 WSL 跑真实 Modal e2e |
| `tests/fakes/` `tests/fixtures/` | WP0 提供；后续包只扩展不重写契约语义 | `fake_codex.py` / `stub_runner.py` / `mock_api.py` / 事件样本 |
| `spike/` | **编排者** | P0 / `SOR-28`；由编排者分支提供，不进生产路径、不作为 CI 依赖 |
| `.github/workflows/` `.cursor/environment.json` | WP0 骨架 | 后续包可追加 CI job，测试仍不得依赖云凭证 |

**后续工作包 Agent：不要改冻结路径。** 假件只扩展场景，不改事件名 / 退出码 / 路径语义。

## 2. 契约冻结

以下文件是跨包接口，合入 `main` 后视为冻结：

- `docs/contracts/filesystem.md`
- `docs/contracts/events.md`
- `docs/contracts/runner-cli.md`
- `docs/contracts/api.yaml`
- `control/backend.py`

解析这四份契约时，事件名、路径、退出码、错误码必须一致（见各文件的 `canonical-yaml` / `x-canonical` 块）。测试入口：`tests/unit/test_contract_consistency.py`。

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
| `CODEX_HOME` | 默认 `$SBX_WORK/.codex` |
| `CODEX_BIN` | 默认 `codex`；测试指向 `tests/fakes/fake_codex.py` |
| `FAKE_CODEX_SCENARIO` | `success` / `resume` / `nonzero` / `hang` / `badjson` / `slow` |
| `FAKE_CODEX_SLOW_SECONDS` | `slow` 场景在 `thread.started` 之后的静默秒数（默认 40） |
| `SBX_BACKEND` | `local` 或 `modal` |

`make test` 不得导入真实 `modal` 客户端并建立连接。`import modal` 仅允许出现在 `ModalBackend` 骨架中，且测试不得触发其方法。

## 4. 密钥与脱敏

- 禁止提交 `.env`、`auth.json`、`.modal.toml`、真实 token。
- fixture、stub、mock、日志、PR、Linear 评论中的 token / password / secret 字段一律 `REDACTED`。
- HTTP Basic 的 mock 口令（`sbx` / `sbx`）只用于本地假服务，不是生产凭证。

## 5. Linear 状态协议

| 时机 | 状态 | 评论 |
| --- | --- | --- |
| 开工 | **In Progress** | 可选：已读 Issue / 契约 |
| PR 已开且 CI 绿 | **In Review** | PR 链接 + `make test` 输出 + 契约符合性自检（事件名 / 路径 / 退出码 / 错误码对照表） |
| 发现契约设计问题 | 不改状态以外的 Issue 字段 | 评论「契约变更请求」+ 理由；**代码仍按 §0 表格实现** |

只改自己 Issue 的状态与评论。不改 Issue 描述、里程碑、项目文档。
