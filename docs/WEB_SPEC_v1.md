# Quant Trader Web Dashboard — 技术规格书 v1.0

## 1. 技术栈

| 层 | 选型 | 理由 |
|---|---|---|
| 后端 | **Flask** (Python) | 和量化项目同一语言，直接读取账本/数据 |
| 实时推送 | **SSE (Server-Sent Events)** | 比 WebSocket 简单，单向数据推送够用，浏览器原生支持 |
| 前端 | **纯 HTML + Tailwind CDN + Chart.js CDN** | 无构建步骤，零依赖，直接 Flask 返回 |
| K线图 | **Lightweight Charts** (TradingView 库) | 专业的K线渲染、标记、时间轴 |

## 2. 页面规划

### 页面1: 总览仪表盘 `/`
- 四张卡片：未实现PnL / 已实现PnL / 胜率 / 持仓数
- 累计收益曲线图（按天，Chart.js 折线图）
- 当前持仓简表（最近5条）
- SSE 每15秒自动刷新数字

### 页面2: 持仓详情 `/positions`
- 全部当前持仓表格：币种 / 入场价 / 当前价 / PnL(杠杆) / 止损价 / 剩余bar / 入场时间
- 绿色/红色标记浮盈浮亏
- 点击一行 → 跳转K线图页
- SSE 每15秒自动刷新

### 页面3: 交易历史 `/history`
- 已平仓交易表格：币种 / 入场价 / 退出价 / 退出原因 / PnL(杠杆) / 持仓时长
- 按天分组
- 筛选器：按币种/按退出原因
- 分页

### 页面4: K线图 `/chart?symbol=TLM&entry_id=4`
- TradingView Lightweight Charts 显示15m K线
- 入场标记（绿色三角）
- 止损线（红色虚线）
- 止盈线（绿色虚线，如有）
- 退出标记（红色X）
- 鼠标悬停显示价格和成交量

## 3. API 接口

### `GET /api/summary`
返回聚合统计。
```json
{
  "unrealized_pnl_pct": 42.97,
  "realized_pnl_pct": -7.59,
  "open_count": 2,
  "closed_count": 2,
  "wins": 2,
  "total_trades": 4,
  "win_rate": 50.0,
  "daily_pnl": [
    {"date": "2026-07-04", "realized": -7.59, "unrealized": 42.97}
  ]
}
```

### `GET /api/positions`
返回当前所有持仓。
```json
[
  {
    "id": 4,
    "symbol": "TLM",
    "entry_price": 0.002419,
    "last_price": 0.002694,
    "pnl_pct_lev": 34.11,
    "sl_price": 0.002177,
    "remaining_bars": 18,
    "entry_ts": "2026-07-04T04:15:00+00:00",
    "bars_held": 6,
    "max_fav": 19.80,
    "max_adv": -3.56
  }
]
```

### `GET /api/history?page=1&per_page=20&symbol=&reason=`
返回已平仓交易。
```json
{
  "trades": [
    {
      "id": 3,
      "symbol": "ARPA",
      "entry_price": 0.01044,
      "exit_price": 0.01122,
      "exit_reason": "time",
      "pnl_pct_lev": 22.41,
      "entry_ts": "...",
      "exit_ts": "...",
      "bars_in_trade": 24,
      "max_fav": 5.36,
      "max_adv": -5.36
    }
  ],
  "total": 2,
  "page": 1,
  "per_page": 20
}
```

### `GET /api/klines/<symbol>?since=<entry_ts>&bars=48`
返回15m K线数据。
```json
{
  "symbol": "TLM",
  "klines": [
    {"t": 1710000000000, "o": 0.0024, "h": 0.0025, "l": 0.0023, "c": 0.0024, "v": 123456}
  ],
  "markers": [
    {"time": 1710000000000, "position": "aboveBar", "color": "green", "shape": "arrowUp", "text": "Entry @ 0.002419"},
    {"time": 1710050000000, "position": "belowBar", "color": "red", "shape": "arrowDown", "text": "SL @ 0.002177"}
  ]
}
```

### `GET /api/events` (SSE)
Server-Sent Events 端点，每15秒推送一次：
```
event: positions
data: { ... positions数据 ... }

event: summary  
data: { ... 汇总数据 ... }
```

## 4. 目录结构

```
quant_trader/
├── web/
│   ├── __init__.py          # Flask app 工厂
│   ├── app.py               # 启动入口: python web/app.py
│   ├── api.py               # REST API 路由
│   ├── sse.py               # SSE 推送
│   ├── static/
│   │   ├── dashboard.js     # 总览页 JS
│   │   ├── positions.js     # 持仓页 JS
│   │   ├── history.js       # 历史页 JS
│   │   └── chart.js         # K线图 JS
│   └── templates/
│       ├── layout.html      # 基础布局（导航栏）
│       ├── dashboard.html   # 总览
│       ├── positions.html   # 持仓
│       ├── history.html     # 历史
│       └── chart.html       # K线图
├── reports/
│   ├── paper/positions.jsonl  # ← API 读取
│   └── paper/recap-*.md
```

## 5. 启动方式

```bash
cd /vol1/1000/quant_trader
source .venv/bin/activate
pip install flask
python web/app.py  # 监听 0.0.0.0:5050
```

浏览器访问 `http://飞牛IP:5050`

## 6. 视觉风格（币安深色主题）

- **背景**: `#0b0e11`（深色）
- **卡片**: `#1e2329`（稍浅）
- **文字**: `#eaecef`（浅灰）, `#848e9c`（次要）
- **绿色**: `#0ecb81`（涨/盈利）
- **红色**: `#f6465d`（跌/亏损）
- **黄色**: `#f0b90b`（BNB色/强调）
- **边框**: `#2b3139`

导航栏顶栏固定，左对齐 logo "Quant Trader"，右对齐 总览 / 持仓 / 历史 链接。

## 7. 端口 & 启动

默认 5050。如果端口被占用，自动尝试 5051, 5052 ... 直到找到可用端口。

## 8. 实现顺序

1. Flask app 骨架 + 导航布局 (layout.html)
2. REST API: /api/summary, /api/positions, /api/history, /api/klines
3. 总览仪表盘页 (dashboard.html + Chart.js)
4. 持仓页 (positions.html)
5. 历史页 (history.html)
6. K线图页 (chart.html + Lightweight Charts)
7. SSE 实时推送 (/api/events)