/** analysis.js — 数据分析页 */
const color = v => v >= 0 ? '#0ecb81' : '#f6465d';
const gold = '#f0b90b';
const gridColor = '#1e2329';
const tickColor = '#848e9c';

let charts = {};

function destroyChart(id) {
    if (charts[id]) { charts[id].destroy(); delete charts[id]; }
}

async function loadAnalysis() {
    const days = document.getElementById('filter-days').value;
    const res = await fetch(`/api/real-analysis?days=${days}`);
    const d = await res.json();
    if (d.error) { console.error(d.error); return; }

    const s = d.summary;
    const colorPnl = color(s.total_pnl);
    document.getElementById('kpi-trades').textContent = s.total_trades;
    document.getElementById('kpi-winrate').textContent = s.win_rate + '%';
    document.getElementById('kpi-pnl').innerHTML = `<span style="color:${colorPnl}">${s.total_pnl >= 0 ? '+' : ''}${s.total_pnl.toFixed(2)} USDT</span>`;
    document.getElementById('kpi-wl').textContent = `${s.wins} / ${s.losses}`;

    // 1. 盈亏分布饼图
    destroyChart('pnl-pie');
    const pieCtx = document.getElementById('chart-pnl-pie');
    if (pieCtx) {
        charts['pnl-pie'] = new Chart(pieCtx, {
            type: 'doughnut',
            data: {
                labels: ['盈利', '亏损'],
                datasets: [{
                    data: [s.wins, s.losses],
                    backgroundColor: ['#0ecb81', '#f6465d'],
                    borderColor: '#0b0e11',
                    borderWidth: 2,
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { position: 'bottom', labels: { color: '#eaecef' } },
                    tooltip: { callbacks: { label: ctx => `${ctx.label}: ${ctx.parsed} (${(ctx.parsed/s.total_trades*100).toFixed(1)}%)` } }
                }
            }
        });
    }

    // 2. PnL 金额分布柱状图
    destroyChart('pnl-dist');
    const distCtx = document.getElementById('chart-pnl-dist');
    if (distCtx) {
        const dist = d.pnl_distribution || [];
        charts['pnl-dist'] = new Chart(distCtx, {
            type: 'bar',
            data: {
                labels: dist.map(x => x.range),
                datasets: [{
                    label: '交易数',
                    data: dist.map(x => x.count),
                    backgroundColor: dist.map(x => x.range.includes('-') ? 'rgba(246,70,93,0.7)' : 'rgba(14,203,129,0.7)'),
                    borderColor: '#0b0e11',
                    borderWidth: 1,
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { labels: { color: '#eaecef' } } },
                scales: {
                    x: { ticks: { color: tickColor }, grid: { color: gridColor } },
                    y: { ticks: { color: tickColor }, grid: { color: gridColor } }
                }
            }
        });
    }

    // 3. 累计权益曲线
    destroyChart('equity');
    const eqCtx = document.getElementById('chart-equity');
    if (eqCtx) {
        const eq = d.equity_curve || [];
        charts['equity'] = new Chart(eqCtx, {
            type: 'line',
            data: {
                labels: eq.map(x => x.date),
                datasets: [{
                    label: '累计盈亏 (USDT)',
                    data: eq.map(x => x.equity),
                    borderColor: gold,
                    backgroundColor: 'rgba(240,185,11,0.1)',
                    fill: true,
                    tension: 0.1,
                    pointRadius: 0,
                    borderWidth: 2,
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { labels: { color: '#eaecef' } } },
                scales: {
                    x: { ticks: { color: tickColor, maxTicksLimit: 8 }, grid: { color: gridColor } },
                    y: { ticks: { color: tickColor }, grid: { color: gridColor } }
                }
            }
        });
    }

    // 4. 每日盈亏柱状图
    destroyChart('daily');
    const dailyCtx = document.getElementById('chart-daily');
    if (dailyCtx) {
        const daily = d.daily_pnl || [];
        charts['daily'] = new Chart(dailyCtx, {
            type: 'bar',
            data: {
                labels: daily.map(x => x.date),
                datasets: [{
                    label: '每日盈亏 (USDT)',
                    data: daily.map(x => x.pnl),
                    backgroundColor: daily.map(x => x.pnl >= 0 ? 'rgba(14,203,129,0.7)' : 'rgba(246,70,93,0.7)'),
                    borderColor: '#0b0e11',
                    borderWidth: 1,
                    borderRadius: 2,
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { labels: { color: '#eaecef' } } },
                scales: {
                    x: { ticks: { color: tickColor, maxTicksLimit: 8 }, grid: { color: gridColor } },
                    y: { ticks: { color: tickColor }, grid: { color: gridColor } }
                }
            }
        });
    }

    // 5. 最佳/最差币种
    renderSymbolChart('top-symbols', d.top_symbols || [], '#0ecb81');
    renderSymbolChart('worst-symbols', d.worst_symbols || [], '#f6465d');
}

function renderSymbolChart(id, data, barColor) {
    destroyChart(id);
    const ctx = document.getElementById('chart-' + id);
    if (!ctx) return;
    const labels = data.slice(0, 10).map(x => x.symbol);
    const values = data.slice(0, 10).map(x => x.pnl);
    charts[id] = new Chart(ctx, {
        type: 'bar',
        data: {
            labels,
            datasets: [{
                label: '盈亏 (USDT)',
                data: values,
                backgroundColor: values.map(v => v >= 0 ? 'rgba(14,203,129,0.7)' : 'rgba(246,70,93,0.7)'),
                borderColor: '#0b0e11',
                borderWidth: 1,
            }]
        },
        options: {
            indexAxis: 'y',
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { labels: { color: '#eaecef' } } },
            scales: {
                x: { ticks: { color: tickColor }, grid: { color: gridColor } },
                y: { ticks: { color: tickColor }, grid: { color: gridColor } }
            }
        }
    });
}

loadAnalysis();