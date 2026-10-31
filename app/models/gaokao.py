"""高考数据的可追溯结构化快照。所有分数必须绑定省份、年份和科类。"""
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.mysql import Base


class GaokaoSource(Base):
    __tablename__ = "gaokao_sources"
    __table_args__ = (UniqueConstraint("source_key", name="uq_gaokao_source_key"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_key: Mapped[str] = mapped_column(String(160))
    publisher: Mapped[str] = mapped_column(String(120))
    province: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    year: Mapped[int] = mapped_column(Integer, index=True)
    dataset_type: Mapped[str] = mapped_column(String(40))
    source_url: Mapped[str] = mapped_column(String(600))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sha256: Mapped[str] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    validation_status: Mapped[str] = mapped_column(String(30), default="pending")
    license_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class GaokaoCutoff(Base):
    __tablename__ = "gaokao_cutoffs"
    __table_args__ = (
        UniqueConstraint("province", "year", "track", "batch", "cutoff_type", name="uq_gaokao_cutoff"),
        Index("ix_gaokao_cutoff_lookup", "province", "year", "track", "batch"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    province: Mapped[str] = mapped_column(String(30))
    year: Mapped[int] = mapped_column(Integer)
    track: Mapped[str] = mapped_column(String(30))
    batch: Mapped[str] = mapped_column(String(60))
    cutoff_type: Mapped[str] = mapped_column(String(30), default="control")
    score: Mapped[int] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_id: Mapped[int] = mapped_column(Integer, index=True)


class GaokaoScoreBand(Base):
    __tablename__ = "gaokao_score_bands"
    __table_args__ = (
        UniqueConstraint("province", "year", "track", "score", name="uq_gaokao_score_band"),
        Index("ix_gaokao_band_rank", "province", "year", "track", "cumulative_rank"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    province: Mapped[str] = mapped_column(String(30))
    year: Mapped[int] = mapped_column(Integer)
    track: Mapped[str] = mapped_column(String(30))
    score: Mapped[int] = mapped_column(Integer)
    candidate_count: Mapped[int] = mapped_column(Integer, default=0)
    cumulative_rank: Mapped[int] = mapped_column(Integer)
    rank_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rank_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_id: Mapped[int] = mapped_column(Integer, index=True)


class GaokaoAdmission(Base):
    __tablename__ = "gaokao_admissions"
    __table_args__ = (
        UniqueConstraint(
            "province", "year", "track", "batch", "record_kind", "university_code", "group_code", "major_code",
            "admission_round", name="uq_gaokao_admission",
        ),
        Index("ix_gaokao_admission_lookup", "province", "school_province", "track", "batch", "min_rank"),
        Index("ix_gaokao_admission_university", "university_name", "province", "year"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    province: Mapped[str] = mapped_column(String(30))
    # province 是生源省；school_province 是院校所在地，不能混用。
    school_province: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    year: Mapped[int] = mapped_column(Integer)
    track: Mapped[str] = mapped_column(String(30))
    batch: Mapped[str] = mapped_column(String(60))
    # 同一批次可能有多轮征集；轮次是业务事实，不能因业务键相同而覆盖。
    admission_round: Mapped[str] = mapped_column(String(40), default="regular")
    # group投档 / group_admit / major_admit；不同层级不能互相覆盖。
    record_kind: Mapped[str] = mapped_column(String(30), default="major_admit")
    university_code: Mapped[str] = mapped_column(String(40))
    university_name: Mapped[str] = mapped_column(String(160))
    group_code: Mapped[str] = mapped_column(String(80), default="")
    group_name: Mapped[str] = mapped_column(String(240), default="")
    major_code: Mapped[str] = mapped_column(String(80), default="")
    major_name: Mapped[str] = mapped_column(String(240), default="")
    subject_requirements: Mapped[str | None] = mapped_column(String(240), nullable=True)
    min_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    avg_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    plan_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    admission_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    submission_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tie_breaker: Mapped[str | None] = mapped_column(String(400), nullable=True)
    plan_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    province_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    source_id: Mapped[int] = mapped_column(Integer, index=True)
    source_locator: Mapped[str | None] = mapped_column(String(240), nullable=True)
    raw_record_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    verification_status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    verification_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    admission_status: Mapped[str] = mapped_column(String(30), default="admitted")
    evidence_note: Mapped[str | None] = mapped_column(Text, nullable=True)
