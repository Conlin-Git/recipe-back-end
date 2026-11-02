# 四川 2026 投档截图：本地批量识别与离线入库

源文件是用户提供的截图，发布机构未由原图独立证明。原表是“投档情况”；最低投档分、位次不等于最终录取分、概率或保证。历史类与物理类分开。提前批、特殊类别以及未满等状态不能当作普通本科 B 段投档记录使用。

## 本批次范围

| 科类 | WebP 文件 | 不同图片 | 不重复的表格行 |
|---|---:|---:|---:|
| 历史类 | 42 | 42 | 1,476 |
| 物理类 | 92 | 91 | 3,256 |

物理文件 `640-70.webp` 与 `640-71.webp` 的字节相同。全部文件按表格网格共计 4,768 行，减去重复页的 36 行后是 4,732 行。这是对**提供的图片**的覆盖，不是对全省招生数据完整性的证明。每页行数和 SHA-256 在 `data/gaokao/collection_runs/2026-sichuan-images-parallel/manifest.snapshot.json`。

## 当前启用状态

用户已确认全部 **133 张不同的图片**。这批的 **4,732 行**均以 `user_confirmed` 状态启用，不会标为考试院官方核验。四川物理类本科 B 段 3,166、提前批 90；历史类本科 B 段 1,388、提前批 88。指南、院校总表及智能问答使用同一条来源白名单。有院校所在地筛选时，只用已知地点；不限所在地时包括未标注地点的院校。15 个截断校名以院校代码展示，3 个分数超过分段表上限的行保留截图所列位次但不按精确名次分档。推荐结果的证据包含科类、原始文件名及行号。

## 首次入库的历史验收（启用前）

本次来源编号为 `82`。审计表实际读回 **4,732 行**；写入投档表的 **4,538 行** 均为 `pending`。逐条回查院校代码、院校名称、专业组、分数、位次和来源指纹，与对应图行全部一致。同包再次执行已返回 `replay: true`，没有新增或覆盖。

| 科类 | 写入本科 B 段 | 仅归档的其他批次 | 待复核 | 审计合计 |
|---|---:|---:|---:|---:|
| 历史类 | 1,380 | 86 | 10 | 1,476 |
| 物理类 | 3,158 | 90 | 8 | 3,256 |

18 条待复核包含 15 条校名在原图中被截断以及 3 条分数超过现有同科分段表上限；其中 2 条属于提前批。详见 [数据库逐项回读](../data/gaokao/collection_runs/2026-sichuan-images-parallel/database.readback.json) 和 [重复运行结果](../data/gaokao/collection_runs/2026-sichuan-images-parallel/replay.result.json)。

## 恢复与部署

在后端仓库根目录，使用项目虚拟环境。离线归档包含两科所有 WebP 原件、已校对的逐页 JSON、成员哈希清单；部署侧不需要 macOS Vision、OCR 模型或桌面路径。

```sh
BUNDLE=data/gaokao/user_import/sichuan_2026_image_evidence_v1.zip
python -m scripts.bundle_sichuan_image_run verify --bundle "$BUNDLE"
# 已按部署文档初始化本项目 MySQL，并导入省级一分一段种子后：
python -m scripts.bundle_sichuan_image_run import --bundle "$BUNDLE"
python -m scripts.bundle_sichuan_image_run import --bundle "$BUNDLE" --apply
# 将这一个明确获准的归档用于推荐；重跑会核对已有记录，不会再添加。
python -m scripts.enable_sichuan_image_bundle --apply
```

`verify` 不连接数据库；`import` 默认只读预演。实际写入先校验原图、行号以及 MySQL 中同年同科的官方分段，再以一个事务分批写表并回读数量。中断后重跑同一命令；同一个导入批次有唯一指纹。数据库记录包括原图行位置、原始字段、拒收或冲突原因。若已有其他来源的投档记录，不覆盖。首次导入保留原始状态；随后执行启用命令，以固定归档哈希重新核对全部物理行并启用。其他来源的 `pending` 行继续排除。

`gaokao_image_imports` 存清单与报告；`gaokao_image_evidence` 存每一不重复图片的表格行（含其他批次、冲突）；重复图片的别名保留在导入清单；启用后 `gaokao_admissions` 保存本批次的本科 B 段及提前批事实。重复运行后的报告带 `replay: true`。复核时用报告内 `bundle_sha` 限定本次数据，例如：

```sql
SELECT track, decision, COUNT(*)
FROM gaokao_image_evidence
WHERE bundle_sha = '<本次报告的 bundle_sha>'
GROUP BY track, decision;
```
