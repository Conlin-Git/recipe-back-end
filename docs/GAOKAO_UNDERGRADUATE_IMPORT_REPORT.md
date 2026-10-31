# 本科院校专业组数据导入报告

更新时间：2026-10-31

## 已核验并入库

来源为广东省教育考试院官方 PDF：

- [2025 年本科普通类（历史）投档情况](https://eea.gd.gov.cn/attachment/0/585/585885/4746781.pdf)：35 页，1,637 条。
- [2025 年本科普通类（物理）投档情况](https://eea.gd.gov.cn/attachment/0/585/585886/4746786.pdf)：67 页，3,503 条。

两份表均逐行保留院校代码、院校名称、专业组代码、计划数、投档人数、投档最低分和投档最低排位，`record_kind=group_admit`，`admission_round=本科普通批正式投档`，正式表中全部为 `verification_status=verified`。原始 PDF 的 SHA-256、下载状态、解析报告和复核说明保存在 `data/gaokao/collection_runs/2025-admission-undergraduate-official-v1/`；可部署 seed 位于 `data/gaokao/seeds/`。

## 数据边界

这批数据是 2025 年在广东招生的本科院校专业组投档数据，属于本科层级的官方历史依据，包含位次，可用于同省同科类位次比较。它不是 2026 年录取结果，也不是专业录取线；系统会在答案中显示年份和“院校专业组投档线”层级。广西 2026 年高职高专数据另存为独立轮次，不与本科推荐混用。

## 部署重建

```bash
.venv/bin/python -m scripts.init_db
.venv/bin/python scripts/import_gaokao_seeds.py --seed-dir data/gaokao/seeds
```

重复执行按来源 SHA 和业务唯一键幂等更新；种子导入不会把未核验数据提升为 verified。

## 2026-10-31 追加：湖南、福建本科院校专业组投档线

- 湖南省教育考试院官方公告及 `2026071901.zip`：普通类本科批历史类 1898 条、物理类 4726 条，共 6624 条；表中同时保留投档线、同分排序字段，未提供最低位次，因此 `min_rank` 保持空值。
- 福建省教育考试院官方公告附件：历史类官方扫描 PDF 通过 150/200 DPI Vision OCR 对院校代码、专业组代码、投档最低分交叉一致后保留 1232 条；物理类保留 2934 条。院校名称优先按福建省教育考试院 2026 本科批招生计划 PDF 的院校代码映射；双分辨率缺失或分数不一致的行未进入 verified。
- 两省均使用独立 `source_id`、批次和投档轮次，不修改或删除已有广东、广西数据；正式 seed 已更新到 `data/gaokao/seeds/manifest.json`，共 9 个 verified 来源、17494 条院校专业组记录。

## 2026-10-31 追加：四川、广东、上海五份官方来源

- 四川省教育考试院[历史类成绩分段表](https://www.sceea.cn/Html/202606/Newsdetail_4858.html) 解析 513 行、[物理类成绩分段表](https://www.sceea.cn/Html/202606/Newsdetail_4857.html) 解析 534 行；分数、人数、累计位次按行校验，官方页面注明 150 分以下略。seed 位于 `data/gaokao/scoreband_seeds/`，审计见 `docs/GAOKAO_SICHUAN_2026_SCOREBAND_AUDIT.json`。
- 广东省教育考试院[2026 历史类本科投档 PDF](https://eea.gd.gov.cn/attachment/0/620/620024/4926622.pdf) 1797 行、[物理类本科投档 PDF](https://eea.gd.gov.cn/attachment/0/620/620025/4926622.pdf) 4250 行，全部保留为院校专业组记录；其中各有 2 行原文明确未公布投档结果，保留记录但不虚构分数/位次。
- 上海市教育考试院[2026 本科普通批院校专业组投档 PDF](https://cdn.shmeea.edu.cn/resource/upload/www/20260706/53767.pdf) 1593 行，按 `track=综合` 入库；上海不拆物理/历史，580 分及以上按原文写入 tie_breaker，PDF 不提供省内位次。
- 本轮 5 个来源均在 `2026-undergraduate-official-v4` 采集 run 中保存断点状态、原始 SHA-256 和人工挂载 artifact；广东/上海 admission seeds 已更新，当前 verified admission seed 为 12 个来源、25134 条记录。

