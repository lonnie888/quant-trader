/** dashboard.js — 总览页 */
(async function () {
    const res = await fetch('/api/summary');
    const d = await res.json();

    function color(v) { return v >= 0 ? '#0ecb81' : '#f6465d'; }

    document.getElementById('unrealized-pnl').innerHTML = `<span style="color:${color(d.unrealized_pnl_pct)}">${d.unrealized_pnl_pct >= 0 ? '+' : ''}${d.unrealized_pnl_pct.toFixed(2)}%</span>`;
    document.getElementById('realized-pnl').innerHTML = `<span style="color:${color(d.realized_pnl_pct)}">${d.realized_pnl_pct >= 0 ? '+' : ''}${d.realized_pnl_pct.toFixed(2)}%</span>`;
    document.getElementById('win-rate').textContent = `${d.win_rate}%`;
    document.getElementById('open-positions').textContent = d.open_count;

    // 每日 PnL 图
    const dailyData = d.daily_pnl || [];
    const chartContainer = document.getElementById('chart-container');
    const chartWrapper = document.getElementById('chart-wrapper');

    if (dailyData.length <= 1) {
        // 数据不足，显示文字代替图表
        chartWrapper.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:250px;color:#848e9c;font-size:14px;">📊 等待更多交易数据后展示收益曲线</div>';
    } else {
        const ctx = document.getElementById('dailyPnlChart').getContext('2d');
        new Chart(ctx, {
            type: 'bar',
            data: {
                labels: dailyData.map(x => x.date),
                datasets: [{
                    label: '已实现 PnL %',
                    data: dailyData.map(x => +(x.realized * 100).toFixed(2)),
                    backgroundColor: dailyData.map(x => x.realized >= 0 ? 'rgba(14,203,129,0.7)' : 'rgba(246,70,93,0.7)'),
                    borderColor: '#0ecb81',
                    borderWidth: 1,
                    borderRadius: 2,
                    barPercentage: 0.3,
                    categoryPercentage: 0.5,
                    maxBarThickness: 50,
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { labels: { color: '#848e9c' } } },
                scales: {
                    x: { ticks: { color: '#848e9c' }, grid: { color: '#1e2329' } },
                    y: { ticks: { color: '#848e9c' }, grid: { color: '#1e2329' } }
                }
            }
        });
    }

    // 当前持仓表
    const posRes = await fetch('/api/positions');
    const positions = await posRes.json();
    const tbody = document.getElementById('positions-table');
    if (!positions.length) {
        tbody.innerHTML = '<p style="color:#848e9c; font-size:14px; padding:20px 0; text-align:center;">暂无持仓</p>';
        return;
    }
    let html = '<table><thead><tr><th>币种</th><th>入场</th><th>最新</th><th>PnL</th><th>剩余 K 线</th></tr></thead><tbody>';
    positions.forEach(p => {
        const c = p.pnl_pct_lev >= 0 ? '#0ecb81' : '#f6465d';
        html += `<tr style="cursor:pointer" onclick="window.location='/chart?symbol=${p.symbol}&entry_id=${p.id}'">
            <td><b>${p.symbol}</b></td>
            <td>${p.entry_price.toFixed(6)}</td>
            <td>${p.last_price.toFixed(6)}</td>
            <td style="color:${c}">${p.pnl_pct_lev >= 0 ? '+' : ''}${p.pnl_pct_lev.toFixed(2)}%</td>
            <td>${p.remaining_bars}</td>
        </tr>`;
    });
    html += '</tbody></table>';
    tbody.innerHTML = html;
})();