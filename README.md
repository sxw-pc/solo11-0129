# 切片任务采证台

工坊从本地切片桌面软件、材料库存快照和机台执行日志采录证据。切片子系统判断构建体积、喷嘴与层高；材料子系统按批次和并行预留计算可用余量；机台子系统核对排队、运行、中断、续打和完成事件；制件验收再核对实物尺寸、耗料与前置版本。SQLite 保留原始导出字节、SHA-256、业务实体和决定日志，JSON 报告可回读。

入口 `slice-import`、`material-reserve`、`machine-import`、`part-accept` 使用 `--db ledger.db export.json`；`inventory-balance --db ledger.db --lot L1` 查询批次消耗、在途预留和实际可用量。验收成功会结算实际耗料并释放未用预留；重放不重复扣减，中断或事件乱序的机台运行不能证明制件完成。

运行：`python -m pip install . --no-deps --no-build-isolation`，`python -m sliceledger demo`，`python -m sliceledger --help`，`python -m pytest -q`。所有演示和测试使用虚构本地文件，不访问网络。
