# AGENTS.md

本文件是仓库内 Agent 的规范来源。任务拆分与状态仍以 Linear 项目 **sbx-browser**（团队 Sorenforge，Issue 前缀 `SOR-`）为准。执行/评审协议：**P2 任务编排与 SWE 执行计划 v4**（独立评审通过即合并）。

## 0. 开工必读

1. 任务说明书是分配给你的 Linear Issue。开工前完整阅读它、「P2 任务编排与 SWE 执行计划 v4」§0 / §5，以及「设计方案 v2」中被该 Issue 引用的章节。
2. 只修改 Issue 中标明属于你的目录（§1 表）。
3. 契约（§2）变更需在 **自己的 Issue 评论** 提出「契约变更请求」，经独立 SWE 审阅后实施；编排者只负责顺序与合并。
4. 提 PR 前 `make lint` 与 `make test` 必须全绿，且不依赖任何云凭证。
5. 禁止把任何凭证、token、密码写入代码、fixture、日志、PR 或 Linear 评论。fixture 里的 token 字段一律用 `REDACTED` 占位。
6. v4 职责：作者（SWE）= 实现 + 自测 + PR 准备；独立评审（SWE，干净环境）= 对当前提交 PASS/退回；编排者 = 派发、状态跟踪、Linear 沟通、PASS 后机械合并。**编排者不实现、不评审、不测试**；旧 Cursor/Bugbot 与「编排者技术把关」规则不再适用。
7. 发现超出本包范围的缺陷：在本项目下新建子 Issue（父 = 当前 Issue），不顺手修。
8. 隔离要求：开发/测试使用独立 HOME/XDG 与剥离的凭证环境（见 `tests/conftest.py`）；不读取、不打印真实凭证；不使用其他 worktree。

## 1. 目录结构与所有权（统一架构 RFC 167）

目标结构以 `docs/architecture/unified/09-repository-structure.md` 为准；实际行为以 `docs/specs/unified/` 为准。一个 Issue 只改其标明的目录。

| 路径 | 职责 |
| --- | --- |
| `protocol/` | runtime 与控制面共享的线协议/数据类型（不依赖 `control`） |
| `control/domain/` | 纯领域模型与不变量（不依赖基础设施） |
| `control/application/` | 用例与端口；唯一写者规则见 `docs/specs/unified/authority.md` |
| `control/persistence/` | PostgreSQL schema/migrations、UoW、仓储（唯一业务权威） |
| `control/jobs/` | Job、claim/fence、定时器与 handler |
| `control/api/` | 唯一 HTTP 面 `/api`（OpenAPI 由 `make openapi` 生成） |
| `control/executors/`、`control/runtime_client/` | Executor 端口（local / Modal）与 sbx-runtime 客户端 |
| `control/integrations/`、`control/security/` | Git/GitHub、邮件、Connection 校验器；vault、密码、脱敏 |
| `runtime/` | `sbx-runtime` daemon 与官方 CLI Harness（不依赖 `control`） |
| `console/` | 唯一前端 |
| `src/sbx/` | SDK 与 CLI |
| `docs/specs/unified/`、`docs-site/` | 实现规格与公开文档 |
| `docs/archive/` | 已退役的旧契约与历史资料（只读，不再规范） |

依赖方向由 `tests/unit/test_layer_boundaries.py` 强制。发现超出本 Issue 范围的缺陷：新建子 Issue，不顺手修。

## 2. 契约

跨包接口：`docs/specs/unified/openapi.yaml`（生成物，`tests/unit/test_openapi_drift.py` 检查漂移）、`protocol/runtime.py` 与 `docs/specs/unified/runtime.md`、`runtime/harnesses/protocol.py`（`Harness` Protocol）与 `docs/specs/unified/harnesses/manifests.json`、`control/persistence/migrations/*.sql`（只追加）。变更这些接口须在 Issue 中提出并经独立评审；旧 `docs/contracts/**` 已归档至 `docs/archive/contracts/`，不再生效。

## 3. 测试与本地环境

```bash
make lint           # ruff check + ruff format --check
make test           # pytest tests/unit tests/integration（嵌入式 PostgreSQL，禁止云凭证）
make console-check  # Console typecheck + vitest + build
make docs-check     # docs-site 构建与链接检查
```

| 变量 | 含义 |
| --- | --- |
| `SBX_DATABASE_URL` | PostgreSQL 连接串（唯一业务权威） |
| `SBX_VAULT_KEYS` | `kid:base64key[,...]`，首个为活动密钥；CredentialVersion 信封加密 |
| `SBX_RUNTIME_MASTER_KEY` | 十六进制；派生 sbx-runtime 每个 lease 的认证密钥 |
| `SBX_EXECUTORS` | 启用的 Executor，默认 `local,modal` |
| `SBX_PUBLIC_URL` / `SBX_ALLOWED_ORIGINS` / `SBX_COOKIE_SECURE` | Console 来源与 cookie 策略 |
| `SBX_RESEND_API_KEY` | 可选；邮件发送（缺省时使用本地 outbox） |

`tests/conftest.py` 剥离宿主凭证（含 `SBX_TEST_*`、`SBX_BENCHMARK_*`）并隔离 HOME/XDG；测试所需变量一律显式传入。`import modal` 只允许出现在 `control/executors/modal.py` 与 `control/integrations/connectors/modal.py`，`make test` 不得触发真实 Modal 连接。真实凭证的检查（`make smoke-modal`、`make check-connectors`）是显式 opt-in，不打印凭证，并且必须回收所建 sandbox。

## 4. 密钥与脱敏

- 禁止提交 `.env`、`auth.json`、`.modal.toml`、真实 token。
- fixture、stub、mock、日志、PR、Linear 评论中的 token / password / secret 字段一律 `REDACTED`。
- 凭证只以加密 CredentialVersion 存储，经 lease 范围的 grant 投递给 sbx-runtime；API 从不返回明文，不打印、不记录、不写入事件。

## 5. 交付协议（v4）

| 时机 | 执行者 | 动作 |
| --- | --- | --- |
| 开工 | 编排者 | Issue 置 **In Progress** |
| 实现完成 | 作者（SWE） | 提交分支 + 自测（`make lint` / `make test` + 针对性检查）+ 开 PR；向编排者交接：分支、HEAD SHA、PR 链接、测试输出、契约符合性自检、已知限制 |
| 评审 | 独立 SWE | 干净环境对当前提交 PASS / 退回；只有影响既有功能的实际 bug 才退回；优化、风格、假设性风险不阻断 |
| 合并 | 编排者 | SWE PASS + 必需 CI 绿后机械合并；Issue 置 Done；冲突/失败测试由 SWE 修复 |

作者不改 Linear 状态与评论；一切状态流转、沟通、合并由编排者执行。发现契约设计问题：在自己 Issue 评论「契约变更请求」+ 理由，**代码仍按 §1 表格实现**。
