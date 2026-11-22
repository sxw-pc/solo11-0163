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

## 线下到线上获客归因（证据驱动）

一次 `service_booking` 只是预约，不等于客户已被获取。真实首次转化需要两条同时成立：客户签署的真实入店（`store_visit`，且其衣物在入店账本达到 `accepted`）与闭合证据——门店/客户/介绍人多签的纸券核销单（`qr_coupon_redeem`），或客户在可信登录会话后本人签署的活动承诺（`login_commitment`）。同一客户多件衣物的多次预约只计一次首次转化，其余记 `repeat_visit`；签署入店被退回/取消（`status=cancelled`）或入店被 blocked，都会移出本轮有效转化，但触点与券事实保留在账本上。

共享家庭手机通过 `shared_device_declaration` 声明，机上无法归属到具体个人的扫码记为 `shared_device_noise`，不参与获客计数；`identity_link` 只做访客到签署客户的连接，介绍人与被介绍人即使共用设备也保持为两个客户。撤回 cookie 同意（`consent_withdrawal`）之后的 web/login 触点标记 `withdrawn` 不可用于归因，但不抹掉此前明确签署的线下纸券。不同活动的同号券以 `(券号, 活动, 发行门店)` 区分；客户转发的截图二次核销被判 invalid，不能冒充被介绍人的回执。

若同一首次入店同时到达互相矛盾的闭合证据（纸券指向 A、登录承诺指向 B），不把同一次入店算给两个活动：客户结论记 `disputed` 并在 reason 中列出待复核的纸券与账号，活动获客数均不增加。无任何签署证据的旧 `campaign_ok.json` 路径保留为 `legacy_provisional`，沿用 last-touch 猜测但 `customer` 为空，不追溯宣称已获真实客户。

派生结果（`touch_classification`、`acquisition_decision`、`customer_acquisition`、各证据的 state/reason）全部由原始事件纯函数重算，`audit` 在空库重放同一批文件必须得到同一份报表。

```powershell
python -m garmentcare garment-import --db state.sqlite --file fixtures/attribution_garments.json
python -m garmentcare campaign-import --db state.sqlite --file fixtures/attribution_paper.json
python -m garmentcare campaign-import --db state.sqlite --file fixtures/attribution_login.json
python -m garmentcare campaign-import --db state.sqlite --file fixtures/attribution_dispute.json
python -m garmentcare campaign-import --db state.sqlite --file fixtures/attribution_consent.json
python -m garmentcare campaign-import --db state.sqlite --file fixtures/attribution_fraud.json
python -m garmentcare report --db state.sqlite
python -m garmentcare audit --db state.sqlite
```

`attribution_paper.json`（纸券 A 闭合，下午 B 扫码仅为共享设备噪声）、`attribution_login.json`（可信登录承诺闭合 B，A 券未核销留待核）、`attribution_dispute.json`（双证据矛盾，标记争议）是同一预约与触点出发的两条闭合路线与争议路线；`attribution_consent.json` 覆盖同意撤回，`attribution_fraud.json` 覆盖撞号、截图重复核销与无效转介绍，`attribution_cancel.json` 在测试中改为 cancelled 验证取消入店。报表中的 `campaign_totals.acquired_by_campaign` 即可复算的每个活动真实获客数。
