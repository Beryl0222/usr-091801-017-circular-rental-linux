# 循环租用资产履约

本项目追踪数码设备从寄租、入仓、验机、出库、交付、归还到维修和再次流转的全过程。每件实物使用不可复用的资产身份，所有权、使用权与保管责任分别记录时间区间。

验机标准具有版本，检查项保存结果和证据哈希。维修、召回、争议保全和报废都会阻断新的出库。物流扫码事件可能离线、重复或乱序。生命周期与减量数据由资产事件推导，不接受终端直接提交汇总值。

## 架构

事件溯源内核，所有状态由事件折叠而来：

- `rental/core.py` — 内容寻址事件（同一事实重复投递得到同一 id）、规范排序的只增事件存储。
- `rental/ledger.py` — 账本投影：保管区间、验机标准版本、租约状态机、差异结算单、阻断原因全部在折叠时按规则计算；非法事件记入 `violations`，折叠是全函数，任意输入确定性收敛。
- `rental/engine.py` — 命令校验与事件签发；事件插入规范顺序中间时自动整体重折叠，在线账本与任意重放一致。
- `rental/stats.py` — 生命周期与电子废弃物减量，只从事件派生。
- `rental/api.py` — HTTP 层：`POST /commands/<name>` 写入，`GET /queries/<name>` 读取。

## 核心不变量

- **实物身份**：`asset_id` 由序列号与归属人内容寻址，报废后永久占用，绝不复用。
- **权利区间互斥**：寄租人交付权利、平台保管责任、承租人使用权构成半开区间 `[start, end)` 链，任意时刻至多一条生效，边界无缝衔接。
- **验机有版本**：标准按 `effective_from` 生效；报告逐项记录结果与 64 位证据哈希，缺项拒绝。
- **差异先算后确认再结算**：归还时由规则 `diff-v1` 对比出库/归还两份验机报告计算定损（非 fail → fail 按标准计费），平台与承租人双方确认后方可结算。
- **争议阻断**：争议未解决时资产不可再租、不可调度、不可结算；历史时间点查询同样返回阻断。
- **转运不穿越**：维修、召回、证据保全（及报废）状态下，调度命令被拒绝，在途扫码不得推进物流。
- **收敛**：事件按 `(发生时间, 事件 id)` 规范排序折叠，物流回调重复或乱序到达，两次计算收敛到同一账本摘要。
- **免押快照**：资格在租约确认时刻快照，之后的政策变化只影响尚未确认的租期。
- **统计只推导**：不存在任何接受客户端上报统计值的命令；减量按公开公式折算（每完成租次 0.5 kg、每次维修延寿 2.0 kg）。

## 命令与查询

命令（`POST /commands/<name>`，冲突返回 409 与机器可读原因）：
`register_asset` `intake_asset` `scrap_asset` `publish_standard` `record_inspection` `create_rental` `confirm_rental` `cancel_rental` `outbound_rental` `deliver_rental` `return_rental` `confirm_difference` `settle_rental` `open_dispute` `resolve_dispute` `start_maintenance` `end_maintenance` `issue_recall` `lift_recall` `start_preservation` `end_preservation` `create_shipment` `record_scan` `update_waiver_policy`

查询（`GET /queries/<name>`，均支持 `at=` 时间点）：
`custody_at` `circulation_eligibility` `standard_at` `asset` `rental` `shipment` `lifecycle_stats` `ewaste_stats` `ledger_digest` `verify_replay` `events`

## 验证

- `python3 service.py --check` — 基础自检（含一次重放收敛探针）
- `python3 -m unittest service_contract test_engine test_api` 或 `npm test` — 全部契约与领域测试
- `python3 service.py --port 8000` — 启动服务（`/health` 探针）
