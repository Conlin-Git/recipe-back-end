# 高考志愿规划 Agent

## 当前链路

高考指南页面和智能问答现在共用 `GaokaoPlannerAgent`：

```text
POST /gaokao/guide 或智能问答
        ↓
GaokaoPlannerAgent
        ├─ 画像与口径校验
        ├─ 读取官方记录与已授权截图（分开标注）
        ├─ 读取同生源省、同科类一分一段表
        ├─ 位次可比性闸门
        └─ 冲 / 稳 / 保整理
        ↓
结构化结果 + agent_plan 审计轨迹
        ↓
（智能问答）LLM 只解释已锁定证据
```

此前只有智能问答调用过 `gaokao_agent`；指南页面直接调用规则服务，存在入口不一致的问题。本轮已经统一为规划 Agent。

## 数据安全边界

- Agent 不生成院校、专业、分数、位次或录取概率。
- 官方记录及获准批次的 `user_confirmed` 截图可以参与推荐；其他待验数据不参与。截图附原图行号，不能核对的位次不做精确分档。
- 没有同口径最低位次时，Agent 将分层步骤置为 `blocked/insufficient`，只保留来源最低分明细。
- 上海 2026 使用“综合”口径，不能拆成物理/历史，也不能拿上海分数横向换算四川位次。
- 结果里的 `agent_plan.evidence_locked=true` 表示解释层不得改写候选记录。

## 后续可扩展

若增加招生计划、专业组限制和志愿顺序组合，可在 `tier_organization` 后增加 `portfolio_builder` 步骤；仍需把组合候选限制在结构化数据 ID 内，LLM 只负责说明取舍和追问缺失条件。
