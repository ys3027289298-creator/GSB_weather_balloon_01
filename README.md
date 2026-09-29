# Weather Balloon Observation Game

标准库实现的气球观测小游戏：`weather/core.py` 为规则核心，
`weather/__main__.py` 提供 CMD 命令与固定数据演示，存档为 JSON。

## 运行

    python -m unittest -v     # 全部测试
    python -m weather demo    # 固定数据演示（输出可重复）
    python -m weather <cmd>   # 执行单条命令

## CMD 命令

- `ingest <sensor> <kind> <value> <unit> <sampled_at>` 录入读数（重复输入幂等）
- `calibrate <sensor> <scale> <offset>` 校准（失败自动恢复旧系数）
- `round` 结算当前回合（超出迟到窗口的读数丢弃并告警）
- `launch` / `cancel` 发射 / 取消（取消不消耗气囊）
- `status` 输出 JSON 状态
- `save <path>` / `load <path>` JSON 存档读写（恢复后序列号不回退）
- `demo` 运行固定演示
