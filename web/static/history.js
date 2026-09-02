/** history.js — 交易历史页 (实盘/模拟盘) */
let currentPage = 1;
const perPage = 20;
let _mode = 'real';

function color(v) { return v >= 0 ? '#0ecb81' : '#f6465d'; }
function fmt(v) { return (v >= 0 ? '+' : '') + v.toFixed(2); }

async function _getMode() {
    try {
        const r = await fetch('/api/mode');
        const d = await r.json();
        _mode = d.mode || 'real';
    } catch (e) { _mode = 'real'; }

    // 更新标题和表头
    const title = document.getElementById('page-title');
    const head = document.getElementById('history-head');
    if (title) {
        title.textContent = _mode === 'paper' ? '交易历史（模拟盘）' : '交易历史（真实账户）';
    }
    if (head) {
        if (_mode === 'paper') {
            head.innerHTML = `<tr>
                <th>币种</th><th>入场价</th><th>退出价</th><th>平仓原因</th>
                <th>盈亏%</th><th>持仓K线</th><th>平仓时间</th><th>开仓时间</th>
            </tr>`;
        } else {
            head.innerHTML = `<tr>
                <th>币种</th><th>入场价</th><th>退出价</th><th>数量</th><th>已实现PnL</th>
                <th>手续费</th><th>资金费</th><th>净盈亏</th><th class="mobile-hide">时间</th>
            </tr>`;
        }
    }
}

async function loadHistory() {
    const symbol = document.getElementById('filter-symbol').value.trim();
    const days = parseInt(document.getElementById('filter-days').value) || 7;
    // 根据模式选择 API (paper 额外带 strategy 参数切换双策略账本)
    const endpoint = _mode === 'paper' ? '/api/history' : '/api/real-history';
    let url = `${endpoint}?page=${currentPage}&per_page=${perPage}&days=${days}`;
    if (_mode === 'paper') {
        const strat = localStorage.getItem('qt_strategy') || '';
        url += `&strategy=${strat}`;
    }
    if (symbol) url += `&symbol=${encodeURIComponent(symbol)}`;

    const res = await fetch(url);
    const d = await res.json();
    const body = document.getElementById('history-body');
    const info = document.getElementById('page-info');

    if (!d.trades || !d.trades.length) {
        body.innerHTML = '<tr><td colspan="9" style="text-align:center; color:#848e9c; padding:30px;">暂无记录</td></tr>';
        info.textContent = '第 0 页，共 0 条';
        document.getElementById('prev-btn').disabled = true;
        document.getElementById('next-btn').disabled = true;
        return;
    }

    if (_mode === 'paper') {
        // 模拟盘: 字段 pnl_pct_lev / entry_ts / exit_ts / exit_reason / bars_in_trade
        body.innerHTML = d.trades.map(t => {
            const pnl = t.pnl_pct_lev || 0;
            const c = color(pnl);
            const reasonMap = { SL: '止损', TP: '止盈', time: '超时', close: '平仓', '': '-' };
            return `<tr>
                <td><b>${t.symbol}</b></td>
                <td>${t.entry_price ? t.entry_price.toFixed(6) : '—'}</td>
                <td>${t.exit_price ? t.exit_price.toFixed(6) : '—'}</td>
                <td style="color:#848e9c;font-size:12px">${reasonMap[t.exit_reason] || t.exit_reason || '-'}</td>
                <td style="color:${c};font-weight:600">${fmt(pnl)}%</td>
                <td style="color:#848e9c;font-size:12px">${t.bars_in_trade ?? '-'}</td>
                <td style="color:#848e9c;font-size:12px">${(t.exit_ts || '').replace('T', ' ').slice(0, 16)}</td>
                <td style="color:#848e9c;font-size:12px">${(t.entry_ts || '').replace('T', ' ').slice(0, 16)}</td>
            </tr>`;
        }).join('');
    } else {
        // 实盘: 字段 realizedPnl / commission / funding / netPnl / time
        body.innerHTML = d.trades.map(t => {
            const c = color(t.netPnl);
            const entryStr = t.entry_price ? t.entry_price.toFixed(6) : '—';
            const exitStr = t.exit_price ? t.exit_price.toFixed(6) : '—';
            const qtyStr = t.qty > 0 ? t.qty : '—';
            return `<tr>
                <td><b>${t.symbol}</b></td>
                <td>${entryStr}</td>
                <td>${exitStr}</td>
                <td>${qtyStr}</td>
                <td style="color:${color(t.realizedPnl)};font-weight:600">${fmt(t.realizedPnl)} USDT</td>
                <td style="color:#848e9c;font-size:12px">${fmt(t.commission)}</td>
                <td style="color:#848e9c;font-size:12px">${fmt(t.funding)}</td>
                <td style="color:${c};font-weight:600">${fmt(t.netPnl)} USDT</td>
                <td style="color:#848e9c;font-size:12px">${t.time.slice(0, 16)}</td>
            </tr>`;
        }).join('');
    }

    const totalPages = Math.ceil(d.total / perPage);
    info.textContent = `第 ${currentPage} 页 / 共 ${d.total} 条`;
    document.getElementById('prev-btn').disabled = currentPage <= 1;
    document.getElementById('next-btn').disabled = currentPage >= totalPages;
}

function prevPage() { if (currentPage > 1) { currentPage--; loadHistory(); } }
function nextPage() { currentPage++; loadHistory(); }

(async function init() {
    await _getMode();
    loadHistory();
})();
