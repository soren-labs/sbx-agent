## AUD06 项目检查命令未按引号规则解析

- 优先级：P2。证据：staging 项目配置、只读数据库查询及源码函数隔离执行。
- 输入：`smoke=python3 -c "print(123)"`。
- 实际 argv：`["python3", "-c", "\"print(123)\""]`。Python 执行的是字符串字面量，exit 0 但没有任何输出。带空格的代码还会被进一步拆成错误参数。
- 原因定位：[parseChecks](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/console/src/features/projects/ProjectsPage.tsx#L30) 直接按空白 split；[后端](https://github.com/soren-labs/sbx-agent/blob/018a0e02370bcae3c3f17ec97aa91b105d232f8b/control/application/projects.py#L30) 逐项保留 argv。
- 修复建议：提供结构化 argv 编辑器，或使用明确指定语法的命令解析器，并显示实际 argv 预览。不要为了兼容引号而无提示地改为任意 shell 执行。
- 回归建议：引号、带空格的路径、空参数、转义、包含等号的参数；验证检查实际执行预期断言而不仅仅是 exit 0。

## 证据边界

截图只展示检查命令输入格式；argv 错误由源码函数执行和存储配置确认，截图不证明检查运行结果。

MP4（如有）是关键帧回放，不是连续录屏。

## 关键帧顺序

1. Supplement only: quoted command entered into name=command args field; no project submitted

## 附件

- [step-01.jpg](step-01.jpg)
