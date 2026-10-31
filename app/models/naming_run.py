"""命理命名结果快照：规则升级时新增版本，不覆盖历史结果。"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.mysql import Base


class NamingRun(Base):
    __tablename__ = "naming_runs"
    __table_args__ = (
        UniqueConstraint("user_id", "input_fingerprint", "ruleset_version", "knowledge_snapshot_id", "variant_index", name="uq_naming_run_version"),
        Index("ix_naming_runs_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    input_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    purpose: Mapped[str] = mapped_column(String(30), default="person_name")
    ruleset_version: Mapped[str] = mapped_column(String(80))
    knowledge_snapshot_id: Mapped[str] = mapped_column(String(120))
    variant_index: Mapped[int] = mapped_column(Integer, default=0)
    input_json: Mapped[str] = mapped_column(Text)
    result_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
