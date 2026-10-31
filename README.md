# recipe-back-end

多智能体智能问答助手后端。基于 FastAPI + LangGraph 多 Agent 编排：管理编排 agent 负责意图分类（追问/全新话题）与领域路由，情感分析 agent 并行分析用户情绪、动态调整回复语气，**菜谱 / 开发 / 闲聊**三个领域问答 agent 各司其职——人设与上下文按域隔离，互不污染。菜谱域和开发域背后各有一套 RAG 检索管线：菜谱是「语义检索 → 精排 → 常识校验」，开发是《JavaScript高级程序设计》（红宝书）电子书知识库「清洗切块 → 语义检索 → 精排」，两个域都是 ReAct 循环 + 会话缓存复用；闲聊域负责日常唠嗑与情感陪伴。流式生成与连接解耦：token 事件缓存在 Redis Stream，客户端断开/刷新不影响生成，随时带断点重连续看；也支持手动停止（协作式中断，抛弃半成品输出）。

配套前端仓库：[recipe-front-end](../recipe-front-end)。

## 功能特性

- **多 Agent 编排（LangGraph）**
  - 管理编排 agent：主模型做意图分类（追问/全新话题）+ 领域路由（菜谱/开发/闲聊），JSON 结构化输出，解析失败按菜谱域检索兜底
  - 情感分析 agent：免费小模型与编排并行，输出情绪标签 + 语气建议注入问答 agent，异常 fail-open 为中性
  - 菜谱问答 agent：ReAct 循环 + `search_recipe` 工具，会话缓存优先、不足自行检索兜底
  - 开发知识问答 agent：JS 专家人设 + ReAct 循环 + `search_dev_book` 工具（红宝书电子书知识库），缓存书摘优先、不足自行检索；闲聊 agent：人设唠嗑、情感陪伴
  - **首轮检索代码层强制**：全新菜谱/开发查询的首轮工具调用不靠模型自觉（实测小概率无视 prompt 甚至 `tool_choice="required"`，凭经验直接答而 token 已流出无法回收）——代码层直接调工具、补齐 AIMessage/ToolMessage 后让模型基于检索结果生成，首轮行为 100% 确定
- **人设与上下文按域隔离（防跨域污染）**
  - 人设按域组装：做菜人设（Char 的忌口、专属菜谱、创意菜小怪癖）只给菜谱 agent；闲聊 agent 只带基础人设；开发 agent 是 JS 高级开发工程师（红宝书活字典）、不带任何生活人设——技术回答不会串菜谱梗
  - 上下文分层注入：追问且摘要够答 → 只带摘要；追问但摘要不足 → 摘要 + Redis 滑窗；**全新话题 → 只带摘要不带滑窗**——上一领域的对话原文不进当前领域的 prompt，开发回答不会被做菜历史带偏
  - 摘要压缩按领域分述（菜谱给做法要点 / 技术给方案要点 / 闲聊给情绪状态），摘要本身不带领域偏见
  - 每个问答 agent 的 system prompt 注入当前时间（东八区）——模型自身没有实时时钟，问「今天几号」不会瞎猜
- **流式生成与连接解耦（断网续传 + 手动停止）**
  - `POST /chat` 只"点火"：起后台 asyncio 任务跑图，token 事件 XADD 进 Redis Stream（`chat:stream:{cid}`），立即返回会话 ID
  - `GET /chat/{id}/stream?last_id=` 是可重入的"订阅"端点：断点重放缓冲 + 阻塞跟随实时事件；断网/刷新/切换会话只断本地连接，后端照跑不误
  - `POST /chat/{id}/stop` 手动停止：置停止标记，生成循环在下一个事件处协作式退出——已生成的部分输出直接抛弃、只落库用户问题（半成品不进热上下文，不污染后续对话），流尾发 `stopped` 终态事件。这是唯一会真正中断生成的操作
  - `chat:gen:{cid}` 生成中标记：POST 防重入（同会话同时只跑一个生成）、会话列表打标（前端刷新后据此自动续看）
- **多轮上下文**：Redis 热上下文（最近 N 轮原文 + 滚动摘要），三级整形（入库剥离图片链接、读时截断旧消息、超预算整轮丢弃）；窗口溢出时后台任务压缩摘要，不阻塞响应；Redis 丢失可从 MySQL 恢复
- **会话级检索缓存（双域同构）**：RAG 命中的菜谱片段写入 `chat:recipes:{cid}`、书摘块写入 `chat:devbook:{cid}`，同会话追问同一道菜/同一知识点直接复用，省 embed/milvus/rerank/校验全链路开销
- **RAG 检索管线（菜谱域）**
  - bge-m3 向量化 → Milvus 余弦相似度粗排（over-fetch 20 条候选）
  - bge-reranker-v2-m3 交叉编码器精排，带相关性阈值
  - 免费小模型（Qwen2.5-7B）对候选做烹饪常识校验，不通过换下一条候选（只多一次调用，不重新检索），全部不通过退化为纯 LLM
  - 校验服务超时/异常时 fail-open，不阻塞主链路
- **RAG 检索管线（开发域：红宝书知识库）**
  - 《JavaScript高级程序设计》PDF 经清洗（页眉页脚剔除/断行修复/目录页过滤）+ 结构感知切块（按章节归组、500 字目标 + 80 字重叠、每块拼「书名+章节」上下文前缀自包含）导入 Milvus `dev_book_chunks` 集合
  - 检索同构但更轻：bge-m3 向量化 → Milvus 粗排 → bge-reranker 精排过阈值即采用——块本身自包含，不做常识校验、不查 Neo4j，省一轮 7B 调用
  - 未命中/检索异常退化为纯 LLM（dev agent 本来就是 JS 专家人设，兜底体验不差）
- **混合存储**：菜谱的向量与元数据在 Milvus；步骤正文与配图、食材关系在 Neo4j，仅候选被采用后才按需补取，省带宽；书摘块自包含、只存 Milvus
- **用户体系**：注册 / 登录（JWT）、资料修改、头像上传、会话增删查
  - **密码密文传输**：`GET /auth/public-key` 下发 RSA 公钥，前端 WebCrypto 加密密码（RSA-OAEP/SHA-256）后再提交，服务端私钥解密后再走 bcrypt——明文不出现在请求体里（网关/代理日志、TLS 终止点后都看不到）
  - **头像服务端压缩**：原图上限放宽到 10MB（手机原图直传），Pillow 按 EXIF 矫正方向、透明底铺白、缩放至 512px 转 JPEG 落盘，静态目录挂载
- **消息未读机制**：会话记录 `last_read_at`，列表接口比对最新消息时间返回 `unread` 标记——后台生成完成时用户可能在别的会话，侧边栏据此打红点；查看历史即已读，另有 `POST /conversations/{id}/read` 显式已读
- **历史消息分页**：`GET /conversations/{id}/messages?before_id=&limit=` 游标分页（limit+1 判 has_more），首屏只给最近一页，更早的前端下拉加载
- **统一响应封装**：`/api` 下 2xx JSON 中间件自动包 `{"code": 0, "data": ..., "msg": ""}`；业务异常/框架 404/参数校验 422 统一 `{"code": 非0, "data": null, "msg": "可读信息"}`（HTTP 状态码保留语义）；SSE 流、图片二进制、204 原样放行
- **图片代理**：`GET /api/v1/image-proxy/{host}/{path}` 中转豆果 CDN 的 http 步骤图——https 站点浏览器按混合内容拦截，经后端走 https 出图；图床域名白名单防 SSRF，带 referer 防防盗链，按内容哈希长缓存
- **限流防护**：登录防爆破（IP + 用户名双维度固定窗口）、对话按用户限流（每次生成触发编排/情感/校验多路 LLM 调用，限流在点火前）
- **可观测性**：可选 LangSmith 全链路 tracing（编排/情感/检索/校验/生成各节点）
- **数据导入脚本**：菜谱 `recipes.xlsx` → Milvus + Neo4j；开发书籍 PDF → Milvus；知识库表格（CSV/XLSX）→ Milvus。统一 upsert/MERGE 幂等（内容 hash 主键），支持增量与 `--fresh` 全量重建，embedding 批量 + 并发 + 限流 + 本地断点缓存，中断重跑不重复花钱

## 架构

```
                 ┌──────────────────────────────────────────────┐
   前端 ───────▶ │                FastAPI                        │
   POST /chat    │  api/v1: chat(点火/订阅/停止) / auth / user /  │
   GET  /stream  │         conv / image-proxy(图片中转)           │
      ▲          │      │                                        │
      │          │  stream_service（流式生成管理：生成与连接解耦） │
      │          │    点火：防重入 → 起后台任务跑图                │
      └──SSE订阅─┤    订阅：last_id 断点重放 + 阻塞跟随            │
                 │    停止：置标记，生成循环协作式退出             │
                 │      │                                        │
                 │  app/graph（LangGraph 多 Agent 编排）           │
                 │      load_context ─┬→ sentiment_agent（情感）   │
                 │                    └→ orchestrator（管理编排）   │
                 │                              │ join 汇聚后路由  │
                 │        ┌── recipe_agent ⇄ recipe_tools(ReAct)  │
                 │        ├── dev_agent ⇄ dev_tools(ReAct)        │
                 │        └── chat_agent（闲聊/情感陪伴）           │
                 │      │                                        │
                 │      ├── context_service       热上下文+滚动摘要 │
                 │      ├── recipe_cache_service  会话菜谱缓存     │
                 │      ├── dev_cache_service     会话书摘缓存     │
                 │      ├── rag_service           菜谱检索+精排+取步骤│
                 │      ├── dev_rag_service       书摘检索+精排    │
                 │      └── verify_service        常识校验(fail-open)│
                 └──────┬────────┬─────────┬─────────┬──────────┘
                        │        │         │         │
                 ┌──────▼───┐ ┌──▼──────┐ ┌▼───────┐ ┌▼──────────┐
                 │  MySQL   │ │ Milvus  │ │ Neo4j  │ │   Redis   │
                 │ 用户/会话 │ │ 菜谱向量 │ │ 步骤/  │ │ 热上下文/ │
                 │ /消息/摘要│ │ 书摘块   │ │ 食材图 │ │ 检索缓存/ │
                 │          │ │ +元数据  │ │        │ │ 流事件缓冲 │
                 └──────────┘ └─────────┘ └────────┘ └───────────┘

   外部模型服务（OpenAI 兼容接口）：
   - 对话生成 / 编排分类：智谱 BigModel（GLM），可开关 thinking 模式
   - Embedding / Rerank / 常识校验 / 摘要压缩 / 情感分析：硅基流动（bge-m3 / bge-reranker-v2-m3 / Qwen2.5-7B）
```

### 一次提问的处理流程

1. **点火**：`POST /chat` 限流校验 → 会话级防重入（生成中标记）→ 无 `conversation_id` 则创建会话（标题取首条消息前 20 字）→ 起后台生成任务，立即返回会话 ID
2. **加载上下文**：`load_context` 读取结构化活动主题、滚动摘要、最近 N 轮滑窗和领域缓存；追问始终保留最近窗口，Redis miss 从 MySQL 恢复
3. **并行分析**：**情感分析 agent**（免费小模型输出情绪 + 语气建议）｜**管理编排 agent**（主模型判断追问/全新话题 + 路由菜谱/开发/闲聊 + 摘要是否够答；追问延续当前话题领域，不按单条消息关键词分类——dev 问题永远走 dev agent，绝不进菜谱检索）
4. **汇聚路由**（人设与上下文按域隔离）：
   - **菜谱 agent**：全新查询首轮由代码层强制调 `search_recipe`（向量化 → Milvus 粗排 → rerank 精排 → 逐条常识校验 → Neo4j 补步骤配图）；追问缓存片段相关就直接回答，不足自行检索兜底；命中片段写回会话缓存
   - **开发 agent**：同构——全新查询首轮强制调 `search_dev_book`（向量化 → Milvus 粗排 → rerank 精排，块自包含不校验）；追问缓存书摘优先，不足自行检索；命中书摘写回会话缓存
   - **闲聊 agent**：人设唠嗑、情感陪伴
5. **事件进流**：边生成边把事件 XADD 进 Redis Stream——只透出带 `final_answer` tag 的最终回答模型 token；meta（conversation_id + rag + question）在编排决策后发出，追问兜底检索时补发修正 meta
6. **订阅分发**：`GET /chat/{id}/stream?last_id=` 重放断点后事件 + 阻塞跟随实时流，客户端随意断开重连；阻塞超时发心跳保活；`POST /chat/{id}/stop` 置停止标记，生成循环在下一个事件处协作式退出（已生成部分抛弃，只落库用户问题）
7. **收尾落库**：用户消息在点火时立即落 MySQL，助手答案、`turn_id` 和活动主题随后持久化；窗口溢出 → 带会话锁的后台任务把滑出消息压缩进结构化滚动摘要

## 技术栈

FastAPI · SQLAlchemy 2.0 (asyncmy) · LangChain / LangGraph · LangSmith · PyMilvus · Neo4j driver · Redis（热上下文 / 检索缓存 / 流事件缓冲 / 限流）· Pydantic Settings · PyJWT · cryptography（RSA-OAEP 密码传输加密）· Pillow（头像压缩）· PyMuPDF（PDF 文本提取）· nh3（HTML 消毒）· uv（依赖管理）

## 目录结构

```
recipe-back-end/
├── app/
│   ├── main.py            # FastAPI 入口：CORS、异常处理、启动连接检查、/health
│   ├── config.py          # 全部配置（pydantic-settings，从 .env 读取）
│   ├── api/
│   │   ├── deps.py        # 依赖注入：DB 会话、当前用户
│   │   └── v1/            # chat(点火+订阅+停止) / auth(含公钥下发) / user /
│   │                    # conversation(分页+已读) / image_proxy(图片中转) 路由
│   ├── graph/             # LangGraph 多 Agent 编排
│   │   ├── builder.py           # 图组装：并行分支 + 条件路由 + 双 ReAct 循环
│   │   ├── state.py             # 图共享状态（路由决策/情感/双域缓存/消息）
│   │   ├── llms.py              # 各 agent 的 ChatOpenAI 实例（tag 区分流式透出）
│   │   ├── prompts.py           # 人设按域组装 + 各领域规则 + 时间注入 + 编排/情感模板
│   │   ├── nodes.py             # 上下文加载(分层防污染)、编排、情感、三个问答 agent
│   │   │                        # （首轮检索代码层强制，不靠模型自觉）
│   │   └── tools/               # recipe_search.py（检索+校验+写缓存）
│   │                            # dev_book_search.py（书摘检索+写缓存）
│   ├── services/          # 业务层
│   │   ├── stream_service.py    # 流式生成管理：点火/后台跑图/Redis Stream 缓冲/订阅/停止
│   │   ├── chat_service.py      # 对话支撑：会话创建查找 + 滚动摘要压缩（按域分述）
│   │   ├── rag_service.py       # 菜谱：向量化、Milvus 检索、rerank、Neo4j 取步骤
│   │   ├── dev_rag_service.py   # 书摘：向量化、Milvus 检索、rerank（块自包含不校验）
│   │   ├── verify_service.py    # 检索结果常识校验（fail-open）
│   │   ├── context_service.py   # Redis 热上下文 + 滚动摘要（三级整形）
│   │   ├── recipe_cache_service.py / dev_cache_service.py  # 会话级检索缓存（追问复用）
│   │   ├── auth_service.py / user_service.py / conversation_service.py
│   ├── crud/              # 数据访问层（SQLAlchemy async）
│   ├── models/            # ORM：user / conversation(含 last_read_at) / message
│   ├── schemas/           # Pydantic 请求/响应模型
│   ├── database/          # mysql / milvus / neo4j / redis 客户端
│   ├── core/              # security(JWT) / passwd_crypto(RSA密码传输加密)
│   │                      # rate_limit(限流) / exceptions+responses(统一响应封装)
│   │                      # / tracing(LangSmith)
│   └── utils/             # sse.py 事件封装 / markdown.py 渲染+消毒
├── scripts/
│   ├── init_db.py               # 建表 + 列迁移（summary / last_read_at）
│   ├── import_recipes.py        # 菜谱导入：Excel → embedding → Milvus + Neo4j
│   ├── import_pdf.py            # 开发书籍导入：PDF → 清洗 → 结构感知切块 → Milvus
│   ├── import_knowledge_table.py  # 知识库表格导入：CSV/XLSX → 清洗 → Milvus
│   ├── ingest_common.py         # 导入公共模块：限流/批量向量化(断点缓存)/幂等写入
│   ├── check_import.py          # 菜谱导入结果校验
│   └── check_pdf_import.py      # 书摘导入质量抽检（自命中率）
├── tests/                 # pytest（api / services）
├── uploads/               # 用户头像等上传文件（静态挂载 /uploads）
├── example/               # 示例资料（红宝书 PDF 等）
├── data/                  # recipes.xlsx、demo PDF、dev_faq.csv、embedding 缓存、docker 数据卷
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

# 登录/注册密码传输加密（RSA-OAEP）：PEM 私钥，换行用 \n 转义
# 生成：openssl genrsa 2048。留空则启动时生成临时密钥（仅开发用，重启后旧密文无法解密）
RSA_PRIVATE_KEY=<PEM 私钥>

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
# 可选：导入开发书籍知识库（dev agent 的红宝书 RAG，不导则退化为纯 LLM 回答）
.venv/bin/python scripts/import_pdf.py --pdf example/JavaScript高级程序设计第4版.pdf --title "JavaScript高级程序设计"
.venv/bin/uvicorn app.main:app --reload        # http://localhost:8000
```

- 交互式 API 文档：<http://localhost:8000/docs>
- 健康检查：`GET /health` 返回 MySQL / Milvus / Neo4j 连接状态
- 启动日志会逐项打印各组件连接结果
- 单 worker 部署：生成任务注册表是进程内 dict；多 worker 需把任务投递改为队列方案（arq/Dramatiq 等）

## 公安备案审核登录态

在运行服务的相同环境与 `.env` 下执行（前端目录按实际路径调整）：

```bash
.venv/bin/python -m scripts.create_filing_review_session --frontend-env ../recipe-front-end/.env.production.local
```

脚本每次新建一个独立、随机密码的审核账号，将带 `filing-review` scope 的 JWT 写入指定 `.local` 环境文件，并显示账号名和 UTC 到期时间，终端不会输出密码或 token。登录态从签发起固定 **30 天**有效，不提供续期；普通账号的 24 小时有效期保持不变。无需修改表结构。

前端重新构建并部署后，无个人登录态的访客会自动进入审核账号。该账号的内容和对话历史为所有访客共享，只应用于公开展示。后端在鉴权依赖中仅放行用户信息读取、会话读取/已读与问答接口，拒绝修改资料、上传头像、删除会话、登出吊销及其他接口；现有用户归属校验和对话限流继续生效。

应先部署后端权限限制，再部署带审核凭证的前端。审核结束后移除前端环境变量并重新构建；如需在到期前让已发布凭证立即失效，可在服务环境调用 `app.core.security.revoke_token(token)`，不要通过公开的登出接口操作共享审核态。

鉴权及环境文件写入回归测试（不连接数据库和 Redis）：

```bash
.venv/bin/python -m unittest tests.api.test_filing_review -v
```

## API 概览

统一前缀 `/api/v1`，除认证与图片代理外均需 `Authorization: Bearer <token>`。JSON 响应统一封装：成功 `{"code": 0, "data": ..., "msg": ""}`，失败 `{"code": 非0, "data": null, "msg": "可读信息"}`（HTTP 状态码保留语义；SSE 流、图片二进制、204 不封装）。

| 模块 | 方法与路径 | 说明 |
|---|---|---|
| 认证 | `GET /auth/public-key` | 下发 RSA 公钥（前端加密密码用） |
| | `POST /auth/register` | 注册（密码传 RSA-OAEP 密文） |
| | `POST /auth/login` | 登录，返回 JWT（防爆破限流；密码同传密文） |
| 对话 | `POST /chat` | 点火：起后台生成任务，返回会话 ID（生成中重复请求 409） |
| | `GET /chat/{id}/stream?last_id=` | SSE 订阅：`last_id` 之后的事件重放 + 阻塞跟随，可反复断开重连 |
| | `POST /chat/{id}/stop` | 手动停止生成（协作式中断，抛弃部分输出；未在生成 409） |
| 会话 | `GET /conversations` | 会话列表（按更新时间倒序，带 `generating` 生成中 / `unread` 未读标记） |
| | `GET /conversations/{id}/messages?before_id=&limit=` | 历史消息游标分页，返回 `{messages, has_more}`（查看即已读） |
| | `POST /conversations/{id}/read` | 显式标记已读 |
| | `DELETE /conversations/{id}` | 删除会话（同时取消生成任务、清理缓冲） |
| 用户 | `GET /users/me` | 当前用户资料 |
| | `PUT /users/me` | 修改资料 |
| | `POST /users/me/avatar` | 上传头像（≤10MB 原图，服务端压缩） |
| 图片 | `GET /image-proxy/{host}/{path}` | 菜谱步骤图中转（豆果图床白名单，防混合内容拦截） |

`GET /chat/{id}/stream` 的 SSE 事件序列（`id` 为 Redis Stream entry id，作断点）：

```
data: {"type": "meta", "conversation_id": 1, "rag": true, "question": "...", "id": "..."}
data: {"type": "delta", "delta": "...", "html": "...", "id": "..."}   # 增量 markdown + 全量渲染 HTML（多帧）
data: {"type": "done", "id": "..."}        # 正常结束；手动停止为 {"type": "stopped"}，异常为 {"type": "error", ...}
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
| `DEV_CACHE_MAX_ITEMS` | 5 | 单会话书摘缓存条数上限 |
| `DEV_BOOK_COLLECTION` | dev_book_chunks | Milvus 书摘块集合名 |
| `DEV_RAG_TOP_K` | 3 | 开发域精排后最终保留的书摘块数 |
| `RAG_TOP_K` | 1 | 最终采用的菜谱数 |
| `RAG_RERANK_CANDIDATES` | 20 | 向量粗排 over-fetch 候选数 |
| `RAG_SCORE_THRESHOLD` | 0.55 | 向量相似度阈值（COSINE） |
| `RAG_VERIFY_MAX_RETRIES` | 3 | 常识校验最大换候选次数 |
| `LANGSMITH_TRACING` | false | 配 API Key 后开启全链路追踪 |

## 数据导入

### 菜谱（recipes.xlsx → Milvus + Neo4j）

```bash
.venv/bin/python scripts/import_recipes.py           # 增量导入（幂等，可重复执行）
.venv/bin/python scripts/import_recipes.py --fresh   # 清空集合后全量重建
.venv/bin/python scripts/check_import.py             # 校验导入结果
```

数据源为 `data/recipes.xlsx`。菜名/简介/原料/步骤等列拼接后批量向量化（多线程 + 本地 pkl 缓存），Milvus 存向量与元数据（upsert），Neo4j 存完整属性与步骤、并由原料列解析出 `Ingredient` 节点和 `HAS_INGREDIENT` 关系（MERGE）。

### 开发书籍（PDF → Milvus dev_book_chunks）

```bash
.venv/bin/python scripts/import_pdf.py --pdf example/JavaScript高级程序设计第4版.pdf --title "JavaScript高级程序设计"
.venv/bin/python scripts/import_pdf.py --pdf xxx.pdf --fresh       # 清集合重导
.venv/bin/python scripts/import_pdf.py --pdf xxx.pdf --dry-run     # 只跑清洗切块看效果，不写库
.venv/bin/python scripts/check_pdf_import.py                       # 抽 20 块做自命中抽检
```

PDF 清洗三板斧：跨页统计剔除页眉页脚、连字符/中英文断行修复、点线目录页过滤。切块走结构感知而非固定长度硬切：按标题（第X章 / 1.1 / 1.1.1）归组段落再贪心装箱，块目标 500 字 + 相邻重叠 80 字，每块拼「书名 + 章节」上下文前缀——块脱离原文位置后仍自包含，检索命中率明显提升。向量化只有语义内容进向量，页码等只进元数据。扫描件需先 OCR（本脚本走 PyMuPDF 文本层提取）。

### 知识库表格（CSV/XLSX → Milvus）

```bash
.venv/bin/python scripts/import_knowledge_table.py                 # 导入默认示例 data/dev_faq.csv
.venv/bin/python scripts/import_knowledge_table.py --file xxx.xlsx # 任意表格（需含 question/answer 列）
.venv/bin/python scripts/import_knowledge_table.py --dry-run       # 只跑清洗，不调向量/不写库
```

清洗四步：列名规范化与必含列校验、HTML/全角/零宽字符内容清洗、按规范化问句去重、内容 hash 主键（重跑 upsert 幂等）。

三个导入脚本共用 `ingest_common.py`：embedding 批量 + 8 线程并发 + 限流 + pkl 断点缓存（中断重跑不重复花钱），Milvus 分批 upsert。

## 测试

```bash
.venv/bin/python -m pytest tests/
```
