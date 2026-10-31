# 命理命名 Agent 技术设计

## Agent 边界

`naming_agent` 与 recipe、dev、chat 并列。命名 Agent 的确定性核心由 `app/naming/engine.py` 执行，负责历法、四柱、五行统计、候选生成、姓名评分和报告证据；LLM 不参与核心分数计算。

输入可以是人名、笔名或网名场景。生产环境应优先使用结构化表单，聊天入口只负责识别意图和补全缺失字段。

## 可复现策略

```text
input_fingerprint = SHA256(canonical_input_json)
cache_key = user_id + input_fingerprint + ruleset_version + knowledge_snapshot_id + variant_index
```

用户 ID 只用于候选排序的稳定盐值，不改变同一八字的基础分析。姓氏、风格、字辈、禁用字和用途变化时，必须生成新的指纹。

`created_at` 只用于审计。规则升级时增加 `ruleset_version`，旧结果保留不覆盖。

## 评分组成

当前 bootstrap 规则集将结果拆成：

- 八字五行平衡启发式：40 分；
- 汉字文化属性和出处：20 分；
- 名字整体结构与重复字检查：15 分；
- 用途和偏好匹配：15 分；
- 未知字、冷僻字、风险提示：10 分。

“喜用神”并不作为未经专家审核的硬结论。当前报告明确写作“五行平衡启发式”，待规则集经过专家审核后再增加流派化结论。

规则输入拆成四个可替换快照：

- `data/bazi_rules.json`：月令、五行生克、日主强弱和格局边界；
- `data/characters.json`：汉字五行和字义，逐字带来源；
- `data/kangxi_strokes.json`：康熙笔画，与 GF 3002 现代笔顺数据隔离；
- `data/risk_terms.json`：负面词、粗俗语义、文化联想和重复字风险。

这四类数据均在结果中携带版本号。扩充时应增加 `school`、`edition`、`annotator`、`reviewed_at` 和冲突记录，不能把不同书籍、流派或地区语料直接拼成一张“标准表”。

## 数据存储

- MySQL：命名运行记录、输入快照、结果快照、规则版本和用户归档；
- 本地 JSON：bootstrap 字符数据和参考文献清单；
- Milvus：后续存放古籍原文、规则解释和检索片段；不用于替代四柱和分数计算。

## 古籍 RAG 落地

命理 Agent 的检索集合为 `naming_knowledge`，字段至少包含 `source_id`、书名、版本、流派、章节、页码、原文、来源 URL、许可状态和 `knowledge_version`。导入脚本是 `scripts/import_naming_classics.py`，使用和菜谱/开发知识库相同的 embedding 与 Milvus 基础设施，但集合完全隔离。

请求链路为：用户问题 → 命理意图路由 → 古籍向量粗排 → rerank → 带证据 ID 的片段 → 命理 Agent 解释。古籍只负责“依据检索”，不直接修改 `NamingEngine` 的四柱、候选和评分结果；报告要同时标注“规则计算证据”和“古籍检索证据”。

当前已下载并导入五部相关古籍的全文录文：`三命通会`、`渊海子平`、`子平真诠评注`、`滴天髓阐微`、`穷通宝鉴`，共 1,260 个向量块。原 PDF 节选只保留在来源验证目录，不再作为生产 RAG 数据。对录文、扫描件、OCR 错字和不同底本，必须保留原行号/页码，并在关键断语处做双版本校对。

## API

```text
POST /api/v1/naming/analyze
POST /api/v1/naming/score
GET  /api/v1/naming/runs/{run_id}
```

返回 `report_markdown`、`bazi`、`score_breakdown`、`candidates`、`evidence` 和 `references`。姓名逆向分析必须同时提供出生信息；只有姓名时仅做语言文化分析，不反推出生八字。

