# recipe-back-end

菜谱智能助手后端。基于 FastAPI + LangChain 生态，核心是一条「语义检索 → 精排 → 常识校验 → 流式生成」的 RAG 对话链路：用户提问后从本地菜谱库检索参考资料交给大模型组织回答，检索未命中或校验不通过时自动退化为纯 LLM 自由问答。

配套前端仓库：[recipe-front-end](../recipe-front-end)。

## 功能特性

- **流式对话（SSE）**：Markdown 增量生成、服务端渲染为 HTML 全量推送，菜谱步骤支持配图
- **RAG 检索管线**
  - bge-m3 向量化 → Milvus 余弦相似度粗排（over-fetch 20 条候选）
  - bge-reranker-v2-m3 交叉编码器精排，带相关性阈值
  - 免费小模型（Qwen2.5-7B）对候选做烹饪常识校验，不通过换下一条候选（只多一次调用，不重新检索），全部不通过退化为纯 LLM
  - 校验服务超时/异常时 fail-open，不阻塞主链路
- **混合存储**：向量与元数据在 Milvus；步骤正文与配图、食材关系在 Neo4j，仅候选被采用后才按需补取，省带宽
- **多轮上下文**：Redis 热上下文（最近 N 轮原文 + 滚动摘要），三级整形（入库剥离图片链接、读时截断旧消息、超预算整轮丢弃）；窗口溢出时后台任务压缩摘要，不阻塞响应；Redis 丢失可从 MySQL 恢复
- **用户体系**：注册 / 登录（JWT）、资料修改、头像上传（静态目录挂载）、会话增删查
- **可观测性**：可选 LangSmith 全链路 tracing（embedding / 检索 / 校验 / 生成各节点）
- **数据导入脚本**：`recipes.xlsx` 批量向量化导入 Milvus + Neo4j，upsert/MERGE 幂等，支持增量与 `--fresh` 全量重建，embedding 带本地缓存

## 架构

```
                 ┌──────────────────────────────────────────────┐
   前端 SSE ───▶ │                FastAPI                        │
                 │  api/v1: chat / auth / user / conversation    │
                 │      │                                        │
                 │  chat_service（对话编排）                      │
                 │      ├── context_service  热上下文 + 滚动摘要   │
                 │      ├── rag_service      检索 + 精排 + 取步骤  │
                 │      └── verify_service   常识校验(fail-open)  │
                 └──────┬────────┬─────────┬─────────┬──────────┘
                        │        │         │         │
                 ┌──────▼───┐ ┌──▼──────┐ ┌▼───────┐ ┌▼─────────┐
                 │  MySQL   │ │ Milvus  │ │ Neo4j  │ │  Redis   │
                 │ 用户/会话 │ │ 菜谱向量 │ │ 步骤/  │ │ 热上下文 │
                 │ /消息/摘要│ │ +元数据  │ │ 食材图 │ │ TTL 7天  │
                 └──────────┘ └─────────┘ └────────┘ └──────────┘

   外部模型服务（OpenAI 兼容接口）：
   - 对话生成：火山引擎方舟（豆包），可开关 thinking 模式
   - Embedding / Rerank / 常识校验 / 摘要压缩：硅基流动（bge-m3 / bge-reranker-v2-m3 / Qwen2.5-7B）
```

### 一次提问的处理流程

1. 无 `conversation_id` 则创建会话，标题取首条消息前 20 字
2. 并行执行：读取 Redis 热上下文（最近 N 轮 + 滚动摘要）｜RAG 检索
3. RAG：问题向量化 → Milvus 取候选 → rerank 精排 → 逐条常识校验 → 命中后按 `cid` 从 Neo4j 补步骤文字与配图
4. 组装 system prompt（人设，见 `chat_service.SYSTEM_PROMPT`）+ 摘要 + 最近轮次 + 参考资料，流式调用对话模型
5. 边生成边推 SSE（`delta` 原始增量 + `html` 全量渲染），完成后消息落 MySQL、更新 Redis 热上下文
6. 上下文窗口溢出 → 后台任务把滑出消息压缩进滚动摘要（免费小模型，不占用付费模型额度）

## 技术栈

FastAPI · SQLAlchemy 2.0 (asyncmy) · LangChain / LangGraph · LangSmith · PyMilvus · Neo4j driver · Redis · Pydantic Settings · PyJWT · nh3（HTML 消毒）· uv（依赖管理）

## 目录结构

```
recipe-back-end/
├── app/
│   ├── main.py            # FastAPI 入口：CORS、异常处理、启动连接检查、/health
│   ├── config.py          # 全部配置（pydantic-settings，从 .env 读取）
│   ├── api/
│   │   ├── deps.py        # 依赖注入：DB 会话、当前用户
│   │   └── v1/            # chat / auth / user / conversation 路由
│   ├── services/          # 业务层
│   │   ├── chat_service.py      # 对话编排：上下文+RAG+流式+落库
│   │   ├── rag_service.py       # 向量化、Milvus 检索、rerank、Neo4j 取步骤
│   │   ├── verify_service.py    # 检索结果常识校验（fail-open）
│   │   ├── context_service.py   # Redis 热上下文 + 滚动摘要
│   │   ├── auth_service.py / user_service.py / conversation_service.py
│   ├── crud/              # 数据访问层（SQLAlchemy async）
│   ├── models/            # ORM：user / conversation / message
│   ├── schemas/           # Pydantic 请求/响应模型
│   ├── database/          # mysql / milvus / neo4j / redis 客户端
│   ├── core/              # security(JWT) / exceptions(统一异常) / tracing(LangSmith)
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

# 对话模型（火山引擎方舟，OpenAI 兼容）
# 注意：资源包类型的 Key，BASE_URL 必须带 /api/plan/v3 路径，标准 /api/v3 会 401
DOUBAO_API_KEY=<your-key>
DOUBAO_BASE_URL=https://ark.cn-beijing.volces.com/api/plan/v3
DOUBAO_LLM_MODEL=<模型名或接入点>
DOUBAO_THINKING=disabled        # 关闭隐藏推理可显著降低生成耗时

# 硅基流动（embedding / rerank / 校验 / 摘要）
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

上下文窗口、RAG 阈值、LangSmith 等更多参数均有默认值，见 [app/config.py](app/config.py)。

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

## API 概览

统一前缀 `/api/v1`，除认证外均需 `Authorization: Bearer <token>`。

| 模块 | 方法与路径 | 说明 |
|---|---|---|
| 认证 | `POST /auth/register` | 注册 |
| | `POST /auth/login` | 登录，返回 JWT |
| 对话 | `POST /chat` | 发送消息，SSE 流式返回 |
| 会话 | `GET /conversations` | 会话列表（按更新时间倒序） |
| | `GET /conversations/{id}/messages` | 历史消息 |
| | `DELETE /conversations/{id}` | 删除会话 |
| 用户 | `GET /users/me` | 当前用户资料 |
| | `PUT /users/me` | 修改资料 |
| | `POST /users/me/avatar` | 上传头像 |

`POST /chat` 的 SSE 事件序列：

```
data: {"conversation_id": 1, "rag": true}     # 首帧：会话 ID + 是否命中 RAG
data: {"delta": "...", "html": "..."}         # 增量 markdown + 全量渲染 HTML（多帧）
data: [DONE]                                  # 结束；异常时为 {"error": "..."} 后接 [DONE]
```

## 关键配置调优（app/config.py）

| 参数 | 默认 | 说明 |
|---|---|---|
| `CHAT_CONTEXT_ROUNDS` | 5 | Redis 热上下文保留轮数 |
| `CHAT_CONTEXT_TOKEN_BUDGET` | 2000 | 发给 LLM 的热上下文字符预算 |
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
