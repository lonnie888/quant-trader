/** positions.js — 持仓页 */
(async function () {
    function strip(sym) { return sym.replace('/USDT:USDT', ''); }

    const res = await fetch('/api/positions');
    const positions = await res.json();
    const body = document.getElementById('positions-body');

    if (!positions.length) {
        body.innerHTML = '<tr><td colspan="8" style="text-align:center; color:#848e9c; padding:30px;">暂无持仓</td></tr>';
        return;
    }

    body.innerHTML = positions.map(p => {
        const c = p.pnl_pct_lev >= 0 ? '#0ecb81' : '#f6465d';
        const sign = p.pnl_pct_lev >= 0 ? '+' : '';
        return `<tr style="cursor:pointer" onclick="window.location='/chart?symbol=${p.symbol}&entry_id=${p.id}'">
            <td><b>${p.symbol}</b></td>
            <td>${p.entry_price.toFixed(6)}</td>
            <td>${p.last_price.toFixed(6)}</td>
            <td style="color:${c};font-weight:600">${sign}${p.pnl_pct_lev.toFixed(2)}%</td>
            <td style="color:#848e9c">${p.sl_price.toFixed(6)}</td>
            <td>${p.remaining_bars}</td>
            <td>${p.bars_held}</td>
            <td style="color:#848e9c;font-size:12px">${p.entry_ts.replace('T',' ').slice(0,16)}</td>
        </tr>`;
    }).join('');
})();