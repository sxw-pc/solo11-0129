# 切片任务采证台

试片载荷采录与额定签发：`force-import` 收本地试验机导出，保留力/位移双时钟原始采样、
同步脉冲与漂移包络、温度、循环计数、打印板号和停机（含右删失）原因；`calibration-import`
收力传感器校准及其更正谱系；`load-rate-files` 按事前固定的合同在每条相容力位移对齐上
辨认首次失效循环，以独立打印板为统计单元计算 Clopper-Pearson 单侧失效风险上界，把每次
签发的可核查依据写入 `outputs/ratings/<型号>.json`。旧版 `force_n` 峰值-only 导出仍按
原路径采录（`peak_force_n`），但峰值本身不参与额定承诺。

额定承诺是对一款打印结构的载荷承诺，不是给单个试片贴合格标签。右删失（未失效前主动
停机）不等于耐受了合同要求的全部循环；同板重复试片共享喷嘴与环境波动，不冒充独立批次；
相邻采样差一个点就改变弹性恢复/残余变形结论时试片列待校准，不挑选对承诺有利的对齐；
迟到位移通道或更正校准会重新解释受影响试片，旧承诺带理由撤回且可回读，不同校准版本
绝不混池。两份事前固定的订单（30 次常温一次性安装、3000 次高温反复开合）即使共用同一
组试片也分别签发，证据不足的档位给出最少补测板数与目标循环，而不是对最大峰值打折。

字段说明见 `examples/load_rating/FIELDS.md`，离线合成数据生成器见
`examples/load_rating/generate.py`（12 个试片、6 块独立板、双时钟、两种合同、校准记录，
每试片不超过 50 个采样点）。

工坊从本地切片桌面软件、材料库存快照和机台执行日志采录证据。切片子系统判断构建体积、喷嘴与层高；材料子系统按批次和并行预留计算可用余量；机台子系统核对排队、运行、中断、续打和完成事件；制件验收再核对实物尺寸、耗料与前置版本。SQLite 保留原始导出字节、SHA-256、业务实体和决定日志，JSON 报告可回读。

入口 `slice-import`、`material-reserve`、`part-accept`、`machine-import`、`lot-inspect`、
`force-import`、`calibration-import` 使用 `--db ledger.db export.json`；
`load-rate-files --db ledger.db --model <型号> --contracts 合同.json --plates 板次.json
[--calibration-id 校准版] [--out-dir outputs/ratings]` 签发或重算额定；
`inventory-balance --db ledger.db --lot L1` 查询批次消耗、在途预留、物理可用量与质检后可用量。验收成功会结算实际耗料并释放未用预留；重放不重复扣减、不重复计入独立样本。制件必须关联已完成且绑定相同作业与预留的机台执行记录。材料批次质检独立核对线径样本和含水率；隔离会阻止后续预留与开机，迟到的隔离证据会撤回已验收制件并保留已消耗材料账。经质检负责人授权的复检可释放材料批次。

运行：`python -m pip install . --no-deps --no-build-isolation`，`python -m sliceledger demo`，`python -m sliceledger --help`，`python -m pytest -q`。所有演示和测试使用虚构本地文件，不访问网络。
