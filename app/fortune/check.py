"""Release preflight: python -m app.fortune.check (no database / network required)."""
import asyncio
import hashlib
import json

from app.fortune.service import ENGINE, analyze
from app.schemas.fortune import FortuneInput


async def check():
    manifest = json.loads(ENGINE.with_name("manifest.json").read_text())
    if hashlib.sha256(ENGINE.read_bytes()).hexdigest() != manifest["bundle_sha256"]:
        raise RuntimeError("紫微引擎产物校验失败，请运行 engine 目录的 npm ci && npm run build")
    root = ENGINE.parent.parent
    for name, digest in manifest["sources"].items():
        if name.startswith("node_modules/"):
            continue
        path = root / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise RuntimeError(f"紫微源码与发布产物不一致：{name}，请重新构建")
    result = await analyze(FortuneInput(calendar_type="solar", birth_date="1990-05-17", birth_time="14:30", gender="female"))
    assert len(result["palaces"]) == 12
    assert len(result["details"]["periods"]) == 12
    assert len(result["details"]["years"]) >= 80
    assert result["details"]["years"][79]["age"] == 80
    assert result["summary"]["ming_branch"] == "戌"
    assert result["summary"]["wuxing"] == "土五局"
    print("紫微排盘发布检查通过：内置产物完整，Node 可运行，完整命盘、12 个大限与逐年流年计算正常。")


if __name__ == "__main__":
    asyncio.run(check())
