# 四川本科普通批广东、上海院校数据导入报告

更新时间：2026-11-01

## 目标与口径

- 生源省：四川；批次：本科普通批/本科批次 B 段。
- 科类：四川 3+1+2 口径的物理类、历史类。
- `school_province` 表示高校所在地，不能由生源省 `province` 推断。
- 最低位次优先采用高校官网公布值；官网只公布最低分时，按四川省教育考试院同年度一分一段表换算并在数据中标注。
- 目前只导入院校组级最低分，没有把专业最低分冒充院校组投档线。

## 已核验来源与数量

### 2025 年

采集批次：`data/gaokao/collection_runs/2025-sichuan-crossprovince-official-v1/`

| 院校所在地 | 院校 | 记录数 | 官方来源 |
| --- | --- | ---: | --- |
| 广东 | 暨南大学 | 3 | https://zsb.jnu.edu.cn/2026/0331/c3454a852783/page.htm |
| 广东 | 深圳大学 | 3 | https://zs.szu.edu.cn/info/1153/2965.htm |
| 广东 | 广东财经大学 | 2 | https://www.gdufe.edu.cn/_upload/article/files/3b/1a/9a8491b44429a675bd114fd3beec/028fe998-0131-45f2-9ac9-12f26c7358d0.pdf |
| 上海 | 复旦大学 | 3 | https://ao.fudan.edu.cn/b7/a6/c36333a767910/page.htm |
| 上海 | 同济大学 | 5 | https://xxgk.tongji.edu.cn/index.php?classid=3140&newsid=20942&t=show |
| 上海 | 东华大学 | 2 | https://zs.dhu.edu.cn/2026/0228/c25199a371806/page.htm |
| 上海 | 上海理工大学 | 3 | https://xxgk.usst.edu.cn/2025/1030/c8490a349660/page.htm |
| 上海 | 上海大学 | 4 | https://bkzsw.shu.edu.cn/info/1798/38010.htm |
| 上海 | 上海海事大学 | 2 | https://info.shmtu.edu.cn/2025/1030/c10187a281226/page.htm |

合计 27 条。最低位次均可由 2025 四川官方分段表换算；深圳大学官网同时公布了四川最低排位，数据中保留高校原始排位并标记为 `official_university_rank`。

### 2026 年

采集批次：`data/gaokao/collection_runs/2026-sichuan-crossprovince-official-v1/`

| 院校所在地 | 院校 | 记录数 | 官方来源 |
| --- | --- | ---: | --- |
| 上海 | 同济大学 | 4 | https://xxgk.tongji.edu.cn/index.php?classid=3140&newsid=21809&t=show |
| 上海 | 上海大学 | 4 | https://bkzsw.shu.edu.cn/info/7833/38950.htm |

合计 8 条。暨南大学查询系统有行为验证，仓库保留 2 条官方查询页人工核对摘录 `raw/jnu-2026-sichuan/payload.txt`，但因不是完整可复核专业组导出而保持 `pending`，不进入推荐。

## 推荐能力

当考生输入 `candidate_year=2027`、`candidate_province=四川`、`track=物理类/历史类`、四川省内位次，并设置 `reference_year=2026` 时，指南服务会：

1. 先按四川同科类、同批次、已核验记录筛选；
2. 再按 `school_provinces` 精确筛选广东或上海院校所在地；
3. 用同口径最低位次计算“冲/稳/保”档位和位次差；
4. 返回来源 URL、来源 SHA-256、来源批次和核验状态。

2026 年只对上述已核验子集给出推荐；数据缺失时返回 `insufficient` 和明确警告，禁止模型自行补充学校或用跨省分数换算位次。

## 离线重放

```bash
.venv/bin/python scripts/import_gaokao_scoreband_seeds.py --seed-dir data/gaokao/scoreband_seeds
.venv/bin/python scripts/import_gaokao_seeds.py --seed-dir data/gaokao/seeds
```

两个 collection run 的 `manifest.snapshot.json`、`state.json`、raw 文件、staging JSONL 和导入后的 seed manifest 共同构成可回溯链路。任何后续补采集都必须创建新批次，禁止覆盖旧 raw 或改变已导入记录的来源哈希。


## 本轮追加：2025-11-01 官方来源 v2

本轮新增 6 条四川招生省口径记录，均已以 `verified` 状态入库：

| 院校所在地 | 学校 | 年份 | 科类 | 数据口径 | 最低分/位次 |
|---|---|---:|---|---|---|
| 广东 | 东莞理工学院 | 2025 | 物理类 | 官网四川本科批出档最低分 | 531（原始 531.2181111）/ 87075 |
| 广东 | 东莞理工学院 | 2025 | 历史类 | 官网四川本科批出档最低分 | 552（原始 552.1901061）/ 19276 |
| 上海 | 华东师范大学 | 2025 | 物理类 | 官网四川普通类本科批逐专业最低录取分汇总 | 635/7273 |
| 上海 | 华东师范大学 | 2025 | 历史类 | 官网四川普通类本科批逐专业最低录取分汇总 | 631/942 |
| 上海 | 华东师范大学 | 2026 | 物理类 | 官网四川普通类本科批逐专业最低录取分汇总 | 634/5669 |
| 上海 | 华东师范大学 | 2026 | 历史类 | 官网四川普通类本科批逐专业最低录取分汇总 | 631/926 |

东莞理工学院记录的分数原始小数保存在 `tie_breaker` 字段；华东师范大学记录明确使用 `record_kind=major_admit`，表示逐专业最低录取分汇总，不冒充院校专业组投档线。原始文件、SHA-256、逐行 staging 和排除反爬文件记录见 `data/gaokao/collection_runs/2025-sichuan-crossprovince-official-v2/`。
