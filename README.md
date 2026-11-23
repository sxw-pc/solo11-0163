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

三个 `*_bad.json` 夹具分别造成材质冲突、工艺越温且转店交接缺口、新增破损索赔。异内容事件 ID 不可覆盖；`file_journal` 保存原始文件及 SHA；`audit` 用所有原始文件在空库重建状态并对比结果。`decision_history` 保留重裁过程。赔偿上限以入店时的签约估值计算，初始已存在的瑕疵不被算作新增损伤。

## 车间溶剂采录与处置

`solvent-import` 独立入账：桶号/批次与进货重量-体积换算依据（`barrels`）、逐次领用（`draws`）、机内回流（`refluxes`）、蒸馏回收（`distillations`）、密封移交（`handovers`）、危废接收回单（`waste_receipts`）、计量器校准（`calibrations`）、盘点（`stock_checks`）与处置路线授权（`routes`）。裁决按每桶（`solvent_decision`）和每次作业（`solvent_operation`）核对质量守恒，报告区分可再用、待检、在途、已移交、可解释蒸发损耗与无法解释的损耗。

- 同一回流液不可同时记作再生原料与外运危废：回收桶流出超过流入即 `conflict`。
- 危废方只回 `accepted`（已接单）不算闭环，移交量留在在途；只有 `weighed` 称重回单才记为已移交，短量计入无法解释损耗。
- 计量器校准迟到时按 `effective_from` 重算受影响计量值与可用/危废量，但已签收的密封重量与回单称重不被重写。
- 只有路线授权、没有物理称重事件时回收液保持待检，不得宣布闭环；无授权路线的物理事件不计入账。
- 每次 `clean` 作业须与领用对齐；intake 的 `allowed_process` 与受理状态决定合法干洗授权，无授权作业标记 `unauthorized`，但其真实溶剂消耗仍保留在环保账中。

```powershell
python -m garmentcare solvent-import --db state.sqlite --file fixtures/solvent_base.json
python -m garmentcare solvent-import --db state.sqlite --file fixtures/solvent_distill.json
python -m garmentcare report --db state.sqlite
python -m garmentcare audit --db state.sqlite
```

基准场景：20 L 合格溶剂、两次各领 8 L、机内回流 14 L、蒸发与布料残留 2 L、原桶余 4 L。蒸馏路线（`solvent_distill.json`）产出 12 L 可复用 + 2 L 危废残渣，账上干净溶剂 16 L；整桶外运路线（`solvent_outbound.json` + 称重回单）交割 14 L 危废，干净溶剂仅原桶 4 L。两条路线都闭合 20 L，但不可复用同一 14 L。

此首版的签名和影像是离线证据标签，不包含真实身份验证、鉴定机构接入或付款。
