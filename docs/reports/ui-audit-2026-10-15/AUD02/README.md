## AUD02 保存被截断的文件会静默丢失尾部

- 优先级：P1。证据：源码及本地临时文件隔离复现；未在用户实际文件上操作。
- 复现：生成 1,000,019 字节 UTF-8 文本，尾部为独立标记；读取后将返回内容和返回 digest 原样写回。
- 实际结果：读取返回 `truncated=true`、1,000,000 字节，写入成功；原文件缩短为 1,000,000 字节，尾部标记丢失。
- 原因定位：[worktree.py](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/runtime/daemon/worktree.py#L172) 截断内容但返回原文件完整 digest；[FilesTab.tsx](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/features/sessions/tabs/FilesTab.tsx#L170) 不检查 `truncated`，可直接保存截断后的内容。版本检查因此无法阻止这类覆盖。
- 修复建议：截断或脱敏后的视图默认只读，并明确解释原因；若需要编辑大文件，应先获取完整内容或使用范围补丁，并在服务端验证编辑基于完整内容，不能只依赖原文件 digest。
- 回归建议：超过读取上限的文件、恰好处于上限的文件、含多字节字符的边界、脱敏视图；保存前后验证完整字节及尾部不丢失。

## 证据边界

本地隔离文件读写复现，无 staging UI 录像；不以无关截图替代数据完整性证据。

MP4（如有）是关键帧回放，不是连续录屏。

## 关键帧顺序

无新的 UI 关键帧。

## 附件


