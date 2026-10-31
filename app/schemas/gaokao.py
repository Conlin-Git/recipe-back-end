from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


DISCLAIMER = (
    "数据仅供志愿填报参考，不构成录取承诺；招生政策、计划和录取结果以当年省教育考试院及招生院校官方发布为准，"
    "因使用本服务产生的任何后果由用户自行核验和承担。"
)


def normalize_batch(value: str) -> str:
    """统一用户常用批次简称，兼容导入数据中的官方批次名称。"""
    value = value.strip()
    return {
        "本科": "本科普通批",
        "本科批": "本科普通批",
        "本科批次": "本科普通批",
    }.get(value, value)


class GaokaoGuideInput(BaseModel):
    # province 保留给旧客户端；新客户端应显式传 candidate_province/admission_province。
    province: str | None = Field(default=None, max_length=30)
    candidate_province: str | None = Field(default=None, max_length=30)
    admission_province: str | None = Field(default=None, max_length=30)
    school_provinces: list[str] = Field(default_factory=list, max_length=3)
    # candidate_year 是考生画像年份，reference_year 是拿来对比的历史投档年份。
    # year 仅保留给旧客户端，收到后会同时填充两个新字段。
    candidate_year: int = Field(default=2026, ge=2022, le=2100)
    reference_year: int = Field(default=2026, ge=2022, le=2100)
    year: int | None = Field(default=None, exclude=True, repr=False)
    track: str = Field(min_length=1, max_length=30)
    rank: int | None = Field(default=None, ge=1)
    score: int | None = Field(default=None, ge=0, le=1000)
    batch: str = Field(default="本科普通批", max_length=60)
    subjects: list[str] = Field(default_factory=list, max_length=6)
    preferred_regions: list[str] = Field(default_factory=list, max_length=10)
    keywords: str = Field(default="", max_length=120)
    limit: int = Field(default=30, ge=1, le=100)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_year(cls, values):
        if not isinstance(values, dict):
            return values
        values = dict(values)
        legacy_province = values.get("province") or values.get("candidate_province") or values.get("admission_province")
        if legacy_province:
            values.setdefault("province", legacy_province)
            values.setdefault("candidate_province", legacy_province)
            values.setdefault("admission_province", legacy_province)
        legacy_year = values.get("year")
        if legacy_year is not None:
            values.setdefault("candidate_year", legacy_year)
            values.setdefault("reference_year", legacy_year)
        return values

    @model_validator(mode="after")
    def require_province_dimensions(self):
        if not self.candidate_province or not self.admission_province:
            raise ValueError("必须提供考生省份和招生省份")
        return self

    @field_validator("province", "candidate_province", "admission_province", "track", "keywords", mode="before")
    @classmethod
    def strip_text(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("batch", mode="before")
    @classmethod
    def canonicalize_batch(cls, value):
        return normalize_batch(value) if isinstance(value, str) else value

    @field_validator("school_provinces", mode="before")
    @classmethod
    def normalize_school_provinces(cls, value):
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]


class GaokaoRecommendation(BaseModel):
    tier: Literal["冲", "稳", "保"]
    rank_status: Literal["已超过", "未达到"]
    record_kind: str = "major_admit"
    admission_round: str = "regular"
    university_name: str
    school_province: str | None = None
    group_name: str = ""
    major_name: str = ""
    subject_requirements: str | None = None
    min_score: int | None = None
    min_rank: int | None = None
    avg_score: int | None = None
    max_score: int | None = None
    admission_count: int | None = None
    submission_count: int | None = None
    admission_status: str | None = None
    historical_years: list[int]
    historical_min_ranks: list[int]
    rank_gap: int | None = None
    plan_count: int | None = None
    confidence: Literal["高", "中", "低"]
    evidence: list[dict]
    reason: str


class GaokaoGuideOut(BaseModel):
    disclaimer: str
    data_status: Literal["ok", "insufficient"]
    profile: dict
    recommendations: list[GaokaoRecommendation]
    sections: dict[str, list[GaokaoRecommendation]] = Field(default_factory=dict)
    sources: list[dict]
    warnings: list[str]
