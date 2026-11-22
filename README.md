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

## 尺寸返工与布料占用账本

`fit.py` 的量尺采录（current_hem/target_hem/allowance/signer）与 `alteration.py` 的旧简单可行性（`delta<=allowance` 即 feasible，否则 needs_material）保持原样；`rework.py` 在两端之间补上裁片布局、下剪/缝合/复量、余布与成本依据。`fixtures/rework/` 提供两条路线的可运行账本：

```powershell
python -m garmentcare fit-import --db state.sqlite --file fixtures/rework/rw_fit.json
python -m garmentcare rework-import --db state.sqlite --file fixtures/rework/rw_baseline.json
python -m garmentcare rework-import --db state.sqlite --file fixtures/rework/rw_fabric.json
python -m garmentcare rework-import --db state.sqlite --file fixtures/rework/rw_route1.json
python -m garmentcare rework-import --db state.sqlite --file fixtures/rework/rw_route2.json
python -m garmentcare rework-import --db state.sqlite --file fixtures/rework/rw_remeasure_r2.json
python -m garmentcare audit --db state.sqlite
```

固定外套（现长 90、目标 94±0.5、折边总宽 5）：

- **窄锁边拆边重做（narrow_let_out）**：客户签署窄锁边工艺（只留 1），释放 4cm 不耗新布，成品边型为 1cm 窄锁边，仅计人工（`nr_*.json`）。
- **同纹镶边（pattern_band）**：保留常规 2cm 折边，从带条纹周期（10cm）的同批次布卷按经纬向与花纹周期裁出镶边，计布料+人工，成品边型为常规锁边加镶边（`rw_*.json`）。

关键业务规则（均有测试覆盖）：

- 裁片合法性按矩形几何判定：幅宽/长度边界、经纬向、`y % pattern_period == 0` 的花纹对齐、瑕疵禁切区、既有不可逆下剪与已预留布局；总面积足够不代表放得下（bolt-B 有 360 平方厘米却 `exhausted`，对应 `waiting_material`）。
- 同一块余布不能分给两件返工：压在既有下剪上的布局判无效，下剪回执被拒且库存不变。
- 下剪即不可逆物料事实：迟到的正确量尺只重算达标判定，已耗面积与成本不回滚、不还回完整卷。
- 复量必须双签且晚于实际缝合；早于缝合的记录标 `valid=0`，不算验收；一件衣物不能借用另一件的复量。
- 迟到洗后缩水（`late_shrinkage`）区分加工前基线与返工后尺寸，93cm 对 94±0.5 判 `rejected_shrinkage`，但保留全部已下剪占用。
- `fabric_ledger` 由下剪回执重算真实消耗/可用余量；`rework_cost_line` 给出每条费用的面积、坐标与单价依据；`cut_open=1` 表示已下剪未完工的在制责任。
- `audit` 先以原始文件在空库重放比对，再由 `rework.audit_recompute` 从量尺、裁片与库存原始记录独立重算布料台账与返工决策，篡改派生表会被检出。

此首版的签名和影像是离线证据标签，不包含真实身份验证、鉴定机构接入或付款。
