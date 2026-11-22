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
python -m garmentcare report --db state.sqlite
```

## 门店“十分钟接待承诺”抽样认证

`quality-import` 仍对每条观察直接计门店评分（`quality_ok.json` 兼容，`store_grade` 不变）。认证流程另走 `certification-import`，证据顺序被强制固定：

1. **先封抽样框**：`cert_plan.json` 固定门店、考核周、忙时/闲时两层抽样框、每层最小样本数、95% 准时阈值与承诺分钟数；计划封盘后任何字段改动都被拒绝。
2. **再签抽查任务**：`cert_tasks.json` 中每个任务在签署时锁定 `promise_minutes`；任务早于封盘时间或无计划都会报错。门店事后把 10 分钟改成 15 分钟（`promise_changes`）只入 `promise_policy` 审计表，不追认旧超时。
3. **到场采独立证据**：`cert_visits.json` 每次访问同时记录排队取号终端回执、正式接待终端回执、观察员、到店留痕和关联的 `service_check_id`。无预先任务的自选记录判为 `out_of_frame`，保留证据但不进随机样本；同一取号票被后续访问复用时先判 `ticket_disputed`，绝不把一个实际访问的两个 `visit_id` 算两份。
4. **物理补证裁决**：`cert_resolution_independent.json`（设备票据链证明独立且晚 18 分钟）与 `cert_resolution_rescan.json`（票据原件证明只是重扫）是同一疑点的两条补证路线，分别导向“不通过并开整改”和“重扫不计样本”。
5. **补样与发证**：`cert_task11_visit.json` 是预先抽中的第 11 个闲时任务；重扫分支下它到场后凑齐每层 5 个独立准时样本才可发证。缺层或低于最小样本数永远是 `pending_more_samples`，不是“不通过”。
6. **对外发布与可撤销**：`cert_publish.json` 冻结对外展示内容（含内容 SHA 与接收者）。迟到终端回执 `cert_late_receipt.json` 可推翻观察时长，自动把已发证书置为 `revoked` 并开整改；补证恢复后另发新版本（`superseded`/新版本号），旧版本展示内容与接收者原样保留。

决策状态为 `certifiable` / `pending_more_samples` / `not_certified`，主结果（有效样本集合、逐层准时统计、引用的独立接待证据、证书版本链）见 `report` 的 `certifications` 段；`audit` 用原始文件在空库重放并逐字段比对。

```powershell
# 分支 A：票据链证明第 10 次为独立晚接待 → 9/10 = 90%，不通过、开整改
python -m garmentcare certification-import --db branch.sqlite --file fixtures/cert_plan.json
python -m garmentcare certification-import --db branch.sqlite --file fixtures/cert_tasks.json
python -m garmentcare certification-import --db branch.sqlite --file fixtures/cert_visits.json
python -m garmentcare certification-import --db branch.sqlite --file fixtures/cert_resolution_independent.json
# 分支 B：重扫 + 第 11 个预抽任务到场 → 10 个独立准时样本，可发证并对外发布
python -m garmentcare certification-import --db branch.sqlite --file fixtures/cert_resolution_rescan.json
python -m garmentcare certification-import --db branch.sqlite --file fixtures/cert_task11_visit.json
python -m garmentcare certification-import --db branch.sqlite --file fixtures/cert_publish.json
python -m garmentcare report --db branch.sqlite
python -m garmentcare audit --db branch.sqlite
```

三个 `*_bad.json` 夹具分别造成材质冲突、工艺越温且转店交接缺口、新增破损索赔。异内容事件 ID 不可覆盖；`file_journal` 保存原始文件及 SHA；`audit` 用所有原始文件在空库重建状态并对比结果。`decision_history` 保留重裁过程，`certification_history` 保留认证决定与证书发布/撤销链。赔偿上限以入店时的签约估值计算，初始已存在的瑕疵不被算作新增损伤。

此首版的签名和影像是离线证据标签，不包含真实身份验证、鉴定机构接入或付款。
