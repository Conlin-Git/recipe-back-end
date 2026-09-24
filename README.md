# recipe-back-end

多智能体智能问答助手后端。基于 FastAPI + LangGraph 多 Agent 编排：管理编排 agent 负责意图分类（追问/全新话题）与领域路由，情感分析 agent 并行分析用户情绪、动态调整回复语气，**菜谱 / 开发 / 闲聊**三个领域问答 agent 各司其职——人设与上下文按域隔离，互不污染。菜谱域背后是完整的 RAG 检索管线（语义检索 → 精排 → 常识校验）；开发域是纯 LLM 架构师问答；闲聊域负责日常唠嗑与情感陪伴。流式生成与连接解耦：token 事件缓存在 Redis Stream，客户端断开/刷新不影响生成，随时带断点重连续看。

配套前端仓库：[recipe-front-end](../recipe-front-end)。

## 功能特性

- **多 Agent 编排（LangGraph）**
  - 管理编排 agent：主模型做意图分类（追问/全新话题）+ 领域路由（菜谱/开发/闲聊），JSON 结构化输出，解析失败按菜谱域检索兜底
  - 情感分析 agent：免费小模型与编排并行，输出情绪标签 + 语气建议注入问答 agent，异常 fail-open 为中性
  - 菜谱问答 agent：ReAct 循环 + `search_recipe` 工具，会话缓存优先、不足自行检索兜底
  - 开发知识问答 agent：纯 LLM 技术问答（架构师视角，回答长度随问题类型伸缩）；闲聊 agent：人设唠嗑、情感陪伴
- **人设与上下文按域隔离（防跨域污染）**
  - 人设按域组装：做菜人设（Char 的忌口、专属菜谱、创意菜小怪癖）只给菜谱 agent；闲聊 agent 只带基础人设；开发 agent 不带任何生活人设——技术回答不会串菜谱梗
  - 上下文分层注入：追问且摘要够答 → 只带摘要；追问但摘要不足 → 摘要 + Redis 滑窗；**全新话题 → 只带摘要不带滑窗**——上一领域的对话原文不进当前领域的 prompt，开发回答不会被做菜历史带偏
  - 摘要压缩按领域分述（菜谱给做法要点 / 技术给方案要点 / 闲聊给情绪状态），摘要本身不带领域偏见
- **流式生成与连接解耦（断网续传）**
  - `POST /chat` 只"点火"：起后台 asyncio 任务跑图，token 事件 XADD 进 Redis Stream（`chat:stream:{cid}`），立即返回会话 ID
  - `GET /chat/{id}/stream?last_id=` 是可重入的"订阅"端点：断点重放缓冲 + 阻塞跟随实时事件；终止按钮/断网/刷新只断本地连接，后端照跑不误
  - `chat:gen:{cid}` 生成中标记：POST 防重入（同会话同时只跑一个生成）、会话列表打标（前端刷新后据此自动续看）
- **多轮上下文**：Redis 热上下文（最近 N 轮原文 + 滚动摘要），三级整形（入库剥离图片链接、读时截断旧消息、超预算整轮丢弃）；窗口溢出时后台任务压缩摘要，不阻塞响应；Redis 丢失可从 MySQL 恢复
- **会话级菜谱缓存**：RAG 命中的菜谱片段写入 `chat:recipes:{conversation_id}`，同会话追问同一道菜直接复用，省 embed/milvus/rerank/校验全链路开销
- **RAG 检索管线（菜谱域）**
  - bge-m3 向量化 → Milvus 余弦相似度粗排（over-fetch 20 条候选）
  - bge-reranker-v2-m3 交叉编码器精排，带相关性阈值
  - 免费小模型（Qwen2.5-7B）对候选做烹饪常识校验，不通过换下一条候选（只多一次调用，不重新检索），全部不通过退化为纯 LLM
  - 校验服务超时/异常时 fail-open，不阻塞主链路
- **混合存储**：向量与元数据在 Milvus；步骤正文与配图、食材关系在 Neo4j，仅候选被采用后才按需补取，省带宽
- **用户体系**：注册 / 登录（JWT）、资料修改、头像上传（静态目录挂载）、会话增删查
- **限流防护**：登录防爆破（IP + 用户名双维度固定窗口）、对话按用户限流（每次生成触发编排/情感/校验多路 LLM 调用，限流在点火前）
- **可观测性**：可选 LangSmith 全链路 tracing（编排/情感/检索/校验/生成各节点）
- **数据导入脚本**：`recipes.xlsx` 批量向量化导入 Milvus + Neo4j，upsert/MERGE 幂等，支持增量与 `--fresh` 全量重建，embedding 带本地缓存

## 架构

```
                 ┌──────────────────────────────────────────────┐
   前端 ───────▶ │                FastAPI                        │
   POST /chat    │  api/v1: chat(点火/订阅) / auth / user / conv  │
   GET  /stream  │      │                                        │
      ▲          │  stream_service（流式生成管理：生成与连接解耦） │
      │          │    点火：防重入 → 起后台任务跑图                │
      └──SSE订阅─┤    订阅：last_id 断点重放 + 阻塞跟随            │
                 │      │                                        │
                 │  app/graph（LangGraph 多 Agent 编排）           │
                 │      load_context ─┬→ sentiment_agent（情感）   │
                 │                    └→ orchestrator（管理编排）   │
                 │                              │ join 汇聚后路由  │
                 │        ┌── recipe_agent ⇄ recipe_tools(ReAct)  │
                 │        ├── dev_agent（纯LLM技术问答）            │
                 │        └── chat_agent（闲聊/情感陪伴）           │
                 │      │                                        │
                 │      ├── context_service       热上下文+滚动摘要 │
                 │      ├── recipe_cache_service  会话菜谱缓存     │
                 │      ├── rag_service           检索+精排+取步骤 │
                 │      └── verify_service        常识校验(fail-open)│
                 └──────┬────────┬─────────┬─────────┬──────────┘
                        │        │         │         │
                 ┌──────▼───┐ ┌──▼──────┐ ┌▼───────┐ ┌▼──────────┐
                 │  MySQL   │ │ Milvus  │ │ Neo4j  │ │   Redis   │
                 │ 用户/会话 │ │ 菜谱向量 │ │ 步骤/  │ │ 热上下文/ │
                 │ /消息/摘要│ │ +元数据  │ │ 食材图 │ │ 菜谱缓存/ │
                 └──────────┘ └─────────┘ └────────┘ │ 流事件缓冲 │
                                                      └───────────┘

   外部模型服务（OpenAI 兼容接口）：
   - 对话生成 / 编排分类：智谱 BigModel（GLM），可开关 thinking 模式
   - Embedding / Rerank / 常识校验 / 摘要压缩 / 情感分析：硅基流动（bge-m3 / bge-reranker-v2-m3 / Qwen2.5-7B）
```

### 一次提问的处理流程

1. **点火**：`POST /chat` 限流校验 → 会话级防重入（生成中标记）→ 无 `conversation_id` 则创建会话（标题取首条消息前 20 字）→ 起后台生成任务，立即返回会话 ID
2. **加载上下文**：`load_context` 一轮并发读 Redis——滚动摘要 + 最近 N 轮滑窗 + 会话菜谱缓存（Redis miss 从 MySQL 恢复）
3. **并行分析**：**情感分析 agent**（免费小模型输出情绪 + 语气建议）｜**管理编排 agent**（主模型判断追问/全新话题 + 路由菜谱/开发/闲聊 + 摘要是否够答；追问延续当前话题领域，不按单条消息关键词分类——dev 追问永远纯 LLM，只有菜谱追问才有检索兜底）
4. **汇聚路由**（人设与上下文按域隔离）：
   - **菜谱 agent**：缓存片段相关就直接回答；不足时自行调 `search_recipe` 工具（向量化 → Milvus 粗排 → rerank 精排 → 逐条常识校验 → Neo4j 补步骤配图），命中片段写回会话缓存
   - **开发 agent**：纯 LLM 技术问答；**闲聊 agent**：人设唠嗑、情感陪伴
5. **事件进流**：边生成边把事件 XADD 进 Redis Stream——只透出带 `final_answer` tag 的最终回答模型 token；meta（conversation_id + rag + question）在编排决策后发出，追问兜底检索时补发修正 meta
6. **订阅分发**：`GET /chat/{id}/stream?last_id=` 重放断点后事件 + 阻塞跟随实时流，客户端随意断开重连；阻塞超时发心跳保活
7. **收尾落库**：消息落 MySQL、更新 Redis 热上下文；窗口溢出 → 后台任务把滑出消息压缩进滚动摘要（免费小模型，不占用付费模型额度）

## 技术栈

FastAPI · SQLAlchemy 2.0 (asyncmy) · LangChain / LangGraph · LangSmith · PyMilvus · Neo4j driver · Redis（热上下文 / 流事件缓冲 / 限流）· Pydantic Settings · PyJWT · nh3（HTML 消毒）· uv（依赖管理）

## 目录结构

```
recipe-back-end/
├── app/
│   ├── main.py            # FastAPI 入口：CORS、异常处理、启动连接检查、/health
│   ├── config.py          # 全部配置（pydantic-settings，从 .env 读取）
│   ├── api/
│   │   ├── deps.py        # 依赖注入：DB 会话、当前用户
│   │   └── v1/            # chat(点火+订阅) / auth / user / conversation 路由
│   ├── graph/             # LangGraph 多 Agent 编排
│   │   ├── builder.py           # 图组装：并行分支 + 条件路由 + ReAct 循环
│   │   ├── state.py             # 图共享状态（路由决策/情感/缓存/消息）
│   │   ├── llms.py              # 各 agent 的 ChatOpenAI 实例（tag 区分流式透出）
│   │   ├── prompts.py           # 人设按域组装 + 各领域规则 + 编排/情感模板
│   │   ├── nodes.py             # 上下文加载(分层防污染)、编排、情感、三个问答 agent
│   │   └── tools/recipe_search.py  # search_recipe 工具（检索+校验+写缓存）
│   ├── services/          # 业务层
│   │   ├── stream_service.py    # 流式生成管理：点火/后台跑图/Redis Stream 缓冲/订阅
│   │   ├── chat_service.py      # 对话支撑：会话创建查找 + 滚动摘要压缩（按域分述）
│   │   ├── rag_service.py       # 向量化、Milvus 检索、rerank、Neo4j 取步骤
│   │   ├── verify_service.py    # 检索结果常识校验（fail-open）
│   │   ├── context_service.py   # Redis 热上下文 + 滚动摘要（三级整形）
│   │   ├── recipe_cache_service.py  # 会话级菜谱片段缓存（追问复用）
│   │   ├── auth_service.py / user_service.py / conversation_service.py
│   ├── crud/              # 数据访问层（SQLAlchemy async）
│   ├── models/            # ORM：user / conversation / message
│   ├── schemas/           # Pydantic 请求/响应模型
│   ├── database/          # mysql / milvus / neo4j / redis 客户端
│   ├── core/              # security(JWT) / rate_limit(限流) / exceptions / tracing(LangSmith)
│   └── utils/             # sse.py 事件封装 / markdown.py 渲染+消毒
├── scripts/
│   ├── init_db.py         # 建表
│   ├── import_recipes.py  # 菜谱导入：Excel → embedding → Milvus + Neo4j
│   └── check_import.py    # 导入结果校验
├── tests/                 # pytest（api / services）
├── uploads/               # 用户头像等上传文件（静态挂载 /uploads）
├── data/                  # recipes.xlsx、embedding 缓存、docker 数据卷
└── docker-compose.yml     # MySQL 8 + Milvus 2.4(etcd/MinIO) + Neo4j 5 + Redis 7
```

## 快速开始

### 1. 启动依赖服务

```bash
docker compose up -d
```

包含 MySQL 8、Milvus 2.4（含 etcd、MinIO）、Neo4j 5（含 APOC）、Redis 7，数据持久化在 `./data/`。

### 2. 配置环境变量

在 `recipe-back-end/` 下创建 `.env`：

```dotenv
# JWT
JWT_SECRET_KEY=<随机长字符串>

# 对话模型（智谱 BigModel，OpenAI 兼容）
# 注意：BASE_URL 必须带 /api/paas/v4 路径
ZHIPU_API_KEY=<your-key>
ZHIPU_BASE_URL=https://open.bigmodel.cn/api/paas/v4
ZHIPU_LLM_MODEL=<模型名，如 glm-4.5-flash>
LLM_THINKING=disabled        # 关闭隐藏推理可显著降低生成耗时

# 硅基流动（embedding / rerank / 校验 / 摘要 / 情感分析）
SILICONFLOW_API_KEY=<your-key>

# MySQL
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=<与 docker-compose 一致>
MYSQL_DATABASE=recipe_bot

# Milvus
MILVUS_HOST=127.0.0.1
MILVUS_PORT=19530
MILVUS_COLLECTION_NAME=recipes
MILVUS_EMBEDDING_DIM=1024       # bge-m3 输出维度

# Neo4j
NEO4J_URI=bolt://127.0.0.1:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=<与 docker-compose 一致>

# Redis
REDIS_HOST=127.0.0.1
REDIS_PORT=6379
```

上下文窗口、RAG 阈值、限流、LangSmith 等更多参数均有默认值，见 [app/config.py](app/config.py)。

### 3. 初始化并启动

```bash
uv sync                                        # 安装依赖（Python ≥ 3.11）
.venv/bin/python scripts/init_db.py            # 初始化 MySQL 数据表
.venv/bin/python scripts/import_recipes.py     # 导入菜谱数据到 Milvus + Neo4j
.venv/bin/uvicorn app.main:app --reload        # http://localhost:8000
```

- 交互式 API 文档：<http://localhost:8000/docs>
- 健康检查：`GET /health` 返回 MySQL / Milvus / Neo4j 连接状态
- 启动日志会逐项打印各组件连接结果
- 单 worker 部署：生成任务注册表是进程内 dict；多 worker 需把任务投递改为队列方案（arq/Dramatiq 等）

## API 概览

统一前缀 `/api/v1`，除认证外均需 `Authorization: Bearer <token>`。

| 模块 | 方法与路径 | 说明 |
|---|---|---|
| 认证 | `POST /auth/register` | 注册 |
| | `POST /auth/login` | 登录，返回 JWT（防爆破限流） |
| 对话 | `POST /chat` | 点火：起后台生成任务，返回 `{"conversation_id": 1}`（生成中重复请求 409） |
| | `GET /chat/{id}/stream?last_id=` | SSE 订阅：`last_id` 之后的事件重放 + 阻塞跟随，可反复断开重连 |
| 会话 | `GET /conversations` | 会话列表（按更新时间倒序，带 `generating` 生成中标记） |
| | `GET /conversations/{id}/messages` | 历史消息 |
| | `DELETE /conversations/{id}` | 删除会话（同时取消生成任务、清理缓冲） |
| 用户 | `GET /users/me` | 当前用户资料 |
| | `PUT /users/me` | 修改资料 |
| | `POST /users/me/avatar` | 上传头像 |

`GET /chat/{id}/stream` 的 SSE 事件序列（`id` 为 Redis Stream entry id，作断点）：

```
data: {"type": "meta", "conversation_id": 1, "rag": true, "question": "...", "id": "..."}
data: {"type": "delta", "delta": "...", "html": "...", "id": "..."}   # 增量 markdown + 全量渲染 HTML（多帧）
data: {"type": "done", "id": "..."}                                   # 结束；异常时为 {"type": "error", ...}
data: [DONE]
```

## 关键配置调优（app/config.py）

| 参数 | 默认 | 说明 |
|---|---|---|
| `CHAT_CONTEXT_ROUNDS` | 5 | Redis 热上下文保留轮数 |
| `CHAT_CONTEXT_TOKEN_BUDGET` | 2000 | 发给 LLM 的热上下文字符预算 |
| `CHAT_RATE_LIMIT_PER_MINUTE` | 20 | 每用户每分钟对话点火上限 |
| `SENTIMENT_LLM_MODEL` | Qwen2.5-7B | 情感分析 agent 用的免费小模型 |
| `RECIPE_CACHE_MAX_ITEMS` | 3 | 单会话菜谱缓存条数上限 |
| `RAG_TOP_K` | 1 | 最终采用的菜谱数 |
| `RAG_RERANK_CANDIDATES` | 20 | 向量粗排 over-fetch 候选数 |
| `RAG_SCORE_THRESHOLD` | 0.55 | 向量相似度阈值（COSINE） |
| `RAG_VERIFY_MAX_RETRIES` | 3 | 常识校验最大换候选次数 |
| `LANGSMITH_TRACING` | false | 配 API Key 后开启全链路追踪 |

## 数据导入

```bash
.venv/bin/python scripts/import_recipes.py           # 增量导入（幂等，可重复执行）
.venv/bin/python scripts/import_recipes.py --fresh   # 清空集合后全量重建
.venv/bin/python scripts/check_import.py             # 校验导入结果
```

数据源为 `data/recipes.xlsx`。菜名/简介/原料/步骤等列拼接后批量向量化（多线程 + 本地 pkl 缓存），Milvus 存向量与元数据（upsert），Neo4j 存完整属性与步骤、并由原料列解析出 `Ingredient` 节点和 `HAS_INGREDIENT` 关系（MERGE）。

## 测试

```bash
.venv/bin/python -m pytest tests/
```
