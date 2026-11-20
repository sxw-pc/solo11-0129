# 载荷额定证据字段说明（load rating schema）

所有文件均为本地 JSON，离线处理，不访问网络。时间戳单位毫秒（ms），力单位牛（N），
位移单位毫米（mm），温度单位摄氏度（°C）。

## 1. 试验机导出 `force_traces.json`

顶层：`version`（字符串，必填）、`rows`（试片数组）。每个试片：

| 字段 | 含义 |
| --- | --- |
| `id` | 试片唯一标识，重复采录按此合并而非新增独立样本 |
| `model` | 结构型号；额定承诺的对象是型号，不是单个试片 |
| `material_lot` | 材料批次 |
| `build_plate` | 打印板号；同板试片共享喷嘴与环境波动，只算一个独立统计单元 |
| `orientation` | 打印方向 |
| `calibration_id` | 采录时力传感器校准版本；原始采样不可变，更正校准通过校准谱系生效 |
| `load_level_n` | 该试片承受的候选载荷档位（N） |
| `cycle` | 与力采样等长的循环序号数组（未采样的循环不出现） |
| `temperature_c` | 与力采样等长的温度数组 |
| `channels.force.t_ms` / `force_n` | 力通道时钟与力值 |
| `channels.displacement.t_ms` / `disp_mm` | 位移通道时钟与位移；允许在后续导出中迟到合并 |
| `sync.force_pulse_t_ms` / `disp_pulse_t_ms` | 两个时钟各自的同步脉冲时刻（等长数组） |
| `sync.drift_ppm` | 有符号漂移率包络 `[下限, 上限]`（us/s，1 ppm = 1 us/s） |
| `sync.offset_tolerance_us` | 两时钟绝对偏移容差（us）；短 trace 上漂移率无法表达的样本错位由它约束 |
| `failure_markers.acoustic_crack_cycle` | 裂纹声学标记所在循环（独立失效路径） |
| `failure_markers.fracture_cycle` | 断裂循环 |
| `stop.reason` | `completed` / `right_censored` / `permanent_set` / `hysteresis_growth` / `acoustic_crack` / `fracture` |

`right_censored` 表示未失效前主动停机（右删失），不等于耐受了合同要求的全部循环。

## 2. 校准 `calibrations.json`

`rows` 中每条：`calibration_id`、`sensor`、`scale_correction`（力值倍率）、
`bias_n`（加性偏置，N）、`zero_band_n`（判定近零力的带宽，N）、`supersedes`
（被本版取代的旧校准 id，无则为 `null`）。不同校准版本的证据绝不混池；旧版承诺在
新版生效时撤回，撤回理由进入决定日志且旧说明可回读。

## 3. 合同 `contracts.json`

`contracts` 中每份事前固定的订单：

| 字段 | 含义 |
| --- | --- |
| `contract_id` | 合同标识 |
| `required_cycles` | 要求循环次数（如 30 或 3000） |
| `permanent_set_mm` | 永久变形失效阈值（mm） |
| `hysteresis_growth_mm` | 相对首循环回线宽度的增量阈值（mm） |
| `temperature_band_c` | 要求覆盖的温区 `[下限, 上限]`；不要求温控时为 `null` |
| `risk_limit` | 合同允许的失效风险上限（事前固定） |
| `confidence` | 单侧置信水平（默认 0.9） |

## 4. 板次 `plates.json`

`plates` 中每块板：`build_plate`、`plate_batch`（独立批次号）、`machine`、`nozzle_id`、
`ambient_note`。统计推断的独立单元是打印板，板内重复试片不冒充独立批次。

## 5. 签发结果 `outputs/ratings/<型号>.json`

- `rating_id`：`<型号>:<有效校准id>`；`status`：`已签发` / `已撤回`
- `basis.contracts.<合同>.certified_load_n`：该合同可印在制件说明里的最大额定载荷，
  证据不足为 `null`
- `basis.contracts.<合同>.levels[]`：每档候选载荷的独立板数、板级结论、失效板数、
  非通过板数、Clopper-Pearson 单侧失效风险上界、是否满足合同上限
- `basis.contracts.<合同>.follow_up_tests[]`：证据不足档位的最少补测——新增独立板数、
  跑到的循环数、温区、需重新校准的板号；右删失板必须跑到合同循环数
- `basis.statistical_method`：统计方法、独立单元、删失处理与各合同置信度
- `basis.withdrawals`：被撤回的旧承诺及其理由
- `basis.sources`：合同与板次文件的 SHA-256，出处可核查

## 判定规则要点

1. 枚举所有与同步脉冲、漂移包络和偏移容差相容的力位移对齐（整数样本偏移 k）。
2. 在每条相容路径上按合同失效定义辨认首次失效循环；相邻采样差一个点就改变弹性恢复/
   残余变形结论、首次失效循环或失效模式时，试片列 `待校准`，不挑选对承诺有利的对齐。
3. 板级取最保守结论；右删失板计为非通过，温区未覆盖板不参与该合同统计。
4. 仅当某档位在所有相容对齐下的板级风险上界都低于合同风险上限时才签发；取满足条件的
   最大连续档位。
5. 迟到的位移通道或更正校准重新解释受影响试片的首次失效时间与额定档位；原始采样、
   同步范围、板号、删失原因和每次签发依据均保留在 SQLite 台账中。
