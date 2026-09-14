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
    print("✅ 数据表创建完成：", ", ".join(Base.metadata.tables.keys()))


if __name__ == "__main__":
    asyncio.run(main())
