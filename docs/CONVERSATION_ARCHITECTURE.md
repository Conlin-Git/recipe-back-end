# 多轮对话架构改造说明

## 运行链路

每一轮对话先创建唯一 `turn_id`，立即持久化用户消息，再进入 LangGraph。图中先加载活动主题、结构化摘要、最近窗口和领域缓存，再由规则路由与模型路由共同决定领域。追问始终保留最近窗口，`summary_sufficient` 只作为兼容信息，不再直接丢弃精确消息。

回答完成后，助手消息、主题状态和热上下文分别写入 MySQL/Redis。Redis Stream 只承载实时事件和短期重放，最终答案以 MySQL 消息为准。

## 新增数据

`conversation_states` 保存每个会话的活动主题 JSON、修订号和更新时间。执行以下命令创建表，重复执行安全：

```bash
.venv/bin/python scripts/init_db.py
```

主题状态包含 `domain`、`topic_id`、`goal`、`entities`、`constraints`、`open_loops` 和 `last_turn_id`。Redis 丢失时会从 MySQL 恢复。

## 路由约束

路由输出必须包含领域、意图、置信度、主题动作、上下文范围、指代消解结果和检索词。模型 JSON 解析失败时使用规则路由：优先延续当前主题；无法判断时进入 chat；不会默认进入 recipe。

## 摘要约束

摘要压缩任务按会话加 Redis 锁，同一会话不会并发覆盖。摘要内容保存为 JSON，分为稳定事实、当前主题、已做决定、未完成事项和实体列表。

## 前端流式约束

SSE 的 `meta`、`delta`、终态事件都携带 `turn_id`。前端按 turn 定位 assistant 消息，重连或切换会话时不会把迟到 token 追加到另一条回答。用户可以对已完成回答点击“重新回答”。
