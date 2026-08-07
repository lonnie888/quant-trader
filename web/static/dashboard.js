/** dashboard.js — 总览页 (仅真实账户) */
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

    // === 真实账户数据 ===
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
                        const symShort = pos.symbol.replace('USDT', '');
                        html += `<tr style="cursor:pointer;" onclick="window.location.href='/chart?symbol=${symShort}'">
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
})();