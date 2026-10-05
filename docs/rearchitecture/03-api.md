# 03 · 统一 API、SDK、CLI

## 总原则

- 唯一前缀 **`/v1`**（新服务全新定义，与旧 `/v1/agents` 不兼容；兼容由迁移期翻译器承担，见 09）。
- 鉴权统一：`Authorization: Bearer sbx_…`（API key）或浏览器 session cookie（同源 + CSRF），两者解析为同一 `Principal{user_id, org_id, scopes}`。无 HTTP Basic、无 operator 特殊路由（运维操作 = 带 `admin:*` scope 的 key）。
- 资源风格：`GET` 列表支持 `limit/cursor`；写操作支持 `Idempotency-Key`；错误统一 `{"error":{"code","message","details","request_id"}}`，`code` 来自单一错误目录。
- 长操作永远返回资源（Thread/Turn），进度通过事件流获取。
- OpenAPI 由代码生成并提交到 `contracts/openapi.yaml`；SDK 从它生成。

## 资源与端点

### Threads（核心）

| Method | Path | 说明 |
|--------|------|------|
| `POST` | `/v1/threads` | 创建；可带首条消息（创建即 Turn #1） |
| `GET` | `/v1/threads` | 列表：`project`、`state`、`label`、`parent`、`origin`、`q`（与 Amp feed 一样的搜索语法）|
| `GET` | `/v1/threads/{id}` | 详情（含 mode、machine 摘要、最新 turn、changeset 摘要） |
| `PATCH` | `/v1/threads/{id}` | title、labels、visibility、pinned |
| `POST` | `/v1/threads/{id}/messages` | **发送消息 = 创建 Turn**；`delivery: queue \| steer \| interrupt` |
| `POST` | `/v1/threads/{id}/cancel` | 中断当前 Turn |
| `POST` | `/v1/threads/{id}/fork` | 以新 mode 或从某 turn 处分叉，可选 `carry_worktree` |
| `POST` | `/v1/threads/{id}/archive` · `/unarchive` | |
| `DELETE` | `/v1/threads/{id}` | |
| `GET` | `/v1/threads/{id}/events` | SSE；`after=<seq>`、`Last-Event-ID`；`?format=json` 分页回放 |
| `GET` | `/v1/threads/{id}/turns` · `/turns/{tid}` | Turn 历史 |
| `POST` | `/v1/threads/{id}/approvals/{aid}` | 回应 `approval.requested` |
| `GET` | `/v1/threads/{id}/export?format=md\|json` | 对齐 Amp `threads markdown/export` |

创建示例：

```json
POST /v1/threads
{
  "project": "prj_123",
  "mode": "codex/high",
  "executor": {"kind": "modal", "size": "standard"},
  "message": {"content": [{"type": "text", "text": "修复 CI 并补测试"}]},
  "labels": ["workflow:release-42"],
  "output_schema": null,
  "connection": "auto"
}
```

`mode` 可以是 `auto`、`auto/high`（由 Router 选 driver）、`<driver>/<profile>`、或 Plugin 提供的自定义 mode。`connection` 仅高级用法可固定。

### Machine 子资源（取代 `/workspace`、`/git`、sandbox 操作）

| Method | Path | 说明 |
|--------|------|------|
| `GET` | `/v1/threads/{id}/machine` | 状态、size、executor、endpoints |
| `POST` | `/v1/threads/{id}/machine/wake` · `/pause` · `/rebuild` | |
| `GET` | `/v1/threads/{id}/files?path=` · `PUT …/files` | 浏览 / 上传 |
| `GET` | `/v1/threads/{id}/changes` · `/changes/diff` | 当前 ChangeSet（committed + uncommitted） |
| `WS` | `/v1/threads/{id}/terminal` | 共享 tmux 会话（需 `terminal` scope + driver/executor capability） |
| `GET` | `/v1/threads/{id}/ports` | 已声明服务（≈ Portals） |

### 交付与审查

| Method | Path | 说明 |
|--------|------|------|
| `POST` | `/v1/threads/{id}/ship` | 发送项目 ship_behavior prompt（可覆盖 `kind`）；返回 Turn |
| `GET` | `/v1/changesets/{id}` · `/deliveries` · `/reviews` | 只读视图 |
| `POST` | `/v1/changesets/{id}/reviews` | 启动独立 review thread（mode 可选，默认与作者不同 driver 或不同 connection） |
| `POST` | `/v1/deliveries/{id}/merge` | 受 independent-review + exact-head 规则保护 |

### Projects / Snapshots

`/v1/projects`（CRUD）、`/v1/projects/{id}/snapshots`（list/delete）、`/v1/projects/{id}/env`（变量与 secret 元数据，secret 值只写不读）。

### Catalog（发现）

| Path | 说明 |
|------|------|
| `GET /v1/drivers` | Driver manifest 摘要 + capabilities + 当前可用性 |
| `GET /v1/modes` | 可用 mode（系统 + plugin + org），含 driver、model、effort、说明 |
| `GET /v1/skills` · `/v1/tools` · `/v1/plugins` | 扩展注册表 |
| `GET /v1/executors` | 可用 executor（含在线 runners） |

### Connections / Integrations

| Method | Path | 说明 |
|--------|------|------|
| `GET/POST` | `/v1/connections` | 列表/创建（`kind`、`driver`、`scope: user\|org`） |
| `POST` | `/v1/connections/{id}/login` | 启动订阅登录流程（设备码 / 浏览器 OAuth / 上传 CLI auth 文件）→ 返回 `login_session` |
| `GET` | `/v1/connections/login-sessions/{id}` | 轮询登录状态 |
| `POST` | `/v1/connections/{id}/check` | Check Access（运行一次最小 Turn） |
| `PATCH` | `/v1/connections/{id}` | priority、model_mapping、slots、active |
| `DELETE` | `/v1/connections/{id}` | |
| `GET/POST/DELETE` | `/v1/integrations` | GitHub App、Modal（BYO compute）、Slack |

### Automations / Webhooks / Usage / Keys / Me

- `PUT/GET/DELETE /v1/threads/{id}/automation`（每 thread ≤ 1）、`POST …/automation/pause|resume|run-now`
- `GET /v1/webhooks`、`POST /v1/hooks/{token}`（**公开入口**，仅持久化后返回 200）
- `GET /v1/usage?group_by=connection|driver|project&from=&to=`
- `/v1/api-keys`、`/v1/me`、`/v1/orgs/{id}/members`
- `/healthz`、`/readyz`（不在 `/v1` 下）

### Runner / sbxd 内部协议（非公开 REST）

- `WS /internal/sbxd/connect`：Machine 或 BYO Runner 出站连接，使用一次性 enrollment token 换取的 machine credential。协议见 05。

## 旧 API → 新 API 映射

| 旧表面 | 旧端点（示例） | 新模型 |
|--------|----------------|--------|
| 公开 Agents API | `POST /v1/agents`、`/v1/agents/{id}/runs`、`/v1/agents/{id}/stream` | `POST /v1/threads`、`POST …/messages`、`GET …/events` |
| Task API | `POST /v1/tasks`、`/v1/tasks/{id}` | Thread + `output_schema` + Turn result |
| Runs | `/v1/runs/{id}`、`/v1/runs/{id}/events` | `/v1/threads/{id}/turns/{tid}`、events 按 turn 过滤 |
| Revisions / Reviews | `/v1/revisions`、`/v1/reviews` | `ChangeSet` / `Review` |
| Delivery | `/v1/…/delivery` | `POST …/ship` + `Delivery` |
| Artifacts / handoff | `/v1/artifacts` | `/v1/threads/{id}/artifacts`、agent-to-agent 文件传输 |
| Workflows | `/v1/workflows/{id}` | `labels` + `parent_thread_id` 查询；批量关闭 = `POST /v1/threads:batchArchive` |
| Accounts | `/v1/accounts`、`/api/accounts` | `/v1/connections` |
| Connections（GitHub/Modal） | `/v1/connections`、`/hosted/github/*` | `/v1/integrations` |
| v2 Sessions | `/api/v2/sessions`、`/messages`、`/retry`、`/changes`、`/deliver` | Thread / messages / `fork` 或重发 / changes / ship |
| Hosted | `/hosted/sessions/*`、`/hosted/auth/*`、`/hosted/sessions/{id}/connect` | 同一 `/v1`；auth = `/auth/*`（cookie 登录页）；direct-connect 由 events 网关统一处理 |
| 内部 `/api/*`（HTTP Basic） | operator 操作 | `admin:*` scope key 调用同一 `/v1` |
| Providers / Models | `/v1/providers`、`/v1/models` | `/v1/drivers`、`/v1/modes` |
| Broker | `broker/` GitHub App 安装中转 | 保留为独立可选服务 `apps/broker`，只服务 `Integration(github_app)` |

## SDK

两门 SDK（Python、TypeScript）形状一致：

```python
from sbx import Sbx

sbx = Sbx()  # SBX_API_KEY / SBX_BASE_URL

# 1) 低层资源
thread = sbx.threads.create(project="prj_123", mode="codex/high", message="Fix CI")
for ev in sbx.threads.events(thread.id, after=0):
    ...

# 2) Amp 风格的 execute()：创建或续写 thread，流式返回，直到 turn 结束
async for msg in sbx.execute("Fix CI", project="prj_123", mode="auto/high"):
    if msg.type == "assistant":
        ...
    if msg.type == "result":
        print(msg.result, msg.usage)

async for msg in sbx.execute("now add tests", thread=thread.id):
    ...
```

- `execute()` 产出 `system(init) / user / assistant / result` 四类消息（Amp/Claude stream-json 兼容视图，见 04 “兼容投影”），方便现有为 Amp/Claude SDK 编写的工具迁移。
- 自动重连：基于 `seq` 续读；`result` 之前断线不会丢事件。
- `sbx.threads.wait(id, timeout=)`、`approve()`、`cancel()`、`artifacts.download()`。

## CLI（`sbx`）

| 命令 | 说明 |
|------|------|
| `sbx` / `sbx -x "prompt"` | 在当前目录**本地执行**（executor=local，事件同步到服务端，可在 console 续写） |
| `sbx -rx "prompt" [--project] [--mode] [--size]` | 远程 Machine 执行（≈ `amp -ox`） |
| `sbx threads list/continue/show/export/archive/label` | Thread 管理 |
| `sbx sync <thread>` | 把远程 Machine 的改动镜像到本地（≈ `amp sync`） |
| `sbx runner [--id] [--dir…] [--discover-dirs]` | 启动 BYO Runner（同一个 sbxd） |
| `sbx connections add/login/check/list/priority` | 订阅与凭证管理 |
| `sbx projects …`、`sbx plugins …`、`sbx skills …` | |
| `sbx server …` | self-host 部署：`init/doctor/upgrade/status`（取代现有 deploy CLI） |
| `--stream-json` | 输出与 SDK 相同的兼容投影 |
