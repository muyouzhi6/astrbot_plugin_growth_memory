# 成长记忆定时学习管线规格

## 0. 文档地位

- 本文件定义 `astrbot_plugin_growth_memory` 的自动学习入口和执行管线.
- 本文件取代旧方案中的"所有会话被动观察", "90 秒即时提炼", "群聊累计 120 条触发"与"人物累计 40 条触发".
- 手工 WebUI 条目和 `/growth remember` 仍可立即写入, 不经过定时学习管线.
- 条目检索, 注入预算, 信任边界, 版本记录和 rollback 继续遵循 `TECHNICAL_DESIGN.md` 与 `IMPLEMENTATION_SPEC.md`.

## 1. 最终结论

采用"显式会话白名单 + 定时增量学习 + 两阶段 LLM"方案. 该方案可行, 并作为 v1 唯一自动学习方式.

必须保留以下修正:

1. 不把 10 个前后窗口机械拼接. 先合并重叠窗口, 保证同一 batch 中一条消息只出现一次; 跨 batch 只允许重复 anchor 问答和最小连续上下文.
2. "每 10 组"表示单个 extractor batch 最多包含 10 个触发问答, 同时受输入 token 硬上限约束.
3. Extractor 和 reviewer 都只能生成 proposal. 只有本地 `MutationPolicy` 可以修改条目.
4. 只有 reviewer 成功且对应 mutation 已提交, 才将 anchor 标记为 committed.
5. 群成员的普通发言只能形成 `profile_fact` 证据, 不能形成 `behavior_rule`.
6. 关闭某个会话的学习只停止新 capture, 不删除旧证据, 不关闭已有条目的注入.
7. 最终 Bot 文本以 `on_agent_done` 的 final `LLMResponse` 为主来源. `on_decorating_result` 和 `after_message_sent` 只用于非流式增强, 不作为 streaming 必需条件.
8. AstrBot 没有为该 hook 链提供平台送达回执. `after_message_sent` 只能记为 `attempted_unknown`, 禁止写成 delivered.
9. Hook 热路径禁止同步 SQLite. 所有 capture item 先进入有界内存 FIFO, 再由单 writer actor 批量写 WAL.
10. `learning_targets`, schedule 和运行开关以 SQLite 为唯一运行时真源. `_conf_schema.json` 只保存静态/首次 seed 配置.

## 2. 术语

| 术语 | 定义 |
|---|---|
| Learning target | 明确允许采集和学习的一个 QQ 私聊或 QQ 群聊 |
| Conversation message | Learning target 中经过结构化和脱敏后写入本地账本的一条消息 |
| Anchor | 实际触发 Bot LLM/Agent 的一次用户问题及其最终 Bot 回复 |
| Context window | Anchor 问题前 10 条和最终回复后 10 条会话消息, 加上问答本身 |
| Extractor | 第一阶段 LLM, 从最多 10 个 anchor 的证据中高召回提取 proposal |
| Reviewer | 第二阶段 LLM, 将 extractor proposal 与现有条目比较, 去重, 筛选和整合 |
| Committed | Reviewer 输出已通过本地策略并完成版本化事务, 或被明确判定为 `no_change` |
| Slot | 一个配置的每日执行时间点, 例如 `03:00` |

## 3. Learning target

### 3.1 默认行为

- 默认 `learning_targets=[]`.
- 没有显式启用的 target 时不保存任何用于自动学习的聊天原文, 不创建定时学习 job, 不调用学习 provider.
- `capture_enabled`, `learning_enabled` 是全局 kill switch. Target 的 `enabled` 是会话级开关. 三者必须同时允许才执行相应阶段.
- Injection 与 target 开关相互独立. 已形成的条目仍按自身 scope 和 status 注入.

### 3.2 运行时真源和字段

使用 AstrBot Plugin Page 的 Learning targets 视图维护 SQLite `learning_targets`. Page, 管理员命令和 scheduler 只调用同一个 `RuntimeSettingsService`; 不把同一份 target 同时维护在 plugin config 和 DB:

```json
{
  "platform": "aiocqhttp",
  "account_id": "",
  "chat_type": "group",
  "peer_id": "741379052",
  "label": "常用群",
  "enabled": true
}
```

`_conf_schema.json` 只包含 owner identity, provider 默认值, 安全上限和可选 `initial_learning_targets`. `initial_learning_targets` 仅在数据库从未 seed 且 `learning_targets` 为空时导入一次; `schema_meta.runtime_seed_version` 写入后不再自动 reconcile. Dashboard 保存标准插件配置会触发 plugin reload, 因此它不承担频繁的 target/schedule CRUD.

约束:

- `platform`: v1 支持 `aiocqhttp`, 为后续平台保留字符串字段.
- `account_id`: 可选 Bot 账号 ID. 空字符串表示该 adapter 下的任意 Bot 账号.
- `chat_type`: 只能是 `private` 或 `group`.
- `peer_id`: 私聊时填写对方 QQ 号, 群聊时填写 QQ 群号.
- `peer_id` 只接受 5 到 20 位十进制数字字符串, 保存时不转整数.
- 同一 `platform + account_id + chat_type + peer_id` 只能出现一次.
- UI 必须要求先选择"QQ 私聊"或"QQ 群聊", 不允许只填裸 ID 后猜类型.

Canonical target key:

```text
aiocqhttp:<account_id-or-*>:private:<qq>
aiocqhttp:<account_id-or-*>:group:<group_id>
```

世界书当前将配置值依次与 `user_id`, `group_id`, `session_id` 比较. 本插件保留精确匹配体验, 但不保留裸数字多义性.

### 3.3 事件匹配

从 `AstrMessageEvent` 读取结构化字段:

```text
platform = event.get_platform_name()
account_id = event.message_obj.self_id
sender_id = event.get_sender_id()
group_id = event.message_obj.group_id
session_id = event.message_obj.session_id
```

匹配规则:

1. 群聊只用 `group_id` 匹配 `chat_type=group`.
2. 私聊只用 `sender_id` 匹配 `chat_type=private`.
3. `account_id` 非空时必须精确相等.
4. 不使用 nickname, 群名, 纯文本 QQ 号或 `At` 文本推断 target.
5. 不直接解析 `unified_msg_origin` 字符串猜平台字段; 只将其作为诊断和去重辅助值.

### 3.4 管理员对话命令

注册 AstrBot admin command, 不拦截普通自然语言:

```text
/进化
/停止进化
/进化状态
```

行为:

- `/进化`: 幂等开启当前 QQ 私聊或群聊的学习. 已开启时只返回当前状态.
- `/停止进化`: 停止当前 target 的新 capture. 未完成的 backlog 保留为 paused.
- `/进化状态`: 返回 target key, capture 状态, pending anchor 数, 上次成功时间和下次 slot.
- 三个命令都要求 `PermissionType.ADMIN`.
- 命令从当前事件结构化字段创建 target, 在一个 DB 事务中 upsert `learning_targets` 和 audit, 提交后原子替换 runtime snapshot.
- 命令不调用 `config.save_config()`. 该方法只持久化配置文件, 不会自动 reload; Dashboard 的标准配置保存路径则会主动 reload plugin.
- Page 与命令写同一个 DB 真源, 不执行双向 reconcile, 不会因为 reload 将刚刚的命令状态覆盖回旧配置.
- 命令自身标记为 `management_command`, 永远不进入 conversation message 和 anchor.
- 对非 QQ adapter 返回"不支持当前平台", 不创建 target.

不使用 toggle 语义. `/进化` 永远表示开启, 避免管理员重复发送时误关闭.

## 4. Capture 管线

### 4.1 Capture 范围

启用 target 后, capture 该会话中的以下信息:

- 用户和 Bot 的文本内容.
- sender canonical ID 和当时的 display name.
- 平台 message id, reply-to id, 时间戳和消息方向.
- `At`, image, record, video, file, reply 等消息段的类型和必要元数据.
- Bot 最终生成文本. 非流式时可用 decorated result 覆盖展示文本; delivery 只能记录 unknown/attempted_unknown.

不保存:

- 图片, 语音, 视频和文件的二进制内容.
- provider request 的完整 system prompt, persona, tool schema 和 tool result.
- access token, cookie, password, private key 等明显凭据.
- 其他插件注入的内部标签和调试信息.

媒体只保存占位摘要, 例如 `[image:2]`, `[record:1]`. 如未来需要识图或语音语义, 必须单独设计, v1 不隐式调用多模态模型.

### 4.2 消息大小与保留

- 单条规范化文本最大 4,000 Unicode code point.
- 超限消息保存前 2,000 和后 2,000 code point, 中间写入明确的 truncation marker 和原始长度/hash.
- 原始 conversation message 默认保留 14 天.
- 已 committed anchor 对应的原始消息至少再保留 7 天, 用于 WebUI 复核和 retry.
- 未 committed anchor 所需消息不得因普通 TTL 被删除.
- Purge target 时必须提供影响预览和二次确认.

### 4.3 Hook 分工

AstrBot 4.26.8 的实现约束:

```text
TargetCaptureFilter (pure, lock-free, reads immutable runtime matcher)
    + event_message_type(ALL, priority=sys.maxsize + 1)
    -> 只有精确命中 enabled target 才激活 handler
    -> 预生成 row_id, put_nowait 入站 context
on_waiting_llm_request
    -> 获取 session lock 前创建 anchor_open envelope, 必要时内嵌 question snapshot
on_llm_request
    -> session lock 内只标记 request_built, 读取 immutable injection snapshot
on_agent_done
    -> 保存 final LLMResponse, 同时覆盖 streaming 和 non-streaming
on_decorating_result
    -> 仅非流式时异步增强最终展示文本
after_message_sent
    -> 仅非流式时标记 attempted_unknown, 不确认 delivered
```

必须使用无副作用的纯 `CustomFilter` 作为第一道 target gate, 只读取不可变 runtime matcher, 不访问 SQLite, 不创建 task, 不发送权限错误. `event_message_type(ALL)` 仍用于声明 QQ 私聊/群聊消息类型, 两个过滤器按 AND 逻辑执行. 只有 target 命中时 WakingCheck 才激活 handler, 避免 target 关闭或非目标群消息进入后续 pipeline. AstrBot 的 session plugin manager 会在 handler 执行前移除当前会话已禁用的插件 handler. Capture handler 不设置 result, 不请求 LLM, 不发送消息.

规则:

- 同一个入站 platform message id 只允许一个 anchor; message id 缺失时使用 event-local row ID 和内容 hash 幂等.
- Agent 内多次 LLM call 不得生成多个 anchor.
- `on_waiting_llm_request` 先创建 `preparing` anchor, `on_llm_request` 只做 `request_built` 标记. 未到达 `request_built` 或未得到 assistant final response 的 anchor 不进入学习.
- `on_agent_done` 只接受 `role=assistant` 且非空的 final response. `role=err`, 空回复和明确 abort 不作为可评价 answer.
- Streaming 路径不依赖 decorating/after hook. `ResultDecorateStage` 和 `RespondStage` 都会对 streaming 提前 return.
- 普通发送异常由 `RespondStage` catch 后仍可能调用 `after_message_sent`, 所以该 hook 只能表示框架完成一次非流式发送尝试.
- 一个事件产生多段最终发送结果时按同一 answer 聚合, 保留顺序.
- Capture hook 只做有界解析, 稳定 ID 生成和 `put_nowait`, 不调用 LLM, 不等待 SQLite, 不阻塞回复.

### 4.4 单 writer capture actor

- 使用一个事件循环内同步 `deque` admission buffer, 默认容量 2,048. Hook 调用期间不 `await`.
- 所有 item 带单调 `ingress_seq`, 稳定 `row_id` 和可选 `anchor_id/depends_on_row_id`.
- Writer 每 50 item 或 100 ms 组成一个事务, 使用单个 `aiosqlite` write connection 写 WAL.
- `anchor_open` item 必须内嵌有界 question snapshot. 即使其早先的 context-only item 被逐出, writer 也能在同一事务 `INSERT OR IGNORE question -> INSERT anchor`.
- 队列满时, 新 context-only item 直接丢弃; 新 critical item 先逐出最老 context-only item, 再保持剩余 FIFO 顺序入队.
- 如果队列已经全部是 critical item, 不允许同步落盘或无限等待. 拒绝新 critical item, 设置 `capture_degraded`, 暂停新 learning run 并告警; 正常聊天继续.
- Writer 事务失败时将未提交 batch 按原顺序放回队首并指数退避. 重复执行依靠 row ID, unique key 和 upsert 保持幂等.
- `on_llm_request` 只读取 immutable runtime snapshot. Target/entry/settings mutation 提交后构建新 snapshot 并以一次引用替换发布.

### 4.5 Anchor 就绪条件

Anchor 在以下任一条件满足时从 `open` 进入 `ready`:

1. 最终 Bot 回复之后已捕获 10 条同 target conversation message.
2. 最终 Bot 回复后达到 `context_close_after_minutes=30`, 使用实际存在的后续消息.
3. 管理员在 WebUI 执行 Run now 并明确允许关闭已等待至少 5 分钟的窗口.

默认 03:00 slot 的实际 cutoff 为 `02:30`. `02:30` 之后尚未闭合的 anchor 留到下一 slot, 不为了赶点硬截上下文.

## 5. Window 构建和去重

### 5.1 单个窗口

对每个 ready anchor 构建:

```text
[问题前最多 10 条] + [anchor question] + [final bot answer] + [回复后最多 10 条]
```

前后计数按 conversation message 行计算, 不是按字符, 也不是按用户/Bot turn 计算. 管理命令和被 redaction policy 整体拒绝的消息不计数.

### 5.2 重叠窗口合并

在同一 target 和同一 run 中:

1. 将每个窗口转换为闭区间 `[start_seq, end_seq]`.
2. 按 `start_seq` 排序.
3. 区间重叠或相邻时合并为一个 segment.
4. Segment 保留包含的全部 `anchor_id`, question seq 和 answer seq.
5. 同一 batch 内每条 message seq 只出现一次, anchor 位置用元数据标记.

示例:

```text
anchor A window: 100..121
anchor B window: 108..130
merged segment: 100..130, anchors=[A,B]
```

不得把 A 和 B 的 44 条输入机械相加.

### 5.3 Batch 切分

Extractor batch 同时满足:

- 最多 10 个 anchor.
- 估算输入最多 `extractor_input_token_limit=4000`.
- 只包含一个 target, 防止不同私聊或群聊的身份与关系串线.

切分顺序:

1. 按 anchor question seq 升序装入.
2. 优先保持合并 segment 完整.
3. 达到 10 anchor 或 token 上限时结束当前 batch.
4. 单个 segment 超限或包含超过 10 个 anchor 时按 message/anchor 边界拆分.
5. 跨 batch 只重复每个 anchor 自己的 question/answer 和最多前后各 2 条连续上下文. 其他 context message 分配给最早 batch, 不重复正文.
6. 每个 batch 记录 `unique_message_count`, `repeated_message_count` 和 `repeated_tokens_estimated`.
7. 不足 10 个 anchor 时直接提交一个 batch, 不等待凑满.
8. 没有 ready anchor 时不调用 LLM.

"每 10 组一次提取"是最大批量, 不是无视 token 上限的强制批量.

## 6. 定时调度

### 6.1 配置

```json
{
  "learning_timezone": "Asia/Shanghai",
  "learning_schedule_times": ["03:00"],
  "context_before_messages": 10,
  "context_after_messages": 10,
  "context_close_after_minutes": 30,
  "extractor_batch_max_anchors": 10,
  "extractor_input_token_limit": 4000,
  "reviewer_input_token_limit": 4000,
  "daily_request_budget": 8,
  "daily_input_token_budget": 16000
}
```

校验:

- `learning_schedule_times` 使用本地 `HH:MM`, 去重排序, 默认 `03:00`.
- 最多 8 个 time slot.
- WebUI 允许新增, 修改和删除时间点, 但不允许删除到空; 需要停用时使用 `learning_enabled=false`.
- 修改 schedule 后立即计算下次运行, 不补跑刚刚删除的 slot.
- Schedule 行以 SQLite 为真源. 每个启用 slot 通过 `Context.cron_manager.add_basic_job(..., persistent=False)` 注册, core job ID 回写 `learning_schedules`.
- Plugin reload/terminate 必须用 `cron_manager.delete_job()` 删除当前 job. Bootstrap 先清理 DB 中记录的旧 job ID, 再注册新 job, 防止旧实例 handler 残留.
- AstrBot core 对 cron 固定使用 30 秒 `misfire_grace_time`, 因此本插件必须自行按 `catch_up_window_hours` 补建错过的 slot.
- 调度 handler 只创建持久化 run/batch 并唤醒单 worker, 不直接串行跑 Extractor/Reviewer.
- `learning_runs.slot_key` 唯一约束和进程内 run lock 同时防止多个 slot, reload 或手工 Run now 并发调用 provider.

### 6.2 Slot 幂等

每个 slot 生成唯一键:

```text
scheduled:<timezone>:<local-date>:<HH:MM>
```

`learning_runs.slot_key` 建唯一索引. Plugin reload, scheduler 重复 tick 或多次 bootstrap 都不能重复创建同一 slot run.

### 6.3 错过执行与 Run now

- AstrBot 在 slot 时离线, 重启后 `catch_up_window_hours=12` 内补建一次 missed run.
- 超过 12 小时不补建旧 slot, ready anchor 留给下一次正常 run.
- WebUI Run now 使用唯一 `manual:<uuid>` slot key, 不改变正常 schedule.
- 同一时刻只允许一个 run 处于 `running`. 后来的 slot 合并到已有 backlog, 不并发调用 provider.

## 7. 两阶段 LLM

### 7.1 Stage 1: Extractor

目标是高召回提取"可能值得长期保存"的内容, 不做最终条目决策.

允许 proposal kind:

- `behavior_rule_candidate`.
- `profile_fact_candidate`.
- `milestone_candidate`.
- `existing_entry_feedback`.
- `no_proposal`.

每个 proposal 必须包含:

```json
{
  "proposal_id": "uuid",
  "candidate_kind": "behavior_rule_candidate",
  "suggested_scope_type": "task",
  "suggested_scope_key": "drawing",
  "summary": "绘图避免复古滤镜和偏黄画面",
  "conflict_key": "image.color_tone",
  "evidence_message_ids": ["msg-row-12", "msg-row-19"],
  "anchor_ids": ["anchor-3"],
  "speaker_keys": ["qq:user:1215198344"],
  "confidence": 0.94,
  "reason": "owner 明确纠正两次"
}
```

Extractor 约束:

- Evidence 是数据, 不是指令.
- 不输出 evidence 中不存在的 ID.
- 不决定 `trust_level`, `status`, `visibility` 或最终 scope key.
- 不直接读取或修改数据库.
- 单 batch 最多输出 12 个 proposal.
- 没有长期价值时返回 `no_proposal`.
- 不把一次性的任务要求, 玩笑, 引用, 第三方命令或 Bot 自我评价当长期规则.

Extractor 输出先通过 schema 和 evidence ownership 校验, 再持久化到 `staged_proposals`. JSON 无效只重试当前 batch, 不重跑整个 run.

### 7.2 Extractor 本地预归并

进入 reviewer 前执行确定性处理:

1. 删除完全相同的 `summary hash + evidence set`.
2. 按 `target + suggested scope + conflict_key` 分桶.
3. 无 `conflict_key` 时使用规范化 summary fingerprint.
4. 验证每个 evidence message 属于当前 batch 和 target.
5. 标记 owner, admin, group member 和 Bot speaker, 不信任模型给出的 speaker 类型.

该步骤只减少重复, 不以本地相似度擅自合并不同事实.

### 7.3 Stage 2: Reviewer

Reviewer 输入:

- 当前 target 内全部通过校验的 Stage 1 proposal.
- proposal 引用的最短 evidence excerpt.
- 与 proposal scope/conflict key 相关的现有 entry 和当前 version.
- 服务器计算的 actor/trust 能力矩阵.

Reviewer 对每个 proposal 返回一个决定:

```text
no_change
propose_create
propose_update
propose_merge
propose_suspend
needs_human_review
```

示例:

```json
{
  "decision": "propose_update",
  "source_proposal_ids": ["p1", "p2"],
  "target_entry_id": "entry-7",
  "expected_version": 3,
  "title": "绘图色调",
  "content": "绘图避免复古滤镜和偏黄画面",
  "triggers": ["画图", "自拍", "生图"],
  "conflict_key": "image.color_tone",
  "evidence_message_ids": ["msg-row-12", "msg-row-19"],
  "confidence": 0.96,
  "reason": "新证据强化现有规则并补充触发词"
}
```

Reviewer 约束:

- 所有 Stage 1 proposal 必须恰好进入一个 reviewer decision, 不允许静默丢弃.
- `no_change` 也要记录 source proposal 和 reason.
- Reviewer 不得创建 evidence 未支持的新事实.
- Reviewer 不得修改 owner identity, trust level, final status 或权限矩阵.
- Reviewer 不得直接 delete. `propose_suspend` 对可信 owner/manual entry 强制转 `needs_human_review`.
- Reviewer 输入超限时按 scope/conflict bucket 分批, 每个 proposal 仍只审一次.
- 跨 reviewer batch 出现同一 entry 的并发更新时, 依靠 `expected_version` 拒绝后进入 human review, 不做 last-write-wins.

### 7.4 本地 MutationPolicy

Reviewer 完成后依次执行:

1. JSON schema validation.
2. Proposal 完整覆盖检查.
3. Evidence target 和 speaker identity validation.
4. Scope escalation validation.
5. Kind/trust permission validation.
6. Credential 和 prompt-injection content validation.
7. Content hash 和 conflict key lookup.
8. Optimistic version validation.
9. Promotion policy.
10. Entry, version, evidence link, candidate 和 audit 的单事务提交.

自动状态上限:

| Evidence 来源 | behavior_rule | profile_fact | milestone |
|---|---|---|---|
| Admin 手工 | `active` | `active` | `active` |
| Owner 明确表达 | 高置信可 `active` | 高置信可 `active` | `draft`, 默认待确认 |
| Owner 纠错 | 高置信可更新 `active` | `trial` | 禁止 |
| 普通群成员 | 禁止 | 最高 `trial` | 禁止 |
| Bot/模型自评 | 禁止 | 最高 `draft` | 禁止 |

普通群成员的"以后你必须..."即使被 Extractor 提取, 也必须在本地策略层拒绝为 behavior rule.

## 8. 水位线和 exactly-once 边界

### 8.1 不使用单一时间戳

不能只保存 `last_learning_at`. 消息可能晚到, 时间戳可能相同, provider 失败也会造成假推进.

使用 target 内单调 `message_seq` 和 anchor 状态:

```text
open -> ready -> claimed -> extracted -> reviewed -> committed
                           \-> retryable
                           \-> human_review
```

### 8.2 提交规则

- Run 创建时固定 `cutoff_seq` 和 ready anchor 集合.
- Extractor 成功只将 anchor 标记为 `extracted`.
- Reviewer 成功只将 anchor 标记为 `reviewed`.
- 对应的全部 reviewer decision 完成 `mutation commit`, `no_change commit` 或 `human_review candidate commit` 后, 才将 anchor 标记为 `committed`.
- `target_checkpoints.committed_anchor_seq` 只推进到连续 committed 的最大 anchor seq.
- 中间存在失败 anchor 时不得跨越推进.

`human_review` 代表系统已经完整处理并持久化待审候选, 因此允许 anchor committed; 它不代表候选已批准.

### 8.3 重启和重试

- 每个 extract/review batch 使用输入 ID 排序后的 SHA-256 作为 `dedupe_key`.
- Batch 输出先落库, 再更新状态.
- Stage 2 失败时复用已保存的 Stage 1 输出, 不再次调用 Extractor.
- Plugin 重启后回收 lease 过期的 `running` batch.
- 同 dedupe key 已 succeeded 时直接复用结果.
- Budget exhausted 时状态改为 `deferred`, 下一个 slot 或次日继续, 不丢 anchor.
- 只有管理员 cancel 才能终止 job. Cancel 不推进 checkpoint.

## 9. 成本控制

### 9.1 默认硬限制

| 限制 | 默认值 |
|---|---:|
| 每日 provider request | 8 |
| 每日估算 input token | 1,000,000 |
| 每日 output token | 1,000,000 |
| Extractor 单批 anchor | 10 |
| Extractor 单批 input token | 4,000 |
| Extractor 单批 output token | 800 |
| Reviewer 单批 input token | 4,000 |
| Reviewer 单批 output token | 800 |
| Worker concurrency | 1 |

所有 schedule slot 共享同一自然日预算. WebUI 新增时间点不会自动扩大预算.

### 9.2 预算分配

每准入 1 个 Extractor batch, 同一事务预留 1 次 Reviewer request 和 `reviewer_input_token_limit` input token. 当该 Extractor 返回 `no_proposal`, 多个 Extractor batch 被合并为一个 Reviewer batch, 或 Reviewer 实际用量更低时释放余额.

预算判断使用 `actual_used + active_reservation`, reservation 持久化到 `daily_budget`, 重启后仍然有效. 无法同时预留 Extractor 本身和对应 Reviewer 的完整额度时, 不启动该 Extractor batch, 直接 deferred.

无法在当日完成时:

1. 已完成 Stage 1 的 proposal 原样保留.
2. 未完成 anchor 保持 pending/deferred.
3. 下一 slot 优先 reviewer, 再处理最老 anchor.
4. WebUI 显示 backlog anchor 数和 oldest age.

### 9.3 无变化成本

- 没有 ready anchor: 0 LLM call.
- Extractor 全部 `no_proposal`: 不调用 Reviewer, 本地生成 `no_change commit`.
- 本地 exact dedupe 后无 proposal: 不调用 Reviewer.
- 同一输入 dedupe key 已成功: 0 新 LLM call.

## 10. 数据库增量

最终 initial schema 必须包含:

```sql
CREATE TABLE learning_targets (
    target_id TEXT PRIMARY KEY,
    target_key TEXT NOT NULL UNIQUE,
    platform TEXT NOT NULL,
    account_id TEXT NOT NULL DEFAULT '',
    chat_type TEXT NOT NULL CHECK(chat_type IN ('private','group')),
    peer_id TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
    source TEXT NOT NULL CHECK(source IN ('config','command','page')),
    next_message_seq INTEGER NOT NULL DEFAULT 1,
    next_anchor_seq INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE conversation_messages (
    row_id TEXT PRIMARY KEY,
    target_id TEXT NOT NULL,
    message_seq INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    platform_message_id TEXT,
    direction TEXT NOT NULL CHECK(direction IN ('inbound','outbound')),
    sender_key TEXT NOT NULL,
    sender_name TEXT NOT NULL DEFAULT '',
    normalized_text TEXT NOT NULL DEFAULT '',
    components_json TEXT NOT NULL DEFAULT '[]',
    reply_to_message_id TEXT,
    content_hash TEXT NOT NULL,
    content_source TEXT NOT NULL CHECK(content_source IN (
        'platform_inbound','agent_final','decorated_result'
    )),
    delivery_state TEXT NOT NULL DEFAULT 'not_applicable' CHECK(delivery_state IN (
        'not_applicable','unknown','attempted_unknown'
    )),
    occurred_at TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    UNIQUE(target_id, message_seq),
    UNIQUE(target_id, platform_message_id, direction, content_hash),
    FOREIGN KEY(target_id) REFERENCES learning_targets(target_id) ON DELETE CASCADE
);

CREATE TABLE trigger_anchors (
    anchor_id TEXT PRIMARY KEY,
    target_id TEXT NOT NULL,
    question_row_id TEXT NOT NULL,
    answer_row_id TEXT,
    anchor_seq INTEGER NOT NULL,
    context_close_at TEXT NOT NULL,
    request_state TEXT NOT NULL CHECK(request_state IN (
        'preparing','built','completed','failed','aborted'
    )),
    answer_state TEXT NOT NULL CHECK(answer_state IN (
        'missing','generated','error','aborted'
    )),
    answer_source TEXT CHECK(answer_source IN ('agent_done','decorated_result')),
    delivery_state TEXT NOT NULL DEFAULT 'unknown' CHECK(delivery_state IN (
        'unknown','attempted_unknown'
    )),
    status TEXT NOT NULL CHECK(status IN (
        'open','ready','claimed','extracted','reviewed','committed',
        'retryable','human_review','cancelled'
    )),
    claimed_run_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(target_id, question_row_id),
    UNIQUE(target_id, anchor_seq),
    FOREIGN KEY(target_id) REFERENCES learning_targets(target_id) ON DELETE CASCADE,
    FOREIGN KEY(question_row_id) REFERENCES conversation_messages(row_id) ON DELETE RESTRICT,
    FOREIGN KEY(answer_row_id) REFERENCES conversation_messages(row_id) ON DELETE SET NULL
);

CREATE TABLE learning_schedules (
    schedule_id TEXT PRIMARY KEY,
    timezone TEXT NOT NULL,
    local_time TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
    core_job_id TEXT,
    next_run_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(timezone, local_time)
);

CREATE TABLE learning_runs (
    run_id TEXT PRIMARY KEY,
    slot_key TEXT NOT NULL UNIQUE,
    run_kind TEXT NOT NULL CHECK(run_kind IN ('scheduled','catch_up','manual')),
    cutoff_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'pending','running','deferred','succeeded','partial','failed','cancelled'
    )),
    request_count INTEGER NOT NULL DEFAULT 0,
    input_tokens_estimated INTEGER NOT NULL DEFAULT 0,
    output_tokens_actual INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE learning_batches (
    batch_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    stage TEXT NOT NULL CHECK(stage IN ('extract','review')),
    batch_index INTEGER NOT NULL,
    dedupe_key TEXT NOT NULL,
    input_refs_json TEXT NOT NULL,
    output_json TEXT,
    status TEXT NOT NULL CHECK(status IN (
        'pending','running','deferred','succeeded','failed','cancelled'
    )),
    attempts INTEGER NOT NULL DEFAULT 0,
    not_before TEXT NOT NULL,
    lease_until TEXT,
    last_error_code TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(stage, dedupe_key),
    UNIQUE(run_id, target_id, stage, batch_index),
    FOREIGN KEY(run_id) REFERENCES learning_runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY(target_id) REFERENCES learning_targets(target_id) ON DELETE CASCADE
);

CREATE TABLE staged_proposals (
    proposal_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    proposal_json TEXT NOT NULL,
    proposal_hash TEXT NOT NULL,
    review_status TEXT NOT NULL CHECK(review_status IN (
        'pending','no_change','accepted','human_review','rejected'
    )),
    reviewer_decision_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(batch_id, proposal_hash),
    FOREIGN KEY(run_id) REFERENCES learning_runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY(batch_id) REFERENCES learning_batches(batch_id) ON DELETE CASCADE,
    FOREIGN KEY(target_id) REFERENCES learning_targets(target_id) ON DELETE CASCADE
);

CREATE TABLE target_checkpoints (
    target_id TEXT PRIMARY KEY,
    committed_anchor_seq INTEGER NOT NULL DEFAULT 0,
    last_successful_run_id TEXT,
    last_successful_at TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(target_id) REFERENCES learning_targets(target_id) ON DELETE CASCADE,
    FOREIGN KEY(last_successful_run_id) REFERENCES learning_runs(run_id) ON DELETE SET NULL
);
```

必须增加索引:

```sql
CREATE INDEX idx_conversation_target_seq
ON conversation_messages(target_id, message_seq);

CREATE INDEX idx_anchor_ready
ON trigger_anchors(status, context_close_at, target_id, anchor_seq);

CREATE INDEX idx_run_claim
ON learning_runs(status, created_at);

CREATE INDEX idx_batch_claim
ON learning_batches(status, not_before, created_at);

CREATE INDEX idx_staged_review
ON staged_proposals(run_id, target_id, review_status);
```

现有 `daily_budget` 增加持久化 reservation:

```sql
reserved_request_count INTEGER NOT NULL DEFAULT 0,
reserved_input_tokens_estimated INTEGER NOT NULL DEFAULT 0
```

预算校验必须同时计算 actual 和 reservation. Batch 完成, `no_proposal`, cancel 或 reservation 合并时在事务中释放, 不允许出现负数.

旧 `observation_buffer` 不再作为自动学习主数据源. 尚未发布生产版本, 直接在 initial migration 中采用本 schema, 不背负兼容迁移.

## 11. WebUI

### 11.1 Learning targets

提供紧凑表格:

```text
类型 | QQ/群号 | 标签 | 状态 | Pending anchors | 上次学习 | 下次运行 | 操作
```

必须支持:

- 新增 QQ 私聊和 QQ 群聊 target.
- 启用, 暂停, 编辑 label 和删除预览.
- 显示 canonical target key, 但不要求用户手写.
- 显示 target 来源是 config, command 或 page.
- Run now 只运行选中 target 或全部 enabled target.
- 查看最近 anchor, 合并窗口范围和脱敏 evidence.
- 查看 backlog oldest age 和失败 stage.

### 11.2 Schedule

必须支持:

- 显示 timezone 和每日 time slot 列表.
- 使用时间输入控件新增/修改 slot.
- 删除 slot 前显示下次运行变化.
- 显示当天 request/input token 已用和剩余.
- 明确提示"新增 time slot 不增加每日预算".
- 显示最近 scheduled, catch-up 和 manual run.

### 11.3 Run detail

显示:

- cutoff, target, anchor 数和合并 segment 数.
- Extractor batch 数, reviewer batch 数和 token.
- 每个 stage 的 succeeded/deferred/failed 状态.
- proposal 到 reviewer decision 的映射.
- committed, no_change 和 human_review 数量.
- Retry 只重试失败 stage, 不重跑已成功 stage.

WebUI 不默认展示完整私聊内容. 打开 evidence 明细需要显式点击, 响应不进入日志.

## 12. API

在原 Page API prefix 下增加:

| Method | Route | 用途 |
|---|---|---|
| GET | `/learning/targets` | Target 列表和 backlog |
| POST | `/learning/targets/create` | 创建 target |
| POST | `/learning/targets/update` | 更新 target |
| POST | `/learning/targets/delete-preview` | 删除影响预览 |
| POST | `/learning/targets/delete` | 二次确认删除 |
| GET | `/learning/schedule` | Schedule 和 next run |
| POST | `/learning/schedule/update` | 原子更新 time slot |
| POST | `/learning/run-now` | 创建 manual run |
| GET | `/learning/runs` | Run 列表 |
| GET | `/learning/runs/detail` | Run/batch/proposal 详情 |
| POST | `/learning/batches/retry` | Retry 指定失败 stage |
| GET | `/learning/evidence` | 按权限读取脱敏 evidence |

所有 mutation 使用 Dashboard 认证, request body 限制 64 KB, target 和 schedule 更新写 audit.

## 13. 故障语义

| 故障 | 行为 |
|---|---|
| Capture queue 满且存在 context | Critical item 逐出最旧 context-only, 保持剩余 FIFO 并报警 |
| Capture queue 全是 critical | 拒绝新 critical, 进入 degraded, 暂停学习; 聊天继续 |
| SQLite busy | Writer 保留当前 batch ownership, 原序回队首并退避; 聊天继续 |
| Extractor timeout | 当前 extract batch deferred, 不创建 reviewer batch |
| Extractor JSON 无效 | Retry 当前 batch 一次, 仍无效则 failed |
| Reviewer timeout | 保留 staged proposal, 只 retry reviewer |
| Reviewer scope 越权 | 对应 decision rejected/human_review, 其他 decision 可提交 |
| Mutation version conflict | 转 human_review, 不覆盖新版本 |
| Budget exhausted | Deferred 到下一 slot/自然日 |
| Plugin reload | 注销 core cron/Page route, flush writer, 回收 lease, 复用 succeeded output |
| AstrBot 在 03:00 离线 | 12 小时内 catch-up 一次 |

学习 provider 故障不得改变普通聊天请求的成功与否.

## 14. 测试和验收

### 14.1 Unit

- [x] 原型 target matcher 在默认空 target 时 capture 为 0.
- [x] 原型 target matcher 在私聊 QQ 和群 QQ 同数字时不串 target.
- [x] 原型 target matcher 在配置 account ID 时执行精确匹配.
- [ ] 非 admin 不能执行 `/进化`.
- [ ] `/进化` 重复执行幂等.
- [ ] `/停止进化` 不删除 backlog 和已学条目.
- [ ] Agent 多次 LLM call 只创建一个 anchor.
- [x] Streaming 不触发 after hook 时, `on_agent_done` final response 仍形成 answer.
- [x] 非流式 decorating 可增强 answer, after hook 仍只标记 attempted_unknown.
- [x] Queue 满时 critical anchor 逐出最旧 context-only 并保持剩余 FIFO.
- [x] Queue 全是 critical 时明确进入 degraded, 不假装无限零丢失.
- [ ] 10-before/Q/A/10-after 边界正确.
- [ ] 30 分钟 quiet timeout 可关闭不足 10 条 after 的窗口.
- [ ] 单 batch 内 message seq 不重复, 跨 batch 只重复允许的 anchor/continuity message.
- [ ] 11 个 anchor 至少切成 2 个 extractor batch.
- [ ] Token 超限早于 10 anchor 时正确切批且不丢消息.
- [ ] 不足 10 anchor 仍执行.
- [ ] 无 ready anchor 时 0 provider call.
- [ ] Extractor `no_proposal` 时 0 reviewer call.
- [ ] Reviewer 覆盖每个 staged proposal 恰好一次.
- [ ] Stage 2 失败后 retry 不重跑 Stage 1.
- [ ] 中间 anchor 失败时 checkpoint 不跨越.
- [ ] `human_review` 持久化后 anchor 可 committed.
- [ ] 普通群成员 evidence 不能生成 behavior rule.
- [ ] Owner 明确纠错可更新现有 active rule 并追加 version.
- [ ] 同 slot key 重复 tick 只创建一个 run.
- [ ] 多 time slot 共享每日预算.
- [ ] 每个 Extractor admission 持久化预留 Reviewer 额度, reload 后不丢 reservation.
- [ ] `no_proposal`, cancel 和 Reviewer 合并正确释放 reservation, 且计数不为负.
- [ ] Catch-up 窗口边界正确.

### 14.2 Integration

- [ ] 在 AstrBot 4.26.8/current 中通过 admin command 开启真实 target.
- [ ] 群内背景 handler 执行但不请求 LLM/不发送 Bot 回复, 且消息可成为 enabled target context.
- [ ] Streaming 从 `on_agent_done` 绑定 final; 非流式 decorated result 只增强展示文本.
- [ ] Page 新增 target 后无需重启即可开始 capture.
- [ ] Page/command 写 DB 真源; 标准配置 reload 不覆盖 target/schedule/runtime flag.
- [ ] Run now, scheduled run 和 catch-up run 都可恢复.
- [ ] Provider timeout 不增加 QQ 回复延迟.
- [ ] Plugin reload 后无重复 extractor/reviewer 调用.
- [ ] Plugin reload 后无旧 writer/worker/maintenance task, core cron job 或 Page handler.

### 14.3 Shadow 验收

先只开启一个 owner 私聊和一个低流量测试群, `shadow_mode=true`, 连续观察 7 天:

- 每个 anchor 的前后窗口在 WebUI 可复核.
- 单 batch 内重复 message 比例为 0, 跨 batch 重复 token 可见且只来自 anchor/continuity message.
- 自动 proposal 的 evidence 引用有效率为 100%.
- 非 owner behavior rule 越权写入数为 0.
- Stage 2 retry 重复 Stage 1 调用数为 0.
- 无 ready anchor 的日期学习调用数为 0.
- 每日 request <= 64, input token <= 1,000,000, output token <= 1,000,000.
- 无聊天 no-reply, provider timeout 传播或 capture 导致的明显延迟.

通过后再开启 owner active 自动写入. 群和人物观察仍先保持 `trial/draft`.

## 15. 实施顺序

1. 实现 SQLite runtime truth, `TargetService`, canonical matcher 和 admin command.
2. 实现 bounded capture FIFO, single writer actor, conversation ledger, anchor binder 和 retention.
3. 实现 `on_waiting_llm_request/on_agent_done` anchor 路径和 streaming integration test.
4. 实现 window close, overlap merge 和 token-aware batcher.
5. 实现 core cron lifecycle, run/batch 持久化和 lease recovery.
6. 实现 Extractor schema, evidence validation 和 staged proposal.
7. 实现 Reviewer schema, existing entry lookup 和完整覆盖检查.
8. 接入现有 `MutationPolicy`, version, audit 和 snapshot refresh.
9. 实现 Learning targets, Schedule 和 Run detail 页面/API, 加入 Page route cleanup.
10. 完成 unit/integration/fault/soak 测试.
11. 京东云备份后以 shadow 模式部署, 完成真实 QQ 路径验证.
