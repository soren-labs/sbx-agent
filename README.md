# sbx-browser

Cursor Cloud Agent 式的 Codex 云端会话：网页新建会话 → 一台按会话创建的 Modal Sandbox 内运行 Codex CLI，多轮对话 → 空闲自动回收。浏览器 + noVNC 实时画面作为 P2 叠加。

## 任务与设计的唯一来源

所有需求、设计、任务拆分与状态都在 Linear 项目 **sbx-browser**（团队 Sorenforge，Issue 前缀 `SOR-`）：

- 项目概述与「已决定」表：Linear 项目描述
- 设计方案 v1（顶部有 v1.1 范围收敛说明）：Linear 项目文档
- 开发任务分配与并发计划 v1：Linear 项目文档
- 当前起点：`SOR-39` WP0（仓库引导 + 接口契约 + 假件与测试脚手架）

仓库内的 `AGENTS.md`、`docs/contracts/` 由 WP0 建立，之后成为代码层面的规范来源。

## 分层（P1 MVP）

```
web/      单页聊天看板（无构建步骤）
control/  sbx-control：FastAPI on Modal，会话 API / modal.Dict 状态机 / SSE / reaper
runtime/  Sandbox 内：镜像定义、entrypoint、runner（Codex 会话驱动）
tests/    unit / integration / e2e / fakes / fixtures —— 全部不依赖云凭证
spike/    P0 验证脚本（WSL 本机执行）
```
