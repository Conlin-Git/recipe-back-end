# 2026 四川本科批次 B 段投档线导入记录

更新时间：2026-11-01

本次导入用户指定微信公众号文章中历史类、物理类两张本科批次 B 段调档线表格图片。微信原文无法直接抓取，已使用可访问的同版文章镜像图片完成提取，并保存文章链接、镜像图片地址及图片哈希。

导入结果：

- 物理类：290 条，覆盖 80 所院校。
- 历史类：117 条，覆盖 50 所院校。
- 合计：407 条，覆盖 85 所院校（两科类院校有交集）。
- 403 条有调档分，并按四川 2026 官方一分一段表换算最低位次。
- 4 条原表标记为“未满”，保留 `min_score=null`、`min_rank=null`，状态为 `not_reported`。
- 所有本批次文章记录状态为 `pending`，因为图片表格仍需逐行和四川省教育考试院或院校官方公告核验；推荐服务只使用 `verified` 记录。

字段口径：`province=四川`、`year=2026`、`batch=本科批次B段`、`record_kind=group投档`。原表只提供科类、院校名称、专业组和调档线；位次是根据官方分段表换算的派生字段，并在 `evidence_note` 标记为 `derived_rank_from_sichuan_2026_score_band`。院校所在地写入 `school_province`。

数据文件：`data/gaokao/user_import/2026_sichuan_b_section_article_admissions.jsonl`。

来源清单：`data/gaokao/user_import/2026_sichuan_b_section_article_source.json`。

数据库来源记录：`gaokao_sources.source_id=81`，原始种子 SHA-256 为 `5bf4340b4f5dfb409895d8afda0446c5a693420ebb3efe4f89c7b6fd3d6174b6`。
