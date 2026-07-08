# Quant Trader

币安 U 本位合约自动化量化系统：每日扫描涨幅榜 Top10，自动跑多策略回测，输出盈利最高的策略。

## 功能
- 涨幅榜扫描：币安 USDT 永续合约 24h 涨幅 Top N
- 自动回测：8 种内置策略 + 参数空间扫描
- 智能选优：盈利优先 + 稳健性约束（最大回撤、夏普、胜率）
- 模拟盘：paper trading 验证
- 报告输出：HTML/Markdown 排行榜

## 目录结构
```
quant_trader/
├── config/         # 配置
├── data/           # 数据获取、存储、加工
├── strategy/       # 策略
├── backtest/       # 回测引擎
├── selection/      # 选优
├── execution/      # 模拟盘 + 风控
├── scripts/        # 入口脚本
└── reports/        # 报告
```

## 快速开始
```bash
pip install -r requirements.txt

# 1. 编辑 config/settings.yaml，填入 API key
# 2. 更新数据并跑一次回测
python -m quant_trader.scripts.update_data
python -m quant_trader.scripts.run_backtest
```

## 评分规则
盈利优先 + 稳健型：
- 收益权重最高
- 最大回撤惩罚加倍
- 硬性约束：max_drawdown ≤ 20%，sharpe ≥ 1.0，trades ≥ 30

## 与 QuantDinger 对比
- 资产：U 本位合约（vs 股票）
- 涨幅榜：每日动态 Top10（vs 静态选股）
- 资金费率/强平：完整支持
- 自动选优：综合评分
