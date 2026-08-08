# 变更记录

## v0.2.3

- 修复 Plugin Page 缺少 toast 节点导致所有操作反馈抛出异常的问题, 增加请求期间防重复提交、加载状态、可访问性播报和移动端操作区布局.
- Reviewer batch 使用数据库 compare-and-set 和 5 分钟 lease 原子抢占, reload 短暂重叠时不重复调用模型; 过期 lease 可自动恢复.
- 插件初始化改为幂等, 防止同一实例重复初始化遗留 writer、ticker 或 Web API route.
- 启动时重新校验 SQLite runtime flags, 脏值自动修复为安全默认值并记录日志.

## v0.2.2

- 将自动 proposal 的证据改为逐条 `message_row_id` 白名单校验, trust、证据条数和证据天数只按该 proposal 实际引用的入站消息计算.
- 新增跨 batch 的消息级证据去重, 防止重叠上下文重复累计并提前把人物或群画像提升为 `trial`.
- 自动学习成功后立即发布新的内存 snapshot, 不再等待插件 reload 才参与后续注入.
- 拒绝自动生成的 `global profile_fact/milestone`, 避免把普通技术问答当成跨会话长期记忆.
- 过期条目最多 5 分钟退出运行 snapshot, 自动草稿 90 天未确认后归档; 成功恢复后清理陈旧错误告警.
- 新增 proposal 证据隔离、global 普通知识拒绝、snapshot 即时刷新、证据去重和条目生命周期回归测试.

## v0.2.1

- 修正 Extractor/Reviewer 每次尝试的请求、输入 token 和输出 token 统计, 延迟 Reviewer 成功后同步 run 状态.
- 为自动人物事实增加跨批次幂等证据累计, 至少 3 条证据且跨 2 天才进入 `trial`; 兼容旧数据库迁移.
- WebUI 增加主人身份编辑、时间点编辑/启停/删除保护、真实今日预算、队列深度、归档条目、证据条数/天数和历史版本回滚选择.
- 保持学习冷路径与聊天热路径隔离, SQLite WAL、版本追加、审计和失败重试策略不变.
- 新增旧版 SQLite 升级幂等、时间点删除保护和人物画像晋级测试.
