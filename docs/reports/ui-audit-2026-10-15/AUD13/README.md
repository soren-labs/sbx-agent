## AUD13 会话列表的归档与关闭筛选无实际隐藏效果

- 优先级：P2。证据：staging 实际复现及源码。
- 复现：归档或关闭本轮测试会话，保持默认未选 Show archived/Show closed。
- 实际行为：这些会话依然出现在 Recent 列表。Archive 后会话页头显示 Idle，直到用户留意正文横幅才知道已归档。
- 原因定位：[AppShell.tsx](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/shell/AppShell.tsx#L100) 基础查询不传 lifecycle；[sessions.page](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/control/persistence/queries.py#L20) 将 null 解释为所有状态。后续额外查询再合并没有实现排除。
- 修复建议：默认显式请求 open；启用相应选项时再并入其他生命周期，或统一通过服务端多状态过滤。页头明确展示 Archived，而不是仅显示 activity 的 Idle。
- 回归建议：默认、仅归档、仅关闭、二者同时显示；归档后立即更新；清晰区分 lifecycle 与 activity。

## 证据边界

当前截图只证明归档页头 Idle 与正文 archived 的矛盾。筛选菜单截图在渲染未完成时截取，已排除；默认列表过滤缺陷另有源码与上一轮实测记录。

MP4（如有）是关键帧回放，不是连续录屏。

## 关键帧顺序

1. Archived session header reports Idle while banner explains archive

## 附件

- [step-01.jpg](step-01.jpg)
