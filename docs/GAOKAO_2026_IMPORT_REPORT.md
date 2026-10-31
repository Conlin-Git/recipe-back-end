# 2026 高考数据导入报告

导入时间：2026-10-31（本地 MySQL）

## 已入库

- `gaokao_sources`：37 个已核验来源（31 个批次线来源、6 个一分一段附件来源）。
- `gaokao_cutoffs`：166 条普通类/特殊类型控制线，覆盖 31 个省级行政区，来源为教育部阳光高考信息平台及省级考试机构文章。
- `gaokao_score_bands`：2,850 条普通类一分一段记录，覆盖天津、上海、江西、山东、海南；每行校验分数、人数、累计人数和位次区间，原始附件 SHA-256 已记录。
- 本科历史依据另见 `docs/GAOKAO_UNDERGRADUATE_IMPORT_REPORT.md`：广东省教育考试院 2025 年本科普通批历史类/物理类共 5,140 条院校专业组投档记录，已 verified 并导出为可部署 seed。

## 未入库

- 2026 年当前正式表中的录取数据为 1,564 条广西招生考试院官方院校专业组投档记录（高职高专普通批第一次/第二次征集，物理类/历史类）。每条记录保留 `record_kind`、`admission_round`、原始行哈希、来源定位、核验状态；空白的官方投档最低分保持 `admission_status=not_reported`，不改写为 0。全国其他省份仍需按省级招办或院校官方来源逐省采集，未核验的数据不会生成推荐。
- 图片 OCR 表格：保留在本地来源包，未直接写入正式表；只有逐行和列口径通过校验的文件才允许入库。

## 重跑

```bash
.venv/bin/python scripts/import_2026_official_cutoffs.py
.venv/bin/python scripts/import_2026_official_scorebands.py
```

脚本按来源摘要和业务唯一键幂等更新。当前阶段只做 2026 年，近五年历史数据不在本次导入范围。
