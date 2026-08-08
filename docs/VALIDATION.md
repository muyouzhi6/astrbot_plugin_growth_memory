# 小本本记下来验证记录

## 当前基线

- 使用 AstrBot `4.26.8`, commit `e80e01c7` 做 SDK 兼容检查.
- 使用 Python 3.12 执行离线测试.
- 保持 AstrBot Core 未修改.
- 保持线上京东云实例未修改.

## 已通过

- [x] 读取 `metadata.yaml` 和 `_conf_schema.json`.
- [x] 在 AstrBot `uv` 环境导入 `GrowthMemory`.
- [x] 注册 9 个 handler.
- [x] 确认 capture filters 顺序为 `TargetCaptureFilter -> EventMessageTypeFilter`.
- [x] 完成 `initialize -> terminate`, 创建 10 个 Web API route, 停止 ticker, 清理 route.
- [x] 通过 42 项 `unittest`.
- [x] 通过 `ruff check .`.
- [x] 通过 `ruff format --check .`.
- [x] 通过 `python3 -m compileall -q .`.
- [x] 验证 SQLite WAL schema, target/schedule/entry/version/rollback/runtime flag.
- [x] 验证默认空 target、群聊/私聊隔离、Bot account 精确匹配.
- [x] 验证有界 FIFO critical overflow 进入 degraded.
- [x] 验证流式答案依赖 `on_agent_done`, 错误回答不学习.
- [x] 验证 Extractor/Reviewer 成功路径和 owner behavior rule 本地权限校验.
- [x] 验证 Reviewer 连续失败不重跑 Extractor.
- [x] 验证每日 request budget 和 input token budget.
- [x] 验证 schedule 12 小时 catch-up 和 slot 幂等.
- [x] 验证 14 天 TTL 不删除未完成 anchor 证据.
- [x] 验证注入不依赖 learning target 开关.
- [x] 验证 Web API 只使用 AstrBot Plugin Page bridge 支持的 `GET/POST`.
- [x] 验证聊天 hook 不等待 SQLite, writer 迟到时仍能绑定最终答案.
- [x] 验证 anchor hook 内存状态 TTL 回收, 不随问答次数无限增长.
- [x] 验证 Reviewer 不能依据模型自报的 trust 修改人工条目.
- [x] 完成 2,000 条上下文和 40 个关键事件的有界 writer 压力探针, 队列清空且 SQLite 无异常.

## 部署后必须验证

- [ ] 在京东云 AstrBot WebUI 安装并 reload, 确认插件卡片和 `小本本记下来` Page 可打开.
- [ ] 在一个测试 QQ 私聊发送 `/进化`, 验证 target 落库且普通回复不受影响.
- [ ] 在一个测试群观察未 @ Bot 的 context 消息只捕获、不触发 LLM 回复.
- [ ] 完成一次真实流式和一次非流式问答, 验证 anchor question/answer.
- [ ] 执行一次真实 Extractor + Reviewer run, 核对 provider 请求数和 token 预算.
- [ ] 在下一轮聊天检查条目实际注入并确认没有泄露 `behavior_only` 内容.
- [ ] reload 插件, 验证 target, schedule, runtime flags, deferred review 和条目均恢复.
- [ ] 连续运行 24 小时, 检查 event loop latency, queue depth, WAL 大小, memory, provider 错误和 QQ 收发.

只有上述部署后检查完成, 才能把状态从“本地可加载实现”提升为“生产验证完成”.
