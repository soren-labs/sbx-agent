## AUD03 发布项目版本会重置表单未展示的配置

- 优先级：P1。证据：源码确认，条件为项目原先包含 API 或 SDK 配置的扩展字段；未修改现有业务项目验证。
- 触发：已配置 services、environment、ship_policy 或非 OpenCode 默认 Harness 的项目，在 Console 中仅修改 Model 后发布版本。
- 原因定位：[ProjectsPage.tsx](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/features/projects/ProjectsPage.tsx#L61) 从零构造 repository/checks/defaults，并固定 `provider_id: opencode`；[normalize_spec](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/control/application/projects.py#L38) 为缺少的字段填入空服务、空环境及默认交付策略。
- 影响：正常 UI 编辑可能清除服务定义和环境配置，并改变检查、独立审阅或合并策略；项目的后续会话使用改变后的配置。
- 修复建议：从当前完整 spec 派生新版本，只覆盖明确编辑的字段；保留隐藏字段及未知字段，避免硬编码 Harness。发布前显示语义差异，特别标明交付策略变化。
- 回归建议：使用包含自定义 ship_policy、services、environment、Harness effort 的项目，只编辑单个可见字段，验证其余配置逐项保留。

## 证据边界

截图仅证明表单字段范围；隐藏配置被重置的风险由源码确认，尚未对业务项目重演。

MP4（如有）是关键帧回放，不是连续录屏。

## 关键帧顺序

1. Supplement only: visible project fields omit services/environment/policy/harness provider

## 附件

- [step-01.jpg](step-01.jpg)
