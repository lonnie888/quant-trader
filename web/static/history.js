/** history.js — 交易历史页 */
let currentPage = 1;
const perPage = 20;

const exitReasons = { stop_loss: '止损', take_profit: '止盈', time: '时间到期', manual: '手动' };

function color(v) { return v >= 0 ? '#0ecb81' : '#f6465d'; }
function sign(v) { return v >= 0 ? '+' : ''; }

async function loadHistory() {
    const symbol = document.getElementById('filter-symbol').value.trim();
    const reason = document.getElementById('filter-reason').value;
    let url = `/api/history?page=${currentPage}&per_page=${perPage}`;
    if (symbol) url += `&symbol=${encodeURIComponent(symbol)}`;
    if (reason) url += `&reason=${encodeURIComponent(reason)}`;

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

    body.innerHTML = d.trades.map(t => {
        const c = color(t.pnl_pct_lev);
        const s = sign(t.pnl_pct_lev);
        const reason = exitReasons[t.exit_reason] || t.exit_reason;
        return `<tr>
            <td><b>${t.symbol}</b></td>
            <td>${t.entry_price.toFixed(6)}</td>
            <td>${t.exit_price.toFixed(6)}</td>
            <td>${reason}</td>
            <td style="color:${c};font-weight:600">${s}${t.pnl_pct_lev.toFixed(2)}%</td>
            <td>${t.bars_in_trade}</td>
            <td style="color:#848e9c;font-size:12px">${t.max_fav != null ? t.max_fav.toFixed(2)+'%' : '—'}</td>
            <td style="color:#848e9c;font-size:12px">${t.max_adv != null ? t.max_adv.toFixed(2)+'%' : '—'}</td>
            <td style="color:#848e9c;font-size:12px">${t.entry_ts.replace('T',' ').slice(0,16)}</td>
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