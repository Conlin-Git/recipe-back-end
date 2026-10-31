# 高考官方数据包

正式数据只能放入经过人工核验的官方文件解析结果。导入脚本不会接受没有来源 URL、发布机构、年份、省份和科类口径的数据。

建议目录：

```text
data/gaokao/<year>/<province>/score_band.csv
data/gaokao/<year>/<province>/cutoff.csv
data/gaokao/<year>/<province>/admission.csv
```

字段模板和导入命令见 `scripts/import_gaokao.py` 与 `docs/GAOKAO_GUIDE_PLAN.md`。当前仓库不放第三方聚合站的“猜测数据”；在官方文件逐省核验并记录 SHA-256 后，才允许写入正式数据库。

## 录取数据采集

来源清单位于 `admission_sources.json`。采集状态位于 `collection_runs/<run-id>/state.json`，可断网后复用相同 run：

```bash
.venv/bin/python scripts/collect_gaokao_sources.py \
  --manifest data/gaokao/admission_sources.json \
  --run-id 2025-admission-undergraduate-official-v1 --retry-failed
```

站点要求验证码或登录时，先从官方页面下载原始文件，再用 `scripts/attach_gaokao_artifact.py` 挂载到相同 run。随后执行 `stage_gaokao_admissions.py` 生成 staging；人工对照原文后使用 `review_gaokao_staging.py --approve` 生成 verified 文件，最后才运行 `import_gaokao.py`。`gaokao_admissions` 和指南接口只接受 verified 记录。

已完成的官方采集批次：`2026-admission-official-v3`。广西招生考试院 2026 年高职高专普通批第一次、第二次征集（物理类/历史类）共 4 个 HTML 原始文件，原始行 1564 行，正式表 1564 行，按 `admission_round` 保留轮次；每个来源均记录 SHA-256、下载状态和人工复核说明。

本科优先批次：`2025-admission-undergraduate-official-v1` 已采集并核验广东省教育考试院 2025 年本科普通类正式投档 PDF（历史 1,637 行、物理 3,503 行），共 5,140 条院校专业组记录，包含计划数、投档人数、最低分和最低排位。已按来源导出到 `data/gaokao/seeds/`，部署时可运行 `.venv/bin/python scripts/import_gaokao_seeds.py` 重建正式表；种子文件和 manifest 保留来源 URL、SHA-256、年份、科类和行数。

从 PDF 原始文件重新 staging 时需要系统安装 poppler（提供 `pdftotext`）；仓库内的 verified seed 已是解析后的 JSONL，部署重建正式表不需要再次解析 PDF。

2026 本科院校专业组追加批次：

- `2026-undergraduate-official-v1`：湖南官方 XLSX（6624 条），下载时因官方站点证书链问题使用了 `--insecure-tls`，文件 SHA-256 已记录，生产环境应优先使用正常证书校验。
- `2026-undergraduate-official-v2`：福建官方扫描 PDF（历史 1232 条、物理 2934 条）。由于附件为扫描 PDF，采集机用 macOS Vision 150/200 DPI 双分辨率交叉识别；只把代码、专业组、最低分两次一致的记录标为 verified，冲突/缺失行不导入。部署使用 seeds，不需要服务器安装 Vision。

此前 verified seed manifest 为 9 个来源、17494 条记录；本轮追加后 admission seed 已更新为 12 个来源、25134 条记录。湖南、福建和上海官方附件没有最低位次，推荐接口会明确提示位次缺失，不会用分数反推排名。


## 2026 四川、广东、上海追加批次

`2026-undergraduate-official-v4` 保存本轮五份来源的断点状态和 SHA-256：四川两个官方成绩分段文章、广东两个本科投档 PDF、上海本科普通批 PDF。原始大文件留在 collection run 的 raw 目录并由 Git 忽略；可部署的结构化结果随仓库保存。

```bash
.venv/bin/python scripts/import_gaokao_seeds.py --seed-dir data/gaokao/seeds
.venv/bin/python scripts/import_gaokao_scoreband_seeds.py --seed-dir data/gaokao/scoreband_seeds
```

四川两份 scoreband seed 共 1047 行，分数覆盖物理 685--150、历史 662--150，原文明确 150 分以下略；广东 2026 本科两科共 6047 条院校专业组记录，上海共 1593 条且使用 `综合` 科类。所有推荐只使用 `verification_status=verified` 数据，缺失位次时返回数据不足。
