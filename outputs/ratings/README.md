# 额定载荷签发结果

每个 `<型号>.json` 是一次可核查的额定签发，字段含义见
`../../examples/load_rating/FIELDS.md`。仓库内的 `CLIP-A.json` 由
`examples/load_rating/generate.py` 的离线合成数据经
`load-rate-files` 生成，可用 `python -m pytest -q` 复算复现。
