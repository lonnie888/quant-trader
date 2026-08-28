/** data.js — K线数据管理器 */
let allSymbols = [];

async function loadCache() {
    try {
        const r = await fetch('/api/kline-manager/symbols');
        const d = await r.json();
        if (d.cached) {
            allSymbols = d.symbols;
            document.getElementById('cache-info').textContent =
                `已缓存 ${d.total} 币种 (${new Date().toLocaleString('zh-CN')})`;
        } else {
            document.getElementById('cache-info').textContent = '无缓存, 点击"重新扫描"';
        }
        updateSummary();
        renderTable();
    } catch (e) {
        console.error(e);
        document.getElementById('cache-info').textContent = '加载缓存失败';
    }
}

async function refreshScan() {
    document.getElementById('cache-info').textContent = '扫描中... (约30秒)';
    const btn = document.querySelector('button[onclick="refreshScan()"]');
    btn.disabled = true;
    btn.textContent = '⏳ 扫描中...';
    try {
        const r = await fetch('/api/kline-manager/scan');
        const d = await r.json();
        allSymbols = d.symbols;
        document.getElementById('cache-info').textContent =
            `✅ 扫描完成: ${d.total} 币种`;
        updateSummary();
        renderTable();
    } catch (e) {
        document.getElementById('cache-info').textContent = '❌ 扫描失败: ' + e;
    } finally {
        btn.disabled = false;
        btn.textContent = '🔄 重新扫描';
    }
}

function updateSummary() {
    const total = allSymbols.length;
    const withData = allSymbols.filter(s => s.rows > 0).length;
    const gaps = allSymbols.filter(s => s.gaps > 0).length;
    const spans = allSymbols.filter(s => s.span_days > 0).map(s => s.span_days).sort((a, b) => a - b);
    const median = spans.length ? spans[Math.floor(spans.length / 2)] : 0;

    document.getElementById('stat-total').textContent = total;
    document.getElementById('stat-full').textContent = withData;
    document.getElementById('stat-gaps').textContent = gaps;
    document.getElementById('stat-span').textContent = median + ' 天';

    document.getElementById('summary-line').textContent =
        `共 ${total} 币种, ${withData} 个有数据, ${gaps} 个有缺口(>2h), 覆盖中位数 ${median} 天`;
}

function renderTable() {
    const kw = (document.getElementById('filter-symbol').value || '').trim().toUpperCase();
    const startF = document.getElementById('filter-start').value;
    const gapF = document.getElementById('filter-gaps').value;

    let list = allSymbols;
    if (kw) list = list.filter(s => s.symbol.toUpperCase().includes(kw));
    if (startF) list = list.filter(s => (s.start_date || '').startsWith(startF));
    if (gapF !== '') list = list.filter(s => (s.gaps > 0) === (gapF === '1'));

    const body = document.getElementById('data-body');
    if (!list.length) {
        body.innerHTML = '<tr><td colspan="8" style="text-align:center;color:#848e9c;padding:30px;">无匹配币种</td></tr>';
        return;
    }
    body.innerHTML = list.map(s => {
        const gapColor = s.gaps > 0 ? '#f6465d' : '#0ecb81';
        const rowColor = s.rows === 0 ? '#848e9c' : '#eaecef';
        return `<tr style="cursor:pointer;" onclick="showDetail('${s.symbol}')">
            <td><b>${s.symbol}</b></td>
            <td>${s.rows.toLocaleString()}</td>
            <td>${s.start_date || '--'}</td>
            <td>${s.end_date || '--'}</td>
            <td>${s.span_days || 0}</td>
            <td style="color:${gapColor};font-weight:600">${s.gaps > 0 ? s.gaps : '—'}</td>
            <td>${s.has_funding ? '✅' : '—'}</td>
            <td style="color:#848e9c;font-size:11px;">查看</td>
        </tr>`;
    }).join('');

    document.getElementById('summary-line').textContent += ` | 筛选后: ${list.length} 币种`;
}

async function showDetail(sym) {
    const card = document.getElementById('detail-card');
    const body = document.getElementById('detail-body');
    card.style.display = '';
    document.getElementById('detail-symbol').textContent = sym;
    body.innerHTML = '加载中...';
    try {
        const r = await fetch('/api/kline-manager/' + sym);
        const d = await r.json();
        if (d.error) {
            body.innerHTML = `<p style="color:#f6465d">${d.error}</p>`;
            return;
        }
        // 渲染月度分布柱状图
        const months = d.months || [];
        const maxRows = Math.max(...months.map(m => m.rows), 1);
        const barsHtml = months.map(m => {
            const h = Math.round((m.rows / maxRows) * 70);
            const missing = m.rows < 100;
            return `<div title="${m.month}: ${m.rows}根" style="width:14px;height:${h}px;background:${missing ? '#f6465d' : '#0ecb81'};border-radius:2px;flex-shrink:0;"></div>`;
        }).join('');
        document.getElementById('detail-bars').innerHTML = barsHtml;
        // 月份文本 (缺失标红)
        document.getElementById('detail-months').innerHTML = months.map(m => {
            const missing = m.rows < 100;
            const color = missing ? '#f6465d' : '#0ecb81';
            return `<span style="color:${color};margin-right:8px;">${m.month}:${m.rows}</span>`;
        }).join('');
        // 滚动到详情
        card.scrollIntoView({ behavior: 'smooth' });
    } catch (e) {
        body.innerHTML = `<p style="color:#f6465d">加载失败: ${e}</p>`;
    }
}

function closeDetail() {
    document.getElementById('detail-card').style.display = 'none';
}

// 初始化
loadCache();
