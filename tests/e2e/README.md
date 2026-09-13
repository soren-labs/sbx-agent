# tests/e2e

Playwright 覆盖两套后端（均无云凭证）：

| 项目 | 端口 | 后端 | 用例 |
| --- | --- | --- | --- |
| `mock-api` | 8787 | `tests.fakes.mock_api` | `chat.spec.ts`（WP1-D 冒烟，保留） |
| `local-control` | 8788 | 真控制面 `SBX_BACKEND=local` + `CODEX_BIN=tests/e2e/scenario_codex.py` → `fake_codex` | `control.spec.ts`（WP2-G / SOR-41 验收 8 条） |

```bash
make test-e2e
```

`local-control` 由 `tests/e2e/serve_local.py` 拉起：`control.app.create_app()` + 挂载 `web/`。禁止 `modal run` / `make image` / `make deploy`，也不写 `tests/e2e_modal/`。

场景包装：`hang` 提示词走 hang；`codex exec resume` 走 resume fixture；其余 success。

每步截图写入 `artifacts/`（本目录已被 `.gitignore` 忽略生成物；PR 中的样张以 `git add -f` 提交）。
