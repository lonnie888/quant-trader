# Pump Pullback 策略完整规格说明

> 平台无关的策略实现文档 — 适用于 TradingView Pine / QuantDinger / backtrader / 自研回测
> 本文档是策略的单一事实来源（single source of truth）

---

## 1. 策略概述

**策略名：** Pump Pullback（泵后回踩入场）

**适用市场：** Binance USDT 永续（USDT-perp），15m K线。

**策略逻辑一句话：**
> 币价经历一段"泵"（快速拉升 ≥ 阈值）后回撤到一定区间，若回调缩量、再遇二次放量且价格站上 EMA，则入场做多，等待第二波拉升。

**K线周期：** 15m（`15m`）

**方向：** 仅做多（long_only）

**持仓方式：** 每仓固定比例权益（默认 10%），5x 杠杆，复利。

---

## 2. 参数表

### 2.1 生产配置（当前生效值 `config/strategies.yaml`）

| 参数 | 符号 | 默认值 | 说明 |
|:----|:----|:----:|:----|
| Pump 检测窗口 | `pump_window` | **12** | 泵检测回看 K 线数 |
| 泵阈值 | `pump_threshold` | **0.13** | 窗口内最高价相对窗口起点收盘价涨幅 ≥ 13% |
| 回撤下限 | `pullback_min` | **0.05** | 当前价距泵高点的回撤 ≥ 5% |
| 回撤上限 | `pullback_max` | **0.30** | 当前价距泵高点的回撤 ≤ 30% |
| 缩量系数 | `vol_shrink` | **0.80** | 回调均量 ≤ 泵窗口最大量 × 0.8 |
| 放量系数 | `vol_recover` | **1.00** | 二次均量 ≥ 回调均量 × 1.0（=1.0 时跳过） |
| EMA 触发溢价 | `trigger_pct` | **0.00** | close ≥ EMA×(1+trigger_pct)（=0 时跳过，仅要求 close>EMA） |
| EMA 周期 | `ema_period` | **12** | EMA(close, 12) |
| 冷却期 | `cooldown` | **12** | 上次平仓后 ≥ 12 根才允许再入场 |
| 最大持仓 | `hold_bars` | **48** | 持仓最多 48 根 K 线（12h）后超时平仓 |
| 止损 | `stop_loss_pct` | **0.12** | 入场价 × (1−0.12) 触发止损 |
| 止盈 | `take_profit_pct` | **0.25** | 入场价 × (1+0.25) 触发止盈 |
| 泵有效期 | `pump_lookback` | **96** | 泵信号最多保留 96 根（24h） |
| 仓位比例 | `target_pct` | **0.10** | 每次开仓使用权益的 10% 保证金 |

### 2.2 代码内建默认（`pump_pullback.py` fallback）

| 参数 | 值 |
|:----|:----:|
| `pump_window` | 8 |
| `pump_threshold` | 0.15 |
| `pullback_min` / `pullback_max` | 0.10 / 0.55 |
| `vol_shrink` / `vol_recover` | 0.80 / 1.2 |
| `trigger_pct` | 0.005 |
| `ema_period` | 9 |
| `pump_lookback` | 96 |
| `cooldown` | 16 |
| `hold_bars` | 24 |
| `stop_loss_pct` | 0.10 |
| `take_profit_pct` | 0.0（不设止盈） |

> ⚠️ 移植到其他平台时请**使用 2.1 生产配置值**（实盘验证过的参数）。

---

## 3. 状态机

策略维护一个隐式状态机，只有两个状态：

```
┌──────────────┐    entry     ┌──────────────┐
│  状态 0: 空仓  │ ──────────→ │  状态 1: 持仓  │
│ (cur = 0)     │             │ (cur = 1)     │
│              │ ←────────── │              │
└──────────────┘   exit      └──────────────┘
  (SL/TP/time/冷却中)           (S>0)
```

**状态变量：**

| 变量 | 类型 | 含义 |
|:----|:----|:----|
| `cur` | int | 0=空仓, 1=持仓 |
| `held` | int | 剩余持仓 K 线数（初始=hold_bars，每根−1） |
| `bars_since_exit` | int | 距上次平仓经过的 K 线数（初始=cooldown） |
| `pump_high` | float | 当前泵的最高价 |
| `pump_bar_idx` | int | 泵高点所在 bar 的索引（全局递增） |
| `pump_vol` | float | 泵窗口内最大成交量 |
| `entry_price` | float | 入场价 |

---

## 4. 入场条件（必须全部满足）

每个 K 线收盘时评估（空仓状态下）：

### 条件 1：泵检测

```
如果 i >= pump_window：
    win_high   = max(high[i - pump_window + 1 .. i])      # 窗口内最高价
    base_close = close[i - pump_window + 1]               # 窗口起点收盘价
    pump_pct   = win_high / base_close - 1.0

    如果 base_close > 0 且 pump_pct >= pump_threshold：
        # 找到窗口内最高价的位置
        local_idx = (i - pump_window + 1) + argmax(high[i - pump_window + 1 .. i])

        # 只更新"更晚出现"的泵高点（防止旧泵被覆盖）
        如果 local_idx > pump_bar_idx：
            pump_high   = win_high
            pump_bar_idx = local_idx
            pump_vol     = max(volume[i - pump_window + 1 .. i])
```

### 条件 2：泵信号有效期内

```
pump_active = (pump_bar_idx >= 0) 且 (i - pump_bar_idx) <= pump_lookback
```

### 条件 3：回撤幅度在区间内

```
retr = 1.0 - close[i] / pump_high        # 回撤比例（0=最高点，0.1=回调10%）

条件：pullback_min <= retr <= pullback_max
```

### 条件 4：缩量回调

```
recent_vol = mean(volume[i-3 .. i])      # 最近 4 根均量

条件：recent_vol <= vol_shrink × pump_vol
```

### 条件 5：EMA 趋势确认

```
ema = EMA(close, ema_period)

条件：trigger_pct <= 0  OR  close[i] >= ema[i] × (1 + trigger_pct)
（生产配置 trigger_pct=0，等价于 close[i] >= ema[i]）
```

### 条件 6：二次放量

```
short_vol = mean(volume[i-6 .. i])       # 最近 7 根均量

条件：vol_recover <= 1.0  OR  short_vol >= vol_recover × recent_vol
（生产配置 vol_recover=1.0，此条件跳过）
```

### 条件 7：冷却期

```
条件：bars_since_exit >= cooldown
```

### ✅ 入场执行

```
当 条件1~7 全部满足 且 当前空仓：
    entry_price = close[i]
    held        = hold_bars
    bars_since_exit = 0
    开多：用权益的 target_pct × 杠杆 开仓（5x，10% 保证金）
```

---

## 5. 出场条件（持仓状态下，按优先级）

### 5.1 止损（最高优先级 — 先检查）

```
如果 low[i] <= entry_price × (1 - stop_loss_pct)：
    在 SL 价格平仓
    bars_since_exit = 0
```

### 5.2 止盈（次优先级）

```
如果 take_profit_pct > 0 且 high[i] >= entry_price × (1 + take_profit_pct)：
    在 TP 价格平仓
    bars_since_exit = 0
```

### 5.3 超时平仓

```
held -= 1
如果 held <= 0：
    按当前 K 线收盘价平仓
    bars_since_exit = 0
```

**⚠️ 关键细节：同根 K 线内如果 low 触及 SL 且 high 触及 TP，按 SL 处理（保守）。**

---

## 6. 资金管理

| 项目 | 值 |
|:----|:----|
| 单仓保证金 | 权益 × `target_pct`（10%） |
| 杠杆 | 5x |
| 单仓名义价值 | 权益 × 10% × 5 = 权益 × 50% |
| 手续费 | 0.04% |
| 滑点 | 0.05%（QuantDinger 默认） |
| 收益计算 | 复利（下一仓基于当前权益） |

**单笔盈亏（杠杆后）：**

```
pnl_pct = (exit_price - entry_price) / entry_price        # 未杠杆
pnl_lev = pnl_pct × 5                                     # 杠杆后
pnl_usdt = equity × 0.10 × pnl_lev                        # 保证金贡献
```

**典型结果（QuantDinger 验证，HEMI/USDT 2025-09~12）：**
- 胜率 ~41%，收益 +17.5%，最大回撤 -24%，夏普 1.16
- **关键：在标的 -89% 的暴跌中仍盈利** → 止损纪律有效

---

## 7. 平台无关伪代码

```python
# 每根 15m K 线收盘时调用（已 warmup）
def on_bar(i, OHLCV):
    close, high, low, vol = OHLCV

    # ── 泵检测（仅在空仓时）──
    if cur == 0 and i >= pump_window:
        win_high = max(high[i-pump_window+1 : i+1])
        base     = close[i-pump_window+1]
        if base > 0 and (win_high/base - 1) >= pump_threshold:
            local_idx = i - pump_window + 1 + argmax(high[i-pump_window+1 : i+1])
            if local_idx > pump_bar_idx:
                pump_high, pump_bar_idx, pump_vol = win_high, local_idx, max(vol[i-pump_window+1:i+1])

    # ── 持仓管理 ──
    if cur == 1:
        if low[i] <= entry_price * (1 - SL):   order_close(reason="SL");  cur=0; bars_since_exit=0; return
        if TP > 0 and high[i] >= entry_price * (1 + TP): order_close(reason="TP"); cur=0; bars_since_exit=0; return
        held -= 1
        if held <= 0:  order_close(reason="time");  cur=0; bars_since_exit=0; return
        return

    # ── 入场评估（空仓）──
    bars_since_exit += 1
    if not (pump_bar_idx >= 0 and i - pump_bar_idx <= pump_lookback):  return
    retr = 1 - close[i]/pump_high
    if not (pullback_min <= retr <= pullback_max):  return
    recent_vol = mean(vol[i-3:i+1])
    if not (recent_vol <= vol_shrink * pump_vol):   return
    if trigger_pct > 0 and close[i] < ema[i]*(1+trigger_pct):  return
    short_vol = mean(vol[i-6:i+1])
    if vol_recover > 1.0 and short_vol < vol_recover * recent_vol:  return
    if bars_since_exit < cooldown:  return

    # ✅ 开仓
    order_open(size=equity * 0.10, leverage=5, reason="pump_pullback")
    entry_price = close[i]; held = hold_bars; bars_since_exit = 0
```

---

## 8. 边界情况与注意事项

1. **warmup 要求**：至少需要 `pump_window + pump_lookback + ema_period` 根历史（建议 120+ 根）才能稳定计算。
2. **泵高点只在空仓时更新**：持仓期间不更新 `pump_high`，避免持仓中被新泵干扰。
3. **泵窗口起点基准**：`base_close` 是窗口**起点**的收盘价，不是窗口前一根——这是与旧版策略（high/low 比例）的关键区别。
4. **SL 优先于 TP**：同根 K 线内两者都触发时按 SL 计（保守，避免把止损当止盈）。
5. **冷却期**：从**平仓**那根开始计，不是入场。
6. **`vol_recover ≤ 1.0` 和 `trigger_pct ≤ 0` 是"跳过检查"**，不是"必过"——移植时注意条件方向。
7. **复利**：每仓基于当时权益，权益增长后仓位自动变大（风险也随之放大）。

---

## 9. 各平台移植要点

### TradingView (Pine v6)
- 已提供 `scripts/pump_pullback_tradingview.pine`
- 用 `strategy.position_size` 判断持仓状态
- SL/TP 用两个独立 `strategy.exit()`（`calc_on_every_tick=true` 柱内触发）

### QuantDinger (Strategy API V2)
- 已提供并通过验证（HEMI/USDT 回测成功）
- `handle_data` 内**不能 import pandas/numpy** → 用纯 Python 循环实现 EMA/max/argmax
- 用 `order_target_percent()` 下单，`get_position()` 读持仓

### backtrader / backtesting.py
- 用 `self.position` 判断持仓（backtesting.py 默认单仓）
- 注意 backtesting.py 的 `next()` 是每根 K 线调用，用 `self.data.high/low/close` 序列

---

*文档版本：v1.0（2026-08-26），与 `quant_trader/quant_trader/strategy/library/pump_pullback.py` 及 `config/strategies.yaml` 对齐*