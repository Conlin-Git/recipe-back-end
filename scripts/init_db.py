"""初始化数据库：按 app/models 里的 ORM 定义创建所有表（已存在的表会跳过）。

用法：.venv/bin/python -m scripts.init_db
后续表结构变更频繁时，建议引入 Alembic 做迁移管理。
"""
import asyncio

from app.database.mysql import Base, engine

# 导入 models 包，确保所有表都注册到 Base.metadata
import app.models  # noqa: F401


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # create_all 不会给已存在的表加列，这里做幂等的增量列迁移
        try:
            await conn.exec_driver_sql(
                "ALTER TABLE conversations ADD COLUMN summary TEXT NULL"
            )
            print("✅ conversations 表新增 summary 列")
        except Exception:
            pass  # 列已存在
        try:
            await conn.exec_driver_sql(
                "ALTER TABLE conversations ADD COLUMN last_read_at DATETIME NULL"
            )
            # 历史会话回填为已读（updated_at），避免迁移后全部冒未读红点
            await conn.exec_driver_sql(
                "UPDATE conversations SET last_read_at = updated_at "
                "WHERE last_read_at IS NULL"
            )
            print("✅ conversations 表新增 last_read_at 列（历史数据已回填）")
        except Exception:
            pass  # 列已存在
        # 高考录取数据需要区分院校专业组投档线与专业录取线，并保留原始行审计字段。
        admission_columns = {
            "school_province": "VARCHAR(30) NULL",
            "record_kind": "VARCHAR(30) NOT NULL DEFAULT 'major_admit'",
            "admission_round": "VARCHAR(40) NOT NULL DEFAULT 'regular'",
            "admission_count": "INT NULL",
            "submission_count": "INT NULL",
            "tie_breaker": "VARCHAR(400) NULL",
            "plan_type": "VARCHAR(80) NULL",
            "source_locator": "VARCHAR(240) NULL",
            "raw_record_hash": "VARCHAR(64) NULL",
            "verification_status": "VARCHAR(30) NOT NULL DEFAULT 'pending'",
            "verified_at": "DATETIME NULL",
            "verification_note": "TEXT NULL",
            "admission_status": "VARCHAR(30) NOT NULL DEFAULT 'admitted'",
        }
        for column, definition in admission_columns.items():
            try:
                await conn.exec_driver_sql(f"ALTER TABLE gaokao_admissions ADD COLUMN {column} {definition}")
            except Exception:
                pass  # 已存在或旧数据库尚未创建高考表
        try:
            await conn.exec_driver_sql("ALTER TABLE gaokao_admissions DROP INDEX ix_gaokao_admission_lookup")
        except Exception:
            pass
        try:
            await conn.exec_driver_sql(
                "ALTER TABLE gaokao_admissions ADD INDEX ix_gaokao_admission_lookup "
                "(province, school_province, track, batch, min_rank)"
            )
        except Exception:
            pass
        # 旧版本唯一键不含 record_kind/轮次；逐步迁移时兼容不同历史命名。
        for index_name in ("uq_gaokao_admission", "uq_gaokao_admission_v2"):
            try:
                await conn.exec_driver_sql(f"ALTER TABLE gaokao_admissions DROP INDEX {index_name}")
            except Exception:
                pass
        try:
            await conn.exec_driver_sql(
                "ALTER TABLE gaokao_admissions ADD CONSTRAINT uq_gaokao_admission_v3 "
                "UNIQUE (province, year, track, batch, record_kind, admission_round, university_code, group_code, major_code)"
            )
        except Exception:
            pass
    print("✅ 数据表创建完成：", ", ".join(Base.metadata.tables.keys()))


if __name__ == "__main__":
    asyncio.run(main())
