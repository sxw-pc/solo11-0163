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

此首版的签名和影像是离线证据标签，不包含真实身份验证、鉴定机构接入或付款。

## 电量差额调查

电量不再按“最近两期同名账期相减”出结论，而是按物理拓扑分解到每个账期：总表增量 → 真实抄表截止时点对齐后的分表覆盖 → 校准/时钟修正后的车间用量 → 实际接在总表而不在分表下的公共负载 → 仍未解释的差额 → 责任表计/支路 → 核查工单。差额不会直接记为衣物工艺耗能或失窃。

采页只取页面上的 `site / meter_id / period / reading / reading_at / time_offset_seconds`；拓扑、时钟校验、校准证书、公共负载与钳形表试验是独立证据。原始累计读数永不改写：时钟与校准修正写在原始读数旁边，迟到的拓扑变更只改归属、不改已经采到的数字；分表被移支路后，该区间读数仍归原支路。

复现：

```powershell
# 主账期：总表 +1000，两条分表 +400/+420，表面差额 180，尚无第三支路证明
python -m garmentcare meter-import --db state.sqlite --file fixtures/meter_topology_ok.json
python -m garmentcare energy-investigate --db state.sqlite --site laundry-a --period 2026-11
python -m garmentcare audit --db state.sqlite

# 补证 A：供应商 B 表校准证书 + 时钟校对 → 车间用量 1000、差额 0、责任在 B 表
python -m garmentcare meter-import --db calib.sqlite --file fixtures/meter_calibration_b.json

# 补证 B：双签钳形表与线路图证明绕过分表的公共风机 180 度 → 责任为缺失计量支路
python -m garmentcare meter-import --db clamp.sqlite --file fixtures/meter_clamp_fan.json

# 同额双证据同时到库：冻结归因、保留双方证据、列出待复测接线段，差额不重复归零
python -m garmentcare meter-import --db conflict.sqlite --file fixtures/meter_evidence_conflict.json
```

相关夹具：`meter_misaligned.json`（错位抄表）、`meter_clock_drift.json`（时钟偏差及迟到校对）、`meter_move_late.json`（移表、含迟到归档）、`meter_reversed.json`（分表倒装）、`meter_main_reversed.json`（总表倒装）、`meter_evidence_conflict.json`（双证据冲突）。`fixtures/meter_ok.json` 的单表 200 度仍按单一计量点处理。

结论状态：`explained`（差额被物理证据闭合）、`unexplained`（尚无证据闭合，附核查工单）、`needs_evidence`（抄表区间未覆盖）、`frozen`（证据互相矛盾，禁止把差额重复归零）、`single-meter`（只有一个计量点）。
