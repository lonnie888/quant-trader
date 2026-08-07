/** history.js — 交易历史页 (真实账户数据) */
let currentPage = 1;
const perPage = 20;

function color(v) { return v >= 0 ? '#0ecb81' : '#f6465d'; }
function fmt(v) { return (v >= 0 ? '+' : '') + v.toFixed(2); }

async function loadHistory() {
    const symbol = document.getElementById('filter-symbol').value.trim();
    const days = parseInt(document.getElementById('filter-days').value) || 7;
    let url = `/api/real-history?page=${currentPage}&per_page=${perPage}&days=${days}`;
    if (symbol) url += `&symbol=${encodeURIComponent(symbol)}`;

    const res = await fetch(url);
    const d = await res.json();
    const body = document.getElementById('history-body');
    const info = document.getElementById('page-info');

    if (!d.trades || !d.trades.length) {
        body.innerHTML = '<tr><td colspan="8" style="text-align:center; color:#848e9c; padding:30px;">暂无记录</td></tr>';
        info.textContent = '第 0 页，共 0 条';
        document.getElementById('prev-btn').disabled = true;
        document.getElementById('next-btn').disabled = true;
        return;
    }

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

    const totalPages = Math.ceil(d.total / perPage);
    info.textContent = `第 ${currentPage} 页 / 共 ${d.total} 条`;
    document.getElementById('prev-btn').disabled = currentPage <= 1;
    document.getElementById('next-btn').disabled = currentPage >= totalPages;
}

function prevPage() { if (currentPage > 1) { currentPage--; loadHistory(); } }
function nextPage() { currentPage++; loadHistory(); }

loadHistory();