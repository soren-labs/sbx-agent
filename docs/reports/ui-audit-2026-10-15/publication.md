# GitHub Issue 合并与发布记录（2026-10-15）

已发布 4 个合并 Issue，22 个 AUD 保留为组内验收项；另将旧 #198 的剩余性能验收、#205 的 resumed usage 疑点明确纳入会话组。没有将历史验收、源码风险或单会话 smoke 误报为本轮完整 E2E。

| 组 | GitHub Issue | 范围 |
| --- | --- | --- |
| G01 | [#227](https://github.com/soren-labs/sbx-agent/issues/227) | 修复工作区文件编辑、终端重试与变更审阅链路 |
| G02 | [#228](https://github.com/soren-labs/sbx-agent/issues/228) | 修复会话执行取消、子会话与历史生命周期的一致性 |
| G03 | [#229](https://github.com/soren-labs/sbx-agent/issues/229) | 保留项目完整配置并对齐检查、服务与交付能力 |
| G04 | [#230](https://github.com/soren-labs/sbx-agent/issues/230) | 让 API key 签发可安全重试，并提供明确的权限选择 |

## 旧 Issue 处理

| 旧 Issue | 结论 | 后续位置 |
| --- | --- | --- |
| [#198](https://github.com/soren-labs/sbx-agent/issues/198) | 关闭为迁移（not planned），没有宣称全部性能目标已达成 | #228 LEGACY198-A/B/C：启动阶段、首次可见内容 p50/p95、慢分配与接管 |
| [#199](https://github.com/soren-labs/sbx-agent/issues/199) | 基础功能已由 #200–#204 合并且有历史验收，交付追踪 completed | #227–#230 追踪当前缺陷，#228 保留 Slot 隔离与回收回归约束 |
| [#205](https://github.com/soren-labs/sbx-agent/issues/205) | 基础功能已由 #206–#211 合并且有历史验收，交付追踪 completed | #228 LEGACY205-USAGE 先核实 resume usage 语义，其余缺陷见四组 |
| [#84](https://github.com/soren-labs/sbx-agent/issues/84) | 旧 `/v1` 一次性授权交接流程已过期，not planned | #229 明确历史边界，未宣称重新实现旧零配置接入 |

每个旧 Issue 都已先评论说明证据、剩余范围和关联新 Issue，再关闭。未执行旧授权链接或读取浏览器 cookie。

## 验证

- GitHub 上四个新 Issue 均为 OPEN，22 个 AUD 验收项各出现一次。
- 已公开的 38 张截图与 13 段关键帧 MP4（共 51 个媒体文件）逐一对照 Git blob hash；Issue 使用固定 commit 链接，后续 agent 不依赖本地文件。
- 本轮针对旧 #198 的镜像/分配实现重跑 `uv run pytest -q tests/unit/executors/test_modal_allocation.py`：16 passed。
- 旧 93 秒场景、完整订阅登录/并发矩阵及 resumed usage 本轮没有重新测量；结论分别依据当前源码、合并记录、历史证据和此前本轮短任务，剩余验证已写入 #228。
