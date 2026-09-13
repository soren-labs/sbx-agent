# web/

单页聊天看板（无构建步骤）。控制面把本目录当作静态文件挂在 `/`；本地开发：

```bash
python -m tests.fakes.mock_api --port 8787
```

打开 <http://127.0.0.1:8787/>。API 基地址默认同源；也可在加载页面前设置：

```html
<script>
  window.SBX_API_BASE = "http://127.0.0.1:8787";
  window.SBX_API_USER = "sbx";
  window.SBX_API_PASSWORD = "sbx";
</script>
```

`sbx` / `sbx` 只用于本地 mock，不是生产凭证。SSE 按 EventSource 语义消费（`id` / `event` / `data` / `retry`、断线自动重连并带 `Last-Event-ID`）。原生 `EventSource` 无法设置 `Authorization`，且 Chromium 会丢掉 URL 里的 `user:pass`，因此客户端用 `fetch` 读 `text/event-stream`。

Playwright：`make test-e2e`（配置在 `playwright.config.ts`，用例在 `../tests/e2e/`）。
