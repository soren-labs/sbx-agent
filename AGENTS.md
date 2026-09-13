# AGENTS.md（引导版）

本文件由 WP0（Linear `SOR-39`）补全为正式版。在此之前，所有 Agent 遵守：

1. 你的任务说明书是分配给你的 Linear Issue；开工前完整阅读它、项目文档「开发任务分配与并发计划 v1」§0 与 §3、以及「设计方案 v1」中被引用的章节。
2. 只修改 Issue 中标明属于你的目录；契约文件（`docs/contracts/`、`control/backend.py`）合入后冻结，需要变更时在 Issue 评论提出请求，不自行修改。
3. 提 PR 前 `make test` 必须全绿，且不依赖任何云凭证。
4. 禁止把任何凭证、token、密码写入代码、fixture、日志、PR 或 Linear 评论。
5. Linear 状态：开工改 In Progress；PR 开且 CI 绿改 In Review，并在 Issue 评论 PR 链接 + 测试输出/截图 + 契约符合性自检。只改自己 Issue 的状态与评论。
6. 发现超出本包范围的缺陷：在本项目下新建子 Issue（父 = 当前 Issue），不顺手修。
