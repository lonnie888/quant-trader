/** dashboard.js — 总览页 (纸上交割 + 真实账户) */
(async function () {
    const color = v => v >= 0 ? '#0ecb81' : '#f6465d';
    const fmt = (v, d = 2) => {
        if (v === null || v === undefined || isNaN(v)) return '--';
        return (v >= 0 ? '+' : '') + Number(v).toFixed(d);
    };
    const setText = (id, val) => {
        const el = document.getElementById(id);
        if (el) el.textContent = val;
    };
    const setHtml = (id, html) => {
        const el = document.getElementById(id);
        if (el) el.innerHTML = html;
    };

    try {
        // === 纸上交割数据 ===
        const pRes = await fetch('/api/summary');
        if (!pRes.ok) { setHtml('chart-wrapper', '<div style="display:flex;align-items:center;justify-content:center;height:250px;color:#848e9c;font-size:14px;">📊 加载失败，请刷新页面</div>'); return; }
        const p = await pRes.json();

        setHtml('unrealized-pnl', `<span style="color:${color(p.unrealized_pnl_pct)}">${fmt(p.unrealized_pnl_pct)}%</span>`);
        setHtml('total-realized-pnl', `<span style="color:${color(p.total_realized_pnl_pct)}">${fmt(p.total_realized_pnl_pct)}%</span>`);
        setHtml('realized-pnl', `<span style="color:${color(p.realized_pnl_pct)}">${fmt(p.realized_pnl_pct)}%</span>`);
        setText('win-rate', `${p.win_rate}%`);
        setText('open-positions', p.open_count);

        // 每日 PnL 图
        const dailyData = p.daily_pnl || [];
        const chartWrapper = document.getElementById('chart-wrapper');
        if (dailyData.length <= 1) {
            if (chartWrapper) chartWrapper.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:250px;color:#848e9c;font-size:14px;">📊 等待更多交易数据后展示收益曲线</div>';
        } else if (typeof Chart !== 'undefined') {
            const ctx = document.getElementById('dailyPnlChart');
            if (ctx) {
                new Chart(ctx.getContext('2d'), {
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
        }

        // 持仓表 (纸交)
        const posRes = await fetch('/api/positions');
        if (posRes.ok) {
            const positions = await posRes.json();
            const tbody = document.getElementById('positions-table');
            if (tbody) {
                if (!positions.length) {
                    tbody.innerHTML = '<p style="color:#848e9c; font-size:14px; padding:20px 0; text-align:center;">暂无持仓</p>';
                } else {
                    let html = '<table><thead><tr><th>币种</th><th>入场</th><th>最新</th><th>PnL</th><th>剩余 K 线</th></tr></thead><tbody>';
                    for (const r of positions) {
                        const pnl = r.pnl_pct_lev || 0;
                        const sym = r.symbol.replace('/USDT:USDT', '');
                        html += `<tr>
                            <td>${sym}</td>
                            <td>${(r.entry_price || 0).toFixed(6)}</td>
                            <td>${(r.last_close || 0).toFixed(6)}</td>
                            <td style="color:${color(pnl)}">${fmt(pnl * 100)}%</td>
                            <td>${r.remaining_bars}</td>
                        </tr>`;
                    }
                    html += '</tbody></table>';
                    tbody.innerHTML = html;
                }
            }
        }
    } catch (e) {
        console.warn('paper ledger load failed:', e);
    }

    // === 真实账户数据 (独立try/catch, 不影响纸交) ===
    try {
        const rRes = await fetch('/api/real-summary');
        if (rRes.ok) {
            const r = await rRes.json();
            setHtml('real-equity', r.totalWalletBalance.toFixed(2) + ' USDT');
            setHtml('real-available', r.availableBalance.toFixed(2) + ' USDT');
            setHtml('real-today-pnl', `<span style="color:${color(r.todayRealizedPnl)}">${fmt(r.todayRealizedPnl, 4)} USDT</span>`);
            setHtml('real-total-return', `<span style="color:${color(r.totalReturnPct)}">${fmt(r.totalReturnPct)}% (${fmt(r.totalReturnUsdt, 2)} USDT)</span>`);
            setText('real-positions-count', r.positionCount);

            const rTbody = document.getElementById('real-positions-table');
            if (rTbody) {
                if (!r.positions || !r.positions.length) {
                    rTbody.innerHTML = '<p style="color:#848e9c; font-size:14px; padding:20px 0; text-align:center;">暂无实盘持仓</p>';
                } else {
                    let html = '<table><thead><tr><th>币种</th><th>方向</th><th>数量</th><th>入场</th><th>标记</th><th>保证金</th><th>未实现盈亏</th></tr></thead><tbody>';
                    for (const pos of r.positions) {
                        html += `<tr>
                            <td>${pos.symbol}</td>
                            <td>${pos.side}</td>
                            <td>${pos.qty}</td>
                            <td>${pos.entry.toFixed(6)}</td>
                            <td>${pos.mark.toFixed(6)}</td>
                            <td>${pos.margin.toFixed(2)}</td>
                            <td style="color:${color(pos.unrealizedPnl)}">${fmt(pos.unrealizedPnl, 4)}</td>
                        </tr>`;
                    }
                    html += '</tbody></table>';
                    rTbody.innerHTML = html;
                }
            }
        }
    } catch (e) {
        console.warn('real account data unavailable:', e);
    }

    // === 真实账户权益曲线 ===
    try {
        const eqRes = await fetch('/api/real-equity');
        if (eqRes.ok && typeof Chart !== 'undefined') {
            const eqData = await eqRes.json();
            const eqWrapper = document.getElementById('real-equity-wrapper');
            if (!eqWrapper) return;
            if (eqData.length < 2) {
                eqWrapper.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:200px;color:#848e9c;font-size:14px;">📊 等待更多数据点后展示权益曲线</div>';
            } else {
                const ctx = document.getElementById('realEquityChart');
                if (ctx) {
                    new Chart(ctx.getContext('2d'), {
                        type: 'line',
                        data: {
                            labels: eqData.map(x => x.t),
                            datasets: [{
                                label: '账户权益 (USDT)',
                                data: eqData.map(x => x.equity),
                                borderColor: '#f0b90b',
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
                            plugins: {
                                legend: { labels: { color: '#848e9c' } },
                                tooltip: { callbacks: { label: ctx => `${ctx.parsed.y.toFixed(2)} USDT` } }
                            },
                            scales: {
                                x: { ticks: { color: '#848e9c', maxTicksLimit: 8 }, grid: { color: '#1e2329' } },
                                y: { ticks: { color: '#848e9c' }, grid: { color: '#1e2329' } }
                            }
                        }
                    });
                }
            }
        }
    } catch (e) {
        console.warn('real equity curve unavailable:', e);
    }
})();