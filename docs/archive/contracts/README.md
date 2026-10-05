# 契约（冻结）

WP0（P1 `SOR-39` / P2 `SOR-59`）合入后本目录冻结。变更请在对应 Issue 评论提出「契约变更请求」，不要直接改这些文件。

| 文件 | 内容 |
| --- | --- |
| `filesystem.md` | `$SBX_WORK` 布局、`HOME=$SBX_WORK/home` 与 `CODEX_HOME` |
| `events.md` | canonical 事件（Codex `--json` 形状）+ runner `sbx.*` 事件 + 非 Codex 翻译规则 |
| `runner-cli.md` | `runner init` / `turn` / `stop` / `export-credentials`、凭证注入与退出码 |
| `api.yaml` | 会话 HTTP API（OpenAPI 3.1，网页内部 `/api/*`，Basic Auth） |
| `api-v1.yaml` | 公开 REST API v1（Cursor Cloud Agents 形状，`/v1/*`，Bearer `sbx_<key>`） |
| `artifacts.md` | SOR-83 工件包格式、采集边界、跨 Agent 交接与评审钉住 |
