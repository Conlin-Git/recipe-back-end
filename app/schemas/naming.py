from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class NamingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    calendar_type: Literal["solar", "lunar"] = "solar"
    birth_date: date
    birth_time: time | None = None
    lunar_leap_month: bool = False
    timezone: str = "Asia/Shanghai"
    birth_place: str | None = Field(default=None, max_length=100)
    purpose: Literal["person_name", "pen_name", "screen_name"] = "person_name"
    surname: str = Field(default="", max_length=10)
    name: str | None = Field(default=None, max_length=20)
    style: str | None = Field(default=None, max_length=100)
    avoid_chars: str = Field(default="", max_length=50)
    generation_char: str | None = Field(default=None, max_length=2)
    given_length: int = Field(default=2, ge=1, le=3)
    preferred_elements: list[Literal["木", "火", "土", "金", "水"]] = Field(default_factory=list)
    variant_index: int = Field(default=0, ge=0, le=99)

    @field_validator("surname", "avoid_chars", "style", "birth_place", "generation_char", "name", mode="before")
    @classmethod
    def strip_text(cls, value):
        return value.strip() if isinstance(value, str) else value


class NamingRunOut(BaseModel):
    id: int
    input_fingerprint: str
    purpose: str
    ruleset_version: str
    knowledge_snapshot_id: str
    variant_index: int
    created_at: datetime
    result: dict
