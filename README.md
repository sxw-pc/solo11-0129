# 切片任务采证台

工坊从本地切片桌面软件、材料库存快照和机台执行日志采录证据。切片子系统判断构建体积、喷嘴与层高；材料子系统按批次和并行预留计算可用余量；机台子系统核对排队、运行、中断、续打和完成事件；制件验收再核对实物尺寸、耗料与前置版本。SQLite 保留原始导出字节、SHA-256、业务实体和决定日志，JSON 报告可回读。

入口 `slice-import`、`material-reserve`、`machine-import`、`part-accept`、`lot-inspect` 使用 `--db ledger.db export.json`；`inventory-balance --db ledger.db --lot L1` 查询批次消耗、在途预留、物理可用量与质检后可用量。验收成功会结算实际耗料并释放未用预留；重放不重复扣减。制件必须关联已完成且绑定相同作业与预留的机台执行记录。材料批次质检独立核对线径样本和含水率；隔离会阻止后续预留与开机，迟到的隔离证据会撤回已验收制件并保留已消耗材料账。经质检负责人授权的复检可释放材料批次。

运行：`python -m pip install . --no-deps --no-build-isolation`，`python -m sliceledger demo`，`python -m sliceledger --help`，`python -m pytest -q`。所有演示和测试使用虚构本地文件，不访问网络。

增强素材提供 `sensor-import` 与 `maintenance-release`，离线采录机台传感器阈值报警和技师复机回执，使用 `--db ledger.db export.json`。可运行样例在 `tests/test_maintenance.py`。

## 离线故障候选与自适应检修试验树

机台温度 / 风扇转速 / 电流的原始报警原样保留；解释层按运行阶段、共享校准和余热语义生成 `sensor_context`。运行日志的 interrupted / resumed 决定某个高温样本是运行中异常还是停机余热。

- `diagnostic-manual-import --db ledger.db manual.json`：采录带版本的本地诊断手册（设备结构、探头位置、单故障、经事前批准的故障组合、传播时延、维修动作与受控试验表）。
- `maintenance-commission --db ledger.db commission.json`：检修前固定委托目标 `repair`（恢复维修动作）或 `root_cause`（质保精确根因）。
- `maintenance-diagnose --db ledger.db`：按当前证据重算候选集合与建议树，输出诊断修订。
- `test-execution --db ledger.db test.json`：登记受控试验的隔离步骤、所需机台状态、执行时刻与结果来源；失败试验仍计入后续热度。
- `maintenance-plans --db ledger.db --commission C1`：读出该委托下所有建议树修订（未走分支的原计划不会被覆盖）。

建议树以最坏停机分钟与试验风险排序，代价相同的树各保留一份依据。候选集合限定为手册中的单故障与已批准组合；试验结果按预设表逐条排除，不会把几个单独可行的结果拼成未批准的多故障解释。诊断与建议不构成已修复证明，也不授权复机；复机仍只经 `maintenance-release` 的技师回执。可运行样例在 `tests/test_diagnosis.py`。
