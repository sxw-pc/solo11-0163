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

## 跨店加工清分与实际划账

顾客只向收件店付款，加工店凭双方签署的委托取得加工费。`settlement-import` 采录第四本账：签署的跨店加工委托、顾客订单价、收单银行回单（`received`/`applied`/`bounce`）、门店间实际划账、赔付出款与退票回单、以及签署的责任协议。每笔外部资金都绑定订单、付款人和收款人；相同金额出现在另一订单上不能重复抵账。银行 `received` 回执不等于 `applied` 入账，退票回单令该笔款项回到暂挂。

```powershell
python -m garmentcare settlement-import --db state.sqlite --file fixtures/settlement_route_a.json
python -m garmentcare settlement-import --db state.sqlite --file fixtures/settlement_route_b.csv
python -m garmentcare report --db state.sqlite
python -m garmentcare audit --db state.sqlite
```

`settlement-import` 同时接受 JSON 与 CSV（按 `record_type` 分列）。报告的每个 `settlements` 条目逐单给出顾客实付/实退、收件店与加工店的应得与已到账、尚待划账、改判后的追回债权（`recovery_receivable`）、冻结责任（`frozen_share`）及暂挂票据（`held_tickets`），并以 `conservation` 校验资金守恒。

- 跨店 operation 缺少双方有效签收时，加工费不能结清；
- `returns.py` 的 `frozen` 争议只暂挂份额，永不当作已付赔款；
- 迟到索赔重裁后，已发生的顾客付款与加工交接保留为事实，新财务责任另立责任协议版本；若加工费已划账后改由加工店承担赔付，生成 B→A 追回债权，不倒写历史划账，凭对应银行出款才能结案。

夹具覆盖两条合法责任路线（A 承担：A 净留 200、B 到账 300；B 承担：A 净留 300、B 净得 200）、退票、先于付款文件到达的赔款回单、跨订单同额以及已划账后的改判追回。

此首版的签名和影像是离线证据标签，不包含真实身份验证、鉴定机构接入或付款。
