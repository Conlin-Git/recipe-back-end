"""公开的模型来源说明，仅返回用于公示的名称，不返回连接配置或凭证。"""
from fastapi import APIRouter
from pydantic import BaseModel

from app.config import settings

router = APIRouter(prefix="/service-info", tags=["服务说明"])


class ModelSource(BaseModel):
    purpose: str
    provider: str
    model: str


class ServiceInfo(BaseModel):
    models: list[ModelSource]


@router.get("", response_model=ServiceInfo)
async def get_service_info():
    providers = {"siliconflow": "硅基流动", "zhipu": "智谱 BigModel", "qwen": "阿里云百炼"}
    # 与 Settings.LLM_MODEL 的选路保持一致：非 siliconflow 时走智谱。
    llm_provider = "siliconflow" if settings.LLM_PROVIDER == "siliconflow" else "zhipu"
    return ServiceInfo(models=[
        ModelSource(purpose="对话生成", provider=providers[llm_provider], model=settings.LLM_MODEL),
        ModelSource(purpose="辅助分析与内容校验", provider="硅基流动",
                    model="、".join(dict.fromkeys([settings.SENTIMENT_LLM_MODEL, settings.SILICONFLOW_LLM_MODEL]))),
        ModelSource(purpose="知识库检索", provider=providers.get(settings.EMBEDDING_PROVIDER, settings.EMBEDDING_PROVIDER),
                    model=settings.EMBEDDING_MODEL),
        ModelSource(purpose="检索结果重排", provider="硅基流动", model=settings.SILICONFLOW_RERANK_MODEL),
    ])
