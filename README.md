# 服装洗护赔付采录台

本地离线的衣物入店证据、工艺履行与返件索赔工作台。输入均为合成 JSON，不访问真实门店或支付服务。三个独立账本分别产生衣物可接收、工艺是否合规及赔偿是否成立的结论；材质的迟到冲突会重算后两账，工艺偏离会改变新损伤的赔付结论。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m garmentcare demo --db demo.sqlite
python -m garmentcare audit --db demo.sqlite
python -m unittest discover -s tests -v
```

可独立采录：

```powershell
python -m garmentcare garment-import --db state.sqlite --file fixtures/garment_ok.json
python -m garmentcare process-import --db state.sqlite --file fixtures/process_ok.json
python -m garmentcare return-import --db state.sqlite --file fixtures/return_ok.json
python -m garmentcare label-import   --db state.sqlite --file fixtures/label_page_ok.json
python -m garmentcare report --db state.sqlite
```

三个 `*_bad.json` 夹具分别造成材质冲突、工艺越温且转店交接缺口、新增破损索赔。异内容事件 ID 不可覆盖；`file_journal` 保存原始文件及 SHA；`audit` 用所有原始文件在空库重建状态并对比结果。`decision_history` 保留重裁过程。赔偿上限以入店时的签约估值计算，初始已存在的瑕疵不被算作新增损伤。

## 护理页采录与两条授权路线

护理页采录（`label-import`）刻意区分四件常被“最新抓取即最新说明”混淆的事实：

| 事实 | 字段 | 说明 |
| --- | --- | --- |
| 抓取时间 | `fetched_at` | 只说明何时采到，不代表何时生效 |
| 页面声明生效时间 | `declared_effective_at` / `effective_at` | 取自页面 `effective` 元数据；缺失则记为 `effective_basis=missing` |
| 来源签署者 | `signer` | 已生效说明必须有厂家签署；无签署页只能作待核资料 |
| 原始页面摘要 | `content_sha` / `page_sha` | `content_sha` 是页面字节摘要；`page_sha` 标识“同一发布”，同字节重复抓取合并为一 |

`label_entry` 每个 SKU 保存裁决结果：`state ∈ {verified, pending, conflict, withdrawn, empty}`、`careness`、被采纳的 `source_event`、`candidates`（可用候选）、`pending_events`（待核）、`conflict_events`（矛盾）、`withdrawn_events`、`superseded_events` 与完整 `source_chain`。`label_revision` 逐步保留每次裁决快照，`catalog_source_chain` 指向最新快照。

裁决要点：

* 只有“有声明生效时间、已到 as-of、且带来源签署”的页面才能成为 `verified`；`report --as-of <时刻>` 用同一批页面与撤回记录在该业务时点重算，不写库、不以抓取时间代替。
* 同一 SKU 的不同来源发布矛盾说明时进入 `conflict`；同来源同版本内容不一致同样 `conflict`，**不取事件 ID 或最大时间**。
* 撤回记录只匹配其明确引用的发布（`target_event` / `target_content_sha` / `target_revision`，且必须同来源）。撤回 v2 后，被其取代的更旧同源版本标记为 `superseded`，**不得以重新抓取旧缓存假装 v2 仍有效**；撤回生效时间早于 as-of 才参与重裁。
* 无生效时间或未签署的页面保留在 `pending`，不能覆盖已生效且经来源确认的说明。

门店实体标签由 `label-import` 的 `labels` 数组独立签录（`source_kind=physical_label`）。衣物通过 `sku` 关联 SKU，通过 `label_route ∈ {manufacturer, physical_label}` 显式选择证据权威：

```powershell
python -m garmentcare label-route --db state.sqlite --garment coat-1 ^
  --route manufacturer --basis "owner relies on signed v2" --at 2026-10-15T10:00:00
```

两条路线使用同一组页面、实体标签与客户签名授权，仅权威选择不同，因此当前可加工决定与复核工作单不同：

* `manufacturer`：采用厂家已生效说明。若与入店 `allowed_process` 一致，现有 `recipe` 可继续；实体标签矛盾只记为 `noted`。
* `physical_label`：采用独立签录的实体标签。若标签与厂家生效说明矛盾（如 `wool/wet` 对 `wool/dry`），`garment_label_review` 给出 `conflict`/`conflict_pending`，`process_decision.new_processing=frozen` 冻结新加工资格；已执行操作与真实返件事实原样保留。
* 任一来源与客户签名 `allowed_process` 矛盾时只打开复核任务，**绝不改写 `intake_photo` 或 `garment` 中客户签名的入店事实**。
* `label_revision` 变化会经 `intake.decide → process.decide → returns.decide` 重算受影响衣物，并在 `decision_history` 记录带证据引用的重裁。

迟到的 v2 撤回通知会让两条路线都重裁：v2 从证据链中退役，旧缓存以 `superseded` 标注而不再提供 `wool/dry` 依据，`label_review` 转入复核并冻结新加工。

## 复现两路线与撤回

以下命令从空库开始，依次采录厂家 v2 页面、实体标签与客户签名授权，然后选择厂家权威路线。`fixtures/label_route_fixture.json` 与 `fixtures/label_route_physical.json` 是测试用的复合包；下面的 `route_*.json` 是同内容的 CLI 单文件形式：

```powershell
python -m garmentcare garment-import --db m.sqlite --file fixtures/route_garment.json
python -m garmentcare label-import   --db m.sqlite --file fixtures/route_manufacturer_v2.json
python -m garmentcare label-import   --db m.sqlite --file fixtures/route_physical_tag.json
python -m garmentcare process-import --db m.sqlite --file fixtures/route_recipe.json
python -m garmentcare return-import  --db m.sqlite --file fixtures/route_return.json
python -m garmentcare label-route    --db m.sqlite --garment coat-1 --route manufacturer ^
  --basis "owner relies on the signed effective manufacturer v2 instruction to continue dry cleaning" ^
  --at 2026-10-15T10:00:00
python -m garmentcare report --db m.sqlite --as-of 2026-10-15
python -m garmentcare audit  --db m.sqlite
```

改用 `--route physical_label` 且 `--basis "owner relies on the independently signed physical tag and pauses pending re-signature"` 即得到第二路：`report` 中 `label_review.task_state=conflict_pending`、`process.new_processing=frozen`，而 `process.nominal_state` 与 `compensation` 保持真实返件事实。随后导入撤回通知：

```powershell
python -m garmentcare label-import --db m.sqlite --file fixtures/label_withdrawal.json
python -m garmentcare report --db m.sqlite
python -m garmentcare audit  --db m.sqlite   # 仍为 matched
```

`tests/test_catalog.py` 用 `label_route_fixture.json`（选择厂家权威）与 `label_route_physical.json`（选择实体标签权威）两路验收同一组页面、标签与客户授权；`label_withdrawal.json` 验证迟到撤回不得复活旧缓存。矛盾来源、抓取乱序、同字节重复抓取、同 URL 改版及无生效时间页分别由 `label_contradiction.json`、`label_fetch_reorder.json`、`label_same_content_refetch.json`、`label_reissue_same_url.json`、`label_pending_no_effective.json` 覆盖。

此首版的签名和影像是离线证据标签，不包含真实身份验证、鉴定机构接入或付款。
