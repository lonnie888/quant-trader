/** dashboard.js — 总览页 (实盘/模拟盘) */
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

    // 读取当前模式
    let mode = 'real';
    try {
        const m = await fetch('/api/mode');
        if (m.ok) {
            const data = await m.json();
            mode = data.mode || 'real';
        }
    } catch (e) {
        console.warn('mode fetch failed:', e);
    }

    // 切换视图
    const realView = document.getElementById('real-view');
    const paperView = document.getElementById('paper-view');
    if (mode === 'paper') {
        if (realView) realView.style.display = 'none';
        if (paperView) paperView.style.display = '';
    } else {
        if (realView) realView.style.display = '';
        if (paperView) paperView.style.display = 'none';
    }

    if (mode === 'paper') {
        // === 模拟盘数据 ===
        const strat = localStorage.getItem('qt_strategy') || '';
        try {
            const sRes = await fetch('/api/summary?strategy=' + strat);
            if (sRes.ok) {
                const s = await sRes.json();
                const initial = s.initial_capital || 100;
                const equity = s.equity || initial;
                const totalReturnPct = ((equity - initial) / initial) * 100;
                const todayPct = s.realized_pnl_pct || 0;
                const winRate = s.win_rate || 0;
                // 模拟盘权益 = API 计算的复利权益
                setHtml('paper-equity', equity.toFixed(2) + ' USDT');
                setHtml('paper-initial', initial.toFixed(2) + ' USDT');
                setHtml('paper-today-pnl', `<span style="color:${color(todayPct)}">${fmt(todayPct)}%</span>`);
                const pnlUsdt = equity - initial;
                setHtml('paper-total-return', `<span style="color:${color(totalReturnPct)}">${fmt(totalReturnPct)}%<br><span style="font-size:14px; color:#848e9c;">${pnlUsdt >= 0 ? '+' : ''}${pnlUsdt.toFixed(2)} USDT</span></span>`);
                setText('paper-positions-count', s.open_count || 0);
                setText('paper-trades-count', s.total_trades || 0);
                setText('paper-winrate', winRate + '%');
            }
        } catch (e) {
            console.warn('paper summary unavailable:', e);
        }

        // 模拟盘权益曲线 - 用 API 的 equity_curve
        try {
            const sRes = await fetch('/api/summary?strategy=' + strat);
            if (sRes.ok && typeof Chart !== 'undefined') {
                const s = await sRes.json();
                const curve = s.equity_curve || [];
                if (curve.length < 1) {
                    const wrap = document.getElementById('paper-equity-wrapper');
                    if (wrap) wrap.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:200px;color:#848e9c;font-size:14px;">📊 等待更多数据点后展示权益曲线</div>';
                } else {
                    const ctx = document.getElementById('paperEquityChart');
                    if (ctx) {
                        new Chart(ctx.getContext('2d'), {
                            type: 'line',
                            data: {
                                labels: curve.map(x => x.date),
                                datasets: [{
                                    label: '模拟盘权益 (USDT)',
                                    data: curve.map(x => x.equity),
                                    borderColor: '#0ecb81',
                                    backgroundColor: 'rgba(14,203,129,0.1)',
                                    fill: true,
                                    tension: 0.1,
                                    pointRadius: 2,
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
                                    x: { ticks: { color: '#848e9c', maxTicksLimit: 10 }, grid: { color: '#1e2329' } },
                                    y: { ticks: { color: '#848e9c' }, grid: { color: '#1e2329' } }
                                }
                            }
                        });
                    }
                }
            }
        } catch (e) {
            console.warn('paper equity curve unavailable:', e);
        }

        // 模拟盘持仓
        try {
            const pRes = await fetch('/api/positions?strategy=' + strat);
            if (pRes.ok) {
                const positions = await pRes.json();
                const pTbody = document.getElementById('paper-positions-table');
                if (pTbody) {
                    if (!positions.length) {
                        pTbody.innerHTML = '<p style="color:#848e9c; font-size:14px; padding:20px 0; text-align:center;">暂无模拟盘持仓</p>';
                    } else {
                        let html = '<table><thead><tr><th>币种</th><th>入场</th><th>标记</th><th>未实现盈亏</th><th>SL</th><th>剩余K线</th></tr></thead><tbody>';
                        for (const pos of positions) {
                            html += `<tr>
                                <td>${pos.symbol}</td>
                                <td>${pos.entry_price.toFixed(6)}</td>
                                <td>${pos.last_price.toFixed(6)}</td>
                                <td style="color:${color(pos.pnl_pct_lev)}">${fmt(pos.pnl_pct_lev)}%</td>
                                <td>${pos.sl_price.toFixed(6)}</td>
                                <td>${pos.remaining_bars}</td>
                            </tr>`;
                        }
                        html += '</tbody></table>';
                        pTbody.innerHTML = html;
                    }
                }
            }
        } catch (e) {
            console.warn('paper positions unavailable:', e);
        }
    } else {
        // === 真实账户数据 ===
        try {
            const rRes = await fetch('/api/real-summary');
            if (rRes.ok) {
                const r = await rRes.json();
                setHtml('real-equity', r.totalWalletBalance.toFixed(2) + ' USDT');
                setHtml('real-available', r.availableBalance.toFixed(2) + ' USDT');
                setHtml('real-today-pnl', `<span style="color:${color(r.todayRealizedPnl)}">${fmt(r.todayRealizedPnl, 4)} USDT</span>`);
                setHtml('real-total-return', `<span style="color:${color(r.totalReturnPct)}">${fmt(r.totalReturnPct)}%<br><span style="font-size:14px; color:#848e9c;">${fmt(r.totalReturnUsdt, 2)} USDT</span></span>`);
                setText('real-positions-count', r.positionCount);

                const rTbody = document.getElementById('real-positions-table');
                if (rTbody) {
                    if (!r.positions || !r.positions.length) {
                        rTbody.innerHTML = '<p style="color:#848e9c; font-size:14px; padding:20px 0; text-align:center;">暂无实盘持仓</p>';
                    } else {
                        let html = '<table><thead><tr><th>币种</th><th>方向</th><th>数量</th><th>入场</th><th>标记</th><th>保证金</th><th>未实现盈亏</th></tr></thead><tbody>';
                        for (const pos of r.positions) {
                            const symShort = pos.symbol.replace('USDT', '');
                            html += `<tr style="cursor:pointer;" onclick="window.open('https://www.binance.com/zh-CN/futures/${pos.symbol}','_blank')">
                                <td>${pos.symbol}</td>
                                <td>${pos.side || '-'}</td>
                                <td>${pos.qty}</td>
                                <td>${pos.entry.toFixed(6)}</td>
                                <td>${(pos.mark || 0).toFixed(6)}</td>
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
    }
})();
