from datetime import date
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DISCLAIMER = "仅供娱乐与传统文化交流，不构成对人生、健康、财富或关系的预测与决策建议。"


class FortuneInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    calendar_type: Literal["solar", "lunar"]
    birth_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    birth_time: str = Field(pattern=r"^\d{2}:\d{2}$")
    gender: Literal["male", "female"]
    lunar_leap_month: bool = False
    utc_offset_minutes: int = Field(default=480, ge=-720, le=840)
    longitude: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    late_zishi: Literal["next-day", "current-day"] = "next-day"

    @field_validator("birth_time")
    @classmethod
    def valid_time(cls, value: str) -> str:
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("请填写有效的出生时间（00:00–23:59）")
        return value

    @model_validator(mode="after")
    def valid_date(self):
        year, month, day = map(int, self.birth_date.split("-"))
        if not (1900 <= year <= 2050 and 1 <= month <= 12 and 1 <= day <= 31):
            raise ValueError("出生日期范围为 1900–2050 年，请检查年月日")
        if self.calendar_type == "solar":
            if self.lunar_leap_month:
                raise ValueError("只有农历日期可以选择闰月")
            try:
                date(year, month, day)
            except ValueError as exc:
                raise ValueError("公历日期不存在，请核对年月日") from exc
        elif day > 30:
            raise ValueError("农历日期不能大于三十")
        if self.utc_offset_minutes % 15:
            raise ValueError("标准时区偏移需为 15 分钟的整数倍")
        return self


class FortuneInterpretInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    birth: FortuneInput
    question: str = Field(default="完整白话解读", min_length=1, max_length=1000)
