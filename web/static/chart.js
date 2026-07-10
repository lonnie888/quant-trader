/** chart.js — Kline chart page using TradingView Lightweight Charts */

(async function () {
    const params = new URLSearchParams(window.location.search);
    const symbol = params.get('symbol') || 'TLM';
    const entryId = params.get('entry_id') || null;

    const label = document.getElementById('chart-symbol-label');
    const container = document.getElementById('chart-container');
    const info = document.getElementById('chart-info');

    if (label) label.textContent = `${symbol}/USDT — 15m`;
    if (info) info.textContent = 'Loading...';

    // Check LightweightCharts is loaded
    if (typeof LightweightCharts === 'undefined') {
        if (info) info.textContent = 'Error: LightweightCharts library not loaded';
        return;
    }

    // Create chart
    const chart = LightweightCharts.createChart(container, {
        width: container.clientWidth,
        height: 500,
        layout: {
            background: { color: '#0b0e11' },
            textColor: '#848e9c',
        },
        grid: {
            vertLines: { color: '#1e2329' },
            horzLines: { color: '#1e2329' },
        },
        crosshair: {
            mode: LightweightCharts.CrosshairMode.Normal,
        },
        timeScale: {
            borderColor: '#2b3139',
            timeVisible: true,
            secondsVisible: false,
        },
        rightPriceScale: {
            borderColor: '#2b3139',
        },
        localization: {
            timeFormatter: (time) => {
                // time is UTCTimestamp (epoch seconds), convert to Beijing time (UTC+8)
                const d = new Date((time + 8 * 3600) * 1000);
                const pad = (n) => String(n).padStart(2, '0');
                return `${d.getUTCFullYear()}-${pad(d.getUTCMonth()+1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
            },
        },
    });

    const candlestickSeries = chart.addSeries(LightweightCharts.CandlestickSeries, {
        upColor: '#0ecb81',
        downColor: '#f6465d',
        borderDownColor: '#f6465d',
        borderUpColor: '#0ecb81',
        wickDownColor: '#f6465d',
        wickUpColor: '#0ecb81',
    });

    // Fetch data
    let url = `/api/klines/${encodeURIComponent(symbol)}?bars=100`;
    if (entryId) url += `&entry_id=${entryId}`;

    try {
        const res = await fetch(url);
        if (!res.ok) {
            if (info) info.textContent = `API error: ${res.status} ${res.statusText}`;
            return;
        }
        const data = await res.json();

        if (!data.klines || data.klines.length === 0) {
            if (info) info.textContent = 'No kline data available for this symbol.';
            return;
        }

        const chartData = data.klines.map(k => ({
            time: Math.floor(k.t / 1000),
            open: k.o,
            high: k.h,
            low: k.l,
            close: k.c,
        }));

        candlestickSeries.setData(chartData);

        // Markers
        if (data.markers && data.markers.length > 0) {
            const markers = data.markers.map(m => ({
                time: Math.floor(m.time / 1000),
                position: m.position,
                color: m.color,
                shape: m.shape,
                text: m.text,
            }));
            LightweightCharts.createSeriesMarkers(candlestickSeries, markers);

            const entryMarker = markers.find(m => m.shape === 'arrowUp' && m.text.startsWith('Entry'));
            const exitMarker = markers.find(m => m.shape === 'arrowDown' && m.text.startsWith('Exit'));
            let infoHtml = `<span style="color:#0ecb81">●</span> ${entryMarker ? entryMarker.text : '—'}`;
            if (exitMarker) {
                infoHtml += ` <span style="color:#848e9c">|</span> <span style="color:#f6465d">●</span> ${exitMarker.text}`;
            }
            if (info) info.innerHTML = infoHtml;
        } else {
            if (info) info.textContent = `${data.klines.length} bars loaded`;
        }

        chart.timeScale().fitContent();

        window.addEventListener('resize', () => {
            chart.applyOptions({ width: container.clientWidth });
        });

    } catch (e) {
        if (info) info.textContent = `Error: ${e.message || 'unknown'}`;
    }
})();