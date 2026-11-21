# 切片任务采证台

工坊从本地切片桌面软件、材料库存快照和机台执行日志采录证据。切片子系统判断构建体积、喷嘴与层高；材料子系统按批次和并行预留计算可用余量；机台子系统核对排队、运行、中断、续打和完成事件；制件验收再核对实物尺寸、耗料与前置版本。SQLite 保留原始导出字节、SHA-256、业务实体和决定日志，JSON 报告可回读。

入口 `slice-import`、`material-reserve`、`machine-import`、`part-accept`、`lot-inspect` 使用 `--db ledger.db export.json`；`inventory-balance --db ledger.db --lot L1` 查询批次消耗、在途预留、物理可用量与质检后可用量。验收成功会结算实际耗料并释放未用预留；重放不重复扣减。制件必须关联已完成且绑定相同作业与预留的机台执行记录。材料批次质检独立核对线径样本和含水率；隔离会阻止后续预留与开机，迟到的隔离证据会撤回已验收制件并保留已消耗材料账。经质检负责人授权的复检可释放材料批次。

运行：`python -m pip install . --no-deps --no-build-isolation`，`python -m sliceledger demo`，`python -m sliceledger --help`，`python -m pytest -q`。所有演示和测试使用虚构本地文件，不访问网络。

增强素材提供 `packing-plan` 和 `carrier-manifest`，从离线装箱与承运导出采录制件和运单回执。入口使用 `--db ledger.db export.json`，可运行样例见 `tests/test_packing.py` 与 `samples/`。

## 装箱与承运字段说明

`packing-plan` 的每一行是一份订单（不超过六件制件、两种箱型），合同在装箱前固定：

- `contract`：`single` 单包裹保管合同（整套制件须装入同一只 `certified` 认证箱，不允许拆箱）或 `multi` 分包裹合同（最多两只合格箱，每箱独立封箱与追踪）。
- `pack_start` / `cutoff`：装箱开始与承运截单时刻（ISO 本地时间）。方案就绪时刻为开始时刻加各箱 `handling_minutes`，赶不上截单的方案不参与选价。
- `parts[]`：`id`；`size` 外廓三边 mm；`weight` kg；`cushion` 缓冲垫厚度 mm（包络每轴加两倍厚度，缓冲垫可相切不得互相穿透）；`fragile_faces` 不可承压易损面（`+x..-z`，他件不得压在其世界顶面，本件也不得以易损底面压在他件上）；`rotations` 显式列出的允许三维旋转，`axes` 为轴置换、`up` 为朝上的制件本地面，未列出的方向不可用，使易损面承压的镜像翻面会被判非法。
- `boxes[]`：`type`；`inner` 内尺寸 mm；`max_weight` 承重 kg；`count` 实数库存（同型号箱按 `类型#序号` 编号，两个包裹不得复用同一箱号）；`certified` 是否认证大箱；`tare` 皮重 kg；`handling_minutes` 单箱作业分钟。
- `carrier`：`dim_divisor` 体积重除数（体积重 = 内尺寸 cm³ ÷ 除数）；`weight_tolerance` 现场称重允差 kg；`tiers` 价阶（每箱按实重与体积重较大者落入首个 `up_to` 不小于它的价阶，取 `price`）。拆两箱与一只大箱谁便宜取决于价阶边界，系统枚举全部可行方案后取总费用最低且能赶上截单者，不按最小空隙或总体积贪心。

可行方案存入 `packing_layout`：每箱一条（箱号、箱型、每件制件的姿态/包络尺寸/箱内坐标、毛重、可行称重区间、体积重、计费重、价阶、运费），坐标原点为箱内底角，可直接复核；另存一份 `summary`（总价、就绪时刻）。找不到布局时存 `diagnosis`，指出最小不可装组合及约束（`dimensions` 尺寸、`orientation_restriction` 方向、`fragile_face` 易损面承压、`weight_capacity` 承重、`certified_box_unavailable` 无认证箱、`box_inventory_insufficient` 箱数不足、`needs_more_than_two_boxes` 超两箱），而不是仅报体积不足；订单状态为 `待协调`。

`carrier-manifest` 的每一行是一只物理箱的交运证据：`packing` 装箱计划号、`box_no` 箱号、`tracking` 追踪号、`seal` 封箱扫描、`weighed` 现场称重 kg、`scanned_at` 扫描时刻。逐箱核对：计划必须 `已装箱`、箱号属于该计划、称重落在规划区间、扫描不晚于截单。一张回执只能交运指明的那只箱；某箱称重不符只该箱 `待核`，其余箱的 `已交运` 与已签回执保留。证据存入 `carrier_box` 后不可改：相同扫描重放为 `duplicate_scan` 不新增包裹，冲突证据判 `waybill_conflict`；签发后移动制件须新版本装箱图（新计划号）与新封箱证据。报告 `packing` 段回读每箱布局、不可装原因与每箱实际交运状态，整单 `shipped` 需全部箱 `已交运`。
