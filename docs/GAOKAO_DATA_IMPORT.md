# 高考数据导入字段与核验清单

正式导入必须同时满足：官方发布机构、可访问来源 URL、原始文件 SHA-256、年份、省份、科类口径和解析行数。只提供网页摘要或第三方聚合表的文件不得导入。

## 字段模板

`score_band.csv`：

```text
province,year,track,score,candidate_count,cumulative_rank,rank_start,rank_end
```

`cutoff.csv`：

```text
province,year,track,batch,cutoff_type,score,note
```

`admission.csv`：

```text
province,year,track,batch,university_code,university_name,group_code,group_name,major_code,major_name,subject_requirements,min_score,min_rank,avg_score,max_score,plan_count,province_code,evidence_note
```

## 导入与检查

### 2026 当前年度官方附件导入

批次线脚本读取已核验的 31 个省级官方文章，导入普通类/特殊类型控制线；一分一段脚本读取官方 PDF/XLS 中能逐行验证的普通类表格。脚本可重复执行，按来源 SHA-256 和业务唯一键幂等更新：

```bash
.venv/bin/python scripts/import_2026_official_cutoffs.py
.venv/bin/python scripts/import_2026_official_scorebands.py
```

当前导入范围以数据库 `gaokao_sources.validation_status=verified` 为准。图片 OCR、无法证明列对应关系的附件不会自动写入 `gaokao_score_bands`；院校专业组录取线必须有官方招生机构/院校文件后才能写入 `gaokao_admissions`，不能用聚合站补齐。

```bash
.venv/bin/python scripts/import_gaokao.py \
  --input data/gaokao/2026/广东/score_band.csv \
  --dataset-type score_band \
  --source-url 'https://省教育考试院官方文件地址' \
  --publisher '广东省教育考试院' \
  --year 2026 --province 广东 --dry-run

.venv/bin/python scripts/check_gaokao_completeness.py
```

正式写库前先执行 `--dry-run`，确认行数、重复主键和字段范围；正式导入后必须运行来源覆盖检查。当前阶段只承诺 2026 年已核验范围；院校专业组录取数据为空时，指南接口只返回位次换算和数据不足，不生成学校推荐。
