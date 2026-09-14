from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # 服务配置
    APP_ENV: str = "dev"
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000

    # JWT
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 10080

    # 对话大模型：火山引擎方舟（资源包 Key，BASE_URL 须带 /api/plan/v3）
    DOUBAO_API_KEY: str = ""
    DOUBAO_BASE_URL: str = ""
    DOUBAO_LLM_MODEL: str = ""
    # 思考模式：disabled 关闭隐藏推理（实测可省 ~60% 生成时间，菜谱问答不需要深推理）；
    # 需要更强推理质量时改 enabled / auto，置空字符串则不传该参数
    DOUBAO_THINKING: str = "disabled"

    @property
    def LLM_API_KEY(self) -> str:
        return self.DOUBAO_API_KEY

    @property
    def LLM_BASE_URL(self) -> str:
        return self.DOUBAO_BASE_URL

    @property
    def LLM_MODEL(self) -> str:
        return self.DOUBAO_LLM_MODEL

    # Embedding（可以是和对话不同的服务商）
    EMBEDDING_PROVIDER: str = "siliconflow"
    QWEN_API_KEY: str = ""
    QWEN_BASE_URL: str = ""
    QWEN_EMBEDDING_MODEL: str = ""
    # 硅基流动（SiliconFlow）embedding，OpenAI 兼容接口，bge-m3 输出 1024 维
    SILICONFLOW_API_KEY: str = ""
    SILICONFLOW_BASE_URL: str = "https://api.siliconflow.cn/v1"
    SILICONFLOW_EMBEDDING_MODEL: str = "BAAI/bge-m3"
    # 硅基流动免费 LLM（用于 RAG 检索结果的常识校验）
    SILICONFLOW_LLM_MODEL: str = "Qwen/Qwen2.5-7B-Instruct"
    # 硅基流动免费 rerank 模型（检索后、校验前做交叉编码器重排）
    SILICONFLOW_RERANK_MODEL: str = "BAAI/bge-reranker-v2-m3"

    @property
    def EMBEDDING_API_KEY(self) -> str:
        return {"qwen": self.QWEN_API_KEY, "doubao": self.DOUBAO_API_KEY, "siliconflow": self.SILICONFLOW_API_KEY}[self.EMBEDDING_PROVIDER]

    @property
    def EMBEDDING_BASE_URL(self) -> str:
        return {"qwen": self.QWEN_BASE_URL, "doubao": self.DOUBAO_BASE_URL, "siliconflow": self.SILICONFLOW_BASE_URL}[self.EMBEDDING_PROVIDER]

    @property
    def EMBEDDING_MODEL(self) -> str:
        return {"qwen": self.QWEN_EMBEDDING_MODEL, "doubao": "", "siliconflow": self.SILICONFLOW_EMBEDDING_MODEL}[self.EMBEDDING_PROVIDER]

    # MySQL
    MYSQL_HOST: str
    MYSQL_PORT: int
    MYSQL_USER: str
    MYSQL_PASSWORD: str
    MYSQL_DATABASE: str
    MYSQL_CHARSET: str = "utf8mb4"

    @property
    def MYSQL_DATABASE_URL(self):
        return f"mysql+asyncmy://{self.MYSQL_USER}:{self.MYSQL_PASSWORD}@{self.MYSQL_HOST}:{self.MYSQL_PORT}/{self.MYSQL_DATABASE}?charset={self.MYSQL_CHARSET}"

    # Milvus
    MILVUS_HOST: str
    MILVUS_PORT: int
    MILVUS_COLLECTION_NAME: str
    MILVUS_EMBEDDING_DIM: int = 1536

    # Neo4j
    NEO4J_URI: str
    NEO4J_USER: str
    NEO4J_PASSWORD: str

    # Redis（会话热上下文缓存）
    REDIS_HOST: str = "127.0.0.1"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: str = ""

    # 对话上下文
    CHAT_CONTEXT_ROUNDS: int = 5          # Redis 热上下文存储窗口：保留最近几轮原文
    CHAT_CONTEXT_TTL_SECONDS: int = 7 * 24 * 3600  # 热上下文缓存 7 天
    # 上下文压缩（发给 LLM 前的整形）
    CHAT_CONTEXT_FULL_ROUNDS: int = 2     # 最近几轮保留全文，更早的截断
    CHAT_CONTEXT_TRUNCATE_CHARS: int = 200   # 较早消息截断长度
    CHAT_CONTEXT_TOKEN_BUDGET: int = 2000    # 热上下文总字符预算（中文≈1字符1token），超出从最旧整轮丢弃
    SUMMARY_SOURCE_MAX_CHARS: int = 500      # 喂给摘要器的每条消息截断长度（提取要点不需要原文，省输入 token）
    # LangSmith 可视化调试（smith.langchain.com，配了 API_KEY 才启用）
    LANGSMITH_TRACING: bool = False
    LANGSMITH_API_KEY: str = ""
    LANGSMITH_PROJECT: str = "recipe-agent"

    # RAG 检索
    RAG_TOP_K: int = 1                    # 重排后最终保留的菜谱数
    RAG_SCORE_THRESHOLD: float = 0.55     # COSINE 相似度阈值，低于则不走 RAG
    RAG_RERANK_CANDIDATES: int = 20       # 重排前 over-fetch 的候选数（向量检索粗排 → rerank 精排）
    RAG_RERANK_SCORE_THRESHOLD: float = 0.3  # rerank 相关性阈值，低于则丢弃；全低于则退化为纯 LLM
    RAG_VERIFY_MAX_RETRIES: int = 3       # 常识校验不通过时的最大重查次数，仍不通过退化为纯 LLM
    RAG_VERIFY_TIMEOUT: float = 8         # 校验调用超时（秒），免费档抖动时 fail-open 放行，不阻塞主链路

settings = Settings()
