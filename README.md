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

## 订单级交付裁决

同一封袋可装多件衣物，但每件衣物的入店基线、工艺和交接签名分别成立。订单账本（`garmentcare/delivery.py`）把袋级签名汇总成订单结论；它不会把各件“可归还”简单相加。

订单状态：`pending`（整单待交付）、`split_available`（可分袋交付）、`partially_delivered`（已部分交付）、`delivered`（已全部交付）、`handoff_dispute`（交接争议）。`undelivered` 给出未交付衣物集合，`frozen` 给出因迟到上游冲突被冻结的新增交付资格，`disputes` 给出冲突明细。

```powershell
# 订单与成员（可先于 garment/process/return 证据导入，后到证据会重裁）
python -m garmentcare order-import --db state.sqlite --file fixtures/order_pair_bag.json
python -m garmentcare garment-import --db state.sqlite --file fixtures/order_pair_garments.json
python -m garmentcare process-import --db state.sqlite --file fixtures/order_pair_process.json
python -m garmentcare return-import --db state.sqlite --file fixtures/order_pair_authority.json

# 路线 A：分袋 —— 只把已完成的甲件分出并独立签收
python -m garmentcare split --db state.sqlite --order order-pair-1 --garment jacket-1 \
  --to-bag bag-jacket-1 --new-seal seal-jacket-1 --at 2026-11-04 --person owner-1
python -m garmentcare handoff --db state.sqlite --order order-pair-1 --bag bag-jacket-1 \
  --seal seal-jacket-1 --person picker-1 --at 2026-11-05
# => 已部分交付；甲件的索赔起算凭证是 bag-jacket-1 / 2026-11-05 的独立签收

# 路线 B：整袋 —— 补齐乙的交接缺口后对原袋一次签收（新库 whole.sqlite）
python -m garmentcare order-import --db whole.sqlite --file fixtures/order_pair_bag.json
python -m garmentcare garment-import --db whole.sqlite --file fixtures/order_pair_garments.json
python -m garmentcare process-import --db whole.sqlite --file fixtures/order_whole_route_process.json
python -m garmentcare return-import --db whole.sqlite --file fixtures/order_pair_authority.json
python -m garmentcare order-import --db whole.sqlite --file fixtures/order_whole_route.json
# => 已全部交付；两件共用 bag-pair-1 / 2026-11-05 的签收
python -m garmentcare report --db state.sqlite
python -m garmentcare audit --db state.sqlite
```

两条路线可分别用 `fixtures/order_split_route.json` 与 `fixtures/order_whole_route.json` 一次导入复现。规则要点：

- 分袋是成员关系的迁移，不是交付；`bag_membership` 保留原袋→新袋的 `from_bag`/`split_event` 血缘，便于追溯。
- 只有自身入店基线 `accepted` 且工艺 `completed` 的衣物可进入交接；不能借用同袋另一件的代领授权、封袋号或签名。
- 一个封袋作为一个整体交接；袋内任一件不合格时整袋交接不成立。
- 同件衣物出现第二个有效签收、签收人与代领授权不符、或封袋号不符时，整单进入 `handoff_dispute`，不取最新记录掩盖冲突。
- 已签收衣物遇到迟到材质冲突：保留真实签收事实（`delivery` 仍为 `delivered`），冻结其新增交付资格与赔付（`claim_frozen=1`，`compensation` 为 `frozen`），不倒写为“从未交付”。
- 订单与证据到达顺序无关，都会重裁受影响订单；`order_decision_history` 保留订单结论历史，`audit` 从原始文件重放必须一致。

此首版的签名和影像是离线证据标签，不包含真实身份验证、鉴定机构接入或付款。
