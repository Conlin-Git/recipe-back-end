"""命名运行的幂等归档服务。"""
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.naming_run import NamingRun
from app.naming.engine import KNOWLEDGE_SNAPSHOT_ID, RULESET_VERSION, NamingEngine, input_fingerprint

engine = NamingEngine()


async def run(db: AsyncSession, user_id: int, payload: dict, mode: str) -> NamingRun:
    normalized = engine.normalize(payload)
    fingerprint = input_fingerprint(user_id, normalized)
    statement = select(NamingRun).where(
        NamingRun.user_id == user_id,
        NamingRun.input_fingerprint == fingerprint,
        NamingRun.ruleset_version == RULESET_VERSION,
        NamingRun.knowledge_snapshot_id == KNOWLEDGE_SNAPSHOT_ID,
        NamingRun.variant_index == int(normalized.get("variant_index", 0)),
    )
    existing = (await db.execute(statement)).scalar_one_or_none()
    if existing:
        return existing
    result = engine.analyze(user_id, normalized) if mode == "analyze" else engine.score(user_id, normalized)
    record = NamingRun(
        user_id=user_id,
        input_fingerprint=fingerprint,
        purpose=normalized.get("purpose", "person_name"),
        ruleset_version=RULESET_VERSION,
        knowledge_snapshot_id=KNOWLEDGE_SNAPSHOT_ID,
        variant_index=int(normalized.get("variant_index", 0)),
        input_json=json.dumps(normalized, ensure_ascii=False, default=str),
        result_json=json.dumps(result, ensure_ascii=False, default=str),
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record


async def get_mine(db: AsyncSession, user_id: int, run_id: int) -> NamingRun | None:
    return (await db.execute(select(NamingRun).where(NamingRun.id == run_id, NamingRun.user_id == user_id))).scalar_one_or_none()


def to_output(record: NamingRun) -> dict:
    return {
        "id": record.id, "input_fingerprint": record.input_fingerprint, "purpose": record.purpose,
        "ruleset_version": record.ruleset_version, "knowledge_snapshot_id": record.knowledge_snapshot_id,
        "variant_index": record.variant_index, "created_at": record.created_at,
        "result": json.loads(record.result_json),
    }
