# 小本本记下来

`astrbot_plugin_growth_memory` 是面向 AstrBot 4.24+ 的个人成长记忆插件. 它只在显式开启的 QQ 私聊或群聊中采集学习证据, 默认 target 列表为空; 已形成的条目按 owner, task, group, person 作用域独立注入, 停止学习不会让已有记忆失效.

## 功能

- 管理员使用 `/进化`, `/停止进化`, `/进化状态` 幂等管理当前 QQ 会话.
- 默认每日 `03:00 Asia/Shanghai` 学习, Plugin Page 最多维护 8 个时间点.
- 每个 anchor 取问题前后各 10 条消息, 重叠窗口去重, 每批最多 10 个 anchor 且输入不超过 4,000 估算 token.
- Extractor 和 Reviewer 分阶段持久化. Reviewer 失败不会重跑已成功的 Extractor.
- 每日默认最多 8 次学习请求和 16,000 输入 token. 单次 provider timeout 45 秒, 即时重试 1 次, 连续失败触发 30 分钟 circuit breaker.
- 单 writer + SQLite WAL + 有界 FIFO. 队列过载时优先保留 anchor/answer, 暂停学习但不阻塞聊天.
- 聊天 hook 不等待 SQLite; anchor 迟到时用内存 pending completion 补绑定, 1 小时 TTL 回收 abandoned state.
- 未产生最终回答的 `missing/retryable` anchor 超过 2 小时自动取消, 防止异常或并发锁造成长期堆积.
- 条目使用追加版本, WebUI 支持查看、编辑和回滚.
- WebUI 可维护主人身份、学习目标和多个时间点, 展示真实预算/队列/证据天数, 支持归档和从历史版本选择回滚.
- 总结 Provider 从 AstrBot 当前已配置的 Chat Provider 动态下拉选择, 可分别设置 Extractor 和 Reviewer; Provider 被删除或改名时保留旧值并明确标为不可用, 不会误调用其他模型.
- Reviewer 的 trust/status 由服务端依据 owner 原话和重复证据推导, 自动学习不能改写人工条目.
- Extractor/Reviewer 必须引用当前窗口内真实入站消息 ID; 证据按消息跨 batch 去重, 不会因重叠窗口虚增计数.
- 自动学习写入后立即刷新内存 snapshot; 过期条目最多 5 分钟退出运行态, 90 天未确认草稿自动归档.
- 单轮注入默认最多 800 估算 token, 只使用内存 snapshot, 不在聊天热路径调用学习模型.
- 原始消息默认保留 14 天; 未完成 anchor 的问题和答案不按普通 TTL 删除.

## 安装

将整个目录安装为 AstrBot 插件, 目录名保持 `astrbot_plugin_growth_memory`. 插件没有第三方运行依赖, AstrBot 4.26.8 可直接加载.

首次在 AstrBot 插件配置中设置:

1. `owner_identities`: 主人身份, 例如 `aiocqhttp:123456789`.
2. 在插件 Page 的“运行设置”中从下拉列表选择 `Extractor 总结 Provider`.
3. 可选地选择独立的 `Reviewer 审查 Provider`; 留空时复用 Extractor Provider. 下拉数据直接来自 AstrBot 当前已配置的 Chat Provider, 不需要重复填写 API Key.
4. `capture_enabled`: 全局 kill switch. 默认 `true`, 但 target 为空时不会采集任何会话.

随后在需要学习的 QQ 私聊或群聊发送 `/进化`, 或从插件详情页进入 `小本本记下来` Page 添加精确 QQ 号/群号. Plugin Page 的 target、schedule 和 runtime settings 写入 SQLite, plugin reload 后保持不变.

数据库位于 AstrBot 分配给插件的 data 目录下:

```text
growth_memory.db
```

## 验证

```bash
PYTHONPATH=.. python3 -m unittest discover -s tests -v
ruff check .
ruff format --check .
python3 -m compileall -q .
```

当前实现通过 50 项离线和组件集成测试, 并在 AstrBot 4.26.8 的 `uv` 环境完成真实 import、handler registry 和 `initialize -> terminate` 生命周期检查; 另完成 2,000 条上下文和 40 个关键事件的 writer 压力探针. 详细证据见 [验证记录](docs/VALIDATION.md).

京东云已完成插件初始化、SQLite 完整性、受保护 Plugin Page API、真实 Extractor + Reviewer 双阶段运行和 reload 前的服务健康检查; 这不是 24 小时 soak 结论. 仍应连续观察队列、WAL、provider 消耗、实际注入和 QQ 收发, 再逐步扩大 target.

## 设计文档

- [技术设计](docs/TECHNICAL_DESIGN.md)
- [工程实施规格](docs/IMPLEMENTATION_SPEC.md)
- [定时学习管线规格](docs/SCHEDULED_LEARNING_PIPELINE.md)
- [验证记录](docs/VALIDATION.md)
- [变更记录](CHANGELOG.md)
