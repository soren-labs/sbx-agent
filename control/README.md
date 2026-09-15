# control/

`sbx-control`：会话 API、状态机、SSE、reaper。后端抽象 `backend.py` 在 WP0 合入后冻结。API 契约见 `docs/contracts/api.yaml`（覆盖 Linear `SOR-31`）。

## 布局

| 文件 | 说明 |
| --- | --- |
| `backend.py` | **冻结** `SandboxBackend` / `LocalProcessBackend` / `ModalBackend` 骨架 |
| `backends/modal.py` | 生产 `ModalBackend`（P0 签名；测试不实例化、不调用） |
| `app.py` | 纯 FastAPI。测试只导入这里。本地 `python -m control.app`（uvicorn），**不是** deploy |
| `deploy.py` | `deploy()`：`python -m modal deploy -m control.modal_app`。`make deploy` / WP1-A `invoke_control_deploy` 入口 |
| `modal_app.py` | `@modal.asgi_app()` + `@modal.concurrent(max_inputs=20)` + reaper `Cron("*/5 * * * *")`；`CONTROL_IMAGE` 带 FastAPI 栈 |
| `store.py` | `SessionStore` / `InMemoryStore` / `ModalDictStore` |
| `run_store.py` | SOR-82/A1 durable run ledger：`RunRecord` / `RunLedger`（terminal 单调不可逆）/ `InMemoryRunStore` / `FileRunStore` / `ModalDictRunStore`（`sbx-runs` Dict） |
| `reaper.py` | 纯函数 `reap(store, backend, now)` |
| `service.py` | 状态机 `creating → idle ⇄ running → closed \| timed_out \| lost` |

## 本地运行

```bash
SBX_BACKEND=local SBX_RUNNER_CMD="python tests/fakes/stub_runner.py" \
  uv run python -m control.app
```

默认 HTTP Basic 为本地假口令 `sbx` / `sbx`（不是生产凭证）。生产从 Modal Secret `sbx-basic-auth`（`SBX_BASIC_USER` / `SBX_BASIC_PASS`）读取。Codex 凭证来自 Secret `sbx-codex-auth`（键 `CODEX_AUTH_JSON`），或进程环境里的 `CODEX_AUTH_JSON`（`Secret.from_dict`）。

Modal 部署：

```bash
uv run python -c "from control.deploy import deploy; deploy()"
```

## 环境变量

| 变量 | 含义 |
| --- | --- |
| `SBX_BACKEND` | `local`（默认）或 `modal` |
| `SBX_RUNNER_CMD` | runner 可执行前缀；WP1-B 未合入时指向 `tests/fakes/stub_runner.py` |
| `SBX_SSE_KEEPALIVE_SECONDS` | SSE `: keepalive` 间隔，默认 15 |
| `SBX_MAX_CONCURRENT` | 每 owner 并发 Sandbox 上限，默认 2 |
| `SBX_IDLE_TIMEOUT_S` | 空闲回收阈值，默认 1800 |
| `SBX_RUN_STORE_DIR` | 本地 run ledger 落盘目录；默认 `$XDG_STATE_HOME/sbx-browser/runs`（modal 后端用 `sbx-runs` Dict） |
