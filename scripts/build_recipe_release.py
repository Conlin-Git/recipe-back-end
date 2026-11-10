"""Build frontend + backend into one LOCAL release directory; never deploy or SSH."""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    backend = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="本地构建 recipe 前后端统一发布产物，不访问服务器")
    parser.add_argument("--frontend", type=Path, default=backend.parent / "recipe-front-end")
    parser.add_argument("--output", type=Path, required=True, help="必须是不存在的新目录")
    args = parser.parse_args()
    frontend, output = args.frontend.resolve(), args.output.resolve()
    if output.exists():
        parser.error("输出目录已存在，请选择新目录以免覆盖已有产物")
    if not (frontend / "package.json").is_file():
        parser.error("找不到前端项目，请通过 --frontend 指定")
    # Resolve prerequisites before creating output. uv build includes all app assets.
    for binary in ("node", "npm", "uv"):
        if not shutil.which(binary):
            parser.error(f"缺少构建工具：{binary}")
    subprocess.run([sys.executable, "-m", "app.fortune.check"], cwd=backend, check=True)
    subprocess.run(["npm", "run", "build"], cwd=frontend, check=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".recipe-build-", dir=output.parent) as temporary:
        staging = Path(temporary) / "release"
        subprocess.run(["uv", "build", "--wheel", "--out-dir", str(staging / "backend")], cwd=backend, check=True)
        shutil.copytree(frontend / "dist", staging / "frontend")
        shutil.copy2(backend / "uv.lock", staging / "backend/uv.lock")
        shutil.copy2(backend / "pyproject.toml", staging / "backend/pyproject.toml")
        subprocess.run(["uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--output-file", str(staging / "backend/requirements.txt")], cwd=backend, check=True, stdout=subprocess.DEVNULL)
        (staging / "README.txt").write_text(
        "Recipe 前后端统一发布产物\n"
        "frontend/: 部署到原 recipe 静态站点目录，继续使用原 /api 反向代理。\n"
        "backend/: Python wheel 包含紫微源码、引擎产物及许可证；按原后端发布流程安装此 wheel 与锁定依赖。\n"
        "新运行时前提：后端宿主机必须有 Node.js 18+（建议 22），无需额外 npm 服务或端口。\n"
        "安装后、重启前在部署虚拟环境运行 python -m app.fortune.check。\n"
        "安装示例（需先确认具体生产操作范围）：\n"
        "uv pip sync --python /path/to/.venv/bin/python backend/requirements.txt\n"
        "uv pip install --python /path/to/.venv/bin/python --no-deps backend/recipe_back_end-0.1.0-py3-none-any.whl\n"
        "保留原环境变量、数据库、上传目录和高考数据；本产物不包含密钥、数据库备份或用户数据。\n"
        "此命令仅构建本地产物，没有连接或修改任何生产服务器。\n", encoding="utf-8",
        )
        staging.rename(output)
    print(f"Recipe 统一发布产物：{output}")


if __name__ == "__main__":
    main()
