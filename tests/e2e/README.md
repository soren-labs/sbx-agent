# tests/e2e

Playwright 对 `tests.fakes.mock_api` 的聊天页覆盖（WP1-D / SOR-32）。

```bash
make test-e2e
```

每步截图写入 `artifacts/`（本目录已被 `.gitignore` 忽略生成物；PR 中的样张以 `git add -f` 提交）。切真控制面的 e2e 在 WP2-G。
