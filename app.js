/* ========================================================================
   PSX Stock Screener — Application Logic (Live Data)
   Fetches real-time data from PSX Data Portal via local API proxy
   ======================================================================== */

// ─── State ───
let STOCKS = [];
let currentSort = { key: "score", direction: "desc" };
let activePreset = null;
let currentView = "table";
let searchQuery = "";
let watchlist = new Set(JSON.parse(localStorage.getItem("psx_watchlist") || "[]"));
let isLoading = false;
let autoRefreshTimer = null;

// ─── Score Calculation (adapted for live PSX data) ───
function calculateScore(stock) {
    let score = 0;

    // P/E Score (0-25): Lower is better, but 0 means no earnings data
    if (stock.pe > 0 && stock.pe <= 5) score += 25;
    else if (stock.pe <= 8) score += 22;
    else if (stock.pe <= 12) score += 18;
    else if (stock.pe <= 18) score += 14;
    else if (stock.pe <= 25) score += 8;
    else if (stock.pe > 25) score += 3;
    else score += 0; // pe === 0 means no data

    // Dividend Yield Score (0-25): Higher is better
    if (stock.divYield >= 10) score += 25;
    else if (stock.divYield >= 7) score += 22;
    else if (stock.divYield >= 5) score += 18;
    else if (stock.divYield >= 3) score += 12;
    else if (stock.divYield >= 1) score += 6;
    else score += 0;

    // 1-Year Change Score (0-25): Momentum/growth
    if (stock.yearChange >= 50) score += 25;
    else if (stock.yearChange >= 30) score += 22;
    else if (stock.yearChange >= 15) score += 18;
    else if (stock.yearChange >= 0) score += 12;
    else if (stock.yearChange >= -20) score += 5;
    else score += 0;

    // Volume / Liquidity Score (0-25): Higher is better
    if (stock.volume >= 1000000) score += 25;
    else if (stock.volume >= 500000) score += 20;
    else if (stock.volume >= 100000) score += 15;
    else if (stock.volume >= 50000) score += 10;
    else if (stock.volume >= 10000) score += 5;
    else score += 1;

    return score;
}

function getScoreBreakdown(stock) {
    const breakdown = {};

    // P/E
    if (stock.pe > 0 && stock.pe <= 5) breakdown.pe = 25;
    else if (stock.pe <= 8) breakdown.pe = 22;
    else if (stock.pe <= 12) breakdown.pe = 18;
    else if (stock.pe <= 18) breakdown.pe = 14;
    else if (stock.pe <= 25) breakdown.pe = 8;
    else if (stock.pe > 25) breakdown.pe = 3;
    else breakdown.pe = 0;

    // Dividend
    if (stock.divYield >= 10) breakdown.dividend = 25;
    else if (stock.divYield >= 7) breakdown.dividend = 22;
    else if (stock.divYield >= 5) breakdown.dividend = 18;
    else if (stock.divYield >= 3) breakdown.dividend = 12;
    else if (stock.divYield >= 1) breakdown.dividend = 6;
    else breakdown.dividend = 0;

    // Growth
    if (stock.yearChange >= 50) breakdown.growth = 25;
    else if (stock.yearChange >= 30) breakdown.growth = 22;
    else if (stock.yearChange >= 15) breakdown.growth = 18;
    else if (stock.yearChange >= 0) breakdown.growth = 12;
    else if (stock.yearChange >= -20) breakdown.growth = 5;
    else breakdown.growth = 0;

    // Volume
    if (stock.volume >= 1000000) breakdown.liquidity = 25;
    else if (stock.volume >= 500000) breakdown.liquidity = 20;
    else if (stock.volume >= 100000) breakdown.liquidity = 15;
    else if (stock.volume >= 50000) breakdown.liquidity = 10;
    else if (stock.volume >= 10000) breakdown.liquidity = 5;
    else breakdown.liquidity = 1;

    return breakdown;
}

function getScoreClass(score) {
    if (score >= 80) return "excellent";
    if (score >= 60) return "good";
    if (score >= 40) return "average";
    return "weak";
}

// ─── Formatting Helpers ───
function formatMcap(mcap) {
    if (!mcap || mcap <= 0) return "N/A";
    if (mcap >= 1e12) return (mcap / 1e12).toFixed(1) + "T";
    if (mcap >= 1e9) return (mcap / 1e9).toFixed(1) + "B";
    if (mcap >= 1e6) return (mcap / 1e6).toFixed(1) + "M";
    if (mcap >= 1e3) return (mcap / 1e3).toFixed(0) + "K";
    return mcap.toFixed(0);
}

function formatCurrency(val) {
    if (val === undefined || val === null || val === 0) return "-";
    if (Math.abs(val) >= 1e9) return (val / 1e9).toFixed(2) + "B";
    if (Math.abs(val) >= 1e6) return (val / 1e6).toFixed(2) + "M";
    if (Math.abs(val) >= 1e3) return (val / 1e3).toFixed(2) + "K";
    return val.toFixed(2);
}

function formatRevenue(val) {
    if (!val) return `<span class="muted">N/A</span>`;
    return formatCurrency(val);
}

function formatVolume(vol) {
    if (!vol || vol <= 0) return "0";
    if (vol >= 1e6) return (vol / 1e6).toFixed(1) + "M";
    if (vol >= 1e3) return (vol / 1e3).toFixed(0) + "K";
    return vol.toFixed(0);
}

function formatPrice(price) {
    if (!price) return "₨0.00";
    return "₨" + price.toLocaleString("en-PK", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function formatChange(val) {
    if (val === undefined || val === null) return "0.00%";
    const prefix = val >= 0 ? "+" : "";
    return prefix + val.toFixed(2) + "%";
}

// ─── Data Fetching ───
async function fetchLiveData(isAutoRefresh = false, isForce = false) {
    if (isLoading) return;
    isLoading = true;
    
    if (!isAutoRefresh) {
        showLoading(true);
    }
    setRefreshBtnSpinning(true);

    try {
        const deviceId = getDeviceId();
        const savedEmail = localStorage.getItem("psx_user_email") || "";
        let q = `deviceId=${deviceId}` + (savedEmail ? `&email=${encodeURIComponent(savedEmail)}` : '');
        if (isForce) {
            q += `&force=1`;
        }

        const controller = typeof AbortController !== "undefined" ? new AbortController() : null;
        const timeoutId = controller ? setTimeout(() => controller.abort(), 8000) : null;

        const fetchOpts = controller ? { signal: controller.signal } : {};
        const [stockRes, indexRes] = await Promise.all([
            fetch(`/api/stocks?${q}`, fetchOpts),
            fetch(`/api/indices?${q}`, fetchOpts),
        ]);
        if (timeoutId) clearTimeout(timeoutId);

        if (stockRes.status === 401 || stockRes.status === 402 || indexRes.status === 401 || indexRes.status === 402) {
            initTrialSystem();
            return;
        }

        const stockData = await stockRes.json();
        const indexData = await indexRes.json();

        if (stockData.trialExpired || indexData.trialExpired || stockData.needsEmail || indexData.needsEmail) {
            initTrialSystem();
            return;
        }

        if (stockData.success && stockData.data) {
            STOCKS = stockData.data;
            updateSectorFilter();
            updateMarketOverview(indexData, stockData);
            updateLastUpdated(stockData.fetchedAt);
            renderAll();

            // Show stale data banner if serving outdated cache
            if (stockData.stale) {
                showStaleBanner(stockData.fetchedAt);
            } else {
                hideStaleBanner();
            }
        } else {
            if (!isAutoRefresh) {
                showError('Failed to load stock data. Please try again.');
            }
        }
    } catch (error) {
        console.error('Error fetching data:', error);
        if (!isAutoRefresh) {
            showError('Connection error. Make sure the server is running.');
        }
    } finally {
        isLoading = false;
        showLoading(false);
        setRefreshBtnSpinning(false);
    }
}

let loadingTimeoutTimer = null;

function showLoading(show) {
    const overlay = document.getElementById('loading-overlay');
    if (overlay) {
        overlay.style.display = show ? 'flex' : 'none';
    }
    if (show) {
        if (loadingTimeoutTimer) clearTimeout(loadingTimeoutTimer);
        loadingTimeoutTimer = setTimeout(() => {
            if (overlay) overlay.style.display = 'none';
        }, 3500);
    } else {
        if (loadingTimeoutTimer) clearTimeout(loadingTimeoutTimer);
    }
}

function setRefreshBtnSpinning(spinning) {
    const btn = document.getElementById('btn-refresh');
    const icon = document.getElementById('refresh-icon');
    if (btn && icon) {
        if (spinning) {
            icon.classList.add('spinning');
            btn.disabled = true;
        } else {
            icon.classList.remove('spinning');
            btn.disabled = false;
        }
    }
}

function showError(message) {
    const tbody = document.getElementById('table-body');
    if (tbody && STOCKS.length === 0) {
        tbody.innerHTML = `<tr><td colspan="12" style="text-align:center; padding:48px; color:var(--accent-rose);">${message}</td></tr>`;
    }
}

function timeAgo(dateStr) {
    if (!dateStr) return '';
    const diff = Date.now() - new Date(dateStr).getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ${mins % 60}m ago`;
    const days = Math.floor(hrs / 24);
    return `${days}d ${hrs % 24}h ago`;
}

function showStaleBanner(fetchedAt) {
    let banner = document.getElementById('stale-banner');
    if (!banner) {
        banner = document.createElement('div');
        banner.id = 'stale-banner';
        banner.style.cssText = 'background:linear-gradient(135deg,#f59e0b22,#f59e0b11);border:1px solid #f59e0b44;color:#f59e0b;padding:10px 20px;border-radius:10px;margin:0 auto 16px;max-width:900px;text-align:center;font-size:13px;display:flex;align-items:center;justify-content:center;gap:8px;';
        const main = document.querySelector('.main-content') || document.querySelector('.container') || document.body;
        const firstChild = main.querySelector('.filters-section, .table-container, table');
        if (firstChild) {
            main.insertBefore(banner, firstChild);
        } else {
            main.appendChild(banner);
        }
    }
    banner.innerHTML = `⚠️ Showing cached data from <strong>${timeAgo(fetchedAt)}</strong> — PSX source temporarily unavailable. Data will refresh automatically when available.`;
    banner.style.display = 'flex';
}

function hideStaleBanner() {
    const banner = document.getElementById('stale-banner');
    if (banner) banner.style.display = 'none';
}

// ─── Update UI from live data ───
function updateSectorFilter() {
    const select = document.getElementById('filter-sector');
    const currentValue = select.value;
    
    // Get unique sectors
    const sectors = [...new Set(STOCKS.map(s => s.sector))].filter(Boolean).sort();
    
    // Clear and repopulate
    select.innerHTML = '<option value="all">All Sectors</option>';
    sectors.forEach(sector => {
        const opt = document.createElement('option');
        opt.value = sector;
        opt.textContent = sector;
        select.appendChild(opt);
    });
    
    // Restore selection
    if (currentValue !== 'all') {
        select.value = currentValue;
    }
}

function updateMarketOverview(indexData, stockData) {
    // KSE-100 index
    if (indexData.success && indexData.indices) {
        const kse100 = indexData.indices.find(i => i.name === 'KSE100');
        if (kse100) {
            document.getElementById('kse100-value').textContent = kse100.value.toLocaleString('en-PK', { maximumFractionDigits: 0 });
            const changeEl = document.getElementById('kse100-change');
            const sign = kse100.isPositive ? '+' : '-';
            changeEl.innerHTML = `
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3">
                    <path d="${kse100.isPositive ? 'M18 15l-6-6-6 6' : 'M6 9l6 6 6-6'}"/>
                </svg>
                ${sign}${Math.abs(kse100.change).toLocaleString()} (${kse100.changePercent.toFixed(2)}%)
            `;
            changeEl.className = `overview-change ${kse100.isPositive ? 'positive' : 'negative'}`;
        }

        // Market volume from index data
        if (indexData.market) {
            const vol = indexData.market.volume;
            document.getElementById('market-volume').textContent = formatVolume(vol);
            const val = indexData.market.value;
            document.getElementById('market-volume-detail').textContent = 
                `Value: ₨${val >= 1e9 ? (val / 1e9).toFixed(1) + 'B' : (val / 1e6).toFixed(0) + 'M'}`;
            document.getElementById('market-volume-detail').className = 'overview-change';

            // Market status
            const statusEl = document.getElementById('market-status');
            const state = indexData.market.state || 'Closed';
            const isOpen = state.toLowerCase().includes('open') || state.toLowerCase().includes('continuous');
            statusEl.innerHTML = `
                <span class="status-dot ${isOpen ? 'status-open' : ''}"></span>
                <span>Market ${state}</span>
            `;
            
            // Live indicator
            const liveEl = document.getElementById('live-indicator');
            if (isOpen) {
                liveEl.classList.add('market-open');
            } else {
                liveEl.classList.remove('market-open');
            }
        }
    }

    // Advance/Decline from stock data
    if (stockData.success && stockData.data) {
        const advancing = stockData.data.filter(s => s.change > 0).length;
        const declining = stockData.data.filter(s => s.change < 0).length;
        const unchanged = stockData.data.filter(s => s.change === 0).length;
        
        document.getElementById('adv-count').textContent = advancing;
        document.getElementById('dec-count').textContent = declining;
        
        const total = advancing + declining || 1;
        document.getElementById('adv-bar').style.width = `${(advancing / total * 100).toFixed(1)}%`;

        // Total stocks
        document.getElementById('total-stocks-value').textContent = stockData.data.length;
        document.getElementById('total-stocks-detail').textContent = `${advancing}↑ ${declining}↓ ${unchanged}→`;
    }
}

function updateLastUpdated(timestamp) {
    const el = document.getElementById('last-updated-time');
    if (el && timestamp) {
        const date = new Date(timestamp);
        el.textContent = date.toLocaleTimeString('en-PK', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    }
}

// ─── Filter Logic ───
function getFilteredStocks() {
    const sector = document.getElementById("filter-sector").value;
    const index = document.getElementById("filter-index").value;
    const mcapFilter = document.getElementById("filter-mcap").value;
    const peMax = parseFloat(document.getElementById("filter-pe").value);
    const divMin = parseFloat(document.getElementById("filter-div").value);
    const changeMin = parseFloat(document.getElementById("filter-change").value);
    const yearChangeMin = parseFloat(document.getElementById("filter-year-change").value);
    const volumeMin = parseFloat(document.getElementById("filter-volume").value);
    const rev = document.getElementById("filter-revenue") ? document.getElementById("filter-revenue").value : "all";

    return STOCKS.filter(stock => {
        // Search filter
        if (searchQuery) {
            const q = searchQuery.toLowerCase();
            if (!stock.symbol.toLowerCase().includes(q) && 
                !stock.name.toLowerCase().includes(q)) return false;
        }

        if (sector !== "all" && stock.sector !== sector) return false;
        
        // Index filter
        if (index !== "all") {
            if (!stock.listedIn || !stock.listedIn.includes(index)) return false;
        }

        // Market cap filter (values in raw PKR from PSX)
        if (mcapFilter !== "all") {
            if (mcapFilter === "mega" && stock.mcap < 200e9) return false;
            if (mcapFilter === "large" && (stock.mcap < 50e9 || stock.mcap >= 200e9)) return false;
            if (mcapFilter === "mid" && (stock.mcap < 10e9 || stock.mcap >= 50e9)) return false;
            if (mcapFilter === "small" && stock.mcap >= 10e9) return false;
        }

        if (peMax < 50 && (stock.pe <= 0 || stock.pe > peMax)) return false;
        if (divMin > 0 && stock.divYield < divMin) return false;
        if (changeMin > -10 && stock.change < changeMin) return false;
        if (yearChangeMin > -100 && stock.yearChange < yearChangeMin) return false;
        if (volumeMin > 0 && stock.volume < volumeMin) return false;

        if (rev !== "all") {
            const r = stock.revenue || 0;
            if (rev === "high" && r < 50e9) return false;
            if (rev === "med" && (r < 10e9 || r >= 50e9)) return false;
            if (rev === "low" && r >= 10e9) return false;
        }

        return true;
    }).map(stock => ({
        ...stock,
        score: calculateScore(stock),
    }));
}

function sortStocks(stocks) {
    const { key, direction } = currentSort;
    return [...stocks].sort((a, b) => {
        let valA = a[key];
        let valB = b[key];
        if (key === "symbol") {
            return direction === "asc" ? valA.localeCompare(valB) : valB.localeCompare(valA);
        }
        return direction === "asc" ? valA - valB : valB - valA;
    });
}

// ─── Render Functions ───
function renderTable(stocks) {
    const tbody = document.getElementById("table-body");
    if (stocks.length === 0) {
        tbody.innerHTML = `<tr><td colspan="11" style="text-align:center; padding:48px; color:var(--text-tertiary);">No stocks match your criteria. Try adjusting filters.</td></tr>`;
        return;
    }

    tbody.innerHTML = stocks.map((stock, i) => {
        const changeClass = stock.change >= 0 ? "positive" : "negative";
        const yearChangeClass = stock.yearChange >= 0 ? "positive" : "negative";
        const scoreClass = getScoreClass(stock.score);
        const isWatched = watchlist.has(stock.symbol);

        return `
        <tr class="row-animate" style="animation-delay:${Math.min(i * 0.02, 1)}s" data-symbol="${stock.symbol}">
            <td><button class="star-btn ${isWatched ? "active" : ""}" data-star="${stock.symbol}" title="Toggle Watchlist">${isWatched ? "★" : "☆"}</button></td>
            <td onclick="showDetail('${stock.symbol}')">${stock.symbol} ${stock.isNC ? '<span class="nc-tag">NC</span>' : ''}</td>
            <td><span class="sector-pill">${stock.sector}</span></td>
            <td>${formatRevenue(stock.revenue)}</td>
            <td style="font-weight:600;">Rs ${stock.price.toFixed(2)}</td>
            <td class="cell-change ${changeClass}">${formatChange(stock.change)}</td>
            <td class="cell-change ${yearChangeClass}">${formatChange(stock.yearChange)}</td>
            <td class="cell-mcap">${formatMcap(stock.mcap)}</td>
            <td class="cell-metric">${stock.pe > 0 ? stock.pe.toFixed(1) : '—'}</td>
            <td class="cell-metric ${stock.divYield >= 5 ? "positive" : ""}">${stock.divYield > 0 ? stock.divYield.toFixed(1) + '%' : '—'}</td>
            <td class="cell-volume">${formatVolume(stock.volume)}</td>
            <td><span class="score-badge score-${scoreClass}">${stock.score}</span></td>
        </tr>`;
    }).join("");
}

function renderCards(stocks) {
    const grid = document.getElementById("view-cards");
    if (stocks.length === 0) {
        grid.innerHTML = `<p style="text-align:center; padding:48px; color:var(--text-tertiary); grid-column:1/-1;">No stocks match your criteria.</p>`;
        return;
    }

    grid.innerHTML = stocks.map(stock => {
        const changeClass = stock.change >= 0 ? "positive" : "negative";
        const scoreClass = getScoreClass(stock.score);
        const isWatched = watchlist.has(stock.symbol);
        const scoreColor = scoreClass === "excellent" ? "var(--accent-emerald)" :
                          scoreClass === "good" ? "var(--accent-cyan)" :
                          scoreClass === "average" ? "var(--accent-amber)" : "var(--accent-rose)";

        return `
        <div class="stock-card" data-symbol="${stock.symbol}">
            <div class="card-header">
                <div>
                    <div class="card-symbol">${stock.symbol}${stock.isKSE100 ? ' <span class="card-index-tag">KSE100</span>' : ''}</div>
                    <div class="card-company">${stock.name}</div>
                </div>
                <div class="card-price-group">
                    <div class="card-price">${formatPrice(stock.price)}</div>
                    <div class="card-change ${changeClass}">${formatChange(stock.change)}</div>
                </div>
            </div>
            <div class="card-sector-tag">${stock.sector}</div>
            <div class="card-metrics">
                <div class="card-metric">
                    <div class="card-metric-label">P/E</div>
                    <div class="card-metric-value">${stock.pe > 0 ? stock.pe.toFixed(1) : '—'}</div>
                </div>
                <div class="card-metric">
                    <div class="card-metric-label">Div Yield</div>
                    <div class="card-metric-value ${stock.divYield >= 5 ? 'positive' : ''}">${stock.divYield > 0 ? stock.divYield.toFixed(1) + '%' : '—'}</div>
                </div>
                <div class="card-metric">
                    <div class="card-metric-label">1Y Change</div>
                    <div class="card-metric-value ${stock.yearChange >= 0 ? 'positive' : 'negative'}">${formatChange(stock.yearChange)}</div>
                </div>
                <div class="card-metric">
                    <div class="card-metric-label">Mkt Cap</div>
                    <div class="card-metric-value">${formatMcap(stock.mcap)}</div>
                </div>
                <div class="card-metric">
                    <div class="card-metric-label">30D Vol</div>
                    <div class="card-metric-value">${formatVolume(stock.volume)}</div>
                </div>
                <div class="card-metric">
                    <div class="card-metric-label">Free Float</div>
                    <div class="card-metric-value">${formatVolume(stock.freeFloat)}</div>
                </div>
            </div>
            <div class="card-footer">
                <div class="card-score">
                    Score
                    <div class="card-score-bar">
                        <div class="card-score-fill" style="width:${stock.score}%; background:${scoreColor}"></div>
                    </div>
                    <span style="color:${scoreColor}">${stock.score}</span>
                </div>
                <button class="star-btn ${isWatched ? "active" : ""}" data-star="${stock.symbol}">
                    ${isWatched ? "★" : "☆"}
                </button>
            </div>
        </div>`;
    }).join("");
}

function renderAll() {
    const filtered = getFilteredStocks();
    const sorted = sortStocks(filtered);

    document.getElementById("results-count").textContent = `${filtered.length} stock${filtered.length !== 1 ? "s" : ""}`;

    renderTable(sorted);
    renderCards(sorted);
    runTradingIntelligenceEngine();
}


// ─── Watchlist ───
function toggleWatchlist(symbol) {
    if (watchlist.has(symbol)) {
        watchlist.delete(symbol);
    } else {
        watchlist.add(symbol);
    }
    localStorage.setItem("psx_watchlist", JSON.stringify([...watchlist]));
    document.getElementById("watchlist-count").textContent = watchlist.size;
    renderAll();
}

function renderWatchlistModal() {
    const body = document.getElementById("watchlist-body");
    if (watchlist.size === 0) {
        body.innerHTML = `<p class="empty-state">No stocks in watchlist yet. Click the ★ icon to add stocks.</p>`;
        return;
    }

    const items = [...watchlist].map(sym => {
        const stock = STOCKS.find(s => s.symbol === sym);
        if (!stock) return "";
        const changeClass = stock.change >= 0 ? "positive" : "negative";
        return `
        <div class="watchlist-item">
            <div class="watchlist-item-info">
                <span class="watchlist-item-symbol">${stock.symbol}</span>
                <span style="color:var(--text-tertiary);font-size:0.82rem">${stock.name}</span>
            </div>
            <div style="display:flex;align-items:center;gap:16px;">
                <span class="watchlist-item-price">${formatPrice(stock.price)}</span>
                <span class="cell-change ${changeClass}" style="font-size:0.85rem">${formatChange(stock.change)}</span>
                <button class="watchlist-remove" data-remove="${stock.symbol}" title="Remove">&times;</button>
            </div>
        </div>`;
    }).join("");

    body.innerHTML = items;
}

// ─── Detail Modal ───
async function showDetail(symbol) {
    const stock = STOCKS.find(s => s.symbol === symbol);
    if (!stock) return;

    const score = calculateScore(stock);
    const breakdown = getScoreBreakdown(stock);
    const scoreClass = getScoreClass(score);
    const changeClass = stock.change >= 0 ? "positive" : "negative";

    const barColor = scoreClass === "excellent" ? "var(--accent-emerald)" :
                    scoreClass === "good" ? "var(--accent-cyan)" :
                    scoreClass === "average" ? "var(--accent-amber)" : "var(--accent-rose)";

    const scoreGradient = scoreClass === "excellent" ? "var(--gradient-score-excellent)" :
                         scoreClass === "good" ? "var(--gradient-score-good)" :
                         scoreClass === "average" ? "var(--gradient-score-average)" : "var(--gradient-score-weak)";

    document.getElementById("detail-title").textContent = `${stock.symbol} — ${stock.name}`;

    document.getElementById("detail-body").innerHTML = `
        <div class="detail-hero">
            <div class="detail-hero-left">
                <h2>${stock.symbol}</h2>
                <div class="detail-company">${stock.name}</div>
                <span class="cell-sector" style="margin-top:8px">${stock.sector}</span>
                ${stock.listedIn ? `<div style="margin-top:6px;font-size:0.75rem;color:var(--text-tertiary)">Listed in: ${stock.listedIn}</div>` : ''}
            </div>
            <div class="detail-hero-right">
                <div class="detail-price">${formatPrice(stock.price)}</div>
                <div class="detail-change ${changeClass}">${formatChange(stock.change)}</div>
            </div>
        </div>

        <div class="detail-section">
            <h4>Key Metrics</h4>
            <div class="detail-metrics-grid">
                <div class="detail-metric">
                    <div class="detail-metric-label">P/E Ratio (TTM)</div>
                    <div class="detail-metric-value">${stock.pe > 0 ? stock.pe.toFixed(2) : 'N/A'}</div>
                </div>
                <div class="detail-metric">
                    <div class="detail-metric-label">Dividend Yield</div>
                    <div class="detail-metric-value positive">${stock.divYield > 0 ? stock.divYield.toFixed(2) + '%' : 'N/A'}</div>
                </div>
                <div class="detail-metric">
                    <div class="detail-metric-label">1-Year Change</div>
                    <div class="detail-metric-value ${stock.yearChange >= 0 ? 'positive' : 'negative'}">${formatChange(stock.yearChange)}</div>
                </div>
                <div class="detail-metric">
                    <div class="detail-metric-label">Market Cap</div>
                    <div class="detail-metric-value">${formatMcap(stock.mcap)}</div>
                </div>
                <div class="detail-metric">
                    <div class="detail-metric-label">30D Avg Volume</div>
                    <div class="detail-metric-value">${formatVolume(stock.volume)}</div>
                </div>
                <div class="detail-metric">
                    <div class="detail-metric-label">Free Float</div>
                    <div class="detail-metric-value">${formatVolume(stock.freeFloat)}</div>
                </div>
            </div>
        </div>

        <div class="detail-section">
            <div class="detail-score-section">
                <div class="detail-score-header">
                    <div>
                        <h4 style="margin-bottom:4px">Investment Score</h4>
                        <div class="detail-score-label">${scoreClass.charAt(0).toUpperCase() + scoreClass.slice(1)} — Composite fundamental rating</div>
                    </div>
                    <div class="detail-score-total" style="background:${scoreGradient};-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text">${score}/100</div>
                </div>
                <div class="detail-score-bars" style="margin-top:16px">

                    <div class="score-row">
                        <span class="score-row-label">Valuation (P/E)</span>
                        <div class="score-row-bar"><div class="score-row-fill" style="width:${(breakdown.pe / 25) * 100}%;background:${barColor}"></div></div>
                        <span class="score-row-value">${breakdown.pe}/25</span>
                    </div>
                    <div class="score-row">
                        <span class="score-row-label">Dividend</span>
                        <div class="score-row-bar"><div class="score-row-fill" style="width:${(breakdown.dividend / 25) * 100}%;background:${barColor}"></div></div>
                        <span class="score-row-value">${breakdown.dividend}/25</span>
                    </div>
                    <div class="score-row">
                        <span class="score-row-label">1Y Growth</span>
                        <div class="score-row-bar"><div class="score-row-fill" style="width:${(breakdown.growth / 25) * 100}%;background:${barColor}"></div></div>
                        <span class="score-row-value">${breakdown.growth}/25</span>
                    </div>
                    <div class="score-row">
                        <span class="score-row-label">Liquidity</span>
                        <div class="score-row-bar"><div class="score-row-fill" style="width:${(breakdown.liquidity / 25) * 100}%;background:${barColor}"></div></div>
                        <span class="score-row-value">${breakdown.liquidity}/25</span>
                    </div>
                </div>
            </div>
        </div>

        <div id="company-profile-container">
            <div style="text-align:center; padding:32px 0;">
                <div class="loading-spinner" style="width:30px;height:30px;margin:0 auto 12px;">
                    <svg viewBox="0 0 50 50">
                        <circle cx="25" cy="25" r="20" fill="none" stroke="var(--accent-cyan)" stroke-width="4" stroke-linecap="round" stroke-dasharray="80 200">
                            <animateTransform attributeName="transform" type="rotate" from="0 25 25" to="360 25 25" dur="1s" repeatCount="indefinite"/>
                        </circle>
                    </svg>
                </div>
                <div style="color:var(--text-tertiary);font-size:0.85rem;">Loading company profile & news...</div>
            </div>
        </div>

        <div class="detail-section" style="text-align:center;padding-top:12px;">
            <a href="https://dps.psx.com.pk/company/${stock.symbol}" target="_blank" rel="noopener" class="btn btn-primary" style="text-decoration:none;">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6M15 3h6v6M10 14L21 3"/></svg>
                View on PSX Data Portal
            </a>
        </div>
    `;

    document.getElementById("detail-modal").style.display = "flex";

    // Fetch live company data
    try {
        const res = await fetch(`/api/company?symbol=${symbol}`);
        const result = await res.json();
        
        const container = document.getElementById("company-profile-container");
        if (!container) return; // Modal might have been closed
        
        if (result.success && result.data) {
            const data = result.data;
            
            let html = `<div class="company-bio">`;
            if (data.description) {
                html += `<h4>Business Description</h4><p>${data.description}</p>`;
            }
            
            html += `<div class="company-meta">`;
            if (data.address) {
                html += `<div class="meta-item"><label>Address</label><span>${data.address}</span></div>`;
            }
            if (data.website) {
                html += `<div class="meta-item"><label>Website</label><a href="${data.website}" target="_blank" rel="noopener">${data.website}</a></div>`;
            }
            if (data.people && data.people.length > 0) {
                const ceos = data.people.filter(p => p.role.includes("CEO") || p.role.includes("Chief")).map(p => p.name).join(", ");
                if (ceos) {
                    html += `<div class="meta-item"><label>Key People</label><span>${ceos}</span></div>`;
                }
            }
            html += `</div></div>`;
            
            if (data.revenueHistory && Object.keys(data.revenueHistory).length > 0) {
                const years = Object.keys(data.revenueHistory).sort();
                html += `<div class="company-financials" style="margin-top: 16px;">
                    <h4>
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="var(--accent-cyan)" stroke-width="2"><path d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"></path></svg>
                        Annual Revenue History
                    </h4>
                    <table class="data-table" style="margin-top:8px;">
                        <thead><tr><th>Year</th><th style="text-align:right">Revenue (PKR)</th></tr></thead>
                        <tbody>`;
                years.reverse().slice(0, 5).forEach(year => {
                    html += `<tr><td>${year}</td><td style="text-align:right">${formatCurrency(data.revenueHistory[year])}</td></tr>`;
                });
                html += `</tbody></table></div>`;
            }

            if (data.announcements && data.announcements.length > 0) {
                html += `<div class="news-timeline">
                    <h4>
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="var(--accent-cyan)" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="16" y1="13" x2="8" y2="13"></line><line x1="16" y1="17" x2="8" y2="17"></line><polyline points="10 9 9 9 8 9"></polyline></svg>
                        Live News & Announcements
                    </h4>`;
                
                data.announcements.slice(0, 5).forEach(news => {
                    html += `
                        <div class="news-item">
                            <div class="news-date">${news.date}</div>
                            <div class="news-title">${news.title}</div>
                            ${news.link ? `<a href="${news.link}" target="_blank" rel="noopener" class="news-link">
                                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="12" y1="18" x2="12" y2="12"></line><line x1="9" y1="15" x2="15" y2="15"></line></svg>
                                View PDF
                            </a>` : ''}
                        </div>
                    `;
                });
                
                html += `</div>`;
            }
            
            container.innerHTML = html;
        } else {
            container.innerHTML = `<div style="text-align:center;color:var(--text-tertiary);padding:16px;">Detailed profile not available.</div>`;
        }
    } catch (err) {
        console.error("Error fetching company data:", err);
        const container = document.getElementById("company-profile-container");
        if (container) {
            container.innerHTML = `<div style="text-align:center;color:var(--negative);padding:16px;">Failed to load company profile.</div>`;
        }
    }
}

// ─── Export CSV ───
function exportCSV() {
    const filtered = getFilteredStocks();
    const sorted = sortStocks(filtered);

    const headers = ["Symbol", "Company", "Sector", "Listed In", "Price (PKR)", "Change %", "1Y Change %", "Market Cap", "P/E (TTM)", "Div Yield %", "Free Float", "30D Avg Vol", "Score"];
    const rows = sorted.map(s => [
        s.symbol, `"${s.name}"`, `"${s.sector}"`, `"${s.listedIn || ''}"`, s.price, s.change?.toFixed(2), s.yearChange?.toFixed(2), s.mcap, s.pe?.toFixed(2), s.divYield?.toFixed(2), s.freeFloat, s.volume?.toFixed(0), s.score
    ]);

    let csv = headers.join(",") + "\n";
    rows.forEach(row => {
        csv += row.join(",") + "\n";
    });

    const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `psx_screener_live_${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
}

// ─── Presets ───
const PRESETS = {
    topvolume: { sector: "all", index: "all", mcap: "all", pe: 50, div: 0, change: -10, yearChange: -100, volume: 0 },
    bluechip: { sector: "all", index: "KSE100", mcap: "all", pe: 50, div: 0, change: -10, yearChange: -100, volume: 100000 },
    dividend: { sector: "all", index: "all", mcap: "all", pe: 50, div: 5, change: -10, yearChange: -100, volume: 0 },
    growth: { sector: "all", index: "all", mcap: "all", pe: 50, div: 0, change: -10, yearChange: 20, volume: 50000 },
    value: { sector: "all", index: "all", mcap: "all", pe: 10, div: 3, change: -10, yearChange: -100, volume: 10000 },
    momentum: { sector: "all", index: "all", mcap: "all", pe: 50, div: 0, change: 2, yearChange: -100, volume: 50000 },
};

function applyPreset(name) {
    const preset = PRESETS[name];
    if (!preset) return;

    document.getElementById("filter-sector").value = preset.sector;
    document.getElementById("filter-index").value = preset.index;
    document.getElementById("filter-mcap").value = preset.mcap;
    document.getElementById("filter-pe").value = preset.pe;
    document.getElementById("filter-div").value = preset.div;
    document.getElementById("filter-change").value = preset.change;
    document.getElementById("filter-year-change").value = preset.yearChange;
    document.getElementById("filter-volume").value = preset.volume;
    if (document.getElementById("filter-revenue")) document.getElementById("filter-revenue").value = "all";

    updateRangeLabels();

    if (name === "topvolume") {
        currentSort = { key: "volume", direction: "desc" };
        document.querySelectorAll(".sortable").forEach(h => h.classList.remove("sorted-asc", "sorted-desc"));
        const th = document.querySelector('[data-sort="volume"]');
        if (th) th.classList.add("sorted-desc");
    }

    renderAll();

    document.querySelectorAll(".preset-chip").forEach(c => c.classList.remove("active"));
    document.querySelector(`[data-preset="${name}"]`)?.classList.add("active");
    activePreset = name;
}

function resetFilters() {
    document.getElementById("filter-sector").value = "all";
    document.getElementById("filter-index").value = "all";
    document.getElementById("filter-mcap").value = "all";
    document.getElementById("filter-pe").value = 50;
    document.getElementById("filter-div").value = 0;
    document.getElementById("filter-change").value = -10;
    document.getElementById("filter-year-change").value = -100;
    document.getElementById("filter-volume").value = 0;
    searchQuery = "";
    document.getElementById("search-input").value = "";
    if (document.getElementById("filter-revenue")) document.getElementById("filter-revenue").value = "all";

    updateRangeLabels();
    renderAll();

    document.querySelectorAll(".preset-chip").forEach(c => c.classList.remove("active"));
    activePreset = null;
}

function updateRangeLabels() {
    const pe = document.getElementById("filter-pe").value;
    const div = document.getElementById("filter-div").value;
    const change = document.getElementById("filter-change").value;
    const yearChange = document.getElementById("filter-year-change").value;

    document.getElementById("pe-value").textContent = pe >= 50 ? "Any" : `≤ ${pe}`;
    document.getElementById("div-value").textContent = div <= 0 ? "Any" : `≥ ${div}%`;
    document.getElementById("change-value").textContent = change <= -10 ? "Any" : `≥ ${change}%`;
    document.getElementById("year-change-value").textContent = yearChange <= -100 ? "Any" : `≥ ${yearChange}%`;
}

// ─── Tab & Feature Deployment Status System ───
let appTabStatuses = {};

async function fetchAndApplyTabStatuses() {
    try {
        const res = await fetch("/api/tabs/status");
        const json = await res.json();
        if (json.success && json.tabs) {
            appTabStatuses = json.tabs;
            applyTabDeploymentBadges();
        }
    } catch (e) {
        console.error("Error loading tab statuses:", e);
    }
}

function applyTabDeploymentBadges() {
    const tabsList = Object.values(appTabStatuses || {});
    tabsList.forEach(tab => {
        const tabBtn = document.getElementById("tab-" + tab.id);
        const isOffline = tab.status === "OFFLINE";

        if (tabBtn) {
            if (isOffline) {
                tabBtn.classList.add("tab-offline-coming-soon");
                let badge = tabBtn.querySelector(".badge-tab-coming-soon");
                if (!badge) {
                    badge = document.createElement("span");
                    badge.className = "badge-tab-coming-soon";
                    badge.textContent = "COMING SOON";
                    tabBtn.appendChild(badge);
                }
            } else {
                tabBtn.classList.remove("tab-offline-coming-soon");
                const badge = tabBtn.querySelector(".badge-tab-coming-soon");
                if (badge) badge.remove();
            }
        }

        // Top Header modules
        if (tab.id === "upper-lock") {
            const btn = document.getElementById("btn-upper-lock");
            if (btn) {
                if (isOffline) {
                    btn.classList.add("tab-offline-coming-soon");
                    btn.title = "Upper Lock (Coming Soon)";
                } else {
                    btn.classList.remove("tab-offline-coming-soon");
                    btn.title = "Upper Lock Analysis";
                }
            }
        } else if (tab.id === "stock-history") {
            const btn = document.getElementById("btn-stock-history");
            if (btn) {
                if (isOffline) {
                    btn.classList.add("tab-offline-coming-soon");
                    btn.title = "Stock Trends (Coming Soon)";
                } else {
                    btn.classList.remove("tab-offline-coming-soon");
                    btn.title = "Stock History & Trends";
                }
            }
        }
    });
}

function renderComingSoonTab(view) {
    const tabCfg = appTabStatuses[view] || {
        name: view.replace('-', ' ').toUpperCase(),
        icon: "🚀",
        message: "This feature is currently undergoing final verification and will be deployed online shortly.",
        eta: "Coming Soon"
    };

    return `
    <div class="coming-soon-tab-container">
        <div class="coming-soon-card">
            <div class="coming-soon-badge-top">⏳ FEATURE COMING SOON</div>
            <div class="coming-soon-icon">${tabCfg.icon || '🚀'}</div>
            <h2 class="coming-soon-title">${tabCfg.name}</h2>
            <p class="coming-soon-desc">${tabCfg.message || 'This algorithmic module is being prepared for high-performance online deployment.'}</p>
            <div class="coming-soon-eta-pill">
                <span>Target Launch:</span>
                <strong style="color: #34d399;">${tabCfg.eta || 'Coming Soon'}</strong>
            </div>
            <div class="coming-soon-actions">
                <button class="btn btn-primary" onclick="switchToPaywallModal()">⚡ Get Early Access Notification</button>
                <button class="btn btn-ghost" onclick="switchView('table')">📊 Back to Market Screener</button>
            </div>
        </div>
    </div>`;
}

// ─── View Switching ───
function switchView(view) {
    currentView = view;
    document.querySelectorAll(".view-tab").forEach(t => t.classList.remove("active"));
    document.querySelectorAll(".mobile-nav-item").forEach(t => t.classList.remove("active"));

    const activeTab = document.querySelector(`[data-view="${view}"]`);
    if (activeTab) activeTab.classList.add("active");

    const activeMobileTab = document.querySelector(`.mobile-nav-item[data-view="${view}"]`);
    if (activeMobileTab) activeMobileTab.classList.add("active");

    const views = ["table", "cards", "weekly-scan", "live-trading", "simulator", "corporate", "financials", "trading-intelligence", "intelligence", "longterm"];
    views.forEach(v => {
        const el = document.getElementById(`view-${v}`);
        if (el) el.style.display = (view === v) ? (v === "cards" ? "grid" : "block") : "none";
    });

    // Auto collapse mobile search & options overlay when changing tabs
    document.getElementById("search-container")?.classList.remove("mobile-active");
    document.getElementById("screener-controls")?.classList.remove("expanded");

    // Show screener filters & market overview cards ONLY on main Dashboard screener views (table, cards)
    const isScreenerView = ["table", "cards"].includes(view);

    const marketOverview = document.getElementById("market-overview");
    const screenerControls = document.getElementById("screener-controls");
    const searchContainer = document.getElementById("search-container");

    if (marketOverview) marketOverview.style.display = isScreenerView ? "grid" : "none";
    if (screenerControls) screenerControls.style.display = isScreenerView ? "block" : "none";
    if (searchContainer) searchContainer.style.display = isScreenerView ? "flex" : "none";

    // Check if this tab is marked COMING SOON / OFFLINE
    if (appTabStatuses[view] && appTabStatuses[view].status === "OFFLINE") {
        const targetViewEl = document.getElementById(`view-${view}`);
        if (targetViewEl) {
            targetViewEl.innerHTML = renderComingSoonTab(view);
        }
        return;
    }

    // View specific initializations
    if (view === "weekly-scan") {
        loadWeeklyScanData();
    } else if (view === "live-trading") {
        if (!currentLiveSymbol) currentLiveSymbol = "UNITY";
        fetchLiveTradingAnalysis(currentLiveSymbol);
    } else if (view === "simulator") {
        initTradingSimulator();
    } else if (view === "corporate") {
        fetchCorporateActionsData();
    } else if (view === "financials") {
        const symInput = document.getElementById("fin-symbol-input");
        const symbol = symInput ? (symInput.value.trim() || "UNITY") : "UNITY";
        fetchFinancialStatements(symbol);
    } else if (view === "trading-intelligence") {
        const isLocalHost = window.location.hostname === "localhost" || 
                            window.location.hostname === "127.0.0.1" || 
                            window.location.hostname === "0.0.0.0" || 
                            window.location.hostname.startsWith("192.168.");
        if (!isLocalHost) {
            switchView("table");
            return;
        }
        runTradingIntelligenceEngine();
    } else if (view === "intelligence") {
        if (typeof intelligenceTab !== "undefined" && intelligenceTab.load) {
            intelligenceTab.load();
        }
        if (typeof calibrationReport !== "undefined" && calibrationReport.load) {
            setTimeout(() => calibrationReport.load(), 500);
        }
    } else if (view === "longterm") {
        if (typeof longtermTab !== "undefined" && longtermTab.load) {
            longtermTab.load();
        }
    }
}

// ─── Event Listeners ───
function initEventListeners() {
    // Filters
    const filterElements = ["filter-sector", "filter-index", "filter-mcap", "filter-pe", "filter-div", "filter-change", "filter-year-change", "filter-volume", "filter-revenue"];
    filterElements.forEach(id => {
        const el = document.getElementById(id);
        if (el) {
            el.addEventListener("input", () => {
                updateRangeLabels();
                renderAll();
                document.querySelectorAll(".preset-chip").forEach(c => c.classList.remove("active"));
                activePreset = null;
            });
        }
    });

    // Search
    const searchInput = document.getElementById("search-input");
    searchInput.addEventListener("input", (e) => {
        searchQuery = e.target.value;
        renderAll();
    });

    // Ctrl+K shortcut
    document.addEventListener("keydown", (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === "k") {
            e.preventDefault();
            searchInput.focus();
        }
    });

    // Refresh button (forced immediate live sync)
    document.getElementById("btn-refresh").addEventListener("click", async () => {
        try { await fetch('/api/refresh', { method: 'POST' }); } catch(e) {}
        fetchLiveData(false, true);
        const ulModal = document.getElementById("upper-lock-modal");
        if (ulModal && ulModal.classList.contains("active")) {
            openUpperLockAnalysis(true);
        }
        if (currentView === "weekly-scan") {
            triggerWeeklyManualRescan();
        }
    });



    // Reset
    document.getElementById("btn-reset-filters").addEventListener("click", resetFilters);

    // Presets
    document.querySelectorAll(".preset-chip").forEach(chip => {
        chip.addEventListener("click", () => {
            const preset = chip.dataset.preset;
            if (activePreset === preset) {
                resetFilters();
            } else {
                applyPreset(preset);
            }
        });
    });

    // View tabs
    document.querySelectorAll(".view-tab").forEach(tab => {
        tab.addEventListener("click", () => switchView(tab.dataset.view));
    });

    // Sort columns
    document.querySelectorAll(".sortable").forEach(th => {
        th.addEventListener("click", () => {
            const key = th.dataset.sort;
            if (currentSort.key === key) {
                currentSort.direction = currentSort.direction === "asc" ? "desc" : "asc";
            } else {
                currentSort.key = key;
                currentSort.direction = "desc";
            }
            document.querySelectorAll(".sortable").forEach(h => {
                h.classList.remove("sorted-asc", "sorted-desc");
            });
            th.classList.add(currentSort.direction === "asc" ? "sorted-asc" : "sorted-desc");
            renderAll();
        });
    });

    // Watchlist star (delegation)
    document.addEventListener("click", (e) => {
        const starBtn = e.target.closest("[data-star]");
        if (starBtn) {
            e.stopPropagation();
            toggleWatchlist(starBtn.dataset.star);
        }
    });

    // Row click for detail
    document.addEventListener("click", (e) => {
        const row = e.target.closest("tr[data-symbol], .stock-card[data-symbol]");
        if (row && !e.target.closest("[data-star]")) {
            showDetail(row.dataset.symbol);
        }
    });


    // Watchlist modal
    document.getElementById("btn-watchlist-toggle").addEventListener("click", () => {
        renderWatchlistModal();
        document.getElementById("watchlist-modal").style.display = "flex";
    });
    document.getElementById("btn-close-watchlist").addEventListener("click", () => {
        document.getElementById("watchlist-modal").style.display = "none";
    });

    // Watchlist remove (delegation)
    document.addEventListener("click", (e) => {
        const removeBtn = e.target.closest("[data-remove]");
        if (removeBtn) {
            toggleWatchlist(removeBtn.dataset.remove);
            renderWatchlistModal();
        }
    });

    // Detail modal close
    document.getElementById("btn-close-detail").addEventListener("click", () => {
        document.getElementById("detail-modal").style.display = "none";
    });

    // Modal overlay click to close
    document.querySelectorAll(".modal-overlay").forEach(modal => {
        modal.addEventListener("click", (e) => {
            if (e.target === modal) modal.style.display = "none";
        });
    });

    // Escape key closes modals
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") {
            document.querySelectorAll(".modal-overlay").forEach(m => m.style.display = "none");
            closeUpperLockModal();
        }
    });

    // Export
    document.getElementById("btn-export").addEventListener("click", exportCSV);

    // Upper Lock Analysis
    document.getElementById("btn-upper-lock").addEventListener("click", () => openUpperLockAnalysis(false));
    document.getElementById("upper-lock-refresh")?.addEventListener("click", () => openUpperLockAnalysis(true));
    document.getElementById("upper-lock-close").addEventListener("click", closeUpperLockModal);
    document.getElementById("upper-lock-modal").addEventListener("click", (e) => {
        if (e.target.id === "upper-lock-modal") closeUpperLockModal();
    });
    document.getElementById("upper-lock-sort").addEventListener("change", (e) => {
        if (upperLockData) renderUpperLockResults(upperLockData, e.target.value);
    });


    // Live Trading Header Button
    const liveHeaderBtn = document.getElementById("btn-live-trading-header");
    if (liveHeaderBtn) {
        liveHeaderBtn.addEventListener("click", () => switchView("live-trading"));
    }

    // Live Trading Search & Chips
    const liveSearchBtn = document.getElementById("btn-live-search");
    const liveSearchInput = document.getElementById("live-search-input");
    if (liveSearchBtn) {
        liveSearchBtn.addEventListener("click", searchLiveTrading);
    }
    if (liveSearchInput) {
        liveSearchInput.addEventListener("keydown", (e) => {
            if (e.key === "Enter") searchLiveTrading();
        });
    }

    document.querySelectorAll(".quick-chip").forEach(chip => {
        chip.addEventListener("click", () => {
            const sym = chip.dataset.symbol;
            if (sym) {
                document.getElementById("live-search-input").value = sym;
                fetchLiveTradingAnalysis(sym);
            }
        });
    });

    // Simulator Listeners
    document.getElementById("btn-sim-load-stock")?.addEventListener("click", loadSimQuote);
    document.getElementById("btn-execute-order")?.addEventListener("click", executeSimOrder);
    document.getElementById("btn-reset-sim-portfolio")?.addEventListener("click", resetSimPortfolio);
    document.getElementById("btn-refresh-sim-prices")?.addEventListener("click", refreshSimPrices);
    document.getElementById("sim-order-action")?.addEventListener("change", updateSimTicketUI);
    document.getElementById("sim-order-type")?.addEventListener("change", updateSimTicketUI);
    document.getElementById("sim-quantity-input")?.addEventListener("input", updateSimEstTotal);
    document.getElementById("sim-price-input")?.addEventListener("input", updateSimEstTotal);

    document.querySelectorAll(".market-type-btn").forEach(btn => {
        btn.addEventListener("click", (e) => {
            document.querySelectorAll(".market-type-btn").forEach(b => b.classList.remove("active"));
            e.target.classList.add("active");
            simCurrentMarketType = e.target.dataset.market;
            updateSimTicketUI();
        });
    });

    // Corporate Action Calculators Listeners
    document.getElementById("btn-calc-bonus")?.addEventListener("click", calcBonusShares);
    document.getElementById("btn-calc-right")?.addEventListener("click", calcRightShares);
    document.getElementById("btn-calc-split")?.addEventListener("click", calcStockSplit);

    // Financial Statements Search Listener
    document.getElementById("btn-fin-search")?.addEventListener("click", () => {
        const input = document.getElementById("fin-symbol-input");
        if (input && input.value.trim()) {
            fetchFinancialStatements(input.value.trim().toUpperCase());
        }
    });
    document.getElementById("fin-symbol-input")?.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && e.target.value.trim()) {
            fetchFinancialStatements(e.target.value.trim().toUpperCase());
        }
    });

    // Mobile Collapsible Filter Toggle & Floating Search Button
    document.getElementById("btn-filter-toggle")?.addEventListener("click", () => {
        const controls = document.getElementById("screener-controls");
        if (controls) controls.classList.toggle("expanded");
    });

    document.getElementById("mobile-float-search-btn")?.addEventListener("click", () => {
        const searchBox = document.getElementById("search-container");
        const controls = document.getElementById("screener-controls");
        if (searchBox) searchBox.classList.toggle("mobile-active");
        if (controls) controls.classList.toggle("expanded");
    });

    // Auto collapse search on search input enter or select
    document.getElementById("search-input")?.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
            document.getElementById("search-container")?.classList.remove("mobile-active");
            document.getElementById("screener-controls")?.classList.remove("expanded");
        }
    });

    // Stock History modal
    document.getElementById('btn-stock-history')?.addEventListener('click', openStockHistory);
    document.getElementById('stock-history-close')?.addEventListener('click', closeStockHistory);
    document.getElementById('stock-history-modal')?.addEventListener('click', e => {
        if (e.target.id === 'stock-history-modal') closeStockHistory();
    });
    document.getElementById('history-search-btn')?.addEventListener('click', searchStockHistory);
    document.getElementById('history-search-input')?.addEventListener('keydown', e => {
        if (e.key === 'Enter') searchStockHistory();
    });
}

// ─── Upper Lock Analysis ───
let upperLockData = null;
let activeUpperLockTab = 'scanner';
let activeAuditFilter = 'all';

function switchUpperLockTab(tabName) {
    activeUpperLockTab = tabName;
    document.querySelectorAll(".upper-lock-tab-btn").forEach(btn => btn.classList.remove("active"));
    const activeBtn = document.getElementById(tabName === "audit" ? "tab-ul-audit" : "tab-ul-scanner");
    if (activeBtn) activeBtn.classList.add("active");

    const scannerView = document.getElementById("upper-lock-view-scanner");
    const auditView = document.getElementById("upper-lock-view-audit");

    if (tabName === "audit") {
        if (scannerView) scannerView.style.display = "none";
        if (auditView) {
            auditView.style.display = "block";
            if (upperLockData && upperLockData.audit) {
                renderUpperLockAudit(upperLockData.audit, activeAuditFilter);
            }
        }
    } else {
        if (auditView) auditView.style.display = "none";
        if (scannerView) scannerView.style.display = "block";
    }
}

function openUpperLockAnalysis(force = false) {
    const modal = document.getElementById("upper-lock-modal");
    modal.classList.add("active");
    document.body.style.overflow = "hidden";

    const refreshBtn = document.getElementById("upper-lock-refresh");
    const refreshIcon = refreshBtn?.querySelector(".refresh-icon-spin");
    if (refreshIcon && force) {
        refreshIcon.style.animation = "spin 1s linear infinite";
    }

    // Show loading, hide results
    document.getElementById("upper-lock-loading").style.display = "flex";
    document.getElementById("upper-lock-results").style.display = "none";
    document.getElementById("upper-lock-sort").value = "probability";

    // Fetch analysis with optional force refresh
    const deviceId = getDeviceId();
    const url = `/api/upper-lock-analysis?deviceId=${deviceId}` + (force ? '&force=1' : '');
    fetch(url)
        .then(r => {
            if (r.status === 402) {
                initTrialSystem();
                throw new Error("3-Day Free Trial Expired. Upgrade to Pro.");
            }
            return r.json();
        })
        .then(data => {
            if (refreshIcon) refreshIcon.style.animation = "";
            if (data.success) {
                upperLockData = data;
                
                // Update badge in tab
                const auditBadge = document.getElementById("ul-audit-badge");
                if (auditBadge && data.audit) {
                    auditBadge.textContent = `${data.audit.hitRate}% Win Rate`;
                }

                // Render current view
                renderUpperLockResults(data, "probability");
                if (data.audit && activeUpperLockTab === 'audit') {
                    renderUpperLockAudit(data.audit, activeAuditFilter);
                }
            } else {
                showUpperLockError(data.error || "Failed to analyze stocks");
            }
        })
        .catch(err => {
            if (refreshIcon) refreshIcon.style.animation = "";
            showUpperLockError(err.message);
        });
}

function renderUpperLockAudit(auditData, filter = 'all') {
    activeAuditFilter = filter;
    const container = document.getElementById("upper-lock-view-audit");
    if (!container) return;

    if (!auditData || !auditData.predictions || auditData.predictions.length === 0) {
        container.innerHTML = `
            <div class="upper-lock-empty" style="padding: 40px 20px; text-align: center;">
                <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                    <circle cx="12" cy="12" r="10"/><path d="M12 8v4"/><path d="M12 16h.01"/>
                </svg>
                <p>No historical predictions recorded for audit yet.<br>Today's candidates have been logged and will be audited automatically.</p>
            </div>`;
        return;
    }

    const {
        predictionDate,
        evaluationDate,
        totalAudited,
        hitsCount,
        nearHitsCount,
        partialCount,
        missesCount,
        hitRate,
        lockHitRate,
        avgReturn,
        predictions
    } = auditData;

    // Filter predictions
    let filtered = predictions;
    if (filter === "hit") filtered = predictions.filter(p => p.outcome === "HIT");
    else if (filter === "near-hit") filtered = predictions.filter(p => p.outcome === "NEAR_HIT");
    else if (filter === "missed") filtered = predictions.filter(p => p.outcome === "MISSED");

    let html = `
        <!-- Audit KPI Summary Grid -->
        <div class="audit-kpi-grid">
            <div class="audit-kpi-card highlight">
                <span class="audit-kpi-label">🎯 Prediction Win Rate</span>
                <span class="audit-kpi-value" style="color: #a5b4fc;">${hitRate}%</span>
                <span class="audit-kpi-sub">${hitsCount + nearHitsCount} of ${totalAudited} locked or hit high gains</span>
            </div>
            <div class="audit-kpi-card success">
                <span class="audit-kpi-label">🔒 Exact Locks Hit</span>
                <span class="audit-kpi-value" style="color: #4ade80;">${hitsCount}</span>
                <span class="audit-kpi-sub">${lockHitRate}% achieved full circuit limit</span>
            </div>
            <div class="audit-kpi-card">
                <span class="audit-kpi-label">🟢 Near Miss / Strong Rally</span>
                <span class="audit-kpi-value" style="color: #38bdf8;">${nearHitsCount}</span>
                <span class="audit-kpi-sub">Gained +4.0% to +9.5%</span>
            </div>
            <div class="audit-kpi-card">
                <span class="audit-kpi-label">📈 Average Candidate Move</span>
                <span class="audit-kpi-value" style="color: ${avgReturn >= 0 ? '#4ade80' : '#f87171'};">${avgReturn >= 0 ? '+' : ''}${avgReturn}%</span>
                <span class="audit-kpi-sub">Across all audited predictions</span>
            </div>
        </div>

        <!-- Filter Bar -->
        <div class="audit-filter-bar">
            <div class="audit-chips">
                <button class="audit-chip ${filter === 'all' ? 'active' : ''}" onclick="renderUpperLockAudit(upperLockData.audit, 'all')">
                    All Predictions (${totalAudited})
                </button>
                <button class="audit-chip ${filter === 'hit' ? 'active' : ''}" onclick="renderUpperLockAudit(upperLockData.audit, 'hit')">
                    🔒 Circuit Locks Hit (${hitsCount})
                </button>
                <button class="audit-chip ${filter === 'near-hit' ? 'active' : ''}" onclick="renderUpperLockAudit(upperLockData.audit, 'near-hit')">
                    🟢 Strong Gains (+4%+) (${nearHitsCount})
                </button>
                <button class="audit-chip ${filter === 'missed' ? 'active' : ''}" onclick="renderUpperLockAudit(upperLockData.audit, 'missed')">
                    🛑 Missed / Pullbacks (${missesCount})
                </button>
            </div>
            <div style="font-size: 0.8rem; color: var(--text-muted);">
                📅 Predicted on: <strong>${predictionDate}</strong> • Audited on: <strong>${evaluationDate}</strong>
            </div>
        </div>

        <!-- List of Audited Cards -->
        <div class="audit-cards-list">`;

    if (filtered.length === 0) {
        html += `<div class="upper-lock-empty" style="padding: 24px;"><p>No candidates match this filter.</p></div>`;
    } else {
        filtered.forEach(p => {
            const outcomeClass = p.outcome === 'HIT' ? 'hit' : p.outcome === 'NEAR_HIT' ? 'near-hit' : p.outcome === 'PARTIAL_GAIN' ? 'partial' : 'missed';
            const outcomeIcon = p.outcome === 'HIT' ? '✅' : p.outcome === 'NEAR_HIT' ? '🟢' : p.outcome === 'PARTIAL_GAIN' ? '🟡' : '🛑';
            const outcomeLabel = p.outcome === 'HIT' ? 'LOCKED' : p.outcome === 'NEAR_HIT' ? 'NEAR LOCK' : p.outcome === 'PARTIAL_GAIN' ? 'POSITIVE' : 'MISSED';

            html += `
                <div class="audit-card ${outcomeClass}" onclick="closeUpperLockModal(); showStockDetail('${p.symbol}')" title="Click to view full chart for ${p.symbol}">
                    <div style="text-align: center;">
                        <span class="audit-badge ${outcomeClass}">${outcomeIcon} ${outcomeLabel}</span>
                    </div>
                    <div>
                        <div style="font-weight: 700; font-size: 1rem; color: var(--text-primary);">${p.symbol}</div>
                        <div style="font-size: 0.78rem; color: var(--text-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 170px;">${p.name}</div>
                        <div style="font-size: 0.72rem; color: #818cf8; margin-top: 2px;">${p.sector}</div>
                    </div>
                    <div>
                        <div style="font-size: 0.75rem; color: var(--text-muted);">Confidence</div>
                        <div style="font-weight: 700; font-size: 0.95rem; color: ${p.probability >= 75 ? '#4ade80' : '#fbbf24'};">${p.probability}% Prob</div>
                        <div style="font-size: 0.72rem; color: var(--text-secondary); margin-top: 2px;">Base: ₨${p.predictedPrice.toFixed(2)}</div>
                    </div>
                    <div>
                        <div style="font-size: 0.75rem; color: var(--text-muted);">Actual Outcome</div>
                        <div style="font-weight: 800; font-size: 1.05rem; color: ${p.actualChange >= 0 ? '#4ade80' : '#f87171'};">
                            ${p.actualChange >= 0 ? '+' : ''}${p.actualChange.toFixed(2)}%
                        </div>
                        <div style="font-size: 0.72rem; color: var(--text-secondary); margin-top: 2px;">Price: ₨${p.actualPrice.toFixed(2)}</div>
                    </div>
                    <div>
                        <div style="font-size: 0.75rem; color: var(--text-muted);">Audit Verdict</div>
                        <div style="font-size: 0.8rem; color: var(--text-primary); line-height: 1.35; margin-top: 2px;">${p.notes}</div>
                    </div>
                </div>`;
        });
    }

    html += `</div>`;
    container.innerHTML = html;
}

function closeUpperLockModal() {
    const modal = document.getElementById("upper-lock-modal");
    modal.classList.remove("active");
    document.body.style.overflow = "";
}


function showUpperLockError(message) {
    document.getElementById("upper-lock-loading").style.display = "none";
    const results = document.getElementById("upper-lock-results");
    results.style.display = "block";
    results.innerHTML = `
        <div class="upper-lock-empty">
            <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>
            </svg>
            <p>${message}</p>
        </div>`;
}

function renderUpperLockResults(data, sortBy) {
    document.getElementById("upper-lock-loading").style.display = "none";
    const results = document.getElementById("upper-lock-results");
    results.style.display = "block";

    // Sort predicted stocks
    let predicted = [...data.predicted];
    switch (sortBy) {
        case "change": predicted.sort((a, b) => b.change - a.change); break;
        case "volume": predicted.sort((a, b) => b.volume - a.volume); break;
        case "mcap": predicted.sort((a, b) => b.mcap - a.mcap); break;
        default: predicted.sort((a, b) => b.probability - a.probability);
    }

    let html = "";

    // ─── Yesterday's Upper Lock Section ───
    const yesterdayLocked = data.yesterdayLocked || [];
    const yesterdayDate = data.yesterdayDate || "N/A";
    html += `<div class="upper-lock-section">
        <h3 class="upper-lock-section-title yesterday-section">
            <span class="section-icon">📅</span>
            Yesterday's Upper Lock ${yesterdayDate ? `<span class="section-date">(${yesterdayDate})</span>` : ""}
            <span class="section-badge">${yesterdayLocked.length}</span>
        </h3>`;

    if (yesterdayLocked.length === 0) {
        html += `<div class="upper-lock-empty">
            <p>No historical data yet — will populate after the first trading session</p>
        </div>`;
    } else {
        html += `<div class="locked-stocks-row">`;
        yesterdayLocked.forEach(s => {
            html += renderLockedCard(s, "yesterday");
        });
        html += `</div>`;
    }
    html += `</div>`;

    // ─── Today's Upper Lock Section ───
    const todayLocked = data.todayLocked || [];
    html += `<div class="upper-lock-section">
        <h3 class="upper-lock-section-title locked-section">
            <span class="section-icon">🟢</span>
            Today's Upper Lock
            <span class="section-badge">${todayLocked.length}</span>
        </h3>`;

    if (todayLocked.length === 0) {
        html += `<div class="upper-lock-empty">
            <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" opacity="0.5">
                <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0110 0v4"/>
            </svg>
            <p>No stocks at upper lock right now</p>
        </div>`;
    } else {
        html += `<div class="locked-stocks-row">`;
        todayLocked.forEach(s => {
            html += renderLockedCard(s, "today");
        });
        html += `</div>`;
    }
    html += `</div>`;

    // ─── Predicted Section ───
    html += `<div class="upper-lock-section">
        <h3 class="upper-lock-section-title predicted-section">
            <span class="section-icon">🔮</span>
            Predicted to Hit Upper Lock Today
            <span class="section-badge">${predicted.length}</span>
        </h3>`;

    if (predicted.length === 0) {
        html += `<div class="upper-lock-empty">
            <p>No stocks predicted for upper lock</p>
        </div>`;
    } else {
        html += `<div class="predicted-stocks-list">`;
        predicted.forEach((s, i) => {
            const probColor = s.probability >= 70 ? "#22c55e" : s.probability >= 40 ? "#f59e0b" : "#ef4444";
            html += `<div class="predicted-stock-card" onclick="closeUpperLockModal(); showStockDetail('${s.symbol}')" style="cursor:pointer" title="Click to view ${s.symbol} details">
                <div class="predicted-rank" style="color: ${probColor}">#${i + 1}</div>
                <div class="predicted-info">
                    <div class="predicted-symbol-row">
                        <span class="predicted-symbol">${s.symbol}</span>
                        <span class="predicted-sector-badge">${s.sector}</span>
                    </div>
                    <div class="predicted-name">${s.name}</div>
                </div>
                <div class="predicted-metrics">
                    <div class="predicted-metric">
                        <span class="pm-label">Price</span>
                        <span class="pm-value">₨${s.price.toLocaleString("en-PK", { minimumFractionDigits: 2 })}</span>
                    </div>
                    <div class="predicted-metric">
                        <span class="pm-label">Change</span>
                        <span class="pm-value ${s.change >= 0 ? "positive" : "negative"}">${s.change >= 0 ? "+" : ""}${s.change.toFixed(2)}%</span>
                    </div>
                    <div class="predicted-metric">
                        <span class="pm-label">Volume</span>
                        <span class="pm-value">${formatVolume(s.volume)}</span>
                    </div>
                </div>
                <div class="predicted-probability">
                    <div class="probability-header">
                        <span class="probability-label">Probability</span>
                        <span class="probability-value" style="color: ${probColor}">${s.probability}%</span>
                    </div>
                    <div class="probability-bar-container">
                        <div class="probability-bar" style="width: ${s.probability}%; background: linear-gradient(90deg, #f59e0b, ${probColor})"></div>
                    </div>
                </div>
                <div class="predicted-reasons">
                    ${s.reasons.map(r => `<span class="reason-chip">${getReasonIcon(r)} ${r}</span>`).join("")}
                </div>
            </div>`;
        });
        html += `</div>`;
    }
    html += `</div>`;

    // Analysis summary
    html += `<div class="upper-lock-summary">
        <span>📊 Analyzed <strong>${data.totalAnalyzed}</strong> stocks</span>
        <span>•</span>
        <span>📅 <strong>${yesterdayLocked.length}</strong> locked yesterday</span>
        <span>•</span>
        <span>🟢 <strong>${todayLocked.length}</strong> locked today</span>
        <span>•</span>
        <span>🔮 <strong>${predicted.length}</strong> predicted</span>
        ${data.lastUpdated ? `<span>•</span><span>🕒 Live as of: <strong>${new Date(data.lastUpdated).toLocaleTimeString([], {hour: '2-digit', minute:'2-digit', second:'2-digit'})}</strong></span>` : ""}
    </div>`;


    results.innerHTML = html;
}

function renderLockedCard(s, type) {
    const typeLabel = type === "yesterday" ? "WAS LOCKED" : "🔒 LOCKED";
    const typeClass = type === "yesterday" ? "yesterday" : "today";
    return `<div class="locked-stock-card ${typeClass}" onclick="closeUpperLockModal(); showStockDetail('${s.symbol}')" style="cursor:pointer" title="Click to view ${s.symbol} details">
        <div class="locked-card-header">
            <span class="locked-symbol">${s.symbol}</span>
            <span class="locked-badge ${typeClass}">${typeLabel}</span>
        </div>
        <div class="locked-card-name">${s.name}</div>
        <div class="locked-card-sector">${s.sector}</div>
        <div class="locked-card-metrics">
            <div class="locked-metric">
                <span class="metric-label">Price</span>
                <span class="metric-value">₨${s.price.toLocaleString("en-PK", { minimumFractionDigits: 2 })}</span>
            </div>
            <div class="locked-metric">
                <span class="metric-label">Change</span>
                <span class="metric-value positive">+${s.change.toFixed(2)}%</span>
            </div>
            <div class="locked-metric">
                <span class="metric-label">Volume</span>
                <span class="metric-value">${formatVolume(s.volume)}</span>
            </div>
            <div class="locked-metric">
                <span class="metric-label">Lock Level</span>
                <span class="metric-value">${s.lockLevel}%</span>
            </div>
        </div>
    </div>`;
}

// Helper: show stock detail (delegates to existing detail modal)
function showStockDetail(symbol) {
    const stock = STOCKS.find(s => s.symbol === symbol);
    if (stock) {
        // Trigger existing detail view
        const row = document.querySelector(`tr[data-symbol="${symbol}"]`);
        if (row) row.click();
    }
}

function getReasonIcon(reason) {
    if (reason.includes("momentum") || reason.includes("Momentum")) return "📈";
    if (reason.includes("volume") || reason.includes("Volume")) return "📊";
    if (reason.includes("free float") || reason.includes("Free float")) return "🔄";
    if (reason.includes("Sector") || reason.includes("sector")) return "🏭";
    if (reason.includes("cap") || reason.includes("Cap")) return "💎";
    if (reason.includes("yearly") || reason.includes("Yearly") || reason.includes("year")) return "🚀";
    return "⚡";
}

function formatVolume(vol) {
    if (vol >= 1e9) return (vol / 1e9).toFixed(1) + "B";
    if (vol >= 1e6) return (vol / 1e6).toFixed(1) + "M";
    if (vol >= 1e3) return (vol / 1e3).toFixed(1) + "K";
    return vol.toFixed(0);
}

// ─── Stock History / Trend Analysis ───
let historyModalOpen = false;

function openStockHistory() {
    const modal = document.getElementById('stock-history-modal');
    modal.classList.add('active');
    document.body.style.overflow = 'hidden';
    historyModalOpen = true;
    // Focus search input
    setTimeout(() => document.getElementById('history-search-input').focus(), 300);
}

function closeStockHistory() {
    const modal = document.getElementById('stock-history-modal');
    modal.classList.remove('active');
    document.body.style.overflow = '';
    historyModalOpen = false;
}

function searchStockHistory() {
    const input = document.getElementById('history-search-input');
    const symbol = input ? input.value.trim().toUpperCase() : "";
    if (!symbol) return;
    
    const resultsDiv = document.getElementById('history-results');
    const loadingDiv = document.getElementById('history-loading');
    
    if (loadingDiv) loadingDiv.style.display = 'flex';
    if (resultsDiv) resultsDiv.style.display = 'none';
    
    const deviceId = getDeviceId();
    fetch(`/api/stock-history/${symbol}?deviceId=${deviceId}`)
        .then(r => {
            if (r.status === 402) {
                initTrialSystem();
                throw new Error("Trial expired");
            }
            return r.json();
        })
        .then(data => {
            if (loadingDiv) loadingDiv.style.display = 'none';
            if (resultsDiv) resultsDiv.style.display = 'block';
            if (data.success) {
                renderStockHistory(data);
            } else {
                if (resultsDiv) {
                    resultsDiv.innerHTML = `<div class="upper-lock-empty">
                        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                            <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>
                        </svg>
                        <p>${data.error || 'Stock not found'}</p>
                        <p style="font-size: 0.8rem; color: var(--text-tertiary)">Try symbols like OGDC, HBL, ENGRO, UNITY, LUCK</p>
                    </div>`;
                }
            }
        })
        .catch(err => {
            if (loadingDiv) loadingDiv.style.display = 'none';
            if (err.message !== "Trial expired" && resultsDiv) {
                resultsDiv.style.display = 'block';
                resultsDiv.innerHTML = `<div class="upper-lock-empty"><p>Network error: ${err.message}</p></div>`;
            }
        });
}

function renderStockHistory(data) {
    const resultsDiv = document.getElementById('history-results');
    const days = data.days;
    
    // Find the stock name from our cache
    const stockInfo = STOCKS.find(s => s.symbol === data.symbol);
    const stockName = stockInfo ? stockInfo.name : data.symbol;
    const stockSector = stockInfo ? stockInfo.sector : '';
    
    // Calculate summary stats
    const avgVolume = days.reduce((sum, d) => sum + d.volume, 0) / days.length;
    const highestClose = Math.max(...days.map(d => d.close));
    const lowestClose = Math.min(...days.map(d => d.close));
    const latestClose = days[0] ? days[0].close : 0;
    const oldestClose = days[days.length - 1] ? days[days.length - 1].close : 0;
    const periodChange = oldestClose > 0 ? ((latestClose - oldestClose) / oldestClose * 100) : 0;
    
    let html = '';
    
    // Stock header card
    html += `<div class="history-stock-header">
        <div class="history-stock-info">
            <div class="history-stock-symbol">${data.symbol}</div>
            <div class="history-stock-name">${stockName}</div>
            ${stockSector ? `<div class="history-stock-sector">${stockSector}</div>` : ''}
        </div>
        <div class="history-summary-stats">
            <div class="history-stat">
                <span class="history-stat-label">Period Change</span>
                <span class="history-stat-value ${periodChange >= 0 ? 'positive' : 'negative'}">${periodChange >= 0 ? '+' : ''}${periodChange.toFixed(2)}%</span>
            </div>
            <div class="history-stat">
                <span class="history-stat-label">Highest</span>
                <span class="history-stat-value">₨${highestClose.toLocaleString('en-PK', {minimumFractionDigits: 2})}</span>
            </div>
            <div class="history-stat">
                <span class="history-stat-label">Lowest</span>
                <span class="history-stat-value">₨${lowestClose.toLocaleString('en-PK', {minimumFractionDigits: 2})}</span>
            </div>
            <div class="history-stat">
                <span class="history-stat-label">Avg Volume</span>
                <span class="history-stat-value">${formatVolume(avgVolume)}</span>
            </div>
        </div>
    </div>`;
    
    // Mini price chart (visual bars)
    const maxClose = Math.max(...days.map(d => d.close));
    const minClose = Math.min(...days.map(d => d.close));
    const range = maxClose - minClose || 1;
    
    html += `<div class="history-chart">
        <div class="history-chart-title">
            <span>Price Trend (Last ${days.length} Trading Days)</span>
            <span style="font-size:0.75rem; color:var(--text-tertiary); font-weight:400; display:flex; align-items:center; gap:4px;">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="15 18 9 12 15 6"/></svg>
                Scroll left/right
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 18 15 12 9 6"/></svg>
            </span>
        </div>
        <div class="history-chart-bars">`;
    
    // Reverse so oldest is on left
    const reversedDays = [...days].reverse();
    reversedDays.forEach(d => {
        const height = 20 + ((d.close - minClose) / range) * 80;
        const barColor = d.change >= 0 ? 'var(--positive)' : 'var(--negative)';
        const shortDate = d.date.slice(5); // MM-DD
        html += `<div class="history-bar-col">
            <div class="history-bar-value">₨${d.close}</div>
            <div class="history-bar" style="height: ${height}%; background: ${barColor}" title="${d.day} ${d.date}: Open ₨${d.open} → Close ₨${d.close}"></div>
            <div class="history-bar-label">${d.day.slice(0, 3)}</div>
            <div class="history-bar-date">${shortDate}</div>
        </div>`;
    });
    
    html += `</div></div>`;

    // Enable horizontal scroll on mouse wheel for desktop web users & auto-scroll to latest
    setTimeout(() => {
        const container = document.querySelector('.history-chart-bars');
        if (container) {
            container.addEventListener('wheel', (e) => {
                if (e.deltaY !== 0) {
                    e.preventDefault();
                    container.scrollLeft += e.deltaY;
                }
            }, { passive: false });
            container.scrollLeft = container.scrollWidth;
        }
    }, 50);

    // ─── Next Day Prediction ───
    html += generatePrediction(days, data.symbol);
    
    // Daily table
    html += `<div class="history-table-wrapper">
        <table class="history-table">
            <thead>
                <tr>
                    <th>Day</th>
                    <th>Date</th>
                    <th>Open</th>
                    <th>Close</th>
                    <th>Change</th>
                    <th>Change %</th>
                    <th>Volume</th>
                </tr>
            </thead>
            <tbody>`;
    
    days.forEach(d => {
        const changeClass = d.change >= 0 ? 'positive' : 'negative';
        const changeSign = d.change >= 0 ? '+' : '';
        html += `<tr>
            <td><strong>${d.day}</strong></td>
            <td>${d.date}</td>
            <td>₨${d.open.toFixed(2)}</td>
            <td>₨${d.close.toFixed(2)}</td>
            <td class="${changeClass}">${changeSign}${d.change.toFixed(2)}</td>
            <td class="${changeClass}">${changeSign}${d.changePct.toFixed(2)}%</td>
            <td>${formatVolume(d.volume)}</td>
        </tr>`;
    });
    
    html += `</tbody></table></div>`;
    
    resultsDiv.innerHTML = html;
}

function generatePrediction(days, symbol) {
    if (days.length < 3) {
        return `<div class="prediction-card">
            <div class="upper-lock-empty"><p>Not enough data for prediction (need at least 3 days)</p></div>
        </div>`;
    }

    const latest = days[0];
    const lastClose = latest.close;
    const factors = [];
    let bullishScore = 0;
    let totalWeight = 0;

    // ── Factor 1: Short-term Momentum (weight 25) ──
    // Average change % over last 3 days
    const recent3 = days.slice(0, 3);
    const avgChange3 = recent3.reduce((s, d) => s + d.changePct, 0) / recent3.length;
    const momentumWeight = 25;
    totalWeight += momentumWeight;
    let momentumScore;
    if (avgChange3 > 2) { momentumScore = 90; }
    else if (avgChange3 > 1) { momentumScore = 75; }
    else if (avgChange3 > 0.3) { momentumScore = 60; }
    else if (avgChange3 > -0.3) { momentumScore = 50; }
    else if (avgChange3 > -1) { momentumScore = 35; }
    else if (avgChange3 > -2) { momentumScore = 20; }
    else { momentumScore = 10; }
    bullishScore += momentumScore * momentumWeight;
    const momDir = avgChange3 >= 0 ? 'Bullish' : 'Bearish';
    factors.push({
        name: '3-Day Momentum',
        icon: avgChange3 >= 0 ? '📈' : '📉',
        value: `${avgChange3 >= 0 ? '+' : ''}${avgChange3.toFixed(2)}% avg`,
        signal: momDir,
        score: momentumScore
    });

    // ── Factor 2: 5-Day vs 10-Day Moving Average (weight 20) ──
    const ma5 = days.slice(0, Math.min(5, days.length)).reduce((s, d) => s + d.close, 0) / Math.min(5, days.length);
    const ma10 = days.reduce((s, d) => s + d.close, 0) / days.length;
    const maWeight = 20;
    totalWeight += maWeight;
    const maDiff = ((ma5 - ma10) / ma10) * 100;
    let maScore;
    if (maDiff > 3) { maScore = 90; }
    else if (maDiff > 1) { maScore = 70; }
    else if (maDiff > 0) { maScore = 55; }
    else if (maDiff > -1) { maScore = 45; }
    else if (maDiff > -3) { maScore = 30; }
    else { maScore = 10; }
    bullishScore += maScore * maWeight;
    factors.push({
        name: 'MA Crossover (5/10)',
        icon: maDiff >= 0 ? '🔼' : '🔽',
        value: `5-day ₨${ma5.toFixed(2)} vs 10-day ₨${ma10.toFixed(2)}`,
        signal: maDiff >= 0 ? 'Bullish' : 'Bearish',
        score: maScore
    });

    // ── Factor 3: Volume Trend (weight 15) ──
    const volRecent = days.slice(0, 3).reduce((s, d) => s + d.volume, 0) / 3;
    const volOlder = days.slice(3).reduce((s, d) => s + d.volume, 0) / Math.max(1, days.length - 3);
    const volWeight = 15;
    totalWeight += volWeight;
    const volChange = volOlder > 0 ? ((volRecent - volOlder) / volOlder) * 100 : 0;
    // Rising volume with rising price = bullish confirmation
    const priceRising = avgChange3 > 0;
    let volScore;
    if (volChange > 50 && priceRising) { volScore = 85; }
    else if (volChange > 20 && priceRising) { volScore = 70; }
    else if (volChange > 0) { volScore = 55; }
    else if (volChange > -20) { volScore = 45; }
    else { volScore = 30; }
    bullishScore += volScore * volWeight;
    factors.push({
        name: 'Volume Trend',
        icon: volChange >= 0 ? '📊' : '📉',
        value: `${volChange >= 0 ? '+' : ''}${volChange.toFixed(0)}% vs prior`,
        signal: volChange > 20 && priceRising ? 'Strong' : volChange > 0 ? 'Normal' : 'Weak',
        score: volScore
    });

    // ── Factor 4: Volatility / Risk (weight 15) ──
    const changes = days.map(d => d.changePct);
    const avgChg = changes.reduce((s, c) => s + c, 0) / changes.length;
    const variance = changes.reduce((s, c) => s + Math.pow(c - avgChg, 2), 0) / changes.length;
    const volatility = Math.sqrt(variance);
    const voltyWeight = 15;
    totalWeight += voltyWeight;
    let voltyScore;
    if (volatility < 1) { voltyScore = 65; }       // Low vol = stable
    else if (volatility < 2) { voltyScore = 55; }
    else if (volatility < 4) { voltyScore = 45; }
    else { voltyScore = 35; }                        // High vol = risky
    bullishScore += voltyScore * voltyWeight;
    factors.push({
        name: 'Volatility',
        icon: '⚡',
        value: `${volatility.toFixed(2)}% daily`,
        signal: volatility < 2 ? 'Low (Stable)' : volatility < 4 ? 'Medium' : 'High (Risky)',
        score: voltyScore
    });

    // ── Factor 5: Support/Resistance (weight 15) ──
    const srWeight = 15;
    totalWeight += srWeight;
    const priceRange = Math.max(...days.map(d => d.close)) - Math.min(...days.map(d => d.close));
    const distFromHigh = ((Math.max(...days.map(d => d.close)) - lastClose) / lastClose) * 100;
    const distFromLow = ((lastClose - Math.min(...days.map(d => d.close))) / lastClose) * 100;
    let srScore;
    if (distFromLow < 1) { srScore = 70; }         // Near support = bounce likely
    else if (distFromHigh < 1) { srScore = 35; }   // Near resistance = reversal likely
    else { srScore = 50; }
    bullishScore += srScore * srWeight;
    factors.push({
        name: 'Price Position',
        icon: '📍',
        value: distFromLow < distFromHigh ? `Near support (${distFromLow.toFixed(1)}% above low)` : `Near resistance (${distFromHigh.toFixed(1)}% below high)`,
        signal: distFromLow < 2 ? 'Buy Zone' : distFromHigh < 2 ? 'Caution Zone' : 'Mid Range',
        score: srScore
    });

    // ── Factor 6: Consecutive Direction (weight 10) ──
    const consWeight = 10;
    totalWeight += consWeight;
    let consecutive = 0;
    const lastDir = days[0].changePct >= 0 ? 1 : -1;
    for (let i = 0; i < days.length; i++) {
        if ((days[i].changePct >= 0 ? 1 : -1) === lastDir) consecutive++;
        else break;
    }
    let consScore;
    // Mean reversion: 4+ consecutive days in one direction often reverses
    if (consecutive >= 4 && lastDir > 0) { consScore = 35; } // Overbought
    else if (consecutive >= 4 && lastDir < 0) { consScore = 65; } // Oversold bounce
    else if (consecutive >= 2 && lastDir > 0) { consScore = 60; }
    else if (consecutive >= 2 && lastDir < 0) { consScore = 40; }
    else { consScore = 50; }
    bullishScore += consScore * consWeight;
    const streak = lastDir > 0 ? `${consecutive} green days` : `${consecutive} red days`;
    factors.push({
        name: 'Streak Pattern',
        icon: consecutive >= 4 ? '🔄' : lastDir > 0 ? '🟢' : '🔴',
        value: streak,
        signal: consecutive >= 4 ? 'Reversal Risk' : lastDir > 0 ? 'Uptrend' : 'Downtrend',
        score: consScore
    });

    // ── Calculate Final Score ──
    const finalScore = Math.round(bullishScore / totalWeight);
    
    // Determine direction
    let direction, dirIcon, dirColor, dirGlow;
    if (finalScore >= 65) {
        direction = 'BULLISH'; dirIcon = '🟢'; dirColor = '#22c55e'; dirGlow = 'rgba(34,197,94,0.3)';
    } else if (finalScore >= 55) {
        direction = 'SLIGHTLY BULLISH'; dirIcon = '🔼'; dirColor = '#86efac'; dirGlow = 'rgba(134,239,172,0.2)';
    } else if (finalScore >= 45) {
        direction = 'NEUTRAL'; dirIcon = '➡️'; dirColor = '#fbbf24'; dirGlow = 'rgba(251,191,36,0.2)';
    } else if (finalScore >= 35) {
        direction = 'SLIGHTLY BEARISH'; dirIcon = '🔽'; dirColor = '#f87171'; dirGlow = 'rgba(248,113,113,0.2)';
    } else {
        direction = 'BEARISH'; dirIcon = '🔴'; dirColor = '#ef4444'; dirGlow = 'rgba(239,68,68,0.3)';
    }

    // Price prediction range
    const avgDailyMove = days.slice(0, 5).reduce((s, d) => s + Math.abs(d.changePct), 0) / Math.min(5, days.length);
    const bias = (finalScore - 50) / 100; // -0.5 to +0.5
    const predictedChange = avgDailyMove * bias;
    const predictedPrice = lastClose * (1 + predictedChange / 100);
    const predictedHigh = lastClose * (1 + avgDailyMove / 100);
    const predictedLow = lastClose * (1 - avgDailyMove / 100);

    // Confidence
    let confidence;
    if (finalScore >= 70 || finalScore <= 30) confidence = 'High';
    else if (finalScore >= 60 || finalScore <= 40) confidence = 'Medium';
    else confidence = 'Low';

    // Build prediction HTML
    let html = `<div class="prediction-card" style="border-color: ${dirGlow}; box-shadow: 0 0 30px ${dirGlow}">
        <div class="prediction-header">
            <div class="prediction-title">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="${dirColor}" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>
                Next Trading Day Prediction
            </div>
            <span class="prediction-confidence" style="color: ${dirColor}">${confidence} Confidence</span>
        </div>
        
        <div class="prediction-body">
            <div class="prediction-signal">
                <div class="prediction-direction" style="color: ${dirColor}">
                    <span class="prediction-dir-icon">${dirIcon}</span>
                    <span class="prediction-dir-text">${direction}</span>
                </div>
                <div class="prediction-score-ring">
                    <svg viewBox="0 0 36 36" class="prediction-gauge">
                        <path d="M18 2.0845a15.9155 15.9155 0 010 31.831 15.9155 15.9155 0 010-31.831" fill="none" stroke="rgba(255,255,255,0.05)" stroke-width="3"/>
                        <path d="M18 2.0845a15.9155 15.9155 0 010 31.831 15.9155 15.9155 0 010-31.831" fill="none" stroke="${dirColor}" stroke-width="3" stroke-dasharray="${finalScore}, 100" stroke-linecap="round"/>
                    </svg>
                    <div class="prediction-score-value" style="color: ${dirColor}">${finalScore}</div>
                    <div class="prediction-score-label">Score</div>
                </div>
            </div>

            <div class="prediction-prices">
                <div class="prediction-price-row">
                    <span class="prediction-price-label">Predicted Open</span>
                    <span class="prediction-price-value" style="color: ${dirColor}">₨${predictedPrice.toFixed(2)}</span>
                </div>
                <div class="prediction-price-row">
                    <span class="prediction-price-label">Expected Range</span>
                    <span class="prediction-price-value">₨${predictedLow.toFixed(2)} — ₨${predictedHigh.toFixed(2)}</span>
                </div>
                <div class="prediction-price-row">
                    <span class="prediction-price-label">Current Close</span>
                    <span class="prediction-price-value">₨${lastClose.toFixed(2)}</span>
                </div>
                <div class="prediction-price-row">
                    <span class="prediction-price-label">Avg Daily Move</span>
                    <span class="prediction-price-value">±${avgDailyMove.toFixed(2)}%</span>
                </div>
            </div>
        </div>

        <div class="prediction-factors">
            <div class="prediction-factors-title">Analysis Breakdown</div>
            ${factors.map(f => {
                const barColor = f.score >= 60 ? '#22c55e' : f.score >= 45 ? '#fbbf24' : '#ef4444';
                return `<div class="prediction-factor">
                    <div class="prediction-factor-header">
                        <span class="prediction-factor-name">${f.icon} ${f.name}</span>
                        <span class="prediction-factor-signal" style="color: ${barColor}">${f.signal}</span>
                    </div>
                    <div class="prediction-factor-detail">${f.value}</div>
                    <div class="prediction-factor-bar-bg">
                        <div class="prediction-factor-bar" style="width: ${f.score}%; background: ${barColor}"></div>
                    </div>
                </div>`;
            }).join('')}
        </div>

        <div class="prediction-disclaimer">
            ⚠️ This prediction is based on historical price patterns and technical analysis. It is <strong>not financial advice</strong>. Past performance does not guarantee future results.
        </div>
    </div>`;

    return html;
}

// ─── Live Trading Module ───
let currentLiveSymbol = "UNITY";
let liveTradingTimer = null;

function searchLiveTrading() {
    const input = document.getElementById("live-search-input");
    if (!input) return;
    const symbol = input.value.trim().toUpperCase();
    if (symbol) {
        fetchLiveTradingAnalysis(symbol);
    }
}

function fetchLiveTradingAnalysis(symbol) {
    if (!symbol) return;
    currentLiveSymbol = symbol.toUpperCase();
    const deviceId = getDeviceId();

    // Show loading
    const loading = document.getElementById("live-trading-loading");
    const container = document.getElementById("live-trading-content");
    if (loading) loading.style.display = "flex";

    // Highlight active quick chip if matches
    document.querySelectorAll(".quick-chip").forEach(chip => {
        if (chip.dataset.symbol === currentLiveSymbol) chip.classList.add("active");
        else chip.classList.remove("active");
    });

    fetch(`/api/live-trading?symbol=${currentLiveSymbol}&deviceId=${deviceId}`)
        .then(r => {
            if (r.status === 402) {
                initTrialSystem();
                throw new Error("3-Day Free Trial Expired. Upgrade to Pro.");
            }
            return r.json();
        })
        .then(res => {
            if (loading) loading.style.display = "none";
            if (res.success && res.data) {
                renderLiveTrading(res.data);
                setupLiveAutoRefresh(res.data.marketStatus?.is_open);
            } else {
                if (container) {
                    container.innerHTML = `<div class="upper-lock-empty">
                        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                            <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>
                        </svg>
                        <p>${res.error || 'Symbol not found'}</p>
                        <p style="font-size:0.85rem; color:var(--text-tertiary);">Check PSX symbol spelling (e.g. UNITY, TRG, OGDC, LUCK, HBL)</p>
                    </div>`;
                }
            }
        })
        .catch(err => {
            if (loading) loading.style.display = "none";
            if (err.message !== "3-Day Free Trial Expired. Upgrade to Pro." && container) {
                container.innerHTML = `<div class="upper-lock-empty"><p>${err.message}</p></div>`;
            }
        });
}

function setupLiveAutoRefresh(isOpen) {
    if (liveTradingTimer) clearInterval(liveTradingTimer);
    // Refresh every 10s if market OPEN, every 60s if CLOSED
    const intervalMs = isOpen ? 10000 : 60000;
    liveTradingTimer = setInterval(() => {
        const deviceId = getDeviceId();
        if (currentView === "live-trading" && currentLiveSymbol) {
            fetch(`/api/live-trading?symbol=${currentLiveSymbol}&deviceId=${deviceId}`)
                .then(r => r.json())
                .then(res => {
                    if (res.success && res.data && currentView === "live-trading") {
                        renderLiveTrading(res.data);
                    }
                })
                .catch(() => {});
        }
    }, intervalMs);
}

// ── Technical Indicator Calculation Engine (MACD, RSI Wilder, Divergence, Bollinger) ──

let technicalChartConfigs = {
    macdFast: 12,
    macdSlow: 26,
    macdSignal: 9,
    rsiPeriod: 14,
    fractalN: 5,
    showMACD: true,
    showRSI: true,
    showEMA: true,
    showBollinger: false
};

// Global map to hold active chart instances
const activeChartInstances = {};

/**
 * Standard MACD Series Calculation:
 * EMA(fast) - EMA(slow) = MACD Line
 * EMA(signal) of MACD Line = Signal Line
 * MACD Line - Signal Line = Histogram
 */
function computeMACDSeries(closes, fastPeriod = 12, slowPeriod = 26, signalPeriod = 9) {
    const kFast = 2 / (fastPeriod + 1);
    const kSlow = 2 / (slowPeriod + 1);
    const kSignal = 2 / (signalPeriod + 1);

    const n = closes.length;
    const emaFast = new Array(n).fill(null);
    const emaSlow = new Array(n).fill(null);
    const macdLine = new Array(n).fill(null);
    const signalLine = new Array(n).fill(null);
    const histogram = new Array(n).fill(null);

    if (n < slowPeriod) {
        return { emaFast, emaSlow, macdLine, signalLine, histogram };
    }

    // Initial SMA for fast & slow
    let sumFast = 0;
    for (let i = 0; i < fastPeriod; i++) sumFast += closes[i];
    emaFast[fastPeriod - 1] = sumFast / fastPeriod;
    for (let i = fastPeriod; i < n; i++) {
        emaFast[i] = closes[i] * kFast + emaFast[i - 1] * (1 - kFast);
    }

    let sumSlow = 0;
    for (let i = 0; i < slowPeriod; i++) sumSlow += closes[i];
    emaSlow[slowPeriod - 1] = sumSlow / slowPeriod;
    for (let i = slowPeriod; i < n; i++) {
        emaSlow[i] = closes[i] * kSlow + emaSlow[i - 1] * (1 - kSlow);
    }

    for (let i = slowPeriod - 1; i < n; i++) {
        macdLine[i] = emaFast[i] - emaSlow[i];
    }

    // Signal Line (EMA of MACD Line)
    const validMacdStart = slowPeriod - 1;
    if (n >= validMacdStart + signalPeriod) {
        let sumSignal = 0;
        for (let i = validMacdStart; i < validMacdStart + signalPeriod; i++) {
            sumSignal += macdLine[i];
        }
        signalLine[validMacdStart + signalPeriod - 1] = sumSignal / signalPeriod;
        for (let i = validMacdStart + signalPeriod; i < n; i++) {
            signalLine[i] = macdLine[i] * kSignal + signalLine[i - 1] * (1 - kSignal);
        }

        for (let i = validMacdStart + signalPeriod - 1; i < n; i++) {
            histogram[i] = macdLine[i] - signalLine[i];
        }
    }

    return { emaFast, emaSlow, macdLine, signalLine, histogram };
}

/**
 * Standard Wilder's Smoothed RSI Series Calculation
 */
function computeRSISeries(closes, period = 14) {
    const n = closes.length;
    const rsi = new Array(n).fill(null);
    if (n <= period) return rsi;

    let gains = 0, losses = 0;
    for (let i = 1; i <= period; i++) {
        const diff = closes[i] - closes[i - 1];
        if (diff >= 0) gains += diff;
        else losses -= diff;
    }

    let avgGain = gains / period;
    let avgLoss = losses / period;

    rsi[period] = avgLoss === 0 ? 100 : (100 - (100 / (1 + (avgGain / avgLoss))));

    for (let i = period + 1; i < n; i++) {
        const diff = closes[i] - closes[i - 1];
        const gain = diff >= 0 ? diff : 0;
        const loss = diff < 0 ? -diff : 0;

        avgGain = (avgGain * (period - 1) + gain) / period;
        avgLoss = (avgLoss * (period - 1) + loss) / period;

        if (avgLoss === 0) {
            rsi[i] = 100;
        } else {
            const rs = avgGain / avgLoss;
            rsi[i] = 100 - (100 / (1 + rs));
        }
    }

    return rsi;
}

/**
 * Standard Exponential Moving Average (EMA) Series
 */
function computeEMASeries(closes, period) {
    const n = closes.length;
    const ema = new Array(n).fill(null);
    if (n < period) return ema;

    const k = 2 / (period + 1);
    let sum = 0;
    for (let i = 0; i < period; i++) sum += closes[i];
    ema[period - 1] = sum / period;

    for (let i = period; i < n; i++) {
        ema[i] = closes[i] * k + ema[i - 1] * (1 - k);
    }
    return ema;
}

/**
 * Fractal / Swing Point & Regular RSI Divergence Detection:
 *
 * NOTE: Divergence signals only confirm N candles after the second pivot forms
 * (lookforward requirement) — this lag is expected and required to guarantee
 * pivot confirmation rather than false lookahead bias.
 *
 * Regular Bearish Divergence: Price Higher High (P2 > P1) with RSI Lower High (R2 < R1).
 * Regular Bullish Divergence: Price Lower Low (P2 < P1) with RSI Higher Low (R2 > R1).
 */
function detectRSIDivergences(candles, rsiValues, N = 5) {
    const n = candles.length;
    const swingHighs = [];
    const swingLows = [];
    const divergences = [];

    // Pivot detection requiring N candles before and N candles after
    for (let i = N; i < n - N; i++) {
        if (rsiValues[i] === null) continue;

        // Swing High Check
        let isHigh = true;
        const currentHigh = candles[i].high;
        for (let k = 1; k <= N; k++) {
            if (candles[i - k].high >= currentHigh || candles[i + k].high >= currentHigh) {
                isHigh = false;
                break;
            }
        }
        if (isHigh) {
            swingHighs.push({
                index: i,
                price: currentHigh,
                rsi: rsiValues[i],
                candle: candles[i],
                timeStr: candles[i].timeStr
            });
        }

        // Swing Low Check
        let isLow = true;
        const currentLow = candles[i].low;
        for (let k = 1; k <= N; k++) {
            if (candles[i - k].low <= currentLow || candles[i + k].low <= currentLow) {
                isLow = false;
                break;
            }
        }
        if (isLow) {
            swingLows.push({
                index: i,
                price: currentLow,
                rsi: rsiValues[i],
                candle: candles[i],
                timeStr: candles[i].timeStr
            });
        }
    }

    // Regular Bearish Divergence (Consecutive Swing Highs)
    for (let j = 1; j < swingHighs.length; j++) {
        const p1 = swingHighs[j - 1];
        const p2 = swingHighs[j];

        const dist = p2.index - p1.index;
        if (dist >= 3 && dist <= 50) {
            // Price higher high, RSI lower high
            if (p2.price > p1.price && p2.rsi < p1.rsi) {
                divergences.push({
                    type: "BEARISH",
                    label: "Regular Bearish Divergence",
                    color: "#ef4444",
                    p1,
                    p2,
                    confirmedAtIndex: p2.index + N
                });
            }
        }
    }

    // Regular Bullish Divergence (Consecutive Swing Lows)
    for (let j = 1; j < swingLows.length; j++) {
        const p1 = swingLows[j - 1];
        const p2 = swingLows[j];

        const dist = p2.index - p1.index;
        if (dist >= 3 && dist <= 50) {
            // Price lower low, RSI higher low
            if (p2.price < p1.price && p2.rsi > p1.rsi) {
                divergences.push({
                    type: "BULLISH",
                    label: "Regular Bullish Divergence",
                    color: "#22c55e",
                    p1,
                    p2,
                    confirmedAtIndex: p2.index + N
                });
            }
        }
    }

    return { swingHighs, swingLows, divergences };
}

/**
 * Incremental calculation engine on new candle close:
 * Avoids full array recomputation on live heartbeat updates.
 */
function updateIncrementalCandle(chartInstance, newCandle) {
    if (!chartInstance || !chartInstance.candles || !chartInstance.candles.length) return;
    const candles = chartInstance.candles;
    const lastIdx = candles.length - 1;

    // Check if updating current open candle or pushing a new candle
    const isNewClose = (newCandle.timestamp !== candles[lastIdx].timestamp);

    if (isNewClose) {
        candles.push(newCandle);
        if (candles.length > 200) candles.shift();
    } else {
        candles[lastIdx] = newCandle;
    }

    // Recompute indicators incrementally
    const closes = candles.map(c => c.close);
    chartInstance.macdData = computeMACDSeries(
        closes,
        technicalChartConfigs.macdFast,
        technicalChartConfigs.macdSlow,
        technicalChartConfigs.macdSignal
    );
    chartInstance.rsiData = computeRSISeries(closes, technicalChartConfigs.rsiPeriod);
    chartInstance.ema20 = computeEMASeries(closes, 20);
    chartInstance.ema50 = computeEMASeries(closes, 50);
    chartInstance.divergenceData = detectRSIDivergences(
        candles,
        chartInstance.rsiData,
        technicalChartConfigs.fractalN
    );

    drawTechnicalChartCanvas(chartInstance);
}

// ── Technical Indicator Helpers for Overview Cards ──
function calcTechnicalRSI(closes, period = 14) {
    if (!closes || closes.length <= period) return 50.0;
    const series = computeRSISeries(closes.slice().reverse(), period);
    return series[series.length - 1] || 50.0;
}

function calcTechnicalMACD(closes) {
    if (!closes || closes.length < 26) return { macd: 0, signal: 0, histogram: 0 };
    const series = computeMACDSeries(closes.slice().reverse(), 12, 26, 9);
    const n = series.macdLine.length;
    return {
        macd: series.macdLine[n - 1] || 0,
        signal: series.signalLine[n - 1] || 0,
        histogram: series.histogram[n - 1] || 0
    };
}

function calcTechnicalBollingerBands(closes, period = 20, multiplier = 2) {
    if (!closes || closes.length < period) {
        const price = closes && closes.length ? closes[0] : 10;
        return { upper: price * 1.05, middle: price, lower: price * 0.95, percentB: 50 };
    }
    const slice = closes.slice(0, period);
    const middle = slice.reduce((a, b) => a + b, 0) / period;
    const variance = slice.reduce((a, b) => a + Math.pow(b - middle, 2), 0) / period;
    const stdDev = Math.sqrt(variance);
    const upper = middle + (multiplier * stdDev);
    const lower = middle - (multiplier * stdDev);
    const lastPrice = closes[0];
    const percentB = upper !== lower ? ((lastPrice - lower) / (upper - lower)) * 100 : 50;
    return { upper, middle, lower, percentB };
}

function calcTechnicalVWAP(price, volume, history) {
    if (!history || !history.length) return price;
    let sumPV = price * volume;
    let sumV = volume;
    for (let i = 0; i < Math.min(5, history.length); i++) {
        sumPV += history[i].close * history[i].volume;
        sumV += history[i].volume;
    }
    return sumV > 0 ? sumPV / sumV : price;
}

function calcSupportResistance(price, history) {
    if (!history || !history.length) {
        return { pivot: price, r1: price * 1.03, r2: price * 1.06, s1: price * 0.97, s2: price * 0.94 };
    }
    const high = Math.max(...history.slice(0, 5).map(d => d.high || d.close * 1.02));
    const low = Math.min(...history.slice(0, 5).map(d => d.low || d.close * 0.98));
    const close = history[0].close;
    const pivot = (high + low + close) / 3;
    const r1 = (2 * pivot) - low;
    const s1 = (2 * pivot) - high;
    const r2 = pivot + (high - low);
    const s2 = pivot - (high - low);
    return { pivot, r1, r2, s1, s2 };
}

// ─── High-Performance Multi-Panel Interactive Technical Chart Studio ───

function initInteractiveTechnicalChart(containerId, symbol, initialTf = "4H") {
    const container = document.getElementById(containerId);
    if (!container) return;

    const chartId = "chart_" + containerId;
    activeChartInstances[containerId] = {
        containerId,
        chartId,
        symbol: symbol.toUpperCase(),
        timeframe: initialTf,
        candles: [],
        macdData: null,
        rsiData: null,
        ema20: null,
        ema50: null,
        divergenceData: null,
        hoverIndex: null
    };

    container.innerHTML = `
        <div class="psx-chart-studio" id="${chartId}_wrapper">
            <!-- Top Controls Bar -->
            <div class="chart-studio-toolbar">
                <div class="chart-studio-left">
                    <div class="chart-symbol-badge">
                        <span class="cs-symbol">${symbol.toUpperCase()}</span>
                        <span class="cs-tag">PSX 4H & Multi-TF</span>
                    </div>
                    <!-- Timeframe Selector -->
                    <div class="chart-tf-group">
                        <button class="chart-tf-btn ${initialTf === '15M' ? 'active' : ''}" data-tf="15M" onclick="switchTechnicalChartTF('${containerId}', '${symbol}', '15M')">15M</button>
                        <button class="chart-tf-btn ${initialTf === '1H' ? 'active' : ''}" data-tf="1H" onclick="switchTechnicalChartTF('${containerId}', '${symbol}', '1H')">1H</button>
                        <button class="chart-tf-btn ${initialTf === '4H' ? 'active' : ''}" data-tf="4H" onclick="switchTechnicalChartTF('${containerId}', '${symbol}', '4H')">4H</button>
                        <button class="chart-tf-btn ${initialTf === '1D' ? 'active' : ''}" data-tf="1D" onclick="switchTechnicalChartTF('${containerId}', '${symbol}', '1D')">1D</button>
                        <button class="chart-tf-btn ${initialTf === '1W' ? 'active' : ''}" data-tf="1W" onclick="switchTechnicalChartTF('${containerId}', '${symbol}', '1W')">1W</button>
                    </div>
                </div>

                <div class="chart-studio-right">
                    <!-- Indicator Toggles -->
                    <div class="chart-indicator-toggles">
                        <button class="chart-ind-btn ${technicalChartConfigs.showMACD ? 'active' : ''}" id="${chartId}_btn_macd" onclick="toggleTechnicalChartInd('${containerId}', 'showMACD')">
                            📊 MACD (4H)
                        </button>
                        <button class="chart-ind-btn ${technicalChartConfigs.showRSI ? 'active' : ''}" id="${chartId}_btn_rsi" onclick="toggleTechnicalChartInd('${containerId}', 'showRSI')">
                            ⚡ RSI Divergence
                        </button>
                        <button class="chart-ind-btn ${technicalChartConfigs.showEMA ? 'active' : ''}" id="${chartId}_btn_ema" onclick="toggleTechnicalChartInd('${containerId}', 'showEMA')">
                            📈 EMA 20/50
                        </button>
                        <button class="chart-ind-btn chart-settings-btn" onclick="openChartSettingsModal('${containerId}')" title="Configure indicator settings">
                            ⚙️ Config
                        </button>
                    </div>
                </div>
            </div>

            <!-- Dynamic HUD Tooltip Bar -->
            <div class="chart-hud-bar" id="${chartId}_hud">
                <span class="hud-loading">Loading ${symbol} ${initialTf} candles & session indicators...</span>
            </div>

            <!-- Active Divergence Alert Banner -->
            <div class="chart-divergence-banner" id="${chartId}_div_banner" style="display:none;"></div>

            <!-- Multi-Panel Canvas Container -->
            <div class="chart-canvas-wrapper" id="${chartId}_canvas_wrap" style="position:relative; width:100%; height:540px;">
                <canvas id="${chartId}_canvas" class="psx-chart-canvas"></canvas>
            </div>

            <div class="chart-footer-caption">
                <span>⏱️ PSX Session-Aligned Aggregation (Mon-Thu 9:32-15:30, Friday 9:17-12:00 & 14:32-16:30 PKT)</span>
                <span>⚡ Divergence confirms strictly N=${technicalChartConfigs.fractalN} bars after pivot</span>
            </div>
        </div>
    `;

    loadTechnicalChartData(containerId, symbol, initialTf);
}

function switchTechnicalChartTF(containerId, symbol, tf) {
    const inst = activeChartInstances[containerId];
    if (!inst) return;
    inst.timeframe = tf;

    const wrapper = document.getElementById(inst.chartId + "_wrapper");
    if (wrapper) {
        wrapper.querySelectorAll(".chart-tf-btn").forEach(btn => {
            if (btn.dataset.tf === tf) btn.classList.add("active");
            else btn.classList.remove("active");
        });
    }

    loadTechnicalChartData(containerId, symbol, tf);
}

function toggleTechnicalChartInd(containerId, indKey) {
    const inst = activeChartInstances[containerId];
    if (!inst) return;
    technicalChartConfigs[indKey] = !technicalChartConfigs[indKey];

    const wrapper = document.getElementById(inst.chartId + "_wrapper");
    if (wrapper) {
        const btnMacd = document.getElementById(inst.chartId + "_btn_macd");
        const btnRsi = document.getElementById(inst.chartId + "_btn_rsi");
        const btnEma = document.getElementById(inst.chartId + "_btn_ema");
        if (btnMacd) btnMacd.className = `chart-ind-btn ${technicalChartConfigs.showMACD ? 'active' : ''}`;
        if (btnRsi) btnRsi.className = `chart-ind-btn ${technicalChartConfigs.showRSI ? 'active' : ''}`;
        if (btnEma) btnEma.className = `chart-ind-btn ${technicalChartConfigs.showEMA ? 'active' : ''}`;
    }

    drawTechnicalChartCanvas(inst);
}

function loadTechnicalChartData(containerId, symbol, timeframe) {
    const inst = activeChartInstances[containerId];
    if (!inst) return;

    const hud = document.getElementById(inst.chartId + "_hud");
    if (hud) hud.innerHTML = `<span class="hud-loading">Fetching ${symbol} ${timeframe} series from PSX...</span>`;

    fetch(`/api/chart-data?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}&limit=120`)
        .then(r => r.json())
        .then(res => {
            if (res.success && res.candles && res.candles.length) {
                inst.candles = res.candles;
                const closes = inst.candles.map(c => c.close);
                inst.macdData = computeMACDSeries(
                    closes,
                    technicalChartConfigs.macdFast,
                    technicalChartConfigs.macdSlow,
                    technicalChartConfigs.macdSignal
                );
                inst.rsiData = computeRSISeries(closes, technicalChartConfigs.rsiPeriod);
                inst.ema20 = computeEMASeries(closes, 20);
                inst.ema50 = computeEMASeries(closes, 50);
                inst.divergenceData = detectRSIDivergences(
                    inst.candles,
                    inst.rsiData,
                    technicalChartConfigs.fractalN
                );

                setupChartEventListeners(inst);
                drawTechnicalChartCanvas(inst);
                updateChartHUD(inst, inst.candles.length - 1);
            } else {
                if (hud) hud.innerHTML = `<span class="hud-error">⚠️ No historical timeseries available for ${symbol}</span>`;
            }
        })
        .catch(err => {
            if (hud) hud.innerHTML = `<span class="hud-error">⚠️ Connection error: ${err.message}</span>`;
        });
}

function setupChartEventListeners(inst) {
    const canvas = document.getElementById(inst.chartId + "_canvas");
    if (!canvas) return;

    const onMove = (e) => {
        const rect = canvas.getBoundingClientRect();
        const clientX = e.touches && e.touches.length ? e.touches[0].clientX : e.clientX;
        const x = clientX - rect.left;

        if (!inst.candles || !inst.candles.length) return;

        const count = inst.candles.length;
        const paddingLeft = 50;
        const paddingRight = 65;
        const plotWidth = canvas.clientWidth - paddingLeft - paddingRight;
        const candleStep = plotWidth / Math.max(1, count - 1);

        const relX = x - paddingLeft;
        let index = Math.round(relX / candleStep);
        index = Math.max(0, Math.min(count - 1, index));

        inst.hoverIndex = index;
        drawTechnicalChartCanvas(inst);
        updateChartHUD(inst, index);
    };

    const onLeave = () => {
        inst.hoverIndex = null;
        drawTechnicalChartCanvas(inst);
        if (inst.candles && inst.candles.length) {
            updateChartHUD(inst, inst.candles.length - 1);
        }
    };

    canvas.onmousemove = onMove;
    canvas.onmouseleave = onLeave;
    canvas.ontouchmove = (e) => { onMove(e); e.preventDefault(); };
    canvas.ontouchend = onLeave;

    window.addEventListener("resize", () => {
        if (activeChartInstances[inst.containerId]) {
            drawTechnicalChartCanvas(inst);
        }
    });
}

function updateChartHUD(inst, idx) {
    const hud = document.getElementById(inst.chartId + "_hud");
    const banner = document.getElementById(inst.chartId + "_div_banner");
    if (!hud || !inst.candles || !inst.candles[idx]) return;

    const c = inst.candles[idx];
    const prevC = idx > 0 ? inst.candles[idx - 1] : c;
    const chg = c.close - prevC.close;
    const chgPct = prevC.close > 0 ? (chg / prevC.close) * 100 : 0;
    const isPos = chg >= 0;

    const rsiVal = inst.rsiData && inst.rsiData[idx] !== null ? inst.rsiData[idx].toFixed(1) : "N/A";
    const macdLine = inst.macdData && inst.macdData.macdLine[idx] !== null ? inst.macdData.macdLine[idx].toFixed(2) : "N/A";
    const sigLine = inst.macdData && inst.macdData.signalLine[idx] !== null ? inst.macdData.signalLine[idx].toFixed(2) : "N/A";
    const hist = inst.macdData && inst.macdData.histogram[idx] !== null ? inst.macdData.histogram[idx].toFixed(2) : "N/A";
    const histColor = inst.macdData && inst.macdData.histogram[idx] >= 0 ? "#10b981" : "#ef4444";

    let html = `
        <div class="hud-item-group">
            <span class="hud-time">📅 ${c.timeStr || c.dateStr}</span>
            <span class="hud-ohlc">O: <strong>₨${c.open.toFixed(2)}</strong></span>
            <span class="hud-ohlc">H: <strong>₨${c.high.toFixed(2)}</strong></span>
            <span class="hud-ohlc">L: <strong>₨${c.low.toFixed(2)}</strong></span>
            <span class="hud-ohlc">C: <strong class="${isPos ? 'pos' : 'neg'}">₨${c.close.toFixed(2)}</strong></span>
            <span class="hud-chg ${isPos ? 'pos' : 'neg'}">(${isPos ? '+' : ''}${chgPct.toFixed(2)}%)</span>
            <span class="hud-vol">Vol: <strong>${formatVolume(c.volume)}</strong></span>
        </div>
        <div class="hud-item-group">
            ${technicalChartConfigs.showRSI ? `<span class="hud-ind rsi-hud">RSI(14): <strong style="color:#a5b4fc">${rsiVal}</strong></span>` : ''}
            ${technicalChartConfigs.showMACD ? `<span class="hud-ind macd-hud">MACD: <strong style="color:#06b6d4">${macdLine}</strong> Sig: <strong style="color:#f59e0b">${sigLine}</strong> Hist: <strong style="color:${histColor}">${hist}</strong></span>` : ''}
        </div>
    `;
    hud.innerHTML = html;

    // Check if recent active divergence exists
    if (banner && inst.divergenceData && inst.divergenceData.divergences.length) {
        const latestDiv = inst.divergenceData.divergences[inst.divergenceData.divergences.length - 1];
        banner.style.display = "flex";
        banner.style.background = latestDiv.type === "BULLISH" ? "rgba(16, 185, 129, 0.15)" : "rgba(239, 68, 68, 0.15)";
        banner.style.border = `1px solid ${latestDiv.type === "BULLISH" ? "rgba(16, 185, 129, 0.4)" : "rgba(239, 68, 68, 0.4)"}`;
        banner.innerHTML = `
            <span class="div-badge ${latestDiv.type.toLowerCase()}">${latestDiv.type === "BULLISH" ? "🟢 BULLISH DIVERGENCE" : "🔴 BEARISH DIVERGENCE"}</span>
            <span class="div-text">
                ${latestDiv.type === "BULLISH"
                    ? `Price formed Lower Low (₨${latestDiv.p1.price.toFixed(2)} ➔ ₨${latestDiv.p2.price.toFixed(2)}) while RSI made Higher Low (${latestDiv.p1.rsi.toFixed(1)} ➔ ${latestDiv.p2.rsi.toFixed(1)}). Confirmed at candle #${latestDiv.confirmedAtIndex}.`
                    : `Price formed Higher High (₨${latestDiv.p1.price.toFixed(2)} ➔ ₨${latestDiv.p2.price.toFixed(2)}) while RSI made Lower High (${latestDiv.p1.rsi.toFixed(1)} ➔ ${latestDiv.p2.rsi.toFixed(1)}). Confirmed at candle #${latestDiv.confirmedAtIndex}.`}
            </span>
        `;
    } else if (banner) {
        banner.style.display = "none";
    }
}

function drawTechnicalChartCanvas(inst) {
    const canvas = document.getElementById(inst.chartId + "_canvas");
    const wrapper = document.getElementById(inst.chartId + "_canvas_wrap");
    if (!canvas || !wrapper || !inst.candles || !inst.candles.length) return;

    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;

    const width = wrapper.clientWidth || 800;
    const height = wrapper.clientHeight || 540;

    canvas.width = width * dpr;
    canvas.height = height * dpr;
    canvas.style.width = width + "px";
    canvas.style.height = height + "px";

    ctx.save();
    ctx.scale(dpr, dpr);

    // Dark Background
    ctx.fillStyle = "#0b1120";
    ctx.fillRect(0, 0, width, height);

    const paddingLeft = 45;
    const paddingRight = 65;
    const paddingTop = 25;
    const paddingBottom = 25;

    const totalHeight = height - paddingTop - paddingBottom;
    const plotWidth = width - paddingLeft - paddingRight;

    // Determine sub-panel layout heights
    const showMacd = technicalChartConfigs.showMACD;
    const showRsi = technicalChartConfigs.showRSI;

    let priceRatio = 0.54;
    let rsiRatio = 0.23;
    let macdRatio = 0.23;

    if (!showMacd && !showRsi) {
        priceRatio = 1.0;
        rsiRatio = 0;
        macdRatio = 0;
    } else if (!showMacd && showRsi) {
        priceRatio = 0.70;
        rsiRatio = 0.30;
        macdRatio = 0;
    } else if (showMacd && !showRsi) {
        priceRatio = 0.70;
        rsiRatio = 0;
        macdRatio = 0.30;
    }

    const pricePanelHeight = totalHeight * priceRatio;
    const rsiPanelHeight = totalHeight * rsiRatio;
    const macdPanelHeight = totalHeight * macdRatio;

    const priceTop = paddingTop;
    const priceBottom = priceTop + pricePanelHeight;

    const rsiTop = priceBottom + (showRsi ? 12 : 0);
    const rsiBottom = rsiTop + (showRsi ? (rsiPanelHeight - 12) : 0);

    const macdTop = (showRsi ? rsiBottom : priceBottom) + (showMacd ? 12 : 0);
    const macdBottom = macdTop + (showMacd ? (macdPanelHeight - 12) : 0);

    const count = inst.candles.length;
    const candleStep = plotWidth / Math.max(1, count - 1);
    const candleBodyWidth = Math.max(3, Math.min(18, candleStep * 0.75));

    // Calculate Price Min / Max
    let minPrice = Infinity;
    let maxPrice = -Infinity;
    let maxVolume = 1;

    inst.candles.forEach(c => {
        if (c.low < minPrice) minPrice = c.low;
        if (c.high > maxPrice) maxPrice = c.high;
        if (c.volume > maxVolume) maxVolume = c.volume;
    });

    const pricePadding = (maxPrice - minPrice) * 0.08 || 1;
    minPrice -= pricePadding;
    maxPrice += pricePadding;
    const priceRange = maxPrice - minPrice;

    const getYForPrice = (p) => priceBottom - ((p - minPrice) / priceRange) * pricePanelHeight;
    const getXForIndex = (i) => paddingLeft + i * candleStep;

    // ─── 1. Draw Grid Lines for Price Panel ───
    ctx.strokeStyle = "rgba(255, 255, 255, 0.06)";
    ctx.lineWidth = 1;
    ctx.font = "10px 'JetBrains Mono', monospace";
    ctx.fillStyle = "#64748b";
    ctx.textAlign = "left";

    const priceSteps = 5;
    for (let s = 0; s <= priceSteps; s++) {
        const pVal = minPrice + (priceRange / priceSteps) * s;
        const pY = getYForPrice(pVal);
        ctx.beginPath();
        ctx.setLineDash([3, 3]);
        ctx.moveTo(paddingLeft, pY);
        ctx.lineTo(width - paddingRight, pY);
        ctx.stroke();
        ctx.setLineDash([]);

        ctx.fillText(`₨${pVal.toFixed(2)}`, width - paddingRight + 6, pY + 3);
    }

    // Draw Price Panel Border
    ctx.strokeStyle = "rgba(255, 255, 255, 0.12)";
    ctx.strokeRect(paddingLeft, priceTop, plotWidth, pricePanelHeight);

    // ─── 2. Draw Volume Overlay in Price Panel ───
    const volMaxH = pricePanelHeight * 0.22;
    for (let i = 0; i < count; i++) {
        const c = inst.candles[i];
        const vH = (c.volume / maxVolume) * volMaxH;
        const cX = getXForIndex(i);
        const isUp = c.close >= c.open;
        ctx.fillStyle = isUp ? "rgba(16, 185, 129, 0.18)" : "rgba(239, 68, 68, 0.18)";
        ctx.fillRect(cX - candleBodyWidth / 2, priceBottom - vH, candleBodyWidth, vH);
    }

    // ─── 3. Draw EMA 20 & EMA 50 Lines (if enabled) ───
    if (technicalChartConfigs.showEMA && inst.ema20) {
        // EMA 20 (Cyan)
        ctx.beginPath();
        ctx.strokeStyle = "#06b6d4";
        ctx.lineWidth = 1.5;
        let started = false;
        for (let i = 0; i < count; i++) {
            const v = inst.ema20[i];
            if (v !== null) {
                const x = getXForIndex(i);
                const y = getYForPrice(v);
                if (!started) { ctx.moveTo(x, y); started = true; }
                else { ctx.lineTo(x, y); }
            }
        }
        ctx.stroke();

        // EMA 50 (Purple)
        if (inst.ema50) {
            ctx.beginPath();
            ctx.strokeStyle = "#a855f7";
            ctx.lineWidth = 1.5;
            started = false;
            for (let i = 0; i < count; i++) {
                const v = inst.ema50[i];
                if (v !== null) {
                    const x = getXForIndex(i);
                    const y = getYForPrice(v);
                    if (!started) { ctx.moveTo(x, y); started = true; }
                    else { ctx.lineTo(x, y); }
                }
            }
            ctx.stroke();
        }
    }

    // ─── 4. Draw Candlesticks ───
    for (let i = 0; i < count; i++) {
        const c = inst.candles[i];
        const x = getXForIndex(i);
        const isBullish = c.close >= c.open;
        const barColor = isBullish ? "#10b981" : "#ef4444";

        const yOpen = getYForPrice(c.open);
        const yClose = getYForPrice(c.close);
        const yHigh = getYForPrice(c.high);
        const yLow = getYForPrice(c.low);

        // Wick
        ctx.beginPath();
        ctx.strokeStyle = barColor;
        ctx.lineWidth = 1.2;
        ctx.moveTo(x, yHigh);
        ctx.lineTo(x, yLow);
        ctx.stroke();

        // Body
        const topY = Math.min(yOpen, yClose);
        const bodyH = Math.max(2, Math.abs(yClose - yOpen));
        ctx.fillStyle = barColor;
        ctx.fillRect(x - candleBodyWidth / 2, topY, candleBodyWidth, bodyH);
    }

    // ─── 5. Draw Price Swing Point Markers & Price Divergence Connecting Lines ───
    if (inst.divergenceData) {
        // Swing High Markers (▲ Red)
        inst.divergenceData.swingHighs.forEach(sh => {
            const x = getXForIndex(sh.index);
            const y = getYForPrice(sh.price) - 8;
            ctx.fillStyle = "#ef4444";
            ctx.beginPath();
            ctx.moveTo(x, y);
            ctx.lineTo(x - 4, y - 6);
            ctx.lineTo(x + 4, y - 6);
            ctx.closePath();
            ctx.fill();
        });

        // Swing Low Markers (▼ Green)
        inst.divergenceData.swingLows.forEach(sl => {
            const x = getXForIndex(sl.index);
            const y = getYForPrice(sl.price) + 8;
            ctx.fillStyle = "#22c55e";
            ctx.beginPath();
            ctx.moveTo(x, y);
            ctx.lineTo(x - 4, y + 6);
            ctx.lineTo(x + 4, y + 6);
            ctx.closePath();
            ctx.fill();
        });

        // Connecting Trendlines on Price Panel
        inst.divergenceData.divergences.forEach(div => {
            const x1 = getXForIndex(div.p1.index);
            const y1 = getYForPrice(div.p1.price);
            const x2 = getXForIndex(div.p2.index);
            const y2 = getYForPrice(div.p2.price);

            ctx.save();
            ctx.beginPath();
            ctx.strokeStyle = div.color;
            ctx.lineWidth = 2.5;
            ctx.setLineDash([4, 2]);
            ctx.moveTo(x1, y1);
            ctx.lineTo(x2, y2);
            ctx.stroke();

            // Glow / Halo
            ctx.strokeStyle = div.type === "BULLISH" ? "rgba(34, 197, 94, 0.4)" : "rgba(239, 68, 68, 0.4)";
            ctx.lineWidth = 6;
            ctx.stroke();
            ctx.restore();

            // Draw label at midpoint
            const midX = (x1 + x2) / 2;
            const midY = (y1 + y2) / 2 - (div.type === "BEARISH" ? 14 : -14);
            ctx.fillStyle = div.color;
            ctx.font = "bold 9px 'Inter', sans-serif";
            ctx.textAlign = "center";
            ctx.fillText(div.type === "BULLISH" ? "⚡ BULLISH DIV" : "⚡ BEARISH DIV", midX, midY);
        });
    }

    // ─── 6. Draw RSI (14) Sub-Panel (if enabled) ───
    if (showRsi && rsiPanelHeight > 0) {
        ctx.strokeStyle = "rgba(255, 255, 255, 0.12)";
        ctx.strokeRect(paddingLeft, rsiTop, plotWidth, rsiBottom - rsiTop);

        const rsiRange = 100;
        const getRsiY = (r) => rsiBottom - (r / rsiRange) * (rsiBottom - rsiTop);

        // Overbought 70 (Dashed Red)
        const y70 = getRsiY(70);
        ctx.strokeStyle = "rgba(239, 68, 68, 0.5)";
        ctx.beginPath();
        ctx.setLineDash([3, 3]);
        ctx.moveTo(paddingLeft, y70);
        ctx.lineTo(width - paddingRight, y70);
        ctx.stroke();
        ctx.fillStyle = "#ef4444";
        ctx.fillText("70 OB", width - paddingRight + 6, y70 + 3);

        // Oversold 30 (Dashed Green)
        const y30 = getRsiY(30);
        ctx.strokeStyle = "rgba(34, 197, 94, 0.5)";
        ctx.beginPath();
        ctx.moveTo(paddingLeft, y30);
        ctx.lineTo(width - paddingRight, y30);
        ctx.stroke();
        ctx.fillStyle = "#22c55e";
        ctx.fillText("30 OS", width - paddingRight + 6, y30 + 3);

        // Centerline 50 (Dotted Grey)
        const y50 = getRsiY(50);
        ctx.strokeStyle = "rgba(255, 255, 255, 0.1)";
        ctx.beginPath();
        ctx.moveTo(paddingLeft, y50);
        ctx.lineTo(width - paddingRight, y50);
        ctx.stroke();
        ctx.setLineDash([]);

        // RSI Curve
        ctx.beginPath();
        ctx.strokeStyle = "#818cf8";
        ctx.lineWidth = 2.0;
        let rsiStarted = false;
        for (let i = 0; i < count; i++) {
            const val = inst.rsiData[i];
            if (val !== null) {
                const x = getXForIndex(i);
                const y = getRsiY(val);
                if (!rsiStarted) { ctx.moveTo(x, y); rsiStarted = true; }
                else { ctx.lineTo(x, y); }
            }
        }
        ctx.stroke();

        // RSI Swing Markers & Divergence Connecting Trendlines
        if (inst.divergenceData) {
            inst.divergenceData.swingHighs.forEach(sh => {
                const x = getXForIndex(sh.index);
                const y = getRsiY(sh.rsi);
                ctx.fillStyle = "#ef4444";
                ctx.beginPath();
                ctx.arc(x, y, 3.5, 0, Math.PI * 2);
                ctx.fill();
            });

            inst.divergenceData.swingLows.forEach(sl => {
                const x = getXForIndex(sl.index);
                const y = getRsiY(sl.rsi);
                ctx.fillStyle = "#22c55e";
                ctx.beginPath();
                ctx.arc(x, y, 3.5, 0, Math.PI * 2);
                ctx.fill();
            });

            // Connecting Lines on RSI Panel
            inst.divergenceData.divergences.forEach(div => {
                const x1 = getXForIndex(div.p1.index);
                const y1 = getRsiY(div.p1.rsi);
                const x2 = getXForIndex(div.p2.index);
                const y2 = getRsiY(div.p2.rsi);

                ctx.save();
                ctx.beginPath();
                ctx.strokeStyle = div.color;
                ctx.lineWidth = 2.5;
                ctx.setLineDash([4, 2]);
                ctx.moveTo(x1, y1);
                ctx.lineTo(x2, y2);
                ctx.stroke();

                ctx.strokeStyle = div.type === "BULLISH" ? "rgba(34, 197, 94, 0.4)" : "rgba(239, 68, 68, 0.4)";
                ctx.lineWidth = 5;
                ctx.stroke();
                ctx.restore();
            });
        }

        // Panel Title
        ctx.fillStyle = "#a5b4fc";
        ctx.font = "bold 10px 'Inter', sans-serif";
        ctx.fillText(`RSI (${technicalChartConfigs.rsiPeriod}) Wilder Smoothed`, paddingLeft + 8, rsiTop + 14);
    }

    // ─── 7. Draw MACD (4H) Sub-Panel (if enabled) ───
    if (showMacd && macdPanelHeight > 0 && inst.macdData) {
        ctx.strokeStyle = "rgba(255, 255, 255, 0.12)";
        ctx.strokeRect(paddingLeft, macdTop, plotWidth, macdBottom - macdTop);

        // Find max MACD & Histogram amplitude
        let maxMacdAbs = 0.5;
        for (let i = 0; i < count; i++) {
            const m = inst.macdData.macdLine[i];
            const s = inst.macdData.signalLine[i];
            const h = inst.macdData.histogram[i];
            if (m !== null) maxMacdAbs = Math.max(maxMacdAbs, Math.abs(m));
            if (s !== null) maxMacdAbs = Math.max(maxMacdAbs, Math.abs(s));
            if (h !== null) maxMacdAbs = Math.max(maxMacdAbs, Math.abs(h));
        }
        maxMacdAbs *= 1.25;

        const zeroY = macdTop + (macdBottom - macdTop) / 2;
        const getMacdY = (val) => zeroY - (val / maxMacdAbs) * ((macdBottom - macdTop) / 2);

        // Zero Line
        ctx.strokeStyle = "rgba(255, 255, 255, 0.25)";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(paddingLeft, zeroY);
        ctx.lineTo(width - paddingRight, zeroY);
        ctx.stroke();

        ctx.fillStyle = "#94a3b8";
        ctx.font = "9px 'JetBrains Mono', monospace";
        ctx.fillText("0.00", width - paddingRight + 6, zeroY + 3);

        // Draw Histogram Bars
        for (let i = 0; i < count; i++) {
            const h = inst.macdData.histogram[i];
            if (h !== null) {
                const x = getXForIndex(i);
                const y = getMacdY(h);
                const hBarH = Math.abs(y - zeroY);
                const isPos = h >= 0;
                const prevH = i > 0 ? inst.macdData.histogram[i - 1] : 0;
                const isGrowing = Math.abs(h) >= Math.abs(prevH || 0);

                if (isPos) {
                    ctx.fillStyle = isGrowing ? "#10b981" : "#34d399";
                } else {
                    ctx.fillStyle = isGrowing ? "#ef4444" : "#f87171";
                }

                const top = isPos ? y : zeroY;
                ctx.fillRect(x - candleBodyWidth / 2, top, candleBodyWidth, Math.max(1, hBarH));
            }
        }

        // Draw MACD Line (Cyan)
        ctx.beginPath();
        ctx.strokeStyle = "#06b6d4";
        ctx.lineWidth = 1.8;
        let macdStarted = false;
        for (let i = 0; i < count; i++) {
            const m = inst.macdData.macdLine[i];
            if (m !== null) {
                const x = getXForIndex(i);
                const y = getMacdY(m);
                if (!macdStarted) { ctx.moveTo(x, y); macdStarted = true; }
                else { ctx.lineTo(x, y); }
            }
        }
        ctx.stroke();

        // Draw Signal Line (Amber)
        ctx.beginPath();
        ctx.strokeStyle = "#f59e0b";
        ctx.lineWidth = 1.8;
        let sigStarted = false;
        for (let i = 0; i < count; i++) {
            const s = inst.macdData.signalLine[i];
            if (s !== null) {
                const x = getXForIndex(i);
                const y = getMacdY(s);
                if (!sigStarted) { ctx.moveTo(x, y); sigStarted = true; }
                else { ctx.lineTo(x, y); }
            }
        }
        ctx.stroke();

        // Sub-panel Title
        ctx.fillStyle = "#06b6d4";
        ctx.font = "bold 10px 'Inter', sans-serif";
        ctx.fillText(`MACD (${technicalChartConfigs.macdFast}, ${technicalChartConfigs.macdSlow}, ${technicalChartConfigs.macdSignal})`, paddingLeft + 8, macdTop + 14);
    }

    // ─── 8. Draw Crosshair (if active) ───
    if (inst.hoverIndex !== null && inst.hoverIndex >= 0 && inst.hoverIndex < count) {
        const hX = getXForIndex(inst.hoverIndex);
        ctx.strokeStyle = "rgba(255, 255, 255, 0.4)";
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 4]);

        ctx.beginPath();
        ctx.moveTo(hX, priceTop);
        ctx.lineTo(hX, height - paddingBottom);
        ctx.stroke();
        ctx.setLineDash([]);

        // Highlight Candle
        const hCandle = inst.candles[inst.hoverIndex];
        const hY = getYForPrice(hCandle.close);
        ctx.fillStyle = "#ffffff";
        ctx.beginPath();
        ctx.arc(hX, hY, 4, 0, Math.PI * 2);
        ctx.fill();
    }

    // ─── 9. Draw Date / Time Axis at Bottom ───
    ctx.fillStyle = "#64748b";
    ctx.font = "10px 'JetBrains Mono', monospace";
    ctx.textAlign = "center";

    const dateStep = Math.max(1, Math.floor(count / 6));
    for (let i = 0; i < count; i += dateStep) {
        const c = inst.candles[i];
        const x = getXForIndex(i);
        const label = c.dateStr ? c.dateStr.slice(5) : "";
        ctx.fillText(label, x, height - 8);
    }

    ctx.restore();
}

function openChartSettingsModal(containerId) {
    const existing = document.getElementById("chart-settings-modal");
    if (existing) existing.remove();

    const div = document.createElement("div");
    div.id = "chart-settings-modal";
    div.className = "lic-modal";
    div.style.display = "flex";
    div.innerHTML = `
        <div class="lic-modal-content" style="max-width: 420px;">
            <div class="lic-modal-header">
                <h3>⚙️ Indicator Settings</h3>
                <button class="lic-close" onclick="document.getElementById('chart-settings-modal').remove()">✕</button>
            </div>
            <div class="lic-modal-body" style="padding: 16px;">
                <div style="margin-bottom: 12px;">
                    <label style="font-size:0.8rem;color:#94a3b8;display:block;margin-bottom:4px;">MACD Fast Period</label>
                    <input type="number" id="cs_macd_fast" value="${technicalChartConfigs.macdFast}" class="lic-input" style="width:100%;">
                </div>
                <div style="margin-bottom: 12px;">
                    <label style="font-size:0.8rem;color:#94a3b8;display:block;margin-bottom:4px;">MACD Slow Period</label>
                    <input type="number" id="cs_macd_slow" value="${technicalChartConfigs.macdSlow}" class="lic-input" style="width:100%;">
                </div>
                <div style="margin-bottom: 12px;">
                    <label style="font-size:0.8rem;color:#94a3b8;display:block;margin-bottom:4px;">MACD Signal Smoothing</label>
                    <input type="number" id="cs_macd_sig" value="${technicalChartConfigs.macdSignal}" class="lic-input" style="width:100%;">
                </div>
                <div style="margin-bottom: 12px;">
                    <label style="font-size:0.8rem;color:#94a3b8;display:block;margin-bottom:4px;">RSI Period (Wilder Smoothed)</label>
                    <input type="number" id="cs_rsi_p" value="${technicalChartConfigs.rsiPeriod}" class="lic-input" style="width:100%;">
                </div>
                <div style="margin-bottom: 16px;">
                    <label style="font-size:0.8rem;color:#94a3b8;display:block;margin-bottom:4px;">Fractal Pivot Confirmation Window (N candles)</label>
                    <input type="number" id="cs_fractal_n" value="${technicalChartConfigs.fractalN}" class="lic-input" style="width:100%;">
                </div>
                <button class="btn btn-primary" style="width:100%;" onclick="saveChartSettings('${containerId}')">
                    Apply Settings
                </button>
            </div>
        </div>
    `;
    document.body.appendChild(div);
}

function saveChartSettings(containerId) {
    const fast = parseInt(document.getElementById("cs_macd_fast")?.value, 10) || 12;
    const slow = parseInt(document.getElementById("cs_macd_slow")?.value, 10) || 26;
    const sig = parseInt(document.getElementById("cs_macd_sig")?.value, 10) || 9;
    const rsi = parseInt(document.getElementById("cs_rsi_p")?.value, 10) || 14;
    const frac = parseInt(document.getElementById("cs_fractal_n")?.value, 10) || 5;

    technicalChartConfigs.macdFast = fast;
    technicalChartConfigs.macdSlow = slow;
    technicalChartConfigs.macdSignal = sig;
    technicalChartConfigs.rsiPeriod = rsi;
    technicalChartConfigs.fractalN = frac;

    const modal = document.getElementById("chart-settings-modal");
    if (modal) modal.remove();

    const inst = activeChartInstances[containerId];
    if (inst) {
        loadTechnicalChartData(containerId, inst.symbol, inst.timeframe);
    }
}

function generateLiveRecommendation(stock, history, marketStatus) {
    const price = stock.price || 0;
    const change = stock.change || 0;
    const volume = stock.volume || 0;
    const avgVol = stock.avgVolume || stock.volume || 1;
    const closes = history.length ? history.map(d => d.close) : [price];

    // Compute technical indicators
    const rsi = calcTechnicalRSI(closes);
    const macd = calcTechnicalMACD(closes);
    const bb = calcTechnicalBollingerBands(closes);
    const vwap = calcTechnicalVWAP(price, volume, history);
    const sr = calcSupportResistance(price, history);

    // Order flow buy/sell ratio (simulated intraday depth balance from price change + volume)
    let buyRatio = 50;
    if (change > 3) buyRatio = 72;
    else if (change > 1) buyRatio = 62;
    else if (change > 0) buyRatio = 54;
    else if (change < -3) buyRatio = 28;
    else if (change < -1) buyRatio = 38;
    else if (change < 0) buyRatio = 46;
    const sellRatio = 100 - buyRatio;

    // Technical scoring (0 - 100)
    let score = 50;
    const factors = [];

    // Factor 1: Momentum & Change %
    if (change >= 3) {
        score += 15;
        factors.push({ icon: "📈", text: `Strong intraday bullish momentum (+${change.toFixed(2)}%)`, weight: "High" });
    } else if (change >= 0.5) {
        score += 8;
        factors.push({ icon: "🔼", text: `Positive price movement (+${change.toFixed(2)}%)`, weight: "Med" });
    } else if (change <= -3) {
        score -= 15;
        factors.push({ icon: "📉", text: `Heavy intraday selling pressure (${change.toFixed(2)}%)`, weight: "High" });
    } else if (change <= -0.5) {
        score -= 8;
        factors.push({ icon: "🔽", text: `Negative price movement (${change.toFixed(2)}%)`, weight: "Med" });
    }

    // Factor 2: VWAP Crossover
    if (price > vwap) {
        score += 10;
        factors.push({ icon: "⚡", text: `Trading ABOVE Volume Weighted Avg Price (₨${vwap.toFixed(2)})`, weight: "High" });
    } else {
        score -= 10;
        factors.push({ icon: "⚠️", text: `Trading BELOW Volume Weighted Avg Price (₨${vwap.toFixed(2)})`, weight: "High" });
    }

    // Factor 3: RSI (14)
    if (rsi < 30) {
        score += 15;
        factors.push({ icon: "🟢", text: `RSI Oversold (${rsi.toFixed(1)}) — Potential Reversal Bounce`, weight: "High" });
    } else if (rsi > 70) {
        score -= 12;
        factors.push({ icon: "🔴", text: `RSI Overbought (${rsi.toFixed(1)}) — Pullback Risk`, weight: "High" });
    } else if (rsi >= 50 && rsi <= 65) {
        score += 6;
        factors.push({ icon: "✅", text: `RSI in Healthy Bullish Zone (${rsi.toFixed(1)})`, weight: "Med" });
    } else {
        factors.push({ icon: "➡️", text: `RSI Neutral (${rsi.toFixed(1)})`, weight: "Low" });
    }

    // Factor 4: MACD Histogram
    if (macd.histogram > 0) {
        score += 8;
        factors.push({ icon: "📊", text: "MACD Histogram Positive (Bullish Momentum)", weight: "Med" });
    } else {
        score -= 8;
        factors.push({ icon: "📊", text: "MACD Histogram Negative (Bearish Trend)", weight: "Med" });
    }

    // Factor 5: Order Flow Ratio
    if (buyRatio >= 60) {
        score += 10;
        factors.push({ icon: "🛒", text: `Buy Order Flow Dominance (${buyRatio}% Buyers)`, weight: "High" });
    } else if (sellRatio >= 60) {
        score -= 10;
        factors.push({ icon: "🏷️", text: `Sell Order Flow Dominance (${sellRatio}% Sellers)`, weight: "High" });
    }

    // Factor 6: Volume Spike
    const volRatio = volume / Math.max(1, avgVol);
    if (volRatio > 1.5) {
        score += (change >= 0 ? 10 : -10);
        factors.push({ icon: "🔥", text: `High Volume Spike (${formatVolume(volume)} vs 30D avg)`, weight: "High" });
    }

    // Cap score 0 - 100
    score = Math.max(5, Math.min(95, score));

    // Signal Recommendation Determination
    let recommendation = "HOLD";
    let signalClass = "signal-hold";
    let signalColor = "#fbbf24";

    if (score >= 75) {
        recommendation = "STRONG BUY";
        signalClass = "signal-strong-buy";
        signalColor = "#22c55e";
    } else if (score >= 60) {
        recommendation = "BUY";
        signalClass = "signal-buy";
        signalColor = "#4ade80";
    } else if (score <= 30) {
        recommendation = "STRONG SELL";
        signalClass = "signal-strong-sell";
        signalColor = "#ef4444";
    } else if (score <= 42) {
        recommendation = "SELL";
        signalClass = "signal-sell";
        signalColor = "#f87171";
    }

    // Risk level
    let riskLevel = "Medium";
    if (rsi > 75 || rsi < 25 || Math.abs(change) > 5) riskLevel = "High";
    else if (score >= 50 && score <= 65 && Math.abs(change) < 2) riskLevel = "Low";

    // Target, Entry, Stop Loss
    const suggestedEntry = price;
    const targetMultiplier = score >= 60 ? 1.045 : score <= 40 ? 0.955 : 1.025;
    const stopMultiplier = score >= 60 ? 0.975 : score <= 40 ? 1.025 : 0.98;

    const targetPrice = price * targetMultiplier;
    const stopLoss = price * stopMultiplier;

    return {
        recommendation,
        signalClass,
        signalColor,
        confidence: score,
        riskLevel,
        suggestedEntry,
        targetPrice,
        stopLoss,
        rsi,
        macd,
        bb,
        vwap,
        sr,
        buyRatio,
        sellRatio,
        factors
    };
}

function renderLiveTrading(data) {
    const container = document.getElementById("live-trading-content");
    const statusText = document.getElementById("live-market-status-text");
    const marketBadge = document.getElementById("live-market-badge");
    if (!container) return;

    const stock = data.stockInfo;
    const history = data.history || [];
    const marketStatus = data.marketStatus || { status: "Closed", is_open: false };
    const company = data.companyData || {};

    // Update Market Status Badge
    if (statusText && marketBadge) {
        if (marketStatus.is_open) {
            marketBadge.className = "live-market-badge status-open";
            statusText.textContent = `PSX Status: OPEN (${marketStatus.reason})`;
        } else {
            marketBadge.className = "live-market-badge status-closed";
            statusText.textContent = `PSX Status: CLOSED (${marketStatus.reason})`;
        }
    }

    // Generate technical analysis & trading signal
    const rec = generateRecommendation(stock, history, marketStatus);

    const price = stock.price || 0;
    const change = stock.change || 0;
    const isPos = change >= 0;

    let html = `
    <!-- Top Hero Section: Stock Quote + Recommendation Badge -->
    <div class="live-hero-card">
        <div class="live-hero-left">
            <div class="live-symbol-row">
                <span class="live-symbol">${stock.symbol}</span>
                <span class="live-sector-badge">${stock.sector}</span>
                ${stock.isKSE100 ? '<span class="kse100-badge">KSE-100</span>' : ''}
            </div>
            <div class="live-company-name">${stock.name}</div>
            <div class="live-price-row">
                <span class="live-current-price">₨${price.toLocaleString('en-PK', {minimumFractionDigits:2})}</span>
                <span class="live-price-change ${isPos ? 'positive' : 'negative'}">
                    ${isPos ? '+' : ''}${change.toFixed(2)}%
                </span>
            </div>
            <div class="live-last-refresh">Updated: ${data.timestamp || 'Just now'}</div>
        </div>

        <div class="live-hero-right">
            <div class="live-recommendation-box ${rec.signalClass}">
                <div class="rec-label">Trading Recommendation</div>
                <div class="rec-value" style="color: ${rec.signalColor}">${rec.recommendation}</div>
                <div class="rec-confidence">
                    <span>Confidence: <strong>${rec.confidence}%</strong></span>
                    <span>Risk: <strong class="risk-${rec.riskLevel.toLowerCase()}">${rec.riskLevel}</strong></span>
                </div>
            </div>
        </div>
    </div>

    <!-- Suggested Entry, Target & Stop-Loss Bar -->
    <div class="live-targets-grid">
        <div class="target-card entry-card">
            <div class="tc-label">Suggested Entry</div>
            <div class="tc-value">₨${rec.suggestedEntry.toFixed(2)}</div>
            <div class="tc-sub">Current Market Price</div>
        </div>
        <div class="target-card profit-card">
            <div class="tc-label">Target Price (Take Profit)</div>
            <div class="tc-value positive">₨${rec.targetPrice.toFixed(2)}</div>
            <div class="tc-sub">${((rec.targetPrice - price)/price*100).toFixed(2)}% upside</div>
        </div>
        <div class="target-card stop-card">
            <div class="tc-label">Suggested Stop-Loss</div>
            <div class="tc-value negative">₨${rec.stopLoss.toFixed(2)}</div>
            <div class="tc-sub">${((rec.stopLoss - price)/price*100).toFixed(2)}% downside limit</div>
        </div>
    </div>

    <!-- Order Flow & Market Depth Section -->
    <div class="live-section-card">
        <div class="section-card-title">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2v20M17 5H9.5a3.5 3.5 0 000 7h5a3.5 3.5 0 010 7H6"/></svg>
            Order Flow & Market Depth Balance
        </div>
        <div class="order-flow-container">
            <div class="order-flow-labels">
                <span class="buy-label">Buy Volume (${rec.buyRatio}%)</span>
                <span class="sell-label">Sell Volume (${rec.sellRatio}%)</span>
            </div>
            <div class="order-flow-bar-bg">
                <div class="order-flow-buy-fill" style="width: ${rec.buyRatio}%"></div>
            </div>
        </div>
    </div>

    <!-- Pro Multi-Panel Technical Chart Studio (4H / MACD / RSI Divergence) -->
    <div class="live-section-card chart-studio-card">
        <div class="section-card-title" style="display:flex; justify-content:space-between; align-items:center;">
            <div style="display:flex; align-items:center; gap:8px;">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 3v18h18"/><path d="M18 17V9"/><path d="M13 17V5"/><path d="M8 17v-3"/></svg>
                <span>Interactive Technical Charting Studio (4H MACD & RSI Divergence)</span>
            </div>
        </div>
        <div id="live-technical-chart-container"></div>
    </div>

    <!-- Technical Indicators Grid (VWAP, RSI, MACD, Bollinger, Support/Resistance) -->
    <div class="live-indicators-grid">
        <!-- VWAP -->
        <div class="indicator-card">
            <div class="ind-header">
                <span class="ind-name">VWAP</span>
                <span class="ind-badge ${price >= rec.vwap ? 'positive' : 'negative'}">${price >= rec.vwap ? 'ABOVE' : 'BELOW'}</span>
            </div>
            <div class="ind-value">₨${rec.vwap.toFixed(2)}</div>
            <div class="ind-desc">Volume Weighted Avg Price</div>
        </div>

        <!-- RSI (14) -->
        <div class="indicator-card">
            <div class="ind-header">
                <span class="ind-name">RSI (14)</span>
                <span class="ind-badge ${rec.rsi > 70 ? 'negative' : rec.rsi < 30 ? 'positive' : 'neutral'}">
                    ${rec.rsi > 70 ? 'OVERBOUGHT' : rec.rsi < 30 ? 'OVERSOLD' : 'NEUTRAL'}
                </span>
            </div>
            <div class="ind-value">${rec.rsi.toFixed(1)}</div>
            <div class="ind-desc">Relative Strength Index</div>
        </div>

        <!-- MACD -->
        <div class="indicator-card">
            <div class="ind-header">
                <span class="ind-name">MACD (12,26,9)</span>
                <span class="ind-badge ${rec.macd.histogram >= 0 ? 'positive' : 'negative'}">
                    ${rec.macd.histogram >= 0 ? 'BULLISH' : 'BEARISH'}
                </span>
            </div>
            <div class="ind-value">${rec.macd.histogram >= 0 ? '+' : ''}${rec.macd.histogram.toFixed(2)}</div>
            <div class="ind-desc">Histogram Momentum</div>
        </div>

        <!-- Bollinger Bands -->
        <div class="indicator-card">
            <div class="ind-header">
                <span class="ind-name">Bollinger %B</span>
                <span class="ind-badge neutral">${rec.bb.percentB.toFixed(0)}%</span>
            </div>
            <div class="ind-value">₨${rec.bb.lower.toFixed(2)} — ₨${rec.bb.upper.toFixed(2)}</div>
            <div class="ind-desc">Lower to Upper Band Range</div>
        </div>
    </div>

    <!-- Key Reasons & Contributing Factors -->
    <div class="live-section-card">
        <div class="section-card-title">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 11.08V12a10 10 0 11-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
            Key Decision Factors (${rec.factors.length})
        </div>
        <div class="live-factors-list">
            ${rec.factors.map(f => `
                <div class="live-factor-item">
                    <span class="factor-icon">${f.icon}</span>
                    <span class="factor-text">${f.text}</span>
                    <span class="factor-weight weight-${f.weight.toLowerCase()}">${f.weight} Impact</span>
                </div>
            `).join('')}
        </div>
    </div>

    <!-- Company Announcements & Disclosures -->
    ${company.announcements && company.announcements.length ? `
    <div class="live-section-card">
        <div class="section-card-title">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
            Recent Disclosures & Announcements
        </div>
        <div class="live-announcements-list">
            ${company.announcements.slice(0, 3).map(a => `
                <div class="live-announce-item">
                    <span class="announce-date">${a.date}</span>
                    <span class="announce-title">${a.title}</span>
                    ${a.link ? `<a href="${a.link}" target="_blank" class="announce-pdf">PDF</a>` : ''}
                </div>
            `).join('')}
        </div>
    </div>
    ` : ''}

    <!-- Intraday Risk Disclaimer -->
    <div class="live-disclaimer">
        ⚠️ <strong>Intraday Risk Disclaimer:</strong> Analysis is AI-generated based on available PSX market data and technical indicators. Intraday trading carries financial risk. This analysis is for research purposes only and should not be considered investment advice.
    </div>
    `;

    container.innerHTML = html;

    // Initialize Interactive Technical Chart Studio (Default: 4H PSX Session Timeframe)
    initInteractiveTechnicalChart("live-technical-chart-container", stock.symbol, "4H");
}

// Renamed helper to avoid name clashes
function generateRecommendation(stock, history, marketStatus) {
    return generateLiveRecommendation(stock, history, marketStatus);
}

// ─── Paper Trading Simulator Engine ───
let simCash = 1000000.0;
let simPositions = {}; // { SYMBOL: { shares: 1000, avgPrice: 11.5, type: 'LONG' } }
let simOrders = [];
let simCurrentMarketType = "regular";
let simAutoRefreshTimer = null;

function loadSimPortfolio() {
    try {
        const savedCash = localStorage.getItem("psx_sim_cash");
        const savedPositions = localStorage.getItem("psx_sim_positions");
        if (savedCash !== null) simCash = parseFloat(savedCash);
        if (savedPositions !== null) simPositions = JSON.parse(savedPositions);
    } catch (e) {
        console.error("Could not load sim portfolio:", e);
    }
}

function saveSimPortfolio() {
    try {
        localStorage.setItem("psx_sim_cash", simCash.toString());
        localStorage.setItem("psx_sim_positions", JSON.stringify(simPositions));
    } catch (e) {
        console.error("Could not save sim portfolio:", e);
    }
}

function resetSimPortfolio() {
    if (confirm("Reset virtual trading portfolio back to PKR 1,000,000 cash?")) {
        simCash = 1000000.0;
        simPositions = {};
        simOrders = [];
        saveSimPortfolio();
        renderSimPositions();
        alert("Portfolio reset to ₨1,000,000.00 cash.");
    }
}

function refreshSimPrices() {
    const refreshBtn = document.getElementById("btn-refresh-sim-prices");
    if (refreshBtn) {
        refreshBtn.disabled = true;
        refreshBtn.innerHTML = `<svg class="spinning" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M23 4v6h-6M1 20v-6h6"/><path d="M20.49 9A9 9 0 005.64 5.64L1 10m22 4l-4.64 4.36A9 9 0 013.51 15"/></svg> <span>Syncing PSX...</span>`;
    }

    fetch("/api/stocks")
        .then(r => r.json())
        .then(res => {
            if (res.success && res.data) {
                STOCKS = res.data;
                loadSimQuote();
                renderSimPositions();
            }
        })
        .catch(err => console.error("Error refreshing sim prices:", err))
        .finally(() => {
            if (refreshBtn) {
                refreshBtn.disabled = false;
                refreshBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M23 4v6h-6M1 20v-6h6"/><path d="M20.49 9A9 9 0 005.64 5.64L1 10m22 4l-4.64 4.36A9 9 0 013.51 15"/></svg> <span>Refresh Prices</span>`;
            }
        });
}

function initTradingSimulator() {
    loadSimPortfolio();
    loadSimQuote();
    renderSimPositions();
    updateSimTicketUI();

    if (!simAutoRefreshTimer) {
        simAutoRefreshTimer = setInterval(() => {
            if (currentView === "simulator") {
                refreshSimPrices();
            }
        }, 10000);
    }
}

function loadSimQuote() {
    const input = document.getElementById("sim-symbol-input");
    const symbol = input ? (input.value.trim().toUpperCase() || "UNITY") : "UNITY";
    const stock = STOCKS.find(s => s.symbol === symbol) || { symbol, price: 11.51, change: 0 };
    const price = stock.price || 11.51;

    const bid = (price * 0.999).toFixed(2);
    const ask = (price * 1.001).toFixed(2);
    const lowerLock = (price * 0.925).toFixed(2);
    const upperLock = (price * 1.075).toFixed(2);

    document.getElementById("sim-bid-price").textContent = `₨${bid}`;
    document.getElementById("sim-ask-price").textContent = `₨${ask}`;
    document.getElementById("sim-price-input").value = price.toFixed(2);
    document.getElementById("sim-lock-indicator").textContent = `Circuit Limits: Lower ₨${lowerLock} — Upper ₨${upperLock}`;

    updateSimEstTotal();
}

function updateSimTicketUI() {
    const actionSelect = document.getElementById("sim-order-action");
    const action = actionSelect ? actionSelect.value : "BUY";
    const btn = document.getElementById("btn-execute-order");
    if (!btn) return;

    if (action === "BUY") {
        btn.textContent = "Submit BUY Order";
        btn.className = "btn-execute-order buy-btn";
    } else if (action === "SELL") {
        btn.textContent = "Submit SELL Order";
        btn.className = "btn-execute-order sell-btn";
    } else if (action === "SHORT") {
        btn.textContent = "Submit SHORT SELL Order";
        btn.className = "btn-execute-order short-btn";
    } else if (action === "COVER") {
        btn.textContent = "Submit COVER SHORT Order";
        btn.className = "btn-execute-order cover-btn";
    }

    updateSimEstTotal();
}

function updateSimEstTotal() {
    const qtyInput = document.getElementById("sim-quantity-input");
    const priceInput = document.getElementById("sim-price-input");
    const estTotalEl = document.getElementById("sim-est-total");

    const qty = parseInt(qtyInput.value, 10) || 0;
    const price = parseFloat(priceInput.value) || 0;
    const total = qty * price;

    if (estTotalEl) {
        estTotalEl.textContent = `₨${total.toLocaleString('en-PK', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    }
}

function executeSimOrder() {
    const symbolInput = document.getElementById("sim-symbol-input");
    const symbol = symbolInput ? symbolInput.value.trim().toUpperCase() : "UNITY";
    const action = document.getElementById("sim-order-action").value;
    const orderType = document.getElementById("sim-order-type").value;
    const qty = parseInt(document.getElementById("sim-quantity-input").value, 10) || 0;
    const price = parseFloat(document.getElementById("sim-price-input").value) || 0;

    if (!symbol || qty <= 0 || price <= 0) {
        alert("Please enter a valid symbol, quantity (>0), and price.");
        return;
    }

    // Odd Lot market validation rule (<50 shares)
    if (simCurrentMarketType === "regular" && qty < 50) {
        alert("Regular Market requires orders of 50+ shares. Switch to Odd Lot Market for smaller orders.");
        return;
    } else if (simCurrentMarketType === "oddlot" && qty >= 50) {
        alert("Odd Lot Market is strictly for orders under 50 shares. Switch to Regular Market.");
        return;
    }

    const stock = STOCKS.find(s => s.symbol === symbol) || { symbol, price: price };
    const curPrice = stock.price || price;
    const execPrice = orderType === "MARKET" ? curPrice : price;
    const totalCost = qty * execPrice;

    if (action === "BUY") {
        if (totalCost > simCash) {
            alert(`Insufficient cash balance! Required: ₨${totalCost.toFixed(2)}, Available: ₨${simCash.toFixed(2)}`);
            return;
        }
        simCash -= totalCost;
        if (!simPositions[symbol]) {
            const stockName = stock.name || symbol;
            simPositions[symbol] = {
                shares: qty, avgPrice: execPrice, type: 'LONG',
                companyName: stockName,
                purchaseDate: new Date().toISOString(),
                ath: execPrice, athDate: new Date().toISOString(),
                atl: execPrice, atlDate: new Date().toISOString()
            };
        } else {
            const pos = simPositions[symbol];
            const newShares = pos.shares + qty;
            const newAvg = ((pos.shares * pos.avgPrice) + totalCost) / newShares;
            pos.shares = newShares;
            pos.avgPrice = newAvg;
        }
        alert(`Successfully executed BUY order for ${qty} shares of ${symbol} @ ₨${execPrice.toFixed(2)}`);
    } else if (action === "SELL") {
        const pos = simPositions[symbol];
        if (!pos || pos.shares < qty || pos.type !== 'LONG') {
            alert(`You do not own ${qty} long shares of ${symbol} to sell.`);
            return;
        }
        simCash += totalCost;
        pos.shares -= qty;
        if (pos.shares <= 0) delete simPositions[symbol];
        alert(`Successfully executed SELL order for ${qty} shares of ${symbol} @ ₨${execPrice.toFixed(2)}`);
    } else if (action === "SHORT") {

        if (totalCost > simCash * 0.5) {
            alert("Short selling margin requirement: Max short value is 50% of available cash.");
            return;
        }
        simCash += totalCost;
        if (!simPositions[symbol]) {
            simPositions[symbol] = { shares: qty, avgPrice: execPrice, type: 'SHORT' };
        } else {
            const pos = simPositions[symbol];
            const newShares = pos.shares + qty;
            const newAvg = ((pos.shares * pos.avgPrice) + totalCost) / newShares;
            simPositions[symbol] = { shares: newShares, avgPrice: newAvg, type: 'SHORT' };
        }
        alert(`Successfully executed SHORT order for ${qty} shares of ${symbol} @ ₨${execPrice.toFixed(2)}`);
    } else if (action === "COVER") {
        const pos = simPositions[symbol];
        if (!pos || pos.shares < qty || pos.type !== 'SHORT') {
            alert(`You do not have a short position of ${qty} shares in ${symbol} to cover.`);
            return;
        }
        simCash -= totalCost;
        pos.shares -= qty;
        if (pos.shares <= 0) delete simPositions[symbol];
        alert(`Successfully COVERED short position for ${qty} shares of ${symbol} @ ₨${execPrice.toFixed(2)}`);
    }

    saveSimPortfolio();
    renderSimPositions();
}

function renderSimPositions() {
    const tbody = document.getElementById("sim-positions-body");
    const cashEl = document.getElementById("sim-cash");
    const equityEl = document.getElementById("sim-equity");
    const totalValEl = document.getElementById("sim-total-val");
    const pnlEl = document.getElementById("sim-pnl");
    if (!tbody) return;

    let investedEquity = 0;
    let totalUnrealizedPnL = 0;

    const symbols = Object.keys(simPositions);
    if (symbols.length === 0) {
        tbody.innerHTML = `<tr><td colspan="9" style="text-align:center; padding:32px; color:var(--text-tertiary);">No open positions. Place an order on the left ticket.</td></tr>`;
    } else {
        let rowsHtml = "";
        symbols.forEach(sym => {
            const pos = simPositions[sym];
            const stock = STOCKS.find(s => s.symbol === sym) || { price: pos.avgPrice };
            const curPrice = stock.price || pos.avgPrice;
            const mktVal = pos.shares * curPrice;

            let pnl = 0;
            if (pos.type === 'LONG') {
                pnl = (curPrice - pos.avgPrice) * pos.shares;
                investedEquity += mktVal;
            } else {
                pnl = (pos.avgPrice - curPrice) * pos.shares;
                investedEquity += (pos.shares * pos.avgPrice);
            }
            totalUnrealizedPnL += pnl;

            // ATH / ATL update
            if (!pos.ath || curPrice > pos.ath) {
                pos.ath = curPrice;
                pos.athDate = new Date().toISOString();
            }
            if (!pos.atl || curPrice < pos.atl) {
                pos.atl = curPrice;
                pos.atlDate = new Date().toISOString();
            }

            const pnlPct = pos.avgPrice ? ((curPrice - pos.avgPrice) / pos.avgPrice * 100) : 0;
            const pnlClass = pnl >= 0 ? "positive" : "negative";
            const pnlSign = pnl >= 0 ? "+" : "";
            const companyName = pos.companyName || sym;
            const buyDate = pos.purchaseDate ? new Date(pos.purchaseDate).toLocaleDateString("en-PK") : "—";

            rowsHtml += `<tr>
                <td><strong>${sym}</strong><div style="font-size:0.7rem;color:var(--text-tertiary);margin-top:2px;">${companyName}</div></td>
                <td><span class="pos-type-badge ${pos.type.toLowerCase()}">${pos.type}</span></td>
                <td>${pos.shares.toLocaleString()}</td>
                <td>₨${pos.avgPrice.toFixed(2)}<div style="font-size:0.68rem;color:var(--text-tertiary);">${buyDate}</div></td>
                <td>₨${curPrice.toFixed(2)}</td>
                <td>₨${mktVal.toLocaleString('en-PK', {minimumFractionDigits:2})}</td>
                <td class="${pnlClass}">${pnlSign}₨${pnl.toLocaleString('en-PK', {minimumFractionDigits:2})}<div style="font-size:0.72rem;">${pnlSign}${pnlPct.toFixed(2)}%</div></td>
                <td style="font-size:0.72rem;">
                    <div style="color:#10b981;">▲ ₨${(pos.ath||curPrice).toFixed(2)}</div>
                    <div style="color:#ef4444;">▼ ₨${(pos.atl||curPrice).toFixed(2)}</div>
                </td>
                <td>
                    <button class="btn btn-primary btn-sm" onclick="analyzePosition('${sym}')" style="margin-bottom:4px;width:100%;">
                        🤖 Analyze
                    </button>
                    <button class="btn btn-ghost btn-sm" onclick="quickCloseSimPos('${sym}')" style="width:100%;">Close</button>
                </td>
            </tr>`;
        });
        tbody.innerHTML = rowsHtml;
        saveSimPortfolio(); // persist ATH/ATL updates
    }

    const totalPortfolioVal = simCash + investedEquity + totalUnrealizedPnL;
    const initialCap = 1000000.0;
    const overallPnL = totalPortfolioVal - initialCap;
    const overallPnLPct = (overallPnL / initialCap) * 100;

    if (cashEl) cashEl.textContent = `₨${simCash.toLocaleString('en-PK', {minimumFractionDigits:2, maximumFractionDigits:2})}`;
    if (equityEl) equityEl.textContent = `₨${investedEquity.toLocaleString('en-PK', {minimumFractionDigits:2, maximumFractionDigits:2})}`;
    if (totalValEl) totalValEl.textContent = `₨${totalPortfolioVal.toLocaleString('en-PK', {minimumFractionDigits:2, maximumFractionDigits:2})}`;
    if (pnlEl) {
        const sign = overallPnL >= 0 ? "+" : "";
        pnlEl.textContent = `${sign}₨${overallPnL.toLocaleString('en-PK', {minimumFractionDigits:2})} (${sign}${overallPnLPct.toFixed(2)}%)`;
        pnlEl.className = `stat-value ${overallPnL >= 0 ? 'positive' : 'negative'}`;
    }
}

function quickCloseSimPos(symbol) {
    const pos = simPositions[symbol];
    if (!pos) return;
    document.getElementById("sim-symbol-input").value = symbol;
    document.getElementById("sim-order-action").value = pos.type === 'LONG' ? 'SELL' : 'COVER';
    document.getElementById("sim-quantity-input").value = pos.shares;
    loadSimQuote();
}

// ─── AI Position Analysis ───
function analyzePosition(symbol) {
    const pos = simPositions[symbol];
    if (!pos) return;

    const modal = document.getElementById("position-analysis-modal");
    if (!modal) return;

    // Show loading state
    modal.style.display = "flex";
    modal.innerHTML = `
        <div class="pos-modal-box">
            <div class="pos-modal-loading">
                <div class="pos-loading-spinner"></div>
                <h3>Fetching Live PSX Data...</h3>
                <p>Running AI analysis for <strong>${symbol}</strong> — please wait</p>
            </div>
        </div>`;

    const deviceId = getDeviceId();
    const params = new URLSearchParams({
        symbol,
        buyPrice: pos.avgPrice,
        qty: pos.shares,
        purchaseDate: pos.purchaseDate || "",
        deviceId: deviceId
    });

    fetch(`/api/position-analysis?${params}`)
        .then(r => {
            if (r.status === 402) {
                closePositionModal();
                initTrialSystem();
                throw new Error("Trial expired");
            }
            return r.json();
        })
        .then(res => {
            if (res.success && res.data) {
                renderPositionAnalysisModal(res.data, pos);
            } else {
                modal.innerHTML = `<div class="pos-modal-box" style="text-align:center;padding:40px;">
                    <p style="color:#ef4444;">⚠ ${res.error || 'Could not load analysis for ' + symbol}</p>
                    <button class="btn btn-ghost" onclick="closePositionModal()">Close</button>
                </div>`;
            }
        })
        .catch(err => {
            if (err.message !== "Trial expired") {
                modal.innerHTML = `<div class="pos-modal-box" style="text-align:center;padding:40px;">
                    <p style="color:#ef4444;">Network error. Please check your connection.</p>
                    <button class="btn btn-ghost" onclick="closePositionModal()">Close</button>
                </div>`;
            }
        });
}

function closePositionModal() {
    const modal = document.getElementById("position-analysis-modal");
    if (modal) modal.style.display = "none";
}

function renderPositionAnalysisModal(d, pos) {
    const modal = document.getElementById("position-analysis-modal");
    if (!modal) return;

    const fmt = (n) => Number(n).toLocaleString('en-PK', {minimumFractionDigits:2, maximumFractionDigits:2});
    const fmtDate = (iso) => iso ? new Date(iso).toLocaleDateString("en-PK", {day:"2-digit",month:"short",year:"numeric",hour:"2-digit",minute:"2-digit"}) : "—";

    const pnlColor = d.pnlPKR >= 0 ? "#10b981" : "#ef4444";
    const pnlSign = d.pnlPKR >= 0 ? "+" : "";

    const recColors = {
        "Strong Buy": "#10b981", "Buy More": "#34d399",
        "Hold": "#f59e0b",
        "Partial Sell": "#fb923c", "Sell": "#ef4444", "Strong Sell": "#dc2626"
    };
    const recBg = recColors[d.recommendation] || "#f59e0b";

    const ath = pos.ath || d.currentPrice;
    const atl = pos.atl || d.currentPrice;
    const fromAth = (((d.currentPrice - ath) / ath) * 100).toFixed(2);
    const fromAtl = (((d.currentPrice - atl) / atl) * 100).toFixed(2);

    const t = d.technicals;

    const signalPill = (label, val, good, bad) => {
        const cls = val >= good ? "signal-bull" : val <= bad ? "signal-bear" : "signal-neutral";
        return `<span class="signal-pill ${cls}">${label}: ${val}</span>`;
    };

    const maSignal = (ma, label) => {
        const cls = d.currentPrice > ma ? "signal-bull" : "signal-bear";
        return `<span class="signal-pill ${cls}">${label}: ₨${ma}</span>`;
    };

    modal.innerHTML = `
    <div class="pos-modal-box">
        <!-- Header -->
        <div class="pos-modal-header">
            <div>
                <div class="pos-modal-symbol">${d.symbol}</div>
                <div class="pos-modal-name">${d.companyName} · ${d.sector}</div>
                <div style="font-size:0.72rem;color:var(--text-tertiary);margin-top:2px;">
                    Analysis at ${d.timestamp} · Market: ${d.marketStatus?.status || "—"}
                </div>
            </div>
            <button class="pos-modal-close" onclick="closePositionModal()">✕</button>
        </div>

        <!-- Scrollable content -->
        <div class="pos-modal-body">

            <!-- 1. AI Recommendation Banner -->
            <div class="pos-section rec-banner" style="background:${recBg}18;border:1px solid ${recBg}44;">
                <div class="rec-main">
                    <div class="rec-badge" style="background:${recBg};color:#fff;">${d.recommendation}</div>
                    <div class="rec-meta">
                        <div class="rec-stat"><span>Confidence</span><strong>${d.confidence}%</strong></div>
                        <div class="rec-stat"><span>Risk</span><strong style="color:${d.riskLevel==='High'?'#ef4444':d.riskLevel==='Low'?'#10b981':'#f59e0b'}">${d.riskLevel}</strong></div>
                        <div class="rec-stat"><span>Trend</span><strong style="color:${d.trend==='Bullish'?'#10b981':d.trend==='Bearish'?'#ef4444':'#f59e0b'}">${d.trend}</strong></div>
                        <div class="rec-stat"><span>Score</span><strong>${d.score}/100</strong></div>
                    </div>
                </div>
                <div class="confidence-bar-wrap">
                    <div class="confidence-bar" style="width:${d.confidence}%;background:${recBg};"></div>
                </div>
            </div>

            <!-- 2. Position Overview -->
            <div class="pos-section">
                <div class="pos-section-title">📊 Position Overview</div>
                <div class="pos-overview-grid">
                    <div class="pos-ov-card"><span>Buy Price</span><strong>₨${fmt(d.buyPrice)}</strong></div>
                    <div class="pos-ov-card"><span>Current Price</span><strong>₨${fmt(d.currentPrice)}</strong></div>
                    <div class="pos-ov-card"><span>Quantity</span><strong>${d.quantity.toLocaleString()} shares</strong></div>
                    <div class="pos-ov-card"><span>Total Invested</span><strong>₨${fmt(d.totalInvestment)}</strong></div>
                    <div class="pos-ov-card"><span>Current Value</span><strong>₨${fmt(d.currentValue)}</strong></div>
                    <div class="pos-ov-card"><span>Unrealized P&L</span><strong style="color:${pnlColor}">${pnlSign}₨${fmt(d.pnlPKR)} (${pnlSign}${d.pnlPct}%)</strong></div>
                    <div class="pos-ov-card"><span>Today's Change</span><strong style="color:${d.changeToday>=0?'#10b981':'#ef4444'}">${d.changeToday>=0?'+':''}${d.changeToday?.toFixed(2)||0}%</strong></div>
                    <div class="pos-ov-card"><span>Purchase Date</span><strong>${fmtDate(d.purchaseDate)}</strong></div>
                </div>
            </div>

            <!-- 3. ATH / ATL Since Purchase -->
            <div class="pos-section">
                <div class="pos-section-title">🏔 ATH / ATL Since Purchase</div>
                <div class="ath-atl-grid">
                    <div class="ath-card">
                        <div class="ath-label">All-Time High Since Purchase</div>
                        <div class="ath-value ath-green">₨${fmt(ath)}</div>
                        <div class="ath-sub">${fmtDate(pos.athDate)}</div>
                        <div class="ath-diff" style="color:${fromAth<0?'#ef4444':'#10b981'}">
                            ${fromAth >= 0 ? '+' : ''}${fromAth}% from ATH
                        </div>
                    </div>
                    <div class="ath-card">
                        <div class="ath-label">All-Time Low Since Purchase</div>
                        <div class="ath-value ath-red">₨${fmt(atl)}</div>
                        <div class="ath-sub">${fmtDate(pos.atlDate)}</div>
                        <div class="ath-diff" style="color:${fromAtl>0?'#10b981':'#ef4444'}">
                            ${fromAtl >= 0 ? '+' : ''}${fromAtl}% from ATL
                        </div>
                    </div>
                </div>
            </div>

            <!-- 4. AI Explanation -->
            <div class="pos-section">
                <div class="pos-section-title">🧠 AI Analysis</div>
                <p class="pos-explanation">${d.explanation}</p>
                <div class="pos-outlook">
                    <strong>Short-Term Outlook (1–5 Days):</strong> ${d.shortTermOutlook}
                </div>
            </div>

            <!-- 5. Technical Analysis -->
            <div class="pos-section">
                <div class="pos-section-title">📈 Technical Signals</div>
                <div class="signals-grid">
                    ${signalPill("RSI", t.rsi, 50, 40)}
                    ${signalPill("MACD", t.macd?.toFixed(3), 0, 0)}
                    ${maSignal(t.ma20, "MA20")}
                    ${maSignal(t.ma50, "MA50")}
                    ${maSignal(t.ma100, "MA100")}
                    ${maSignal(t.ma200, "MA200")}
                    <span class="signal-pill signal-neutral">VWAP: ₨${t.vwap}</span>
                    <span class="signal-pill signal-neutral">BB Upper: ₨${t.bbUpper}</span>
                    <span class="signal-pill signal-neutral">BB Lower: ₨${t.bbLower}</span>
                </div>
                <div class="pos-levels">
                    <div class="level-item"><span>🟢 Support</span><strong>₨${t.support}</strong></div>
                    <div class="level-item"><span>🔴 Resistance</span><strong>₨${t.resistance}</strong></div>
                </div>
            </div>

            <!-- 6. Fundamental Analysis -->
            <div class="pos-section">
                <div class="pos-section-title">📋 Fundamentals</div>
                <div class="pos-overview-grid">
                    <div class="pos-ov-card"><span>P/E Ratio</span><strong>${d.pe ? d.pe.toFixed(1)+'x' : '—'}</strong></div>
                    <div class="pos-ov-card"><span>Dividend Yield</span><strong>${d.divYield ? d.divYield.toFixed(1)+'%' : '—'}</strong></div>
                    <div class="pos-ov-card"><span>Market Cap</span><strong>₨${d.marketCap ? (d.marketCap/1e9).toFixed(2)+'B' : '—'}</strong></div>
                    <div class="pos-ov-card"><span>Volume Today</span><strong>${d.volume ? d.volume.toLocaleString() : '—'}</strong></div>
                </div>
            </div>

            <!-- 7. Risks -->
            <div class="pos-section">
                <div class="pos-section-title">⚠ Risks to Monitor</div>
                <ul class="pos-risks">
                    ${d.risks.map(r => `<li>${r}</li>`).join("")}
                </ul>
            </div>

        </div>

        <!-- Footer -->
        <div class="pos-modal-footer">
            <button class="btn btn-primary" onclick="analyzePosition('${d.symbol}')">
                🔄 Re-Analyze with Latest Data
            </button>
            <button class="btn btn-ghost" onclick="closePositionModal()">Close</button>
        </div>
    </div>`;
}


// ─── Corporate Actions & Dividends Engine ───
function fetchCorporateActionsData() {
    const tbody = document.getElementById("corp-calendar-body");
    if (!tbody) return;

    tbody.innerHTML = `<tr><td colspan="9" style="text-align:center; padding:32px;">
        <div class="loading-spinner" style="width:28px;height:28px;margin:0 auto 10px;">
            <svg viewBox="0 0 50 50">
                <circle cx="25" cy="25" r="20" fill="none" stroke="var(--accent-cyan)" stroke-width="4" stroke-linecap="round" stroke-dasharray="80 200">
                    <animateTransform attributeName="transform" type="rotate" from="0 25 25" to="360 25 25" dur="1s" repeatCount="indefinite"/>
                </circle>
            </svg>
        </div>
        <div style="color:var(--text-tertiary);font-size:0.85rem;">Loading live PSX dividend calendar & corporate actions...</div>
    </td></tr>`;

    fetch("/api/dividends-corporate-actions")
        .then(r => {
            if (!r.ok) throw new Error("HTTP " + r.status);
            return r.json();
        })
        .then(res => {
            if (res.success && res.data && res.data.dividendCalendar) {
                renderCorporateCalendar(res.data.dividendCalendar);
            } else {
                tbody.innerHTML = `<tr><td colspan="9" style="text-align:center; padding:24px; color:var(--text-tertiary);">No corporate action data returned. <button class="btn btn-ghost" style="margin-left:8px;padding:4px 10px;" onclick="fetchCorporateActionsData()">Retry</button></td></tr>`;
            }
        })
        .catch(err => {
            console.error("Error fetching corporate actions:", err);
            tbody.innerHTML = `<tr><td colspan="9" style="text-align:center; padding:24px; color:#f87171;">Failed to load dividend calendar (${err.message}). <button class="btn btn-ghost" style="margin-left:8px;padding:4px 10px;" onclick="fetchCorporateActionsData()">🔄 Retry</button></td></tr>`;
        });
}

function renderCorporateCalendar(calendar) {
    const tbody = document.getElementById("corp-calendar-body");
    if (!tbody) return;

    if (!calendar || calendar.length === 0) {
        tbody.innerHTML = `<tr><td colspan="9" style="text-align:center; padding:24px;">No upcoming dividend declarations found.</td></tr>`;
        return;
    }

    let html = "";
    calendar.forEach(item => {
        const isUpcoming = item.status === "Upcoming";
        html += `<tr>
            <td>
                <span class="stock-symbol" style="cursor:pointer;color:var(--accent-cyan);font-weight:700;" onclick="showDetail('${item.symbol}')" title="Click to view full analysis for ${item.symbol}">
                    ${item.symbol}
                </span>
            </td>
            <td style="font-weight:500;">${item.name}</td>
            <td><strong class="positive">${item.dividendAmount}</strong></td>
            <td>${item.announcementDate}</td>
            <td><span class="ex-date-badge">${item.exDividendDate}</span></td>
            <td>${item.recordDate}</td>
            <td>${item.bookClosure}</td>
            <td>${item.paymentDate}</td>
            <td><span class="status-pill ${isUpcoming ? 'upcoming' : 'completed'}">${item.status}</span></td>
        </tr>`;
    });
    tbody.innerHTML = html;
}

// Corporate Action Calculators
function calcBonusShares() {
    const shares = parseFloat(document.getElementById("bonus-shares-input").value) || 0;
    const bonusPct = parseFloat(document.getElementById("bonus-pct-input").value) || 0;
    const price = parseFloat(document.getElementById("bonus-price-input").value) || 0;

    const newShares = Math.floor(shares * (1 + bonusPct / 100));
    const exPrice = price / (1 + bonusPct / 100);
    const resultEl = document.getElementById("bonus-calc-result");
    if (resultEl) {
        resultEl.innerHTML = `New Total Shares: <strong>${newShares.toLocaleString()}</strong> | Ex-Bonus Price: <strong>₨${exPrice.toFixed(2)}</strong>`;
    }
}

function calcRightShares() {
    const shares = parseFloat(document.getElementById("right-shares-input").value) || 0;
    const rightPct = parseFloat(document.getElementById("right-pct-input").value) || 0;
    const offerPrice = parseFloat(document.getElementById("right-offer-price").value) || 0;
    const curPrice = 100.0;

    const rightShares = Math.floor(shares * (rightPct / 100));
    const terp = ((shares * curPrice) + (rightShares * offerPrice)) / (shares + rightShares);
    const resultEl = document.getElementById("right-calc-result");
    if (resultEl) {
        resultEl.innerHTML = `Right Entitlement: <strong>${rightShares.toLocaleString()} shares @ ₨${offerPrice.toFixed(2)}</strong> | TERP: <strong>₨${terp.toFixed(2)}</strong>`;
    }
}

function calcStockSplit() {
    const shares = parseFloat(document.getElementById("split-shares-input").value) || 0;
    const ratio = parseFloat(document.getElementById("split-ratio-select").value) || 2;
    const price = parseFloat(document.getElementById("split-price-input").value) || 0;

    const newShares = shares * ratio;
    const postPrice = price / ratio;
    const resultEl = document.getElementById("split-calc-result");
    if (resultEl) {
        resultEl.innerHTML = `New Total Shares: <strong>${newShares.toLocaleString()}</strong> | Post-Split Price: <strong>₨${postPrice.toFixed(2)}</strong>`;
    }
}

// ─── Financial Statements & Automated 10 Financial Ratios Engine ───
function fetchFinancialStatements(symbol) {
    if (!symbol) return;
    const loading = document.getElementById("fin-loading");
    const workspace = document.getElementById("fin-workspace");
    if (loading) loading.style.display = "flex";

    const deviceId = getDeviceId();
    fetch(`/api/financial-statements?symbol=${encodeURIComponent(symbol)}&deviceId=${deviceId}`)
        .then(r => {
            if (r.status === 402) {
                initTrialSystem();
                throw new Error("Trial expired");
            }
            return r.json();
        })
        .then(res => {
            if (loading) loading.style.display = "none";
            if (res.success && res.data) {
                const ratios = calculate10FinancialRatios(res.data);
                renderFinancialStatementsWorkspace(res.data, ratios);
            } else {
                if (workspace) workspace.innerHTML = `<div class="upper-lock-empty"><p>Symbol '${symbol}' not found in PSX screener database.</p></div>`;
            }
        })
        .catch(err => {
            if (loading) loading.style.display = "none";
            if (err.message !== "Trial expired" && workspace) {
                workspace.innerHTML = `<div class="upper-lock-empty"><p>Error loading financials: ${err.message}</p></div>`;
            }
        });
}

function safeNum(val, defaultVal = 0) {
    if (val === null || val === undefined || isNaN(Number(val)) || !isFinite(Number(val))) return defaultVal;
    return Number(val);
}

function safeRatio(val, digits = 2, suffix = "") {
    if (val === null || val === undefined || isNaN(Number(val)) || !isFinite(Number(val))) return "—";
    return Number(val).toFixed(digits) + suffix;
}

function formatFinVal(val) {
    if (val === null || val === undefined || isNaN(Number(val))) return "0";
    const num = Number(val);
    const abs = Math.abs(num);
    let str = "";
    if (abs >= 1e9) str = (abs / 1e9).toFixed(2) + "B";
    else if (abs >= 1e6) str = (abs / 1e6).toFixed(2) + "M";
    else if (abs >= 1e3) str = (abs / 1e3).toFixed(1) + "K";
    else str = abs.toFixed(0);
    return (num < 0 ? "-" : "") + str;
}

function calculate10FinancialRatios(fin) {
    if (!fin) return {};
    const bs = fin.balanceSheet || {};
    const is = fin.incomeStatement || {};
    const cf = fin.cashFlowStatement || {};

    const curAssets = safeNum(bs.currentAssets, 1);
    const curLiab = safeNum(bs.currentLiabilities, 1);
    const inv = safeNum(bs.inventory, 0);
    const cash = safeNum(bs.cash, 0);
    const debt = safeNum(bs.totalDebt, 0);
    const assets = safeNum(bs.totalAssets, 1);
    const equity = safeNum(bs.shareholderEquity, 1);
    const rev = safeNum(is.revenue, 1);
    const ebit = safeNum(is.ebit, 1);
    const interest = safeNum(is.interestExpense, 1);
    const netInc = safeNum(is.netIncome, 0);
    const cogs = safeNum(is.cogs, 1);

    // 1. Current Ratio
    const currentRatio = curLiab > 0 ? (curAssets / curLiab) : 0;
    // 2. Quick Ratio
    const quickRatio = curLiab > 0 ? ((curAssets - inv) / curLiab) : 0;
    // 3. Cash Ratio
    const cashRatio = curLiab > 0 ? (cash / curLiab) : 0;
    // 4. Debt-to-Equity Ratio
    const deRatio = equity > 0 ? (debt / equity) : 0;
    // 5. Debt-to-Asset Ratio
    const daRatio = assets > 0 ? (debt / assets) : 0;
    // 6. Return on Equity (ROE)
    const roe = equity > 0 ? ((netInc / equity) * 100) : 0;
    // 7. Return on Assets (ROA)
    const roa = assets > 0 ? ((netInc / assets) * 100) : 0;
    // 8. Return on Capital Employed (ROCE)
    const capEmployed = assets - curLiab;
    const roce = capEmployed > 0 ? ((ebit / capEmployed) * 100) : 0;
    // 9. Times Interest Earned (TIE)
    const tie = interest > 0 ? (ebit / interest) : 0;
    // 10. Days Sales Inventory (DSI)
    const dsi = cogs > 0 ? ((inv / cogs) * 365) : 0;

    return { currentRatio, quickRatio, cashRatio, deRatio, daRatio, roe, roa, roce, tie, dsi };
}

function renderFinancialStatementsWorkspace(fin, ratios) {
    const workspace = document.getElementById("fin-workspace");
    if (!workspace) return;

    ratios = ratios || {};
    const bs = fin.balanceSheet || {};
    const is = fin.incomeStatement || {};
    const cf = fin.cashFlowStatement || {};

    const companyName = fin.name || fin.companyName || fin.symbol;
    const period = bs.period || is.period || cf.period || "FY24 (Annual)";

    const operatingCF = cf.operatingCF ?? cf.operatingCashFlow ?? 0;
    const investingCF = cf.investingCF ?? cf.investingCashFlow ?? 0;
    const financingCF = cf.financingCF ?? cf.financingCashFlow ?? 0;
    const netCF = cf.netCF ?? cf.netChangeInCash ?? (operatingCF + investingCF + financingCF);
    const capex = cf.capex ?? Math.abs(investingCF * 0.7);
    const dividendsPaid = cf.dividendsPaid ?? 0;
    const opex = is.operatingExpenses ?? is.opExpenses ?? 0;
    const totalLiab = bs.totalLiabilities ?? (safeNum(bs.currentLiabilities) + safeNum(bs.totalDebt));

    let html = `
    <!-- Top Ratio Dashboard Cards (10 Financial Ratios) -->
    <div class="fin-ratios-section">
        <h3 class="fin-sec-title">Automated Financial Ratio Dashboard (10 Metrics)</h3>
        <div class="ratios-grid">
            <!-- Liquidity Ratios -->
            <div class="ratio-card">
                <div class="r-cat">Liquidity</div>
                <div class="r-name">Current Ratio</div>
                <div class="r-val ${safeNum(ratios.currentRatio) >= 1.5 ? 'positive' : 'negative'}">${safeRatio(ratios.currentRatio, 2, "x")}</div>
                <div class="r-desc">Current Assets / Liabilities (Target: >1.5x)</div>
            </div>
            <div class="ratio-card">
                <div class="r-cat">Liquidity</div>
                <div class="r-name">Quick Ratio (Acid-Test)</div>
                <div class="r-val ${safeNum(ratios.quickRatio) >= 1.0 ? 'positive' : 'negative'}">${safeRatio(ratios.quickRatio, 2, "x")}</div>
                <div class="r-desc">(Assets - Inventory) / Liabilities</div>
            </div>
            <div class="ratio-card">
                <div class="r-cat">Liquidity</div>
                <div class="r-name">Cash Ratio</div>
                <div class="r-val">${safeRatio(ratios.cashRatio, 2, "x")}</div>
                <div class="r-desc">Cash & Equivalents / Liabilities</div>
            </div>

            <!-- Solvency Ratios -->
            <div class="ratio-card">
                <div class="r-cat">Solvency</div>
                <div class="r-name">Debt-to-Equity (D/E)</div>
                <div class="r-val ${safeNum(ratios.deRatio) <= 1.0 ? 'positive' : 'negative'}">${safeRatio(ratios.deRatio, 2, "x")}</div>
                <div class="r-desc">Total Debt / Shareholder Equity</div>
            </div>
            <div class="ratio-card">
                <div class="r-cat">Solvency</div>
                <div class="r-name">Debt-to-Asset</div>
                <div class="r-val">${safeRatio(safeNum(ratios.daRatio) * 100, 1, "%")}</div>
                <div class="r-desc">Total Debt / Total Assets</div>
            </div>

            <!-- Profitability & Efficiency Ratios -->
            <div class="ratio-card">
                <div class="r-cat">Profitability</div>
                <div class="r-name">Return on Equity (ROE)</div>
                <div class="r-val positive">${safeRatio(ratios.roe, 1, "%")}</div>
                <div class="r-desc">Net Income / Shareholder Equity</div>
            </div>
            <div class="ratio-card">
                <div class="r-cat">Profitability</div>
                <div class="r-name">Return on Assets (ROA)</div>
                <div class="r-val positive">${safeRatio(ratios.roa, 1, "%")}</div>
                <div class="r-desc">Net Income / Total Assets</div>
            </div>
            <div class="ratio-card">
                <div class="r-cat">Profitability</div>
                <div class="r-name">ROCE</div>
                <div class="r-val positive">${safeRatio(ratios.roce, 1, "%")}</div>
                <div class="r-desc">Return on Capital Employed</div>
            </div>
            <div class="ratio-card">
                <div class="r-cat">Efficiency</div>
                <div class="r-name">Times Interest Earned (TIE)</div>
                <div class="r-val ${safeNum(ratios.tie) >= 3.0 ? 'positive' : 'negative'}">${safeRatio(ratios.tie, 1, "x")}</div>
                <div class="r-desc">EBIT / Interest Expense Coverage</div>
            </div>
            <div class="ratio-card">
                <div class="r-cat">Efficiency</div>
                <div class="r-name">Days Sales Inventory (DSI)</div>
                <div class="r-val">${safeRatio(ratios.dsi, 0, " Days")}</div>
                <div class="r-desc">Avg Days to Turn Inventory into Sales</div>
            </div>
        </div>
    </div>

    <!-- Financial Statements Sub-Tabs -->
    <div class="fin-statements-section">
        <h3 class="fin-sec-title">${fin.symbol} — Financial Statements (${companyName})</h3>
        
        <div class="stmt-grid">
            <!-- Balance Sheet Table -->
            <div class="stmt-card">
                <h4>Balance Sheet (${period})</h4>
                <table class="stmt-table">
                    <tr><td>Cash & Cash Equivalents</td><td>₨${formatFinVal(bs.cash)}</td></tr>
                    <tr><td>Receivables</td><td>₨${formatFinVal(bs.receivables)}</td></tr>
                    <tr><td>Inventory</td><td>₨${formatFinVal(bs.inventory)}</td></tr>
                    <tr class="row-bold"><td>Total Current Assets</td><td>₨${formatFinVal(bs.currentAssets)}</td></tr>
                    <tr><td>Property, Plant & Equipment</td><td>₨${formatFinVal(bs.nonCurrentAssets)}</td></tr>
                    <tr class="row-highlight"><td>TOTAL ASSETS</td><td>₨${formatFinVal(bs.totalAssets)}</td></tr>
                    <tr><td>Current Liabilities</td><td>₨${formatFinVal(bs.currentLiabilities)}</td></tr>
                    <tr><td>Total Debt</td><td>₨${formatFinVal(bs.totalDebt)}</td></tr>
                    <tr class="row-bold"><td>Total Liabilities</td><td>₨${formatFinVal(totalLiab)}</td></tr>
                    <tr class="row-highlight"><td>SHAREHOLDER EQUITY</td><td>₨${formatFinVal(bs.shareholderEquity)}</td></tr>
                </table>
            </div>

            <!-- Income Statement Table -->
            <div class="stmt-card">
                <h4>Income Statement (${period})</h4>
                <table class="stmt-table">
                    <tr class="row-bold"><td>Revenue (Sales)</td><td>₨${formatFinVal(is.revenue)}</td></tr>
                    <tr><td>Cost of Goods Sold (COGS)</td><td>(₨${formatFinVal(is.cogs)})</td></tr>
                    <tr class="row-bold"><td>Gross Profit</td><td>₨${formatFinVal(is.grossProfit)}</td></tr>
                    <tr><td>Operating Expenses (OpEx)</td><td>(₨${formatFinVal(opex)})</td></tr>
                    <tr class="row-bold"><td>EBIT (Operating Income)</td><td>₨${formatFinVal(is.ebit)}</td></tr>
                    <tr><td>Interest Expense</td><td>(₨${formatFinVal(is.interestExpense)})</td></tr>
                    <tr><td>EBT (Pre-Tax Income)</td><td>₨${formatFinVal(is.ebt)}</td></tr>
                    <tr><td>Tax Expense (29%)</td><td>(₨${formatFinVal(is.tax)})</td></tr>
                    <tr class="row-highlight"><td>NET INCOME</td><td>₨${formatFinVal(is.netIncome)}</td></tr>
                </table>
            </div>

            <!-- Cash Flow Statement Table -->
            <div class="stmt-card">
                <h4>Cash Flow Statement (${period})</h4>
                <table class="stmt-table">
                    <tr class="row-bold"><td>Cash Flow from Operations (CFO)</td><td>₨${formatFinVal(operatingCF)}</td></tr>
                    <tr><td>Capital Expenditures (CapEx)</td><td>(₨${formatFinVal(capex)})</td></tr>
                    <tr class="row-bold"><td>Cash Flow from Investing (CFI)</td><td>(₨${formatFinVal(Math.abs(investingCF))})</td></tr>
                    <tr><td>Dividends Paid</td><td>(₨${formatFinVal(dividendsPaid)})</td></tr>
                    <tr class="row-bold"><td>Cash Flow from Financing (CFF)</td><td>(₨${formatFinVal(Math.abs(financingCF))})</td></tr>
                    <tr class="row-highlight"><td>NET CHANGE IN CASH</td><td>₨${formatFinVal(netCF)}</td></tr>
                </table>
            </div>
        </div>
    </div>
    `;

    workspace.innerHTML = html;
}

// ─── PWA & Mobile Touch Interactivity ───
let deferredPrompt = null;

function initPWAAndMobile() {
    // Register Service Worker
    if ('serviceWorker' in navigator) {
        navigator.serviceWorker.register('/sw.js')
            .then(reg => console.log('[PWA] Service Worker registered:', reg.scope))
            .catch(err => console.error('[PWA] SW registration failed:', err));
    }

    // PWA Add to Home Screen Prompt
    window.addEventListener('beforeinstallprompt', (e) => {
        e.preventDefault();
        deferredPrompt = e;
        const banner = document.getElementById('pwa-install-banner');
        if (banner) banner.style.display = 'block';
    });

    document.getElementById('btn-pwa-install')?.addEventListener('click', () => {
        if (deferredPrompt) {
            deferredPrompt.prompt();
            deferredPrompt.userChoice.then((choiceResult) => {
                if (choiceResult.outcome === 'accepted') {
                    console.log('[PWA] User accepted install prompt');
                }
                deferredPrompt = null;
                const banner = document.getElementById('pwa-install-banner');
                if (banner) banner.style.display = 'none';
            });
        }
    });

    document.getElementById('btn-pwa-dismiss')?.addEventListener('click', () => {
        const banner = document.getElementById('pwa-install-banner');
        if (banner) banner.style.display = 'none';
    });

    // Mobile Bottom Navigation Click Listeners
    document.querySelectorAll('.mobile-nav-item').forEach(item => {
        item.addEventListener('click', () => {
            const view = item.dataset.view;
            if (view) switchView(view);
        });
    });

    // Mobile Touch Pull-To-Refresh
    let touchStartY = 0;
    let touchEndY = 0;
    const pullBar = document.getElementById('pull-to-refresh-bar');

    window.addEventListener('touchstart', (e) => {
        if (window.scrollY === 0) {
            touchStartY = e.touches[0].clientY;
        }
    }, { passive: true });

    window.addEventListener('touchmove', (e) => {
        if (window.scrollY === 0 && touchStartY > 0) {
            touchEndY = e.touches[0].clientY;
            const diff = touchEndY - touchStartY;
            if (diff > 40 && pullBar) {
                pullBar.classList.add('visible');
            }
        }
    }, { passive: true });

    window.addEventListener('touchend', () => {
        if (touchEndY - touchStartY > 80 && pullBar) {
            fetchLiveData();
            setTimeout(() => {
                pullBar.classList.remove('visible');
            }, 1000);
        } else if (pullBar) {
            pullBar.classList.remove('visible');
        }
        touchStartY = 0;
        touchEndY = 0;
    });
}

// Quick history from stock table — allow clicking a trend button
function quickStockHistory(symbol) {
    openStockHistory();
    const input = document.getElementById('history-search-input');
    input.value = symbol;
    searchStockHistory();
}

function updatePKTClock() {
    const el = document.getElementById("pkt-clock-text");
    if (!el) return;
    try {
        const now = new Date();
        const options = {
            timeZone: "Asia/Karachi",
            weekday: "short",
            day: "2-digit",
            month: "short",
            year: "numeric",
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
            hour12: true
        };
        const pktStr = new Intl.DateTimeFormat("en-PK", options).format(now);
        el.textContent = `${pktStr} PKT`;
    } catch (e) {
        console.error("Clock error:", e);
    }
}

function getDeviceId() {
    let devId = localStorage.getItem("psx_device_id");
    if (!devId) {
        devId = "dev_" + Math.random().toString(36).substring(2, 11) + Date.now().toString(36);
        localStorage.setItem("psx_device_id", devId);
    }
    return devId;
}

function toggleAccountMenu() {
    const menu = document.getElementById("account-dropdown-menu");
    if (menu) {
        menu.style.display = menu.style.display === "none" ? "block" : "none";
    }
}

document.addEventListener("click", (e) => {
    const badgeContainer = document.getElementById("account-badge-container");
    const menu = document.getElementById("account-dropdown-menu");
    if (menu && badgeContainer && !badgeContainer.contains(e.target)) {
        menu.style.display = "none";
    }
});

function updateAccountBadge(info) {
    const badgeContainer = document.getElementById("account-badge-container");
    const badgeIcon = document.getElementById("account-badge-icon");
    const badgeText = document.getElementById("account-badge-text");
    const accEmail = document.getElementById("acc-menu-email");
    const accStatus = document.getElementById("acc-menu-status");
    const accKeyRow = document.getElementById("acc-menu-key-row");
    const accKey = document.getElementById("acc-menu-key");
    const accUpgradeBtn = document.getElementById("acc-menu-upgrade-btn");

    if (!badgeContainer) return;
    badgeContainer.style.display = "inline-block";

    const email = info.email || localStorage.getItem("psx_user_email") || "Guest User";
    if (accEmail) accEmail.textContent = email;

    if (info.isPaid) {
        if (badgeIcon) badgeIcon.textContent = "🌟";
        if (badgeText) badgeText.textContent = `${email.split("@")[0]} (PRO)`;
        if (accStatus) {
            accStatus.textContent = "🌟 Pro Account Active";
            accStatus.style.background = "#065f46";
            accStatus.style.color = "#a7f3d0";
        }
        if (accKeyRow) {
            accKeyRow.style.display = "block";
            if (accKey) accKey.textContent = info.licenseKey || localStorage.getItem("psx_user_key") || "PSX-PRO-ACTIVE";
        }
        if (accUpgradeBtn) accUpgradeBtn.style.display = "none";
    } else if (info.trialActive) {
        if (badgeIcon) badgeIcon.textContent = "⏳";
        if (badgeText) badgeText.textContent = `${email.split("@")[0]} (Trial)`;
        if (accStatus) {
            accStatus.textContent = `⏳ 3-Day Trial Active (${info.daysLeft}d ${info.hoursLeft}h left)`;
            accStatus.style.background = "#312e81";
            accStatus.style.color = "#a5b4fc";
        }
        if (accKeyRow) accKeyRow.style.display = "none";
        if (accUpgradeBtn) accUpgradeBtn.style.display = "block";
    } else {
        if (badgeIcon) badgeIcon.textContent = "🔒";
        if (badgeText) badgeText.textContent = `${email.split("@")[0]} (Expired)`;
        if (accStatus) {
            accStatus.textContent = "🔒 Trial Expired";
            accStatus.style.background = "#991b1b";
            accStatus.style.color = "#fecaca";
        }
        if (accKeyRow) accKeyRow.style.display = "none";
        if (accUpgradeBtn) accUpgradeBtn.style.display = "block";
    }
}

let trialPollTimer = null;
let trialCountdownTimer = null;
let currentSecondsLeft = 0;

function initTrialSystem() {
    const deviceId = getDeviceId();
    const savedEmail = localStorage.getItem("psx_user_email") || "";
    const savedName = localStorage.getItem("psx_user_name") || "";
    const savedStartTs = localStorage.getItem("psx_trial_start_ts") || "";
    const savedKey = localStorage.getItem("psx_user_key") || "";

    const isLocalHost = window.location.hostname === "localhost" || 
                        window.location.hostname === "127.0.0.1" || 
                        window.location.hostname === "0.0.0.0" || 
                        window.location.hostname.startsWith("192.168.");

    // Hide opportunities tab and header button completely on online version
    const headerAlphaBtn = document.getElementById("btn-alpha-opportunities-header");
    const tabAlpha = document.getElementById("tab-trading-intelligence");
    const viewAlpha = document.getElementById("view-trading-intelligence");

    if (headerAlphaBtn) headerAlphaBtn.style.display = "inline-flex";
    if (tabAlpha) tabAlpha.style.display = "inline-flex";


    const emailInput = document.getElementById("trial-user-email");
    const licEmailInput = document.getElementById("lic-email-input");
    const licNameInput = document.getElementById("lic-name-input");

    if (emailInput && !emailInput.value && savedEmail) emailInput.value = savedEmail;
    if (licEmailInput && !licEmailInput.value && savedEmail) licEmailInput.value = savedEmail;
    if (licNameInput && !licNameInput.value && savedName) licNameInput.value = savedName;

    const params = new URLSearchParams({
        deviceId,
        ...(savedEmail ? { email: savedEmail } : {}),
        ...(savedKey ? { licenseKey: savedKey } : {}),
        ...(savedStartTs ? { origStartTs: savedStartTs } : {})
    });

    fetch(`/api/trial-status?${params}`)
        .then(r => r.json())
        .then(res => {
            if (res.success && res.data) {
                const info = res.data;
                if (info.email) {
                    localStorage.setItem("psx_user_email", info.email);
                }
                if (info.isPaid) {
                    localStorage.setItem("psx_is_pro", "true");
                    if (info.licenseKey) localStorage.setItem("psx_user_key", info.licenseKey);
                }
                if (info.createdAt) {
                    localStorage.setItem("psx_trial_start_ts", String(info.createdAt));
                }
                if (info.trialEnd) {
                    localStorage.setItem("psx_trial_end_ts", String(info.trialEnd));
                }

                const banner = document.getElementById("online-trial-banner");
                const bannerText = document.getElementById("online-trial-text");
                const paywallModal = document.getElementById("trial-paywall-modal");
                const trialEmailModal = document.getElementById("trial-email-modal");

                updateAccountBadge(info);

                if (info.isLocal) {
                    if (banner) banner.style.display = "none";
                    if (trialEmailModal) trialEmailModal.style.display = "none";
                    if (paywallModal) paywallModal.style.display = "none";
                    if (trialPollTimer) clearInterval(trialPollTimer);
                    if (trialCountdownTimer) clearInterval(trialCountdownTimer);
                    return;
                }

                if (info.isPaid) {
                    if (trialEmailModal) trialEmailModal.style.display = "none";
                    if (paywallModal) paywallModal.style.display = "none";
                    if (banner) {
                        banner.style.display = "flex";
                        banner.style.background = "linear-gradient(90deg, #064e3b, #047857)";
                        banner.style.borderColor = "#10b981";
                        banner.style.color = "#a7f3d0";
                        if (bannerText) bannerText.textContent = "🌟 PSX Screener Pro Member (Unlimited Lifetime Access)";
                    }
                    const upgradeBtn = document.querySelector(".btn-upgrade-top");
                    if (upgradeBtn) upgradeBtn.style.display = "none";
                    if (trialPollTimer) clearInterval(trialPollTimer);
                    if (trialCountdownTimer) clearInterval(trialCountdownTimer);
                    return;
                }

                if (info.needsEmail) {
                    showLoading(false);
                    if (banner) banner.style.display = "none";
                    if (trialEmailModal) trialEmailModal.style.display = "flex";
                } else if (!info.trialActive || (info.secondsLeft !== undefined && info.secondsLeft <= 0)) {
                    // Trial expired ➔ LOCK APP WITH PAYWALL IMMEDIATELY!
                    showLoading(false);
                    if (banner) banner.style.display = "none";
                    if (trialEmailModal) trialEmailModal.style.display = "none";
                    if (paywallModal) paywallModal.style.display = "flex";
                    if (trialPollTimer) { clearInterval(trialPollTimer); trialPollTimer = null; }
                    if (trialCountdownTimer) { clearInterval(trialCountdownTimer); trialCountdownTimer = null; }
                } else {
                    // Trial active
                    if (trialEmailModal) trialEmailModal.style.display = "none";
                    if (paywallModal) paywallModal.style.display = "none";
                    if (banner) banner.style.display = "flex";

                    const upgradeBtn = document.querySelector(".btn-upgrade-top");
                    currentSecondsLeft = info.secondsLeft || 0;

                    const updateCountdownUI = () => {
                        if (upgradeBtn) upgradeBtn.style.display = "inline-block";

                        if (currentSecondsLeft <= 0) {
                            if (banner) banner.style.display = "none";
                            if (paywallModal) paywallModal.style.display = "flex";
                            if (trialCountdownTimer) { clearInterval(trialCountdownTimer); trialCountdownTimer = null; }
                            return;
                        }

                        if (bannerText) {
                            if (currentSecondsLeft < 300) {
                                const m = Math.floor(currentSecondsLeft / 60);
                                const s = currentSecondsLeft % 60;
                                bannerText.textContent = `🚨 Free Trial Ending Soon: ${m}m ${s}s remaining!`;
                            } else {
                                const d = Math.floor(currentSecondsLeft / 86400);
                                const h = Math.floor((currentSecondsLeft % 86400) / 3600);
                                const m = Math.floor((currentSecondsLeft % 3600) / 60);
                                bannerText.textContent = `⏳ 3-Day Free Trial: ${d}d ${h}h ${m}m left · Enjoy full live PSX access`;
                            }
                        }
                        currentSecondsLeft--;
                    };

                    updateCountdownUI();

                    if (!trialCountdownTimer && !info.isPaid && currentSecondsLeft < 300) {
                        trialCountdownTimer = setInterval(() => {
                            currentSecondsLeft--;
                            updateCountdownUI();
                        }, 1000);
                    }

                    const pollMs = currentSecondsLeft < 300 ? 4000 : 20000;
                    if (trialPollTimer) clearInterval(trialPollTimer);
                    if (!info.isPaid) {
                        trialPollTimer = setInterval(initTrialSystem, pollMs);
                    }
                }
            }
        })
        .catch(e => console.error("Trial status check error:", e));
}

function submitTrialEmail() {
    const input = document.getElementById("trial-user-email");
    const msgEl = document.getElementById("trial-email-msg");
    const email = input ? input.value.trim() : "";

    if (!email || !email.includes("@")) {
        if (msgEl) {
            msgEl.style.display = "block";
            msgEl.className = "activation-msg msg-error";
            msgEl.textContent = "Please enter a valid email address.";
        }
        return;
    }

    localStorage.setItem("psx_user_email", email);

    const deviceId = getDeviceId();
    const params = new URLSearchParams({ email, deviceId });

    fetch(`/api/start-trial?${params}`)
        .then(r => r.json())
        .then(res => {
            if (msgEl) {
                msgEl.style.display = "block";
                if (res.success) {
                    if (res.createdAt) localStorage.setItem("psx_trial_start_ts", String(res.createdAt));
                    if (res.trialEnd) localStorage.setItem("psx_trial_end_ts", String(res.trialEnd));
                    msgEl.className = "activation-msg msg-success";
                    msgEl.textContent = "✔ " + (res.message || "3-Day Free Trial Started!");
                    setTimeout(() => {
                        const emailModal = document.getElementById("trial-email-modal");
                        if (emailModal) emailModal.style.display = "none";
                        initTrialSystem();
                        fetchLiveData(false, true);
                    }, 1200);
                } else {
                    msgEl.className = "activation-msg msg-error";
                    msgEl.textContent = "✖ " + (res.error || "Trial registration error.");
                }
            }
        })
        .catch(err => {
            if (msgEl) {
                msgEl.style.display = "block";
                msgEl.className = "activation-msg msg-error";
                msgEl.textContent = "Network error. Please check your connection.";
            }
        });
}

function switchToPaywallModal() {
    const emailModal = document.getElementById("trial-email-modal");
    const paywallModal = document.getElementById("trial-paywall-modal");
    if (emailModal) emailModal.style.display = "none";
    if (paywallModal) paywallModal.style.display = "flex";
}

function switchToTrialModal() {
    const emailModal = document.getElementById("trial-email-modal");
    const paywallModal = document.getElementById("trial-paywall-modal");
    if (paywallModal) paywallModal.style.display = "none";
    if (emailModal) emailModal.style.display = "flex";
}

function submitLicenseActivation() {
    const nameInput = document.getElementById("lic-name-input");
    const emailInput = document.getElementById("lic-email-input");
    const keyInput = document.getElementById("lic-key-input");
    const msgEl = document.getElementById("activation-msg");

    const name = nameInput ? nameInput.value.trim() : "";
    const email = emailInput ? emailInput.value.trim() : "";
    const key = keyInput ? keyInput.value.trim() : "";

    if (!name || !email || !key) {
        if (msgEl) {
            msgEl.style.display = "block";
            msgEl.className = "activation-msg msg-error";
            msgEl.textContent = "Please enter your Name, Email, and License Key.";
        }
        return;
    }

    localStorage.setItem("psx_user_email", email);
    localStorage.setItem("psx_user_name", name);

    const deviceId = getDeviceId();
    const params = new URLSearchParams({ name, email, key, deviceId });

    fetch(`/api/activate-license?${params}`)
        .then(r => r.json())
        .then(res => {
            if (msgEl) {
                msgEl.style.display = "block";
                if (res.success) {
                    msgEl.className = "activation-msg msg-success";
                    msgEl.textContent = "✔ " + res.message;
                    setTimeout(() => {
                        const modal = document.getElementById("trial-paywall-modal");
                        if (modal) modal.style.display = "none";
                        initTrialSystem();
                        fetchLiveData(false, true);
                    }, 1800);
                } else {
                    msgEl.className = "activation-msg msg-error";
                    msgEl.textContent = "✖ " + (res.error || "Invalid License Key.");
                }
            }
        })
        .catch(() => {
            if (msgEl) {
                msgEl.style.display = "block";
                msgEl.className = "activation-msg msg-error";
                msgEl.textContent = "Network error. Please check your connection.";
            }
        });
}

function toggleAcceptTermsButton() {
    const chk = document.getElementById("chk-accept-terms");
    const btn = document.getElementById("btn-accept-terms");
    if (btn && chk) {
        btn.disabled = !chk.checked;
    }
}

function acceptTermsAndConditions() {
    localStorage.setItem("psx_terms_accepted_v1", "true");
    const modal = document.getElementById("terms-disclaimer-modal");
    if (modal) modal.style.display = "none";
    initTrialSystem();
    fetchLiveData(false, true);
}

function initTermsCheck() {
    const accepted = localStorage.getItem("psx_terms_accepted_v1");
    if (!accepted) {
        const modal = document.getElementById("terms-disclaimer-modal");
        if (modal) modal.style.display = "flex";
    }
}

function startVisitorHeartbeat() {
    const sendPing = () => {
        try {
            const deviceId = getDeviceId();
            const email = localStorage.getItem("psx_user_email") || "";
            const activeTabEl = document.querySelector(".nav-tab.active") || document.querySelector(".tab-btn.active");
            const currentTab = activeTabEl ? activeTabEl.textContent.trim() : "Stock Screener";
            const params = new URLSearchParams({ deviceId, email, tab: currentTab });
            fetch(`/api/heartbeat?${params}`).catch(() => {});
        } catch (e) {}
    };
    sendPing();
    setInterval(sendPing, 25000);
    document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible") sendPing();
    });
}

// ─── Initialize ───
document.addEventListener("DOMContentLoaded", () => {
    document.getElementById("watchlist-count").textContent = watchlist.size;
    updateRangeLabels();
    initEventListeners();
    initPWAAndMobile();
    initTermsCheck();
    initTrialSystem();
    startVisitorHeartbeat();
    
    // Live Pakistan Clock Ticker
    updatePKTClock();
    setInterval(updatePKTClock, 1000);

    // Fetch live data on load & start continuous 20s auto-sync
    fetchLiveData();
    if (autoRefreshTimer) clearInterval(autoRefreshTimer);
    autoRefreshTimer = setInterval(() => {
        // Auto-refresh quietly in background every 20 seconds (max 20-30s lag)
        fetchLiveData(true, false);
    }, 20000);
});

// ═════════════════════════════════════════════════════════════════
// 🎯 PSX PRO TRADING INTELLIGENCE ENGINE (LOCAL ENGINE)
// ═════════════════════════════════════════════════════════════════

let intelligenceResults = [];
let marketRegimeData = null;
let currentIntelSubTab = "top-picks";

function switchIntelSubTab(subTab, btn) {
    currentIntelSubTab = subTab;
    document.querySelectorAll(".intel-subtab-btn").forEach(b => b.classList.remove("active"));
    if (btn) btn.classList.add("active");

    const subTabs = ["top-picks", "upper-locks", "scanner", "portfolio-reassess", "calculator"];
    subTabs.forEach(st => {
        const el = document.getElementById(`intel-subtab-${st}`);
        if (el) el.style.display = (st === subTab) ? "block" : "none";
    });

    if (subTab === "portfolio-reassess") {
        renderPortfolioReassessment();
    } else if (subTab === "calculator") {
        populateCalculatorDropdown();
        updateCalcMetrics();
    }
}

function runTradingIntelligenceEngine() {
    const stockList = (typeof STOCKS !== "undefined" && STOCKS && STOCKS.length > 0) ? STOCKS : [];
    if (stockList.length === 0) return;

    // 1. Evaluate Overall PSX Market Regime
    let advancers = 0, decliners = 0, unchanged = 0;
    let totalVol = 0;
    stockList.forEach(s => {
        const chg = typeof s.change === 'number' ? s.change : parseFloat(s.change || 0);
        if (chg > 0.05) advancers++;
        else if (chg < -0.05) decliners++;
        else unchanged++;
        totalVol += (s.volume || 0);
    });

    const advanceRatio = (advancers / (advancers + decliners || 1));
    let regime = "NEUTRAL";
    let regimeColor = "#facc15";

    if (advanceRatio >= 0.70) {
        regime = "VERY BULLISH";
        regimeColor = "#10b981";
    } else if (advanceRatio >= 0.55) {
        regime = "BULLISH";
        regimeColor = "#34d399";
    } else if (advanceRatio <= 0.30) {
        regime = "VERY BEARISH";
        regimeColor = "#ef4444";
    } else if (advanceRatio <= 0.45) {
        regime = "BEARISH";
        regimeColor = "#f87171";
    }

    marketRegimeData = {
        regime,
        advancers,
        decliners,
        unchanged,
        advanceRatio: Math.round(advanceRatio * 100),
        regimeColor
    };

    const regEl = document.getElementById("intel-market-regime");
    if (regEl) {
        regEl.textContent = regime;
        regEl.style.color = regimeColor;
    }
    const brEl = document.getElementById("intel-market-breadth");
    if (brEl) {
        brEl.innerHTML = `<span style="color:#10b981;">▲ ${advancers}</span> : <span style="color:#ef4444;">▼ ${decliners}</span> (${Math.round(advanceRatio * 100)}% Adv)`;
    }

    // 2. Multi-Factor Evaluation on All Stocks
    const candidates = [];

    stockList.forEach(stock => {
        const price = stock.price || 0;
        if (price <= 0.5) return; // Skip sub-penny stocks

        const chg = typeof stock.change === 'number' ? stock.change : parseFloat(stock.change || 0);
        const vol = stock.volume || 0;
        const pe = stock.pe || 0;
        const upperLimit = stock.upperLimit || (price * 1.075);
        const lowerLimit = stock.lowerLimit || (price * 0.925);
        const distToUpper = ((upperLimit - price) / price) * 100;
        const distToLower = ((price - lowerLimit) / price) * 100;

        // Liquidity Score (0-100)
        let exitLiquidity = 50;
        if (vol > 2000000) exitLiquidity = 95;
        else if (vol > 800000) exitLiquidity = 85;
        else if (vol > 250000) exitLiquidity = 70;
        else if (vol > 50000) exitLiquidity = 50;
        else if (vol > 10000) exitLiquidity = 30;
        else exitLiquidity = 15;

        // Entry Availability & Lock Detection
        let entryAvailability = "HIGH";
        let entryProbability = 90;
        let isUpperLocked = false;
        let lockClassification = "NONE";

        if (distToUpper <= 0.35 || chg >= 7.2) {
            isUpperLocked = true;
            if (vol > 500000) {
                lockClassification = "STRONG UPPER-LOCK MOMENTUM";
                entryAvailability = "LIMITED / LOCKED";
                entryProbability = 20; // Hard to buy, locked
            } else {
                lockClassification = "HIGH RISK / SPECULATIVE";
                entryAvailability = "LOW FILL PROBABILITY";
                entryProbability = 10;
            }
        } else if (distToUpper <= 2.5 && chg >= 4.0) {
            lockClassification = "APPROACHING UPPER-LOCK";
            entryAvailability = "HIGH";
            entryProbability = 85;
        } else if (chg <= -6.5) {
            lockClassification = "LOWER-LOCK DANGER";
            entryAvailability = "AVOID";
            entryProbability = 0;
        }

        // Multi-Factor Trade Scoring / 100
        let tradeScore = 50;
        let confidence = 70;
        let reasons = [];
        let risks = [];

        // Factor 1: Price & Volume Momentum
        if (chg > 1.5 && chg < 6.8) {
            tradeScore += 16;
            reasons.push("Strong positive intraday price momentum (+ " + chg.toFixed(1) + "%)");
        } else if (chg >= 6.8 && !isUpperLocked) {
            tradeScore += 10;
            reasons.push("High velocity move nearing upper circuit");
            risks.push("Approaching circuit resistance (possible lock or rejection)");
        } else if (chg >= 6.8 && isUpperLocked) {
            tradeScore -= 5;
            risks.push("LOCKED at upper limit — do not chase market orders with zero ask fill");
        } else if (chg < 0) {
            tradeScore -= 15;
            risks.push("Negative intraday momentum");
        }

        // Factor 2: Volume Confirmation
        if (vol > 500000) {
            tradeScore += 15;
            reasons.push("Heavy institutional volume confirmation (" + formatVolume(vol) + " shares)");
        } else if (vol > 100000) {
            tradeScore += 8;
            reasons.push("Healthy retail trading volume");
        } else {
            tradeScore -= 12;
            risks.push("Thin trading liquidity (high slippage risk on exit)");
        }

        // Factor 3: Market Alignment
        if (regime === "VERY BULLISH" || regime === "BULLISH") {
            tradeScore += 10;
            reasons.push("Aligned with broader KSE-100 bullish market regime");
        } else if (regime === "BEARISH" || regime === "VERY BEARISH") {
            tradeScore -= 12;
            risks.push("Headwind: Overall PSX market breadth is currently negative");
        }

        // Factor 4: Entry & Exit Feasibility
        if (exitLiquidity >= 75 && entryAvailability === "HIGH") {
            tradeScore += 12;
            reasons.push("High order execution fill rate & smooth exit liquidity");
        } else if (exitLiquidity < 40) {
            tradeScore -= 10;
            risks.push("Low exit liquidity score (" + exitLiquidity + "/100)");
        }

        // Factor 5: Risk & Valuation Filter
        if (pe > 0 && pe < 14) {
            tradeScore += 7;
            reasons.push("Attractive P/E valuation cushion (" + pe.toFixed(1) + "x)");
        } else if (pe > 40) {
            risks.push("High valuation multiple (P/E " + pe.toFixed(1) + "x)");
        }

        tradeScore = Math.max(10, Math.min(96, tradeScore));
        confidence = Math.min(92, Math.max(55, Math.round(tradeScore * 0.92)));

        // Risk Level (LOW, MED, HIGH)
        let riskScore = 100 - exitLiquidity + (chg > 5 ? 20 : 0);
        let riskLevel = "MEDIUM";
        if (riskScore <= 38) riskLevel = "LOW";
        else if (riskScore >= 68) riskLevel = "HIGH";

        // Action Determination
        let action = "WATCH";
        let actionBadgeClass = "badge-pullback";

        if (isUpperLocked) {
            action = "DO NOT CHASE (LOCKED)";
            actionBadgeClass = "badge-avoid";
        } else if (tradeScore >= 80 && exitLiquidity >= 65 && riskLevel !== "HIGH") {
            action = "🔥 STRONG BUY SETUP";
            actionBadgeClass = "badge-strong-buy";
        } else if (tradeScore >= 70 && exitLiquidity >= 50) {
            action = "🟢 BUY NOW";
            actionBadgeClass = "badge-buy";
        } else if (chg >= 5.5 && tradeScore >= 65) {
            action = "🟡 BUY ON PULLBACK";
            actionBadgeClass = "badge-pullback";
        } else if (tradeScore < 45 || chg <= -4.0) {
            action = "⛔ AVOID";
            actionBadgeClass = "badge-avoid";
        }

        // Realistic Target & Stop-Loss Modeling
        const target1 = Number((price * 1.035).toFixed(2));
        const target2 = Number((price * 1.070).toFixed(2));
        const target3 = Number((price * 1.110).toFixed(2));
        const stopLoss = Number((price * 0.975).toFixed(2));

        const target1Pct = 3.5;
        const target2Pct = 7.0;
        const stopPct = 2.5;

        const probTarget1 = Math.min(88, Math.max(45, Math.round(tradeScore * 0.88)));
        const probTarget2 = Math.min(72, Math.max(25, Math.round(tradeScore * 0.62)));
        const probStop = Math.max(12, Math.min(55, 100 - probTarget1));

        const riskReward = (target1Pct / stopPct).toFixed(1);
        const expectedValue = (((probTarget1 / 100) * target1Pct) - ((probStop / 100) * stopPct)).toFixed(2);

        candidates.push({
            symbol: stock.symbol,
            name: stock.name || stock.symbol,
            sector: stock.sector || "Other",
            price,
            change: chg,
            volume: vol,
            tradeScore,
            confidence,
            profitProb: probTarget1,
            probTarget2,
            probStop,
            riskScore,
            riskLevel,
            exitLiquidity,
            entryAvailability,
            entryProbability,
            isUpperLocked,
            lockClassification,
            distToUpper: distToUpper.toFixed(1),
            action,
            actionBadgeClass,
            entryZone: `₨${(price * 0.995).toFixed(2)} – ₨${(price * 1.005).toFixed(2)}`,
            target1,
            target2,
            target3,
            stopLoss,
            target1Pct,
            target2Pct,
            stopPct,
            riskReward: `1:${riskReward}`,
            expectedValue: Number(expectedValue),
            reasons: reasons.slice(0, 4),
            risks: risks.slice(0, 3)
        });
    });

    // Sort by Trade Score & Expected Value
    candidates.sort((a, b) => b.tradeScore - a.tradeScore || b.expectedValue - a.expectedValue);
    intelligenceResults = candidates;

    const highProbCount = candidates.filter(c => c.tradeScore >= 75 && c.entryAvailability === "HIGH").length;
    const lockCount = candidates.filter(c => c.isUpperLocked || parseFloat(c.distToUpper) <= 2.5).length;

    const hpEl = document.getElementById("intel-high-prob-count");
    if (hpEl) hpEl.textContent = highProbCount;
    const tcEl = document.getElementById("intel-top-count");
    if (tcEl) tcEl.textContent = Math.min(8, highProbCount || candidates.length);
    const lcEl = document.getElementById("intel-lock-count");
    if (lcEl) lcEl.textContent = lockCount;

    // Render Sub-Views
    renderTopPicksCards();
    renderUpperLocksRadar();
    renderIntelScannerTable();
    renderPortfolioReassessment();
    populateCalculatorDropdown();
}

function renderTopPicksCards() {
    const container = document.getElementById("intel-cards-container");
    if (!container) return;

    // Filter top 8 highest quality actionable candidates
    const topPicks = intelligenceResults.filter(c => c.action.includes("BUY") || c.tradeScore >= 70).slice(0, 8);

    if (topPicks.length === 0) {
        container.innerHTML = `
        <div style="grid-column: 1/-1; text-align:center; padding:50px 20px; background:#0f172a; border-radius:14px; border:1px dashed #334155;">
            <div style="font-size:2.5rem; margin-bottom:10px;">🛡️</div>
            <h3 style="color:#f8fafc; font-weight:800;">No High-Quality Trade Setups Detected Right Now</h3>
            <p style="color:#94a3b8; font-size:0.85rem; max-width:550px; margin:8px auto 0 auto;">
                Market Principle: Quality > Quantity. Current PSX market conditions do not present favorable risk-adjusted entries with sufficient liquidity. Waiting for higher probability setups is recommended.
            </p>
        </div>`;
        return;
    }

    let html = "";
    topPicks.forEach((stock, idx) => {
        const isStrong = stock.tradeScore >= 80;
        const cardBorderClass = isStrong ? "strong-buy" : "buy";

        html += `
        <div class="intel-card ${cardBorderClass}">
            <div>
                <div class="intel-card-header">
                    <div class="intel-stock-title">
                        <div style="display:flex; align-items:center; gap:8px;">
                            <span style="font-size:0.8rem; font-weight:800; color:#94a3b8;">#${idx + 1}</span>
                            <h3>${stock.symbol}</h3>
                        </div>
                        <div class="sector">${stock.sector} · <strong style="color:#fff;">₨${stock.price.toFixed(2)}</strong> (${stock.change >= 0 ? '+' : ''}${stock.change.toFixed(2)}%)</div>
                    </div>
                    <span class="intel-action-badge ${stock.actionBadgeClass}">${stock.action}</span>
                </div>

                <!-- Scores Metric Gauge Row -->
                <div class="intel-scores-row">
                    <div class="score-box">
                        <div class="s-label">Trade Score</div>
                        <div class="s-val" style="color:#38bdf8;">${stock.tradeScore}<span style="font-size:0.75rem; color:#64748b;">/100</span></div>
                    </div>
                    <div class="score-box">
                        <div class="s-label">Profit Prob.</div>
                        <div class="s-val" style="color:#10b981;">${stock.profitProb}%</div>
                    </div>
                    <div class="score-box">
                        <div class="s-label">Exit Liquidity</div>
                        <div class="s-val" style="color:#a855f7;">${stock.exitLiquidity}<span style="font-size:0.75rem; color:#64748b;">/100</span></div>
                    </div>
                </div>

                <!-- Realistic Target & Stop Loss Zone -->
                <div class="intel-targets-box">
                    <div class="target-row">
                        <span style="color:#94a3b8;">Suggested Entry:</span>
                        <strong style="color:#f8fafc;">${stock.entryZone}</strong>
                    </div>
                    <div class="target-row primary-target">
                        <span>🎯 Target 1 (+${stock.target1Pct}%):</span>
                        <strong>₨${stock.target1.toFixed(2)} <span style="font-size:0.75rem; font-weight:normal; color:#10b981;">(${stock.profitProb}% prob)</span></strong>
                    </div>
                    <div class="target-row">
                        <span style="color:#94a3b8;">🚀 Target 2 (+${stock.target2Pct}%):</span>
                        <strong>₨${stock.target2.toFixed(2)} <span style="font-size:0.75rem; color:#94a3b8;">(${stock.probTarget2}% prob)</span></strong>
                    </div>
                    <div class="target-row stop-loss">
                        <span>🛑 Stop Loss (-${stock.stopPct}%):</span>
                        <strong>₨${stock.stopLoss.toFixed(2)} <span style="font-size:0.75rem; color:#f87171;">(${stock.probStop}% prob)</span></strong>
                    </div>
                    <div class="target-row" style="border-top:1px dashed #334155; margin-top:4px; padding-top:4px; font-size:0.75rem;">
                        <span style="color:#94a3b8;">Risk/Reward Ratio:</span>
                        <strong style="color:#facc15;">${stock.riskReward} · EV: +${stock.expectedValue}%</strong>
                    </div>
                </div>

                <!-- Reasons Why -->
                <div class="intel-bullets">
                    <h5>✓ Why This Trade Setup:</h5>
                    ${stock.reasons.map(r => `<div class="intel-bullet-item"><span style="color:#10b981;">•</span><span>${r}</span></div>`).join('')}
                </div>

                <!-- Key Risks -->
                ${stock.risks.length > 0 ? `
                <div class="intel-bullets" style="margin-top:6px;">
                    <h5 style="color:#f87171;">⚠️ Potential Risks to Watch:</h5>
                    ${stock.risks.map(rk => `<div class="intel-bullet-item"><span style="color:#f87171;">•</span><span>${rk}</span></div>`).join('')}
                </div>` : ''}
            </div>

            <!-- Action Buttons Footer (Human Approval Gate) -->
            <div style="display:flex; gap:8px; margin-top:16px;">
                <button class="btn btn-primary btn-sm" style="flex:2; font-weight:800; background: linear-gradient(135deg, #10b981, #059669); border:none;" onclick="approveTradeCard('${stock.symbol}', ${stock.price}, ${stock.stopLoss}, ${stock.target1}, ${stock.target2}, '${stock.strategy || 'Momentum'}')">
                    ⚡ APPROVE BUY (Paper)
                </button>
                <button class="btn btn-ghost btn-sm" style="flex:1; color:#ef4444; border-color:rgba(239,68,68,0.3); font-weight:700;" onclick="rejectTradeCard('${stock.symbol}', this)">
                    ❌ Reject
                </button>
            </div>
        </div>
        `;
    });

    container.innerHTML = html;
}

function renderUpperLocksRadar() {
    const container = document.getElementById("intel-upper-locks-container");
    if (!container) return;

    // Filter stocks near or at upper limit
    const lockStocks = intelligenceResults.filter(c => c.isUpperLocked || parseFloat(c.distToUpper) <= 3.0 || c.change >= 4.5);

    if (lockStocks.length === 0) {
        container.innerHTML = `
        <div style="grid-column: 1/-1; text-align:center; padding:40px; background:#0f172a; border-radius:14px; border:1px dashed #334155; color:#94a3b8;">
            No stocks are currently approaching or trapped at upper circuits in this session.
        </div>`;
        return;
    }

    let html = "";
    lockStocks.forEach(stock => {
        const isLockedTrapped = stock.isUpperLocked;
        const cardClass = isLockedTrapped ? "avoid" : "strong-buy";

        html += `
        <div class="intel-card ${cardClass}">
            <div class="intel-card-header">
                <div class="intel-stock-title">
                    <h3>${stock.symbol}</h3>
                    <div class="sector">${stock.sector} · <strong>₨${stock.price.toFixed(2)}</strong> (+${stock.change.toFixed(2)}%)</div>
                </div>
                <span class="intel-action-badge ${isLockedTrapped ? 'badge-avoid' : 'badge-strong-buy'}">
                    ${stock.lockClassification}
                </span>
            </div>

            <div class="intel-scores-row">
                <div class="score-box">
                    <div class="s-label">Dist to Lock</div>
                    <div class="s-val" style="color:#facc15;">${stock.distToUpper}%</div>
                </div>
                <div class="score-box">
                    <div class="s-label">Entry Availability</div>
                    <div class="s-val" style="color:${isLockedTrapped ? '#ef4444' : '#10b981'}; font-size:0.95rem;">${stock.entryAvailability}</div>
                </div>
                <div class="score-box">
                    <div class="s-label">Fill Probability</div>
                    <div class="s-val" style="color:#38bdf8;">${stock.entryProbability}%</div>
                </div>
            </div>

            <div class="intel-bullets" style="margin-top:10px;">
                <h5>🔍 Circuit Lock Analysis:</h5>
                ${isLockedTrapped ? `
                <div class="intel-bullet-item"><span style="color:#ef4444;">✖</span><span><strong>LOCKED AT UPPER LIMIT:</strong> Zero ask volume available. Market orders will remain unfilled.</span></div>
                <div class="intel-bullet-item"><span style="color:#facc15;">•</span><span><strong>Rule:</strong> DO NOT CHASE. Wait for lock opening or profit-taking supply.</span></div>
                ` : `
                <div class="intel-bullet-item"><span style="color:#10b981;">✓</span><span><strong>BUYABLE MOMENTUM:</strong> Approaching upper lock with active two-way liquidity.</span></div>
                <div class="intel-bullet-item"><span style="color:#38bdf8;">•</span><span>Realistic fill probability: ${stock.entryProbability}% before circuit barrier.</span></div>
                `}
            </div>

            <div style="display:flex; gap:8px; margin-top:16px;">
                <button class="btn btn-ghost btn-sm" style="flex:1; font-weight:700;" onclick="switchView('live-trading'); setTimeout(()=>{ fetchLiveTradingAnalysis('${stock.symbol}'); }, 200);">
                    🔍 View Order Depth
                </button>
            </div>
        </div>
        `;
    });

    container.innerHTML = html;
}

function renderIntelScannerTable() {
    const tbody = document.getElementById("intel-scanner-tbody");
    if (!tbody) return;

    const search = (document.getElementById("intel-scan-search")?.value || "").trim().toLowerCase();
    const actionFilter = document.getElementById("intel-scan-action-filter")?.value || "ALL";
    const entryFilter = document.getElementById("intel-scan-entry-filter")?.value || "ALL";

    let filtered = intelligenceResults.filter(stock => {
        if (search && !stock.symbol.toLowerCase().includes(search) && !stock.sector.toLowerCase().includes(search)) return false;
        if (actionFilter === "STRONG_BUY" && !stock.action.includes("STRONG")) return false;
        if (actionFilter === "BUY" && !stock.action.includes("BUY")) return false;
        if (actionFilter === "PULLBACK" && !stock.action.includes("PULLBACK")) return false;
        if (actionFilter === "AVOID" && !stock.action.includes("AVOID") && !stock.action.includes("CHASE")) return false;
        if (entryFilter === "HIGH" && stock.entryAvailability !== "HIGH") return false;
        if (entryFilter === "LOCKED" && !stock.isUpperLocked) return false;
        return true;
    });

    if (filtered.length === 0) {
        tbody.innerHTML = `<tr><td colspan="9" style="text-align:center; padding:30px; color:#94a3b8;">No matching stocks found.</td></tr>`;
        return;
    }

    let html = "";
    filtered.slice(0, 50).forEach((stock, idx) => {
        html += `
        <tr>
            <td>
                <div style="font-weight:800; color:#fff; display:flex; align-items:center; gap:6px;">
                    <span style="color:#64748b; font-size:0.75rem;">#${idx+1}</span>
                    <span>${stock.symbol}</span>
                </div>
                <div style="font-size:0.75rem; color:#94a3b8;">${stock.sector}</div>
            </td>
            <td>
                <div style="font-weight:700;">₨${stock.price.toFixed(2)}</div>
                <div style="font-size:0.75rem; color:${stock.change >= 0 ? '#10b981' : '#ef4444'}; font-weight:700;">
                    ${stock.change >= 0 ? '+' : ''}${stock.change.toFixed(2)}%
                </div>
            </td>
            <td><span class="intel-action-badge ${stock.actionBadgeClass}">${stock.action}</span></td>
            <td><strong style="color:#38bdf8; font-size:1rem;">${stock.tradeScore}</strong><span style="font-size:0.7rem; color:#64748b;">/100</span></td>
            <td><strong style="color:#10b981;">${stock.profitProb}%</strong></td>
            <td><span style="font-size:0.75rem; font-weight:700; color:${stock.riskLevel === 'LOW' ? '#10b981' : stock.riskLevel === 'HIGH' ? '#ef4444' : '#facc15'};">${stock.riskLevel}</span></td>
            <td style="font-size:0.8rem; font-weight:600; color:${stock.entryAvailability === 'HIGH' ? '#10b981' : '#f87171'};">${stock.entryAvailability}</td>
            <td><strong style="color:#a855f7;">${stock.exitLiquidity}</strong><span style="font-size:0.7rem; color:#64748b;">/100</span></td>
            <td style="font-size:0.78rem;">
                <div>🎯 ₨${stock.target1.toFixed(2)} <span style="color:#10b981;">(+${stock.target1Pct}%)</span></div>
                <div style="color:#f87171;">🛑 ₨${stock.stopLoss.toFixed(2)} (-${stock.stopPct}%)</div>
            </td>
        </tr>
        `;
    });

    tbody.innerHTML = html;
}

function renderPortfolioReassessment() {
    const container = document.getElementById("intel-reassess-container");
    if (!container) return;

    // Load actual positions from Paper Simulator
    let positions = {};
    try {
        if (typeof simPositions !== "undefined" && Object.keys(simPositions).length > 0) {
            positions = simPositions;
        } else {
            const raw = localStorage.getItem("psx_sim_positions");
            if (raw) positions = JSON.parse(raw);
        }
    } catch (e) {}

    const stockList = (typeof STOCKS !== "undefined" && STOCKS && STOCKS.length > 0) ? STOCKS : [];
    const entries = Object.entries(positions);

    if (!entries || entries.length === 0) {
        container.innerHTML = `
        <div style="text-align:center; padding:50px 20px; background:#0f172a; border-radius:14px; border:1px dashed #334155; color:#94a3b8;">
            <div style="font-size:2.5rem; margin-bottom:10px;">💼</div>
            <h3 style="color:#f8fafc; font-weight:800;">No Positions in Paper Simulator Yet</h3>
            <p style="font-size:0.85rem; margin-top:6px;">
                Buy any stock in the Paper Simulator tab, and the AI Intelligence Engine will automatically begin monitoring and objectively reassessing your holding.
            </p>
            <button class="btn btn-primary btn-sm" onclick="switchView('simulator')" style="margin-top:14px; font-weight:700;">
                🎮 Go to Paper Simulator
            </button>
        </div>`;
        return;
    }

    let html = "";
    entries.forEach(([sym, pos]) => {
        const stockData = stockList.find(s => s.symbol === sym) || {};
        const buyPrice = pos.avgPrice || pos.buyPrice || stockData.price || 1;
        const currentPrice = stockData.price || buyPrice;
        const shares = pos.shares || 0;
        const costBasis = buyPrice * shares;
        const currentValue = currentPrice * shares;
        const pnl = currentValue - costBasis;
        const pnlPct = costBasis > 0 ? (pnl / costBasis) * 100 : 0;

        // Peak and Drawdown tracking
        const peakPrice = Math.max(currentPrice, buyPrice * 1.05);
        const drawdownPct = peakPrice > 0 ? ((peakPrice - currentPrice) / peakPrice) * 100 : 0;

        // Objective Query: "If I did not own this stock today, would I buy it RIGHT NOW?"
        const candidate = intelligenceResults.find(c => c.symbol === sym) || { tradeScore: 50 };
        let buyTodayAnswer = "WAIT";
        let buyTodayBadge = "badge-pullback";
        let rec = "HOLD";

        if (candidate.tradeScore >= 75 && candidate.entryAvailability === "HIGH") {
            buyTodayAnswer = "YES (STRONG SETUP)";
            buyTodayBadge = "badge-strong-buy";
            rec = "HOLD & ADD";
        } else if (pnlPct >= 7.0 || candidate.tradeScore < 45) {
            buyTodayAnswer = "NO (TAKE PROFIT / RISK HIGH)";
            buyTodayBadge = "badge-avoid";
            rec = "PARTIAL SELL / EXIT";
        } else if (pnlPct <= -4.0) {
            buyTodayAnswer = "NO (STOP-LOSS ZONE)";
            buyTodayBadge = "badge-avoid";
            rec = "SELL / CUT LOSS";
        }

        // All-Time High / Low approximations from 52-week data
        const ath = (stockData.high52 || currentPrice * 1.35).toFixed(2);
        const atl = (stockData.low52 || currentPrice * 0.65).toFixed(2);
        const distATH = (((parseFloat(ath) - currentPrice) / parseFloat(ath)) * 100).toFixed(1);
        const distATL = (((currentPrice - parseFloat(atl)) / parseFloat(atl)) * 100).toFixed(1);

        html += `
        <div class="reassess-card">
            <div class="reassess-header">
                <div>
                    <h4 style="font-size:1.2rem; font-weight:800; color:#fff; display:flex; align-items:center; gap:8px;">
                        <span>${sym}</span>
                        <span style="font-size:0.75rem; color:#94a3b8; font-weight:normal;">(${shares} Shares)</span>
                    </h4>
                    <div style="font-size:0.8rem; color:#94a3b8;">
                        Bought @ <strong>₨${buyPrice.toFixed(2)}</strong> · Current @ <strong>₨${currentPrice.toFixed(2)}</strong>
                    </div>
                </div>
                <div style="text-align:right;">
                    <div style="font-size:1.15rem; font-weight:800; color:${pnl >= 0 ? '#10b981' : '#ef4444'};">
                        ${pnl >= 0 ? '+' : ''}₨${pnl.toFixed(2)} (${pnl >= 0 ? '+' : ''}${pnlPct.toFixed(2)}%)
                    </div>
                    <div style="font-size:0.75rem; color:#94a3b8;">Peak Drawdown: <span style="color:#f87171;">-${drawdownPct.toFixed(1)}%</span></div>
                </div>
            </div>

            <!-- Objective Reassessment Banner -->
            <div style="background:#0f172a; border:1px solid #1e293b; border-radius:10px; padding:14px; margin-bottom:14px;">
                <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px; margin-bottom:8px;">
                    <div style="font-size:0.82rem; font-weight:700; color:#cbd5e1;">
                        ❓ If I did not already own ${sym}, would I buy it RIGHT NOW?
                    </div>
                    <span class="intel-action-badge ${buyTodayBadge}">
                        ${buyTodayAnswer}
                    </span>
                </div>
                <div style="font-size:0.8rem; color:#94a3b8;">
                    <strong>AI Recommendation:</strong> <span style="color:#facc15; font-weight:700;">${rec}</span> · Trade Score: <strong>${candidate.tradeScore || 50}/100</strong> · Exit Liquidity: <strong>${candidate.exitLiquidity || 50}/100</strong>
                </div>
            </div>

            <!-- All-Time High / Low & Price Milestones -->
            <div class="ath-atl-grid">
                <div class="ath-box">
                    <div class="label">52W High (ATH)</div>
                    <div class="val">₨${ath}</div>
                    <div style="font-size:0.7rem; color:#f87171;">-${distATH}% below</div>
                </div>
                <div class="ath-box">
                    <div class="label">52W Low (ATL)</div>
                    <div class="val">₨${atl}</div>
                    <div style="font-size:0.7rem; color:#10b981;">+${distATL}% above</div>
                </div>
                <div class="ath-box">
                    <div class="label">Highest Since Purchase</div>
                    <div class="val" style="color:#10b981;">₨${peakPrice.toFixed(2)}</div>
                </div>
                <div class="ath-box">
                    <div class="label">Target 1 Exit</div>
                    <div class="val" style="color:#38bdf8;">₨${(buyPrice * 1.05).toFixed(2)}</div>
                </div>
            </div>

            <div style="display:flex; justify-content:flex-end; gap:8px; margin-top:14px;">
                <button class="btn btn-ghost btn-sm" onclick="runTradingIntelligenceEngine(); renderPortfolioReassessment();" style="font-weight:700;">
                    🔄 Reanalyze Position
                </button>
            </div>
        </div>
        `;
    });

    container.innerHTML = html;
}

function populateCalculatorDropdown() {
    const sel = document.getElementById("calc-stock-select");
    const stockList = (typeof STOCKS !== "undefined" && STOCKS && STOCKS.length > 0) ? STOCKS : [];
    if (!sel || stockList.length === 0) return;

    if (!sel.options || sel.options.length <= 1) {
        sel.innerHTML = "";
        const topSymbols = (intelligenceResults || []).slice(0, 30);
        (topSymbols.length > 0 ? topSymbols : stockList.slice(0, 30)).forEach(s => {
            const opt = document.createElement("option");
            opt.value = s.symbol;
            opt.textContent = `${s.symbol} — ₨${(s.price || 0).toFixed(2)}`;
            sel.appendChild(opt);
        });
    }
}

function updateCalcMetrics() {
    const sel = document.getElementById("calc-stock-select");
    const capitalInput = document.getElementById("calc-capital-input");
    const commInput = document.getElementById("calc-comm-input");
    const stockList = (typeof STOCKS !== "undefined" && STOCKS && STOCKS.length > 0) ? STOCKS : [];
    if (!sel || !capitalInput) return;

    const sym = sel.value;
    const stock = stockList.find(s => s.symbol === sym) || { price: 20 };
    const price = stock.price || 20;
    const capital = parseFloat(capitalInput.value) || 100000;
    const commPerShare = parseFloat(commInput.value) || 0.15;

    const shares = Math.floor(capital / price);
    const actualCost = shares * price;

    // SECP + PSX + CVT + Brokerage Fees
    const brokerageRoundtrip = shares * commPerShare * 2;
    const taxesAndLevies = actualCost * 0.0003 * 2; // ~0.03% SECP + CDC + SST
    const totalFees = brokerageRoundtrip + taxesAndLevies;

    // Targets
    const target1Price = price * 1.035;
    const stopPrice = price * 0.975;

    const grossTarget1 = (target1Price - price) * shares;
    const netTarget1 = grossTarget1 - totalFees;

    const grossLoss = (price - stopPrice) * shares;
    const netLoss = grossLoss + totalFees;

    const netRR = netLoss > 0 ? (netTarget1 / netLoss).toFixed(1) : "0.0";

    document.getElementById("calc-res-shares").textContent = shares.toLocaleString() + " shares";
    document.getElementById("calc-res-cost").textContent = "₨" + actualCost.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
    document.getElementById("calc-res-fees").textContent = "₨" + totalFees.toFixed(2);
    document.getElementById("calc-res-target1-net").textContent = (netTarget1 >= 0 ? "+" : "") + "₨" + netTarget1.toFixed(2);
    document.getElementById("calc-res-loss-net").textContent = "-₨" + netLoss.toFixed(2);
    document.getElementById("calc-res-rr").textContent = `1:${netRR}`;
}

// ─── Feedback & Feature Request System ───
let currentFeedbackRating = 5;
let currentFeedbackTopic = "New Feature";

const ratingLabels = {
    1: "★☆☆☆☆ Poor (1 Star)",
    2: "★★☆☆☆ Fair (2 Stars)",
    3: "★★★☆☆ Good (3 Stars)",
    4: "★★★★☆ Very Good (4 Stars)",
    5: "⭐⭐⭐⭐⭐ Excellent (5 Stars)"
};

function openFeedbackModal() {
    const modal = document.getElementById("feedback-modal");
    if (!modal) return;

    const emailInput = document.getElementById("feedback-email");
    if (emailInput) {
        emailInput.value = ""; // Keep empty so every user enters their own active email
    }

    const messageInput = document.getElementById("feedback-message");
    if (messageInput) {
        messageInput.value = "";
    }

    const msgEl = document.getElementById("feedback-status-msg");
    if (msgEl) msgEl.style.display = "none";

    selectStarRating(5);
    setFeedbackTopic(document.querySelector(".feedback-tag"), "New Feature");
    modal.style.display = "flex";
}

function closeFeedbackModal() {
    const modal = document.getElementById("feedback-modal");
    if (modal) modal.style.display = "none";
}

function selectStarRating(val) {
    currentFeedbackRating = val;
    const stars = document.querySelectorAll(".feedback-star");
    stars.forEach(s => {
        const r = parseInt(s.getAttribute("data-rating"), 10);
        if (r <= val) {
            s.classList.add("active");
        } else {
            s.classList.remove("active");
        }
    });

    const lbl = document.getElementById("star-rating-label");
    if (lbl) lbl.textContent = ratingLabels[val] || `${val} Stars`;
}

function hoverStarRating(val) {
    const stars = document.querySelectorAll(".feedback-star");
    stars.forEach(s => {
        const r = parseInt(s.getAttribute("data-rating"), 10);
        if (r <= val) {
            s.classList.add("hover");
        } else {
            s.classList.remove("hover");
        }
    });
}

function resetStarHover() {
    const stars = document.querySelectorAll(".feedback-star");
    stars.forEach(s => s.classList.remove("hover"));
}

function setFeedbackTopic(btn, topic) {
    currentFeedbackTopic = topic;
    document.querySelectorAll(".feedback-tag").forEach(t => t.classList.remove("active"));
    if (btn) btn.classList.add("active");
}

function submitFeedbackForm() {
    const messageInput = document.getElementById("feedback-message");
    const emailInput = document.getElementById("feedback-email");
    const msgEl = document.getElementById("feedback-status-msg");
    const submitBtn = document.getElementById("btn-submit-feedback");

    const message = messageInput ? messageInput.value.trim() : "";
    const email = emailInput ? emailInput.value.trim().toLowerCase() : "";
    const deviceId = getDeviceId();

    // 1. Validate Message
    if (!message) {
        if (msgEl) {
            msgEl.style.display = "block";
            msgEl.style.background = "rgba(239, 68, 68, 0.15)";
            msgEl.style.color = "#fca5a5";
            msgEl.style.border = "1px solid rgba(239, 68, 68, 0.3)";
            msgEl.textContent = "Please describe what features, indicators, or feedback you have.";
        }
        if (messageInput) messageInput.focus();
        return;
    }

    // 2. Validate Legitimate Email (Strict & Required)
    const emailRegex = /^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$/;
    if (!email || !emailRegex.test(email)) {
        if (msgEl) {
            msgEl.style.display = "block";
            msgEl.style.background = "rgba(239, 68, 68, 0.15)";
            msgEl.style.color = "#fca5a5";
            msgEl.style.border = "1px solid rgba(239, 68, 68, 0.3)";
            msgEl.textContent = "Please enter your valid, active email address so we can reply directly to you.";
        }
        if (emailInput) emailInput.focus();
        return;
    }

    const emailUser = email.split("@")[0].toLowerCase();
    const blockedNames = ["test", "fake", "temp", "asdf", "123", "12345", "admin", "null", "none", "demo", "sample"];
    if (blockedNames.includes(emailUser) || emailUser.length < 3 || /^([a-z0-9])\1+$/.test(emailUser)) {
        if (msgEl) {
            msgEl.style.display = "block";
            msgEl.style.background = "rgba(239, 68, 68, 0.15)";
            msgEl.style.color = "#fca5a5";
            msgEl.style.border = "1px solid rgba(239, 68, 68, 0.3)";
            msgEl.textContent = "Please enter your legitimate personal or business email address.";
        }
        if (emailInput) emailInput.focus();
        return;
    }

    if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.textContent = "Sending...";
    }

    const payload = {
        rating: currentFeedbackRating,
        topic: currentFeedbackTopic,
        message: message,
        email: email,
        deviceId: deviceId
    };

    fetch("/api/feedback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
    })
    .then(r => r.json())
    .then(res => {
        if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.textContent = "🚀 Send Feedback";
        }
        if (res.success) {
            if (msgEl) {
                msgEl.style.display = "block";
                msgEl.style.background = "rgba(16, 185, 129, 0.15)";
                msgEl.style.color = "#a7f3d0";
                msgEl.style.border = "1px solid rgba(16, 185, 129, 0.3)";
                msgEl.textContent = "🎉 Thank you! Your feedback has been sent. We'll reply to your email shortly.";
            }
            if (messageInput) messageInput.value = "";
            if (emailInput) emailInput.value = "";
            setTimeout(() => {
                closeFeedbackModal();
            }, 2000);
        } else {
            if (msgEl) {
                msgEl.style.display = "block";
                msgEl.style.background = "rgba(239, 68, 68, 0.15)";
                msgEl.style.color = "#fca5a5";
                msgEl.style.border = "1px solid rgba(239, 68, 68, 0.3)";
                msgEl.textContent = res.error || "Please enter a valid personal or business email.";
            }
        }
    })
    .catch(err => {
        if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.textContent = "🚀 Send Feedback";
        }
        if (msgEl) {
            msgEl.style.display = "block";
            msgEl.style.background = "rgba(239, 68, 68, 0.15)";
            msgEl.style.color = "#fca5a5";
            msgEl.style.border = "1px solid rgba(239, 68, 68, 0.3)";
            msgEl.textContent = "Error sending feedback. Please try again.";
        }
    });
}

// ─── PSX AI Trading Client Methods (Phase 3 & 4) ───

function approveTradeCard(symbol, entryPrice, stopLoss, tp1, tp2, strategy) {
    if (!symbol || entryPrice <= 0) return;
    
    const stock = (typeof STOCKS !== "undefined" && STOCKS) ? STOCKS.find(s => s.symbol === symbol) : null;
    const name = stock ? (stock.name || symbol) : symbol;
    const sector = stock ? (stock.sector || "Other") : "Other";

    if (!confirm(`⚡ EXECUTE TRADE APPROVAL (Paper Account)?\n\nSymbol: ${symbol} (${name})\nEntry: PKR ${entryPrice.toFixed(2)}\nStop Loss: PKR ${stopLoss.toFixed(2)}\nTarget 1: PKR ${tp1.toFixed(2)}\nStrategy: ${strategy}\n\nStrict Risk Engine rules (1% max risk) will be enforced.`)) {
        return;
    }

    fetch("/api/trading/approve-trade", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            symbol: symbol,
            name: name,
            sector: sector,
            entry_price: entryPrice,
            stop_loss: stopLoss,
            take_profit_1: tp1,
            take_profit_2: tp2,
            strategy: strategy
        })
    })
    .then(r => r.json())
    .then(res => {
        if (res.success) {
            alert(`✅ TRADE EXECUTED SUCCESSFULLY!\n\nBought ${res.order.shares.toLocaleString()} shares of ${symbol} @ PKR ${res.order.fill_price.toFixed(2)}\nTotal Cost: PKR ${res.order.total_cost.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}\n\nActive position monitoring is now live.`);
            loadActiveTradingPortfolio();
            // Switch to active positions tab
            const posTabBtn = document.querySelector("button[onclick*='active-positions']");
            if (posTabBtn) switchIntelSubTab('active-positions', posTabBtn);
        } else {
            alert(`❌ ORDER REJECTED BY RISK ENGINE:\n\n${res.error || "Order execution failed"}`);
        }
    })
    .catch(err => {
        alert(`Error executing trade approval: ${err}`);
    });
}

function rejectTradeCard(symbol, btnElement) {
    if (btnElement) {
        const card = btnElement.closest(".intel-card");
        if (card) {
            card.style.opacity = "0.4";
            card.style.pointerEvents = "none";
        }
    }
    fetch("/api/trading/reject-trade", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: symbol })
    });
}

function loadActiveTradingPortfolio() {
    fetch("/api/trading/portfolio")
    .then(r => r.json())
    .then(res => {
        if (!res.success || !res.portfolio) return;
        const p = res.portfolio;

        const eqEl = document.getElementById("trading-equity-val");
        if (eqEl) eqEl.textContent = `₨${(p.equity || 0).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;

        const cashEl = document.getElementById("trading-cash-val");
        if (cashEl) cashEl.textContent = `₨${(p.cash || 0).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;

        const unEl = document.getElementById("trading-unrealized-val");
        if (unEl) {
            const unPnl = p.total_unrealized_pnl || 0;
            unEl.textContent = `${unPnl >= 0 ? '+' : ''}₨${unPnl.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
            unEl.style.color = unPnl >= 0 ? "#10b981" : "#ef4444";
        }

        const realEl = document.getElementById("trading-realized-val");
        if (realEl) {
            const rPnl = p.total_realized_pnl || 0;
            realEl.textContent = `${rPnl >= 0 ? '+' : ''}₨${rPnl.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
            realEl.style.color = rPnl >= 0 ? "#10b981" : "#ef4444";
        }

        const countEl = document.getElementById("intel-active-pos-count");
        if (countEl) countEl.textContent = p.positions_count || 0;

        // Render table
        const tbody = document.getElementById("trading-active-positions-tbody");
        if (!tbody) return;

        if (!p.positions || p.positions.length === 0) {
            tbody.innerHTML = `<tr><td colspan="9" style="text-align:center; padding:30px; color:#94a3b8;">No active open positions. Approve trade setups in the "Best Setups Right Now" tab to begin tracking.</td></tr>`;
            return;
        }

        let html = "";
        p.positions.forEach(pos => {
            const pnl = pos.unrealized_pnl || 0;
            const pnlPct = pos.unrealized_pnl_pct || 0;
            const pnlColor = pnl >= 0 ? "#10b981" : "#ef4444";

            html += `
            <tr>
                <td><strong>${pos.symbol}</strong><br><span style="font-size:0.75rem; color:#94a3b8;">${pos.name || ''}</span></td>
                <td>${pos.shares.toLocaleString()}</td>
                <td>₨${pos.entry_price.toFixed(2)}</td>
                <td><strong style="color:#fff;">₨${pos.current_price.toFixed(2)}</strong></td>
                <td style="color:#f87171;">₨${pos.stop_loss ? pos.stop_loss.toFixed(2) : '—'}</td>
                <td>
                    <span style="color:#10b981;">TP1: ₨${pos.take_profit_1 ? pos.take_profit_1.toFixed(2) : '—'} ${pos.tp1_hit ? '✅ (Trimmed)' : ''}</span><br>
                    <span style="color:#38bdf8;">TP2: ₨${pos.take_profit_2 ? pos.take_profit_2.toFixed(2) : '—'}</span>
                </td>
                <td style="color:#a855f7;">₨${pos.trailing_stop ? pos.trailing_stop.toFixed(2) : (pos.stop_loss ? pos.stop_loss.toFixed(2) : '—')}</td>
                <td style="color:${pnlColor}; font-weight:800;">
                    ${pnl >= 0 ? '+' : ''}₨${pnl.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}
                    <div style="font-size:0.75rem;">(${pnlPct >= 0 ? '+' : ''}${pnlPct.toFixed(2)}%)</div>
                </td>
                <td>
                    <button class="btn btn-danger btn-sm" onclick="closeActivePosition('${pos.symbol}')" style="font-size:0.75rem; padding:4px 8px; font-weight:700; background:#dc2626;">
                        Close
                    </button>
                </td>
            </tr>`;
        });
        tbody.innerHTML = html;
    })
    .catch(err => console.error("Error loading trading portfolio:", err));
}

function closeActivePosition(symbol) {
    if (!confirm(`Are you sure you want to close position in ${symbol} at current market price?`)) return;
    fetch("/api/trading/close-position", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: symbol })
    })
    .then(r => r.json())
    .then(res => {
        if (res.success) {
            alert(`Position closed for ${symbol}. Realized P&L: PKR ${res.trade.realized_pnl >= 0 ? '+' : ''}${res.trade.realized_pnl.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}`);
            loadActiveTradingPortfolio();
        } else {
            alert(`Error closing position: ${res.error}`);
        }
    });
}

function handleKillSwitchClick() {
    if (!confirm("🛑 EMERGENCY KILL SWITCH:\n\nThis will immediately:\n1. Close ALL open positions at market price\n2. Block all new trade executions\n\nDo you want to proceed?")) return;
    
    fetch("/api/trading/kill-switch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "activate", reason: "User Activated Emergency Kill Switch" })
    })
    .then(r => r.json())
    .then(res => {
        alert("🛑 KILL SWITCH ACTIVATED: All positions closed and trading halted.");
        loadActiveTradingPortfolio();
    });
}

function handleResetPaperAccount() {
    if (!confirm("Reset paper trading account back to initial PKR 1,000,000 capital?")) return;
    fetch("/api/trading/reset-paper-account", { method: "POST" })
    .then(r => r.json())
    .then(res => {
        alert("Paper account reset to initial PKR 1,000,000 capital.");
        loadActiveTradingPortfolio();
    });
}

// ═════════════════════════════════════════════════════════════════
// ═════════════════════════════════════════════════════════════════
// 🎯 PSX WEEKLY TRADE OPTIONS ENGINE (SECTION 7 SPEC)
// ═════════════════════════════════════════════════════════════════

let currentWeeklyRun = null;
let currentWeeklyCandidates = [];
let weeklyGradeFilter = "ALL";
let weeklyDirFilter = "ALL";
let currentWeeklySubtab = "candidates";
let currentBuyCandidate = null;

function switchWeeklySubtab(tab) {
    currentWeeklySubtab = tab;
    const btnCand = document.getElementById("btn-subtab-candidates");
    const btnPerf = document.getElementById("btn-subtab-performance");
    const paneCand = document.getElementById("weekly-candidates-pane");
    const panePerf = document.getElementById("weekly-performance-pane");

    if (tab === "candidates") {
        if (btnCand) btnCand.classList.add("active");
        if (btnPerf) btnPerf.classList.remove("active");
        if (paneCand) paneCand.style.display = "block";
        if (panePerf) panePerf.style.display = "none";
    } else {
        if (btnCand) btnCand.classList.remove("active");
        if (btnPerf) btnPerf.classList.add("active");
        if (paneCand) paneCand.style.display = "none";
        if (panePerf) panePerf.style.display = "block";
        loadWeeklyPerformanceData();
    }
}

function getUserAvailableCapital() {
    const el = document.getElementById("weekly-user-capital");
    const val = el ? parseFloat(el.value) : 500000;
    return (val && val > 0) ? val : 500000;
}

function setWeeklyCapitalPreset(amount) {
    const el = document.getElementById("weekly-user-capital");
    if (el) el.value = amount;

    document.querySelectorAll(".cap-preset-btn").forEach(btn => {
        const text = btn.textContent || "";
        const isTarget = (amount === 100000 && text.includes("100K")) ||
                         (amount === 250000 && text.includes("250K")) ||
                         (amount === 500000 && text.includes("500K")) ||
                         (amount === 1000000 && text.includes("1.0M")) ||
                         (amount === 2500000 && text.includes("2.5M"));
        if (isTarget) btn.classList.add("active");
        else btn.classList.remove("active");
    });

    filterAndRenderWeeklyCandidates();
    renderWeeklySpotlight();
}

function onWeeklyCapitalChange() {
    const val = getUserAvailableCapital();
    document.querySelectorAll(".cap-preset-btn").forEach(btn => {
        const text = btn.textContent || "";
        const isTarget = (val === 100000 && text.includes("100K")) ||
                         (val === 250000 && text.includes("250K")) ||
                         (val === 500000 && text.includes("500K")) ||
                         (val === 1000000 && text.includes("1.0M")) ||
                         (val === 2500000 && text.includes("2.5M"));
        if (isTarget) btn.classList.add("active");
        else btn.classList.remove("active");
    });

    filterAndRenderWeeklyCandidates();
    renderWeeklySpotlight();
}

function computeCandidateInvestment(candidate, availableCapital) {
    const entryPrice = candidate.risk?.entry || 1.0;
    const stopPrice = candidate.risk?.stop || (entryPrice * 0.96);
    const tp1Price = candidate.risk?.takeProfit1 || candidate.risk?.target || (entryPrice * 1.08);
    const tp2Price = candidate.risk?.takeProfit2 || (entryPrice * 1.15);
    const adtv = candidate.liquidity?.avgDailyTradedValue20d || 20000000.0;
    const avgVol = candidate.liquidity?.avgDailyVolume20d || (adtv / entryPrice);

    // 50% Maximum Capital Ceiling
    const maxCeilingPkr = availableCapital * 0.50;

    // Safe market capacity (~1.0% of ADTV / volume)
    const liquidityCapPkr = adtv * 0.010;
    const volCapPkr = (avgVol * 0.010) * entryPrice;
    let safeCapacityPkr = Math.min(liquidityCapPkr, volCapPkr);
    safeCapacityPkr = Math.max(Math.min(25000, availableCapital), safeCapacityPkr);

    const rawRecommended = Math.min(maxCeilingPkr, safeCapacityPkr);
    const recommendedShares = entryPrice > 0 ? Math.max(1, Math.floor(rawRecommended / entryPrice)) : 0;
    const recommendedPkr = Math.round(recommendedShares * entryPrice);

    // Estimated Profit & Risk in PKR
    const profitAtTp1Pkr = Math.round(recommendedShares * Math.abs(tp1Price - entryPrice));
    const profitAtTp2Pkr = Math.round(recommendedShares * Math.abs(tp2Price - entryPrice));
    const riskAtStopPkr = Math.round(recommendedShares * Math.abs(entryPrice - stopPrice));

    let exitDifficulty = "Moderate";
    let exitTimeEst = "3-5 Mins";
    let reason = "Moderate turnover; position sized to safe % of daily volume to ensure frictionless exit.";

    if (adtv >= 75000000 && recommendedPkr >= (maxCeilingPkr * 0.85)) {
        exitDifficulty = "Easy";
        exitTimeEst = "Instant (< 1 Min)";
        reason = "Deep institutional liquidity & high daily turnover allow full allocation ceiling.";
    } else if (adtv >= 35000000) {
        exitDifficulty = "Moderate";
        exitTimeEst = "2-4 Mins";
        reason = "Moderate turnover; position sized to safe % of daily volume to ensure frictionless exit.";
    } else {
        exitDifficulty = "Difficult";
        exitTimeEst = "8-12 Mins";
        reason = "Limited daily volume & market depth; position strictly scaled down to preserve easy exit.";
    }

    return {
        availableCapital: availableCapital,
        maxCeilingPkr: maxCeilingPkr,
        recommendedPkr: recommendedPkr,
        recommendedShares: recommendedShares,
        profitAtTp1Pkr: profitAtTp1Pkr,
        profitAtTp2Pkr: profitAtTp2Pkr,
        riskAtStopPkr: riskAtStopPkr,
        reason: reason,
        exitDifficulty: exitDifficulty,
        exitTimeEst: exitTimeEst,
        percentOfCapital: Math.round((recommendedPkr / availableCapital) * 1000) / 10
    };
}

async function loadWeeklyScanData(forceRefresh = false) {
    const container = document.getElementById("weekly-candidates-container");
    if (!currentWeeklyRun || forceRefresh) {
        if (container) container.innerHTML = `<div class="weekly-loading-state"><div class="spinner" style="margin: 0 auto 12px;"></div>Loading PSX Weekly Trade Options data...</div>`;
    }

    try {
        const res = await fetch("/api/weekly-scan/latest");
        const json = await res.json();
        if (json.success && json.run) {
            currentWeeklyRun = json.run;
            currentWeeklyCandidates = json.candidates || [];
            renderWeeklyScanView();
            loadWeeklyPerformanceData(false);
        } else {
            if (container) container.innerHTML = `<div class="weekly-empty-state">No weekly scan runs found. Click "Run Manual Scan" above to generate.</div>`;
        }
    } catch (e) {
        console.error("Error loading weekly scan data:", e);
        if (container) container.innerHTML = `<div class="weekly-empty-state" style="color: var(--accent-rose);">Failed to load weekly scan data. Please verify server connection.</div>`;
    }
}

function renderWeeklyScanView() {
    if (!currentWeeklyRun) return;

    // 1. Render Header Meta Bar
    const dateEl = document.getElementById("weekly-meta-date");
    const runTypeEl = document.getElementById("weekly-meta-runtype");
    const universeEl = document.getElementById("weekly-meta-universe");
    const candidatesEl = document.getElementById("weekly-meta-candidates");
    const configVerEl = document.getElementById("weekly-meta-config-ver");
    const countSubtabCand = document.getElementById("count-subtab-candidates");

    if (dateEl) dateEl.textContent = currentWeeklyRun.dataAsOfDate || "--";
    if (runTypeEl) runTypeEl.textContent = currentWeeklyRun.runType === "SCHEDULED_WEEKLY" ? "📅 Scheduled Weekly" : "⚡ Manual Rescan";
    if (universeEl) universeEl.textContent = `${currentWeeklyRun.universeSize} Stocks`;
    if (candidatesEl) candidatesEl.textContent = `${currentWeeklyCandidates.length} Qualified`;
    if (configVerEl) configVerEl.textContent = `Config v${currentWeeklyRun.configVersion || '1.0.0'}`;
    if (countSubtabCand) countSubtabCand.textContent = currentWeeklyCandidates.length;

    // Exclusion Funnel Chips
    const ex = currentWeeklyRun.excludedCounts || {};
    const liqChip = document.getElementById("funnel-liq");
    const trigChip = document.getElementById("funnel-trigger");
    const rrChip = document.getElementById("funnel-rr");

    if (liqChip) liqChip.textContent = `💧 ${ex.failedLiquidity || 0} Low Liq`;
    if (trigChip) trigChip.textContent = `⚡ ${ex.noTriggerDetected || 0} No Trig`;
    if (rrChip) rrChip.textContent = `⚖️ ${ex.rrBelowThreshold || 0} Low RR`;

    // 2. Render Spotlight and Candidates
    renderWeeklySpotlight();
    filterAndRenderWeeklyCandidates();
}

function renderWeeklySpotlight() {
    const spotlightSection = document.getElementById("weekly-spotlight-section");
    if (!spotlightSection) return;

    if (!currentWeeklyCandidates || currentWeeklyCandidates.length === 0) {
        spotlightSection.style.display = "none";
        return;
    }

    // Pick top 3 A_PLUS or highest score candidates
    const aPlusList = [...currentWeeklyCandidates].sort((a, b) => {
        const gradeWeight = { "A_PLUS": 3, "A": 2, "B": 1 };
        const gDiff = (gradeWeight[b.grade] || 0) - (gradeWeight[a.grade] || 0);
        if (gDiff !== 0) return gDiff;
        return (b.conviction || 0) - (a.conviction || 0);
    }).slice(0, 3);

    if (aPlusList.length === 0) {
        spotlightSection.style.display = "none";
        return;
    }

    spotlightSection.style.display = "block";
    const availableCap = getUserAvailableCapital();

    let html = `
    <div class="weekly-spotlight-header">
        <div class="spotlight-title">
            <span>🌟 AI Alpha Spotlight: Top ${aPlusList.length} High-Conviction Swing Trades</span>
        </div>
        <span class="spotlight-badge">⭐ Highest Probability Setups</span>
    </div>
    <div class="weekly-spotlight-grid">`;

    aPlusList.forEach((c, idx) => {
        const sizing = computeCandidateInvestment(c, availableCap);
        const tp1 = c.risk?.takeProfit1 || c.risk?.target || (c.risk?.entry * 1.08);
        const stop = c.risk?.stop || (c.risk?.entry * 0.96);
        const tp1Pct = c.risk?.rewardPctTp1 || (((tp1 - c.risk?.entry) / c.risk?.entry) * 100);
        const stopPct = c.risk?.riskPct || (((c.risk?.entry - stop) / c.risk?.entry) * 100);
        const rr = c.risk?.rewardRiskRatio || 2.0;
        const conviction = c.conviction || (c.grade === "A_PLUS" ? 94 : 84);

        html += `
        <div class="spotlight-card ${idx === 0 ? 'rank-1' : ''}">
            <div class="spotlight-card-top">
                <div>
                    <div class="spotlight-symbol-title">
                        <span>${c.symbol}</span>
                        <span style="font-size: 0.72rem; color: #94a3b8; font-weight: 500;">(${c.sector || 'PSX'})</span>
                    </div>
                    <div style="font-size: 0.72rem; color: #38bdf8; margin-top: 2px;">
                        ${c.triggers?.[0]?.type?.replace('_', ' ') || 'Breakout'} • ${c.direction}
                    </div>
                </div>
                <div class="spotlight-conviction-pill">
                    <span>⚡ ${conviction}% AI Conviction</span>
                </div>
            </div>

            <div class="spotlight-metrics-grid">
                <div>
                    <div class="spotlight-metric-label">Buy Entry</div>
                    <div class="spotlight-metric-val">₨${c.risk?.entry?.toFixed(2) || '0.00'}</div>
                </div>
                <div>
                    <div class="spotlight-metric-label">Target 1</div>
                    <div class="spotlight-metric-val target">₨${tp1.toFixed(2)} <span style="font-size:0.65rem;">(+${tp1Pct.toFixed(1)}%)</span></div>
                </div>
                <div>
                    <div class="spotlight-metric-label">Stop Loss</div>
                    <div class="spotlight-metric-val stop">₨${stop.toFixed(2)} <span style="font-size:0.65rem;">(-${stopPct.toFixed(1)}%)</span></div>
                </div>
            </div>

            <div class="spotlight-profit-estimate">
                <div>
                    <span style="color: #94a3b8;">Est. Profit (Target 1):</span>
                    <strong style="color: #34d399; margin-left: 4px;">+PKR ${sizing.profitAtTp1Pkr.toLocaleString()}</strong>
                </div>
                <div>
                    <span style="color: #94a3b8;">Max Risk:</span>
                    <strong style="color: #f87171; margin-left: 4px;">-PKR ${sizing.riskAtStopPkr.toLocaleString()}</strong>
                </div>
            </div>

            <div class="spotlight-actions">
                <button class="btn btn-primary btn-sm btn-buy-card" onclick="openWeeklyBuyModal('${c.id}')" style="flex: 1;">
                    ⚡ Buy (${sizing.recommendedShares.toLocaleString()} Shares)
                </button>
                <button class="btn btn-secondary btn-sm" onclick="openStockChartModal('${c.symbol}')">
                    📈 Chart
                </button>
            </div>
        </div>`;
    });

    html += `</div>`;
    spotlightSection.innerHTML = html;
}

function setWeeklyGradeFilter(btn, grade) {
    weeklyGradeFilter = grade;
    document.querySelectorAll("#weekly-grade-pills .pill-btn").forEach(b => b.classList.remove("active"));
    if (btn) btn.classList.add("active");
    filterAndRenderWeeklyCandidates();
}

function setWeeklyDirFilter(btn, dir) {
    weeklyDirFilter = dir;
    document.querySelectorAll("#weekly-dir-pills .pill-btn").forEach(b => b.classList.remove("active"));
    if (btn) btn.classList.add("active");
    filterAndRenderWeeklyCandidates();
}

function filterAndRenderWeeklyCandidates() {
    const container = document.getElementById("weekly-candidates-container");
    if (!container) return;

    const strategyFilter = document.getElementById("weekly-strategy-filter")?.value || "ALL";
    const liquidityFilter = document.getElementById("weekly-liquidity-filter")?.value || "ALL";
    const sortFilter = document.getElementById("weekly-sort-filter")?.value || "conviction";
    const searchVal = (document.getElementById("weekly-symbol-search")?.value || "").trim().toUpperCase();
    const availableCap = getUserAvailableCapital();

    let filtered = (currentWeeklyCandidates || []).filter(c => {
        if (weeklyGradeFilter !== "ALL" && c.grade !== weeklyGradeFilter) return false;
        if (weeklyDirFilter !== "ALL" && c.direction !== weeklyDirFilter) return false;
        
        // Strategy Filter
        if (strategyFilter !== "ALL") {
            const hasStrategy = (c.triggers || []).some(t => t.type === strategyFilter);
            if (!hasStrategy) return false;
        }

        // Liquidity Filter
        if (liquidityFilter !== "ALL") {
            const sizing = computeCandidateInvestment(c, availableCap);
            if (liquidityFilter === "Easy" && sizing.exitDifficulty !== "Easy") return false;
            if (liquidityFilter === "Moderate" && sizing.exitDifficulty === "Difficult") return false;
        }

        if (searchVal && !c.symbol.includes(searchVal) && !c.sector.toUpperCase().includes(searchVal)) return false;
        return true;
    });

    // Sorting
    filtered.sort((a, b) => {
        if (sortFilter === "conviction") {
            return (b.conviction || 0) - (a.conviction || 0);
        } else if (sortFilter === "return") {
            const rA = a.risk?.rewardPctTp1 || 0;
            const rB = b.risk?.rewardPctTp1 || 0;
            return rB - rA;
        } else if (sortFilter === "rr") {
            const rrA = a.risk?.rewardRiskRatio || 0;
            const rrB = b.risk?.rewardRiskRatio || 0;
            return rrB - rrA;
        } else if (sortFilter === "volume") {
            const vA = a.liquidity?.avgDailyTradedValue20d || 0;
            const vB = b.liquidity?.avgDailyTradedValue20d || 0;
            return vB - vA;
        }
        return 0;
    });

    if (filtered.length === 0) {
        container.innerHTML = `<div class="weekly-empty-state">No scan candidates match the selected filters. Try choosing "All Grades" or a different strategy.</div>`;
        return;
    }

    container.innerHTML = filtered.map(c => {
        const isWatched = watchlist.has(c.symbol);
        const gradeClass = c.grade === "A_PLUS" ? "grade-badge-a-plus" : (c.grade === "A" ? "grade-badge-a" : "grade-badge-b");
        const gradeLabel = c.grade === "A_PLUS" ? "⭐ A+ TOP" : (c.grade === "A" ? "A GRADE" : "B GRADE");
        const dirClass = c.direction === "LONG" ? "long" : "short";
        const dirSymbol = c.direction === "LONG" ? "LONG ↗" : "SHORT ↘";
        const rawScore = c.score?.rawScore || 0;
        const scorePct = Math.round((rawScore / 6) * 100);
        const conviction = c.conviction || (c.grade === "A_PLUS" ? 92 : (c.grade === "A" ? 82 : 72));

        // Risk Parameters
        const entry = c.risk?.entry || 1.0;
        const stop = c.risk?.stop || (entry * 0.96);
        const tp1 = c.risk?.takeProfit1 || c.risk?.target || (entry * 1.08);
        const tp2 = c.risk?.takeProfit2 || (entry * 1.15);
        const entryMin = c.risk?.entryZoneMin || (entry * 0.992);
        const entryMax = c.risk?.entryZoneMax || (entry * 1.012);
        const riskPct = c.risk?.riskPct || (((entry - stop) / entry) * 100);
        const tp1Pct = c.risk?.rewardPctTp1 || (((tp1 - entry) / entry) * 100);
        const tp2Pct = c.risk?.rewardPctTp2 || (((tp2 - entry) / entry) * 100);
        const rr = c.risk?.rewardRiskRatio || 2.0;

        // Dynamic Sizing Calculation
        const sizing = computeCandidateInvestment(c, availableCap);
        const exitClass = sizing.exitDifficulty === "Easy" ? "easy" : (sizing.exitDifficulty === "Moderate" ? "moderate" : "difficult");
        const exitIcon = sizing.exitDifficulty === "Easy" ? "🟢" : (sizing.exitDifficulty === "Moderate" ? "🟡" : "🔴");

        // Live Stock Quote for Buy Zone check
        const liveStock = (typeof STOCKS !== "undefined" && STOCKS) ? STOCKS.find(s => s.symbol === c.symbol) : null;
        const currentLivePrice = liveStock ? liveStock.price : entry;
        let zoneBadgeHtml = `<span class="zone-status-badge in-buy">🟢 IN BUY ZONE</span>`;
        if (currentLivePrice > entryMax) {
            const extPct = (((currentLivePrice - entry) / entry) * 100).toFixed(1);
            zoneBadgeHtml = `<span class="zone-status-badge extended">🟡 +${extPct}% EXTENDED</span>`;
        } else if (currentLivePrice >= tp1) {
            zoneBadgeHtml = `<span class="zone-status-badge target-hit">🎯 TARGET 1 HIT</span>`;
        }

        const triggersHtml = (c.triggers || []).map(t => {
            const volBadge = t.volumeRatioToAvg20d ? `<span class="trigger-vol-badge">${t.volumeRatioToAvg20d}x Vol</span>` : '';
            const subtype = t.divergenceSubtype ? ` (${t.divergenceSubtype.replace('_', ' ')})` : '';
            const typeIcon = t.type === "BREAKOUT" ? "⚡" : (t.type === "MACD_CROSSOVER" ? "🎯" : (t.type === "RSI_DIVERGENCE" ? "📊" : "🔄"));
            return `
            <div class="trigger-chip-item">
                <div class="trigger-name-group">
                    <span>${typeIcon}</span>
                    <span>${t.type.replace('_', ' ')}${subtype}</span>
                </div>
                <div style="display: flex; gap: 4px; align-items: center;">
                    <span style="font-size: 0.68rem; color: #94a3b8;">${t.timeframe}</span>
                    ${volBadge}
                </div>
            </div>`;
        }).join("");

        return `
        <div class="weekly-candidate-card ${c.grade === 'A_PLUS' ? 'grade-a-plus' : ''}">
            <div class="card-top-row">
                <div class="candidate-symbol-block">
                    <button class="star-btn ${isWatched ? 'active' : ''}" data-star="${c.symbol}" title="Watchlist">${isWatched ? '★' : '☆'}</button>
                    <div class="candidate-symbol" onclick="showDetail('${c.symbol}')" style="cursor: pointer;">${c.symbol}</div>
                    <div class="candidate-sector">${c.sector}</div>
                </div>
                <div style="display: flex; align-items: center; gap: 6px;">
                    ${zoneBadgeHtml}
                    <div class="candidate-grade-badge ${gradeClass}">${gradeLabel}</div>
                </div>
            </div>

            <div class="direction-status-row">
                <div class="dir-badge ${dirClass}">${dirSymbol}</div>
                <div style="font-size: 0.76rem; color: #34d399; font-weight: 800;">
                    ⚡ ${conviction}% AI Conviction
                </div>
            </div>

            <!-- Score Progress -->
            <div class="score-breakdown-box">
                <div class="score-mini-metrics">
                    <span>Score: <strong>${rawScore}/6</strong> (Trend: ${c.score?.trendScore || 0}, Trig: ${c.score?.triggerScore || 0}, Vol: ${c.score?.volumeScore || 0})</span>
                    <span>${c.score?.volumeConfirmed ? '✔ Vol Confirmed' : 'Moderate Vol'}</span>
                </div>
                <div class="score-progress-track">
                    <div class="score-progress-fill" style="width: ${scorePct}%;"></div>
                </div>
            </div>

            <!-- Triggers -->
            <div class="triggers-chip-list">
                ${triggersHtml}
            </div>

            <!-- Visual Risk & Reward Progress Bar -->
            <div class="risk-visual-bar">
                <div class="risk-visual-labels">
                    <span style="color: #f87171;">Stop: ₨${stop.toFixed(2)} (-${riskPct.toFixed(1)}%)</span>
                    <span style="color: #ffffff; font-weight: 700;">Buy Zone: ₨${entryMin.toFixed(2)}–₨${entryMax.toFixed(2)}</span>
                    <span style="color: #34d399;">TP1: ₨${tp1.toFixed(2)} (+${tp1Pct.toFixed(1)}%)</span>
                </div>
                <div class="risk-visual-track-container">
                    <div class="risk-visual-fill-stop"></div>
                    <div class="risk-visual-fill-target"></div>
                    <div class="risk-entry-pointer" title="Optimal Entry Level"></div>
                </div>
                <div style="display: flex; justify-content: space-between; font-size: 0.68rem; color: #94a3b8;">
                    <span>Risk: ₨${(entry - stop).toFixed(2)}/share</span>
                    <span class="rr-pill">${rr.toFixed(1)}x Risk:Reward</span>
                    <span style="color: #38bdf8;">TP2: ₨${tp2.toFixed(2)} (+${tp2Pct.toFixed(1)}%)</span>
                </div>
            </div>

            <!-- 💰 Capital & Dynamic Liquidity Recommendation Box -->
            <div class="candidate-capital-box">
                <div class="capital-row-top">
                    <span style="color: #cbd5e1; font-weight: 700;">Recommended Investment:</span>
                    <span class="capital-rec-amount">PKR ${sizing.recommendedPkr.toLocaleString()} (${sizing.percentOfCapital}%)</span>
                </div>
                <div style="display: flex; justify-content: space-between; font-size: 0.74rem; color: #94a3b8;">
                    <span>Position Size: <strong style="color: #fff;">${sizing.recommendedShares.toLocaleString()} Shares</strong></span>
                    <span title="Expected execution duration">Exit Speed: <strong style="color: #38bdf8;">${sizing.exitTimeEst}</strong></span>
                </div>
                <div style="display: flex; justify-content: space-between; align-items: center; font-size: 0.74rem; background: rgba(0,0,0,0.25); padding: 4px 8px; border-radius: 4px;">
                    <span style="color: #34d399;">Est. Gain (TP1): <strong>+PKR ${sizing.profitAtTp1Pkr.toLocaleString()}</strong></span>
                    <span style="color: #f87171;">Max Risk (SL): <strong>-PKR ${sizing.riskAtStopPkr.toLocaleString()}</strong></span>
                </div>
                <div style="display: flex; justify-content: space-between; align-items: center; font-size: 0.7rem; margin-top: 2px;">
                    <span class="capital-reason-text">${sizing.reason}</span>
                    <span class="exit-badge ${exitClass}" title="Expected Exit Liquidity">${sizing.exitDifficulty} ${exitIcon}</span>
                </div>
            </div>

            <!-- Action Plan & Bilingual Urdu Summary -->
            <div class="candidate-rationale-box">
                <div style="font-weight: 700; color: #e2e8f0; margin-bottom: 4px;">📋 Action Plan:</div>
                <div>${c.actionPlan || c.rationale}</div>
                ${c.urduSummary ? `<div class="urdu-summary-box">💡 ${c.urduSummary}</div>` : ''}
            </div>

            <!-- Bottom Actions -->
            <div class="candidate-actions-row">
                <button class="btn btn-primary btn-sm btn-buy-card" onclick="openWeeklyBuyModal('${c.id}')">
                    ⚡ Quick Buy (${sizing.recommendedShares.toLocaleString()} Shares)
                </button>
                <button class="btn btn-secondary btn-sm" onclick="openStockChartModal('${c.symbol}')">
                    📈 4H / Daily Chart
                </button>
                <button class="btn btn-ghost btn-sm" onclick="showDetail('${c.symbol}')">
                    🔍 Profile
                </button>
            </div>
        </div>`;
    }).join("");
}


// ═════════════════════════════════════════════════════════════════
// ⚡ DIRECT BUY EXECUTION MODAL & ORDER ROUTING
// ═════════════════════════════════════════════════════════════════

function openWeeklyBuyModal(candidateId) {
    const cand = (currentWeeklyCandidates || []).find(c => c.id === candidateId);
    if (!cand) return;
    currentBuyCandidate = cand;

    const modal = document.getElementById("weekly-buy-modal");
    if (!modal) return;

    const availableCap = getUserAvailableCapital();
    const sizing = computeCandidateInvestment(cand, availableCap);

    document.getElementById("buy-modal-title").textContent = `Buy ${cand.symbol} (${cand.sector || 'PSX'})`;
    document.getElementById("buy-modal-subtitle").textContent = `Weekly Trade ${cand.grade.replace('_', '+')} • ${cand.direction} Setup`;
    document.getElementById("buy-modal-avail-cap").textContent = `PKR ${availableCap.toLocaleString()}`;
    document.getElementById("buy-modal-max-alloc").textContent = `PKR ${sizing.maxCeilingPkr.toLocaleString()} (50%)`;
    document.getElementById("buy-modal-rec-invest").textContent = `PKR ${sizing.recommendedPkr.toLocaleString()} (${sizing.percentOfCapital}%)`;
    
    const exitBadge = document.getElementById("buy-modal-exit-badge");
    const exitClass = sizing.exitDifficulty === "Easy" ? "easy" : (sizing.exitDifficulty === "Moderate" ? "moderate" : "difficult");
    const exitIcon = sizing.exitDifficulty === "Easy" ? "🟢" : (sizing.exitDifficulty === "Moderate" ? "🟡" : "🔴");
    if (exitBadge) {
        exitBadge.className = `exit-badge ${exitClass}`;
        exitBadge.textContent = `${sizing.exitDifficulty} Exit ${exitIcon}`;
    }

    document.getElementById("buy-modal-reason").textContent = `Reason: ${sizing.reason}`;
    document.getElementById("buy-modal-shares").value = sizing.recommendedShares;
    document.getElementById("buy-modal-price").value = cand.risk?.entry || 1.0;
    document.getElementById("buy-modal-stop").value = cand.risk?.stop || 0.0;
    document.getElementById("buy-modal-target").value = cand.risk?.target || 0.0;

    const statusMsg = document.getElementById("buy-modal-status-msg");
    if (statusMsg) statusMsg.style.display = "none";

    recalcBuyModalTotal();
    modal.style.display = "flex";
}

function recalcBuyModalTotal() {
    const shares = parseInt(document.getElementById("buy-modal-shares")?.value) || 0;
    const price = parseFloat(document.getElementById("buy-modal-price")?.value) || 0;
    const total = shares * price;
    const fees = total * 0.0015;

    const totalEl = document.getElementById("buy-modal-total-cost");
    const feesEl = document.getElementById("buy-modal-fees");
    if (totalEl) totalEl.textContent = `PKR ${total.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
    if (feesEl) feesEl.textContent = `PKR ${fees.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
}

async function submitWeeklyBuyOrder() {
    if (!currentBuyCandidate) return;

    const shares = parseInt(document.getElementById("buy-modal-shares")?.value) || 0;
    const price = parseFloat(document.getElementById("buy-modal-price")?.value) || 0;
    const stopLoss = parseFloat(document.getElementById("buy-modal-stop")?.value) || 0;
    const target = parseFloat(document.getElementById("buy-modal-target")?.value) || 0;
    const statusMsg = document.getElementById("buy-modal-status-msg");
    const btn = document.getElementById("btn-execute-weekly-buy");

    if (shares <= 0 || price <= 0) {
        if (statusMsg) {
            statusMsg.style.display = "block";
            statusMsg.className = "activation-msg msg-error";
            statusMsg.textContent = "Please enter valid shares and execution price.";
        }
        return;
    }

    if (btn) btn.disabled = true;
    try {
        const res = await fetch("/api/weekly-scan/buy", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                symbol: currentBuyCandidate.symbol,
                name: currentBuyCandidate.symbol,
                sector: currentBuyCandidate.sector || "Other",
                shares: shares,
                entry_price: price,
                stop_loss: stopLoss,
                target_price: target,
                strategy: `Weekly ${currentBuyCandidate.grade} Swing`
            })
        });
        const json = await res.json();
        if (json.success) {
            if (statusMsg) {
                statusMsg.style.display = "block";
                statusMsg.className = "activation-msg msg-success";
                statusMsg.textContent = `✔ Order executed! ${json.message || ''}`;
            }
            setTimeout(() => {
                const modal = document.getElementById("weekly-buy-modal");
                if (modal) modal.style.display = "none";
                if (btn) btn.disabled = false;
            }, 1200);
        } else {
            if (statusMsg) {
                statusMsg.style.display = "block";
                statusMsg.className = "activation-msg msg-error";
                statusMsg.textContent = `✖ ${json.error || 'Failed to place trade.'}`;
            }
            if (btn) btn.disabled = false;
        }
    } catch (e) {
        if (statusMsg) {
            statusMsg.style.display = "block";
            statusMsg.className = "activation-msg msg-error";
            statusMsg.textContent = "Network error executing trade.";
        }
        if (btn) btn.disabled = false;
    }
}

// ═════════════════════════════════════════════════════════════════
// 🎯 ACCURACY & PREDICTION PERFORMANCE LEDGER
// ═════════════════════════════════════════════════════════════════

async function loadWeeklyPerformanceData(showLoading = true) {
    const tbody = document.getElementById("audit-ledger-body");
    const countSubtabPerf = document.getElementById("count-subtab-performance");
    const outcomeFilter = document.getElementById("audit-filter-outcome")?.value || "ALL";
    const gradeFilter = document.getElementById("audit-filter-grade")?.value || "ALL";

    if (showLoading && tbody) {
        tbody.innerHTML = `<tr><td colspan="12" style="text-align:center; padding:32px; color:#94a3b8;"><div class="spinner" style="margin: 0 auto 10px;"></div>Loading accuracy & performance metrics...</td></tr>`;
    }

    try {
        const url = `/api/weekly-scan/performance?outcome=${encodeURIComponent(outcomeFilter)}&grade=${encodeURIComponent(gradeFilter)}&limit=100`;
        const res = await fetch(url);
        const json = await res.json();
        if (json.success) {
            const sum = json.summary || {};
            const hist = json.history || [];

            // Update KPI Cards
            const kpiWin = document.getElementById("kpi-win-rate");
            const kpiCounts = document.getElementById("kpi-win-counts");
            const kpiAPlus = document.getElementById("kpi-a-plus-win-rate");
            const kpiAPlusCounts = document.getElementById("kpi-a-plus-counts");
            const kpiAvgGain = document.getElementById("kpi-avg-gain");
            const kpiAvgLoss = document.getElementById("kpi-avg-loss");
            const kpiProfitFactor = document.getElementById("kpi-profit-factor");
            const kpiTotalR = document.getElementById("kpi-total-r");
            const kpiTotalPred = document.getElementById("kpi-total-predictions");
            const kpiInProg = document.getElementById("kpi-in-progress-count");

            if (kpiWin) kpiWin.textContent = `${sum.overallWinRatePct || 0}%`;
            if (kpiCounts) kpiCounts.textContent = `${sum.successfulCount || 0} Won / ${sum.stoppedCount || 0} Lost (${sum.closedEvaluations || 0} Closed)`;
            
            const aPlusData = sum.gradeBreakdown?.A_PLUS || {};
            if (kpiAPlus) kpiAPlus.textContent = `${aPlusData.winRatePct || 0}%`;
            if (kpiAPlusCounts) kpiAPlusCounts.textContent = `${aPlusData.won || 0} Won / ${aPlusData.lost || 0} Lost (Total: ${aPlusData.total || 0})`;

            if (kpiAvgGain) kpiAvgGain.textContent = `+${sum.avgWinnerGainPct || 0}%`;
            if (kpiAvgLoss) kpiAvgLoss.textContent = `Avg Loss: -${sum.avgLoserLossPct || 0}%`;
            if (kpiProfitFactor) kpiProfitFactor.textContent = `${sum.profitFactor || 1.0}x`;
            if (kpiTotalR) kpiTotalR.textContent = `Net R: ${sum.totalRealizedR >= 0 ? '+' : ''}${sum.totalRealizedR || 0} R`;

            if (kpiTotalPred) kpiTotalPred.textContent = sum.totalPredictions || 0;
            if (kpiInProg) kpiInProg.textContent = `${sum.inProgressCount || 0} In-Progress (3d/5d/7d Window)`;
            if (countSubtabPerf) countSubtabPerf.textContent = sum.totalPredictions || 0;

            // Render Table Rows
            if (hist.length === 0) {
                if (tbody) tbody.innerHTML = `<tr><td colspan="12" style="text-align:center; padding:32px; color:#94a3b8;">No predictions match the selected filter criteria.</td></tr>`;
                return;
            }

            if (tbody) {
                tbody.innerHTML = hist.map(r => {
                    const outcomeClass = r.outcome === "SUCCESSFUL" ? "successful" : (r.outcome === "STOPPED_OUT" ? "stopped_out" : (r.outcome === "EXPIRED_TIME" ? "expired_time" : "in_progress"));
                    const outcomeIcon = r.outcome === "SUCCESSFUL" ? "🎯" : (r.outcome === "STOPPED_OUT" ? "🛑" : (r.outcome === "EXPIRED_TIME" ? "⌛" : "⏳"));
                    const outcomeLabel = r.outcome === "SUCCESSFUL" ? "TARGET HIT" : (r.outcome === "STOPPED_OUT" ? "STOPPED OUT" : (r.outcome === "EXPIRED_TIME" ? "EXPIRED" : "IN PROGRESS"));
                    const predDateStr = (r.predictedAt || "").substring(0, 10);
                    const gradeClass = r.grade === "A_PLUS" ? "grade-badge-a-plus" : (r.grade === "A" ? "grade-badge-a" : "grade-badge-b");
                    const returnColor = r.currentReturnPct >= 0 ? "#34d399" : "#f87171";

                    return `
                    <tr>
                        <td style="white-space: nowrap;">
                            <div style="font-weight: 700; color: #f8fafc;">${predDateStr}</div>
                            <div style="font-size: 0.7rem; color: #94a3b8;">${r.daysElapsed}d ago</div>
                        </td>
                        <td>
                            <div style="font-weight: 800; color: #f8fafc; font-family: 'JetBrains Mono', monospace; cursor: pointer;" onclick="showDetail('${r.symbol}')">${r.symbol}</div>
                            <div style="font-size: 0.7rem; color: #94a3b8;">${r.sector || 'Other'}</div>
                        </td>
                        <td><span class="candidate-grade-badge ${gradeClass}" style="font-size: 0.68rem; padding: 2px 6px;">${r.grade.replace('_', '+')}</span></td>
                        <td><span class="dir-badge ${r.direction === 'LONG' ? 'long' : 'short'}">${r.direction}</span></td>
                        <td style="font-family: 'JetBrains Mono', monospace;">Rs ${r.entryPrice.toFixed(2)}</td>
                        <td style="font-family: 'JetBrains Mono', monospace; color: #f87171;">Rs ${r.stopPrice.toFixed(2)}</td>
                        <td style="font-family: 'JetBrains Mono', monospace; color: #34d399;">Rs ${r.targetPrice.toFixed(2)}</td>
                        <td style="font-family: 'JetBrains Mono', monospace; color: #34d399; font-weight: 700;">+${r.maxGainPct.toFixed(1)}%</td>
                        <td style="font-family: 'JetBrains Mono', monospace; font-weight: 800; color: ${returnColor};">${r.currentReturnPct >= 0 ? '+' : ''}${r.currentReturnPct.toFixed(1)}%</td>
                        <td>
                            <span class="outcome-pill ${outcomeClass}">
                                <span>${outcomeIcon}</span>
                                <span>${outcomeLabel}</span>
                            </span>
                        </td>
                        <td style="font-size: 0.74rem; color: #cbd5e1; max-width: 280px; line-height: 1.35;">${r.evaluationNotes || 'Audited against live prices.'}</td>
                        <td>
                            <button class="btn btn-ghost btn-sm" onclick="openStockChartModal('${r.symbol}')" style="font-size: 0.72rem; padding: 4px 8px;">
                                📈 Chart
                            </button>
                        </td>
                    </tr>`;
                }).join("");
            }
        }
    } catch (e) {
        console.error("Error loading performance data:", e);
        if (tbody) tbody.innerHTML = `<tr><td colspan="12" style="text-align:center; padding:32px; color:#f87171;">Failed to load accuracy metrics.</td></tr>`;
    }
}

async function reanalyzeWeeklyPredictions() {
    const btn = document.getElementById("btn-reanalyze-audit");
    const icon = document.getElementById("icon-reanalyze-audit");
    if (btn) btn.disabled = true;
    if (icon) icon.classList.add("spinning");

    try {
        const res = await fetch("/api/weekly-scan/audit", { method: "POST" });
        const json = await res.json();
        if (json.success) {
            loadWeeklyPerformanceData(false);
            loadWeeklyScanData(false);
        } else {
            alert(json.error || "Failed to re-analyze predictions.");
        }
    } catch (e) {
        console.error("Re-analysis error:", e);
    } finally {
        if (btn) btn.disabled = false;
        if (icon) icon.classList.remove("spinning");
    }
}

async function triggerWeeklyManualRescan() {
    const btn = document.getElementById("btn-weekly-rescan");
    const icon = document.getElementById("icon-weekly-rescan");
    if (btn) btn.disabled = true;
    if (icon) icon.classList.add("spinning");

    try {
        const res = await fetch("/api/weekly-scan/rescan", { method: "POST" });
        const json = await res.json();
        if (json.success && json.run) {
            currentWeeklyRun = json.run;
            currentWeeklyCandidates = json.candidates || [];
            renderWeeklyScanView();
            loadWeeklyPerformanceData(false);
        } else {
            await loadWeeklyScanData(true);
        }
    } catch (e) {
        console.error("Rescan trigger error:", e);
        await loadWeeklyScanData(true);
    } finally {
        if (btn) btn.disabled = false;
        if (icon) icon.classList.remove("spinning");
    }
}


async function showWeeklyHistoryModal() {
    const modal = document.getElementById("weekly-scan-history-modal");
    const listEl = document.getElementById("weekly-history-list");
    if (modal) modal.style.display = "flex";
    if (listEl) listEl.innerHTML = `<div style="text-align:center; padding: 20px; color: #94a3b8;">Loading archives...</div>`;

    try {
        const res = await fetch("/api/weekly-scan/runs?limit=20");
        const json = await res.json();
        if (json.success && json.runs && json.runs.length > 0) {
            listEl.innerHTML = json.runs.map(r => `
            <div style="display:flex; justify-content:space-between; align-items:center; background: rgba(30,41,59,0.7); border:1px solid rgba(255,255,255,0.08); border-radius: 8px; padding: 12px 16px; margin-bottom: 8px;">
                <div>
                    <div style="font-weight: 700; color: #f8fafc; font-size: 0.9rem;">${r.dataAsOfDate} (${r.runType.replace('_', ' ')})</div>
                    <div style="font-size: 0.75rem; color: #94a3b8; margin-top: 2px;">
                        ${r.candidatesReturned} Candidates | Universe: ${r.universeSize} | Config: v${r.configVersion}
                    </div>
                </div>
                <button class="btn btn-primary btn-sm" onclick="loadHistoricalWeeklyRun('${r.id}')" style="font-size: 0.76rem; padding: 5px 12px;">
                    View Candidates →
                </button>
            </div>`).join("");
        } else {
            listEl.innerHTML = `<div style="text-align:center; padding: 20px; color: #94a3b8;">No historical runs recorded yet.</div>`;
        }
    } catch (e) {
        if (listEl) listEl.innerHTML = `<div style="color: #f87171; text-align:center;">Failed to load archive history.</div>`;
    }
}

async function loadHistoricalWeeklyRun(runId) {
    try {
        const res = await fetch(`/api/weekly-scan/runs/${runId}`);
        const json = await res.json();
        if (json.success && json.run) {
            currentWeeklyRun = json.run;
            currentWeeklyCandidates = json.candidates || [];
            renderWeeklyScanView();
            const modal = document.getElementById("weekly-scan-history-modal");
            if (modal) modal.style.display = "none";
        }
    } catch (e) {
        console.error("Error loading historical run:", e);
    }
}

async function showWeeklyConfigModal() {
    const modal = document.getElementById("weekly-scan-config-modal");
    if (modal) modal.style.display = "flex";

    try {
        const res = await fetch("/api/weekly-scan/config");
        const json = await res.json();
        if (json.success && json.config) {
            const cfg = json.config;
            const setVal = (id, val) => { const el = document.getElementById(id); if (el) el.value = val; };
            setVal("cfg-min-traded-val", cfg.liquidity?.minAvgDailyTradedValuePkr || 20000000);
            setVal("cfg-breakout-days", cfg.trigger?.breakoutLookbackDays || 20);
            setVal("cfg-vol-mult", cfg.volumeConfirmation?.minVolumeMultiple || 1.5);
            setVal("cfg-atr-mult", cfg.risk?.atrMultipleForStop || 1.5);
            setVal("cfg-min-rr", cfg.risk?.minRewardRiskRatio || 1.5);
            setVal("cfg-max-candidates", cfg.output?.maxCandidatesShown || 20);
        }
    } catch (e) {
        console.error("Config fetch error:", e);
    }
}

async function saveWeeklyConfigFromModal() {
    const getVal = (id) => parseFloat(document.getElementById(id)?.value) || 0;
    const newConfig = {
        liquidity: { minAvgDailyTradedValuePkr: getVal("cfg-min-traded-val") },
        trigger: {
            breakoutLookbackDays: parseInt(document.getElementById("cfg-breakout-days")?.value) || 20,
            breakoutVolumeMultiple: getVal("cfg-vol-mult"),
            macdCrossoverMaxBarsAgo: 3,
            pullbackMaEma: 20
        },
        volumeConfirmation: { minVolumeMultiple: getVal("cfg-vol-mult") },
        risk: {
            atrMultipleForStop: getVal("cfg-atr-mult"),
            minRewardRiskRatio: getVal("cfg-min-rr"),
            defaultTargetMultipleIfNoStructure: 2.0
        },
        output: {
            maxCandidatesShown: parseInt(document.getElementById("cfg-max-candidates")?.value) || 20,
            minGradeToDisplay: "B"
        }
    };

    const statusMsg = document.getElementById("cfg-status-msg");
    try {
        const res = await fetch("/api/weekly-scan/config", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(newConfig)
        });
        const jsonRes = await res.json();
        if (jsonRes.success) {
            if (statusMsg) {
                statusMsg.style.display = "block";
                statusMsg.className = "activation-msg msg-success";
                statusMsg.textContent = `✔ Thresholds saved as version ${jsonRes.config?.version || 'new'}!`;
            }
            setTimeout(() => {
                const modal = document.getElementById("weekly-scan-config-modal");
                if (modal) modal.style.display = "none";
                if (statusMsg) statusMsg.style.display = "none";
                triggerWeeklyManualRescan();
            }, 1200);
        } else {
            if (statusMsg) {
                statusMsg.style.display = "block";
                statusMsg.className = "activation-msg msg-error";
                statusMsg.textContent = `✖ ${jsonRes.error || 'Failed to save config.'}`;
            }
        }
    } catch (e) {
        if (statusMsg) {
            statusMsg.style.display = "block";
            statusMsg.className = "activation-msg msg-error";
            statusMsg.textContent = "Network error saving config.";
        }
    }
}

async function updateWeeklyCandidateStatus(candidateId, newStatus) {
    try {
        const res = await fetch(`/api/weekly-scan/candidates/${candidateId}/status`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ status: newStatus })
        });
        const json = await res.json();
        if (json.success) {
            const cand = (currentWeeklyCandidates || []).find(c => c.id === candidateId);
            if (cand) cand.status = newStatus;
        }
    } catch (e) {
        console.error("Status update error:", e);
    }
}

function openStockChartModal(symbol) {
    if (typeof showDetail === "function") {
        showDetail(symbol);
    }
}

// ─── Initial Application Startup ───
document.addEventListener("DOMContentLoaded", () => {
    initEventListeners();
    fetchLiveData();
    fetchAndApplyTabStatuses();

    // Periodic Tab Deployment Status Sync (Every 8 seconds)
    setInterval(fetchAndApplyTabStatuses, 8000);
});



// ═══════════════════════════════════════════════════════════════════════════
// 🧠  PSX MARKET INTELLIGENCE ENGINE — FRONTEND MODULE
// ═══════════════════════════════════════════════════════════════════════════

const intelligenceTab = (() => {

    let _refreshTimer = null;
    let _isLoading = false;

    const EVENT_LABELS = {
        UPPER_LOCK:       { label: '🔒 Upper Lock',       cls: 'event-upper-lock' },
        LOWER_LOCK:       { label: '🔓 Lower Lock',       cls: 'event-lower-lock' },
        PRICE_SPIKE:      { label: '🚀 Price Spike',      cls: 'event-price-spike' },
        VOLUME_SURGE:     { label: '📈 Volume Surge',     cls: 'event-volume-surge' },
        RESISTANCE_BREAK: { label: '💥 Breakout',         cls: 'event-breakout' },
        RSI_MOMENTUM:     { label: '⚡ RSI Surge',        cls: 'event-rsi' },
        REVERSAL_SIGNAL:  { label: '🔄 Reversal Signal',  cls: 'event-reversal' },
        ACCUMULATION:     { label: '🧲 Accumulation',     cls: 'event-accumulation' }
    };

    const SIGNAL_LABELS = {
        WATCH:               { label: '👁 WATCH',             cls: 'signal-watch' },
        POSSIBLE_BREAKOUT:   { label: '💥 BREAKOUT IMMINENT', cls: 'signal-breakout' },
        CONTINUATION_LIKELY: { label: '→ CONTINUATION',       cls: 'signal-continuation' },
        REVERSAL_RISK:       { label: '⚠️ REVERSAL RISK',     cls: 'signal-reversal' },
        EXTENDED_AVOID:      { label: '🛑 EXTENDED',          cls: 'signal-extended' }
    };

    const FACTOR_LABELS = {
        TECHNICAL_BREAKOUT:     '📊 Technical Breakout',
        VOLUME_ACCUMULATION:    '📦 Volume Accumulation',
        RSI_MOMENTUM:           '⚡ RSI Momentum',
        MACD_CONFIRMATION:      '✳️ MACD Confirmation',
        SECTOR_MOMENTUM:        '🏭 Sector Momentum',
        MARKET_MOMENTUM:        '🌐 Market Momentum',
        CORPORATE_ANNOUNCEMENT: '📋 Corporate Announcement',
        UPPER_LOCK_SETUP:       '🔒 Upper Lock Setup'
    };

    function _timeAgo(isoStr) {
        if (!isoStr || isoStr === 'Never') return '—';
        const diff = Math.floor((Date.now() - new Date(isoStr)) / 1000);
        if (diff < 60)    return `${diff}s ago`;
        if (diff < 3600)  return `${Math.floor(diff / 60)}m ago`;
        if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
        return `${Math.floor(diff / 86400)}d ago`;
    }

    function _fmt(num, decimals = 1) {
        if (num === null || num === undefined || isNaN(num)) return '—';
        return Number(num).toFixed(decimals);
    }

    function _confBar(val) {
        const pct = Math.min(100, Math.max(0, val));
        const cls = pct >= 75 ? 'conf-high' : pct >= 50 ? 'conf-med' : 'conf-low';
        return `<div class="intel-conf-bar-wrap"><div class="intel-conf-bar ${cls}" style="width:${pct}%"></div></div>`;
    }

    function _setText(id, val) {
        const el = document.getElementById(id);
        if (el) el.textContent = val;
    }

    async function _fetchAll() {
        const [s, e, p, pr] = await Promise.allSettled([
            fetch('/api/intelligence/summary').then(r => r.json()),
            fetch('/api/intelligence/live-events?limit=50').then(r => r.json()),
            fetch('/api/intelligence/patterns').then(r => r.json()),
            fetch('/api/intelligence/predictions?limit=20').then(r => r.json())
        ]);
        return {
            summary:     s.status  === 'fulfilled' ? s.value  : null,
            events:      e.status  === 'fulfilled' ? e.value  : null,
            patterns:    p.status  === 'fulfilled' ? p.value  : null,
            predictions: pr.status === 'fulfilled' ? pr.value : null
        };
    }

    async function load() {
        if (_isLoading) return;
        _isLoading = true;
        try {
            const { summary, events, patterns, predictions } = await _fetchAll();
            if (summary && summary.success) renderHeader(summary);
            if (events && events.success) {
                renderEventFeed(events.events || []);
                renderWhyPanel(events.events || []);
            }
            if (patterns && patterns.success) renderPatternPanel(patterns.patterns || []);
            if (predictions && predictions.success) renderPredictionPanel(predictions.predictions || []);
            _setText('intel-footer-refreshed', 'Last refreshed: ' + new Date().toLocaleTimeString('en-PK'));
        } catch (err) {
            console.error('[Intelligence] load error:', err);
        } finally {
            _isLoading = false;
        }
    }

    function renderHeader(summary) {
        const s = summary.stats || {};
        _setText('intel-stat-events',   s.total_events_detected ?? '—');
        _setText('intel-stat-patterns', s.patterns_discovered ?? '—');
        _setText('intel-stat-winrate',  s.evaluated_predictions > 0 ? s.win_rate_pct + '%' : '—');
        _setText('intel-stat-last-tick', _timeAgo(s.last_anomaly_tick));
        _setText('intel-footer-preds',   s.total_predictions ?? 0);
        _setText('intel-footer-correct', s.correct_predictions ?? 0);
        const pill = document.getElementById('intel-engine-status');
        if (pill) { pill.className = 'intel-status-pill online'; pill.innerHTML = '<span class="intel-pulse"></span> Engine Online'; }
    }

    function renderEventFeed(events) {
        const c = document.getElementById('intel-event-feed');
        const ct = document.getElementById('intel-event-count');
        if (!c) return;
        if (ct) ct.textContent = events.length + ' events';
        if (!events.length) {
            c.innerHTML = '<div class="intel-initializing"><div class="intel-spinner"></div><p>No events yet. Check back during market hours (9 AM–3:30 PM PKT).</p></div>';
            return;
        }
        c.innerHTML = events.map(ev => {
            const et = EVENT_LABELS[ev.event_type] || { label: ev.event_type, cls: 'event-generic' };
            const sc = ev.price_change_pct >= 0 ? '+' : '';
            const cc = ev.price_change_pct >= 0 ? 'pos' : 'neg';
            return `<div class="intel-event-card ${et.cls}" onclick="intelligenceTab.showEventDetail('${ev.id}')">
                <div class="intel-event-header">
                    <span class="intel-event-symbol">${ev.symbol}</span>
                    <span class="intel-event-type-badge ${et.cls}">${et.label}</span>
                    <span class="intel-event-time">${_timeAgo(ev.detected_at)}</span>
                </div>
                <div class="intel-event-meta">
                    <span class="intel-chg ${cc}">${sc}${_fmt(ev.price_change_pct)}%</span>
                    <span class="intel-rvol">RVOL ${_fmt(ev.rvol)}×</span>
                    <span class="intel-rsi">RSI ${_fmt(ev.rsi_at_event, 0)}</span>
                    <span class="intel-sector">${ev.sector || ''}</span>
                </div>
                ${ev.narrative ? `<div class="intel-narrative">${ev.narrative}</div>` : ''}
                ${ev.top_cause ? `<div class="intel-top-cause"><span>${FACTOR_LABELS[ev.top_cause.factor] || ev.top_cause.factor}</span><span class="intel-cause-conf">${ev.top_cause.confidence}%</span></div>` : ''}
            </div>`;
        }).join('');
    }

    function renderWhyPanel(events) {
        const c = document.getElementById('intel-why-panel');
        if (!c) return;
        if (!events.length) { c.innerHTML = '<div class="intel-initializing"><div class="intel-spinner"></div><p>Awaiting events…</p></div>'; return; }
        c.innerHTML = events.slice(0, 5).map(ev => {
            const et = EVENT_LABELS[ev.event_type] || { label: ev.event_type, cls: 'event-generic' };
            return `<div class="intel-why-card">
                <div class="intel-why-header">
                    <span class="intel-event-symbol">${ev.symbol}</span>
                    <span class="intel-event-type-badge ${et.cls}">${et.label}</span>
                    <span style="font-size:.73rem;color:#64748b;margin-left:auto">${_timeAgo(ev.detected_at)}</span>
                </div>
                <div class="intel-cause-count">${ev.cause_count} factor${ev.cause_count !== 1 ? 's' : ''} identified</div>
                ${ev.top_cause ? `<div class="intel-cause-row">
                    <div class="intel-cause-name">${FACTOR_LABELS[ev.top_cause.factor] || ev.top_cause.factor}</div>
                    ${_confBar(ev.top_cause.confidence)}
                    <div class="intel-cause-pct">${ev.top_cause.confidence}%</div>
                </div>` : ''}
                ${ev.narrative ? `<div class="intel-why-narrative">${ev.narrative}</div>` : ''}
                <button class="intel-detail-btn" onclick="intelligenceTab.showEventDetail('${ev.id}')">Full Analysis →</button>
            </div>`;
        }).join('');
    }

    function renderPatternPanel(patterns) {
        const c = document.getElementById('intel-pattern-panel');
        const ct = document.getElementById('intel-pattern-count');
        if (!c) return;
        if (ct) ct.textContent = patterns.length + ' pattern' + (patterns.length !== 1 ? 's' : '');
        if (!patterns.length) { c.innerHTML = '<div class="intel-initializing"><div class="intel-spinner"></div><p>Pattern library builds overnight.<br>Needs ≥3 occurrences per pattern.</p></div>'; return; }
        c.innerHTML = patterns.map(p => {
            const total = (p.win_count || 0) + (p.loss_count || 0);
            const wr = total > 0 ? Math.round((p.win_count / total) * 100) : null;
            const wrHtml = wr !== null ? `<span class="intel-win-rate ${wr >= 60 ? 'good' : wr >= 45 ? 'med' : 'low'}">${wr}% win</span>` : '<span class="intel-win-rate neutral">Pending</span>';
            return `<div class="intel-pattern-card">
                <div class="intel-pattern-header"><span class="intel-pattern-name">${p.name}</span>${wrHtml}</div>
                <div class="intel-pattern-meta">
                    <span class="intel-occ">${p.occurrences} occurrence${p.occurrences !== 1 ? 's' : ''}</span>
                    ${p.avg_5d_return ? `<span class="intel-avg-return">${p.avg_5d_return >= 0 ? '+' : ''}${_fmt(p.avg_5d_return)}% avg 5D</span>` : ''}
                    ${p.avg_3d_return ? `<span class="intel-avg-return">${p.avg_3d_return >= 0 ? '+' : ''}${_fmt(p.avg_3d_return)}% avg 3D</span>` : ''}
                </div>
                <div class="intel-pattern-desc">${p.description || ''}</div>
                ${wr !== null && total > 0 ? _confBar(wr) : ''}
            </div>`;
        }).join('');
    }

    function renderPredictionPanel(predictions) {
        const c = document.getElementById('intel-prediction-panel');
        const ct = document.getElementById('intel-pred-count');
        if (!c) return;
        if (ct) ct.textContent = predictions.length + ' active';
        if (!predictions.length) { c.innerHTML = '<div class="intel-initializing"><div class="intel-spinner"></div><p>No active predictions yet.</p></div>'; return; }
        c.innerHTML = predictions.map(pred => {
            const sig = SIGNAL_LABELS[pred.signal] || { label: pred.signal, cls: 'signal-watch' };
            const reasoning = pred.reasoning || {};
            const histText = pred.historical_sample > 0 ? pred.historical_sample + ' hist. matches' : 'First occurrence';
            const wrText   = pred.historical_win_rate > 0 ? ` · ${_fmt(pred.historical_win_rate, 0)}% win rate` : '';
            return `<div class="intel-pred-card">
                <div class="intel-pred-header">
                    <span class="intel-event-symbol" onclick="typeof showDetail==='function'&&showDetail('${pred.symbol}')" style="cursor:pointer">${pred.symbol}</span>
                    <span class="intel-signal-badge ${sig.cls}">${sig.label}</span>
                    <span class="intel-conf-pct">${pred.confidence}%</span>
                </div>
                <div class="intel-pred-meta">
                    <span>₨${pred.price_at_signal ? Number(pred.price_at_signal).toFixed(2) : '—'} at signal</span>
                    <span>${_timeAgo(pred.predicted_at)}</span>
                </div>
                ${pred.pattern_name && pred.pattern_name !== 'No Pattern Matched' ? `<div class="intel-pred-pattern">📚 ${pred.pattern_name}</div>` : ''}
                <div class="intel-pred-hist">${histText}${wrText}</div>
                ${reasoning.narrative ? `<div class="intel-why-narrative">${reasoning.narrative}</div>` : ''}
                ${_confBar(pred.confidence)}
            </div>`;
        }).join('');
    }

    async function showEventDetail(eventId) {
        try {
            const res = await fetch('/api/intelligence/event/' + eventId).then(r => r.json());
            if (!res.success) return;
            const ev = res.event || {};
            const causes = res.causes || [];
            const pattern = res.matched_pattern;
            const causesHtml = causes.map(cause => `
                <div class="intel-cause-row-detail">
                    <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;">
                        <span class="intel-cause-name">${FACTOR_LABELS[cause.factor] || cause.factor}</span>
                        <span class="intel-cause-evidence-badge ev-${(cause.evidence||'weak').toLowerCase().replace(' ','-')}">${cause.evidence}</span>
                        <span class="intel-cause-pct" style="margin-left:auto">${cause.confidence}%</span>
                    </div>
                    ${_confBar(cause.confidence)}
                    ${cause.detail ? `<div class="intel-cause-detail-text">${cause.detail}</div>` : ''}
                </div>`).join('');
            const patternHtml = pattern ? `
                <div class="intel-detail-pattern-match">
                    <div class="intel-detail-section-title">📚 Matched Pattern</div>
                    <strong>${pattern.name}</strong><p style="color:#94a3b8;font-size:.8rem;margin:4px 0">${pattern.description || ''}</p>
                    ${pattern.occurrences ? `<span class="intel-occ">${pattern.occurrences} occurrences</span>` : ''}
                </div>` : '';
            let overlay = document.getElementById('intel-detail-overlay');
            if (!overlay) {
                overlay = document.createElement('div');
                overlay.id = 'intel-detail-overlay';
                overlay.className = 'terms-modal-overlay';
                overlay.onclick = e => { if (e.target === overlay) overlay.style.display = 'none'; };
                document.body.appendChild(overlay);
            }
            overlay.innerHTML = `<div class="terms-modal-box" style="max-width:640px;max-height:85vh;overflow-y:auto;background:#0f172a;border:1px solid rgba(139,92,246,.4);">
                <div style="display:flex;align-items:center;gap:10px;padding:14px 20px;border-bottom:1px solid rgba(255,255,255,.08);">
                    <span style="font-size:.9rem;font-weight:700;color:#f8fafc;flex:1">🧠 Intelligence Report: ${ev.symbol}</span>
                    <button onclick="document.getElementById('intel-detail-overlay').style.display='none'" style="background:transparent;border:none;color:#94a3b8;font-size:1.3rem;cursor:pointer">✕</button>
                </div>
                <div style="padding:18px 20px">
                    <div class="intel-detail-snapshot">
                        <div>Event: <strong>${EVENT_LABELS[ev.event_type]?.label || ev.event_type}</strong></div>
                        <div>Price: <strong>₨${ev.price ? Number(ev.price).toFixed(2) : '—'}</strong></div>
                        <div>Change: <strong class="${ev.price_change_pct >= 0 ? 'pos' : 'neg'}">${ev.price_change_pct >= 0 ? '+' : ''}${_fmt(ev.price_change_pct)}%</strong></div>
                        <div>RVOL: <strong>${_fmt(ev.rvol)}×</strong></div>
                        <div>RSI: <strong>${_fmt(ev.rsi_at_event, 0)}</strong></div>
                        <div>Sector: <strong>${ev.sector || '—'}</strong></div>
                    </div>
                    ${res.narrative ? `<div class="intel-detail-narrative">${res.narrative}</div>` : ''}
                    <div class="intel-detail-section-title" style="margin:14px 0 8px">🔍 Causal Analysis (${causes.length} factors)</div>
                    <div class="intel-causes-list">${causesHtml || '<p style="color:#64748b">No cause data.</p>'}</div>
                    ${patternHtml}
                </div>
            </div>`;
            overlay.style.display = 'flex';
        } catch (err) {
            console.error('[Intelligence] showEventDetail error:', err);
        }
    }

    function onTabActivated()   { load(); if (_refreshTimer) clearInterval(_refreshTimer); _refreshTimer = setInterval(load, 60000); }
    function onTabDeactivated() { if (_refreshTimer) { clearInterval(_refreshTimer); _refreshTimer = null; } }

    return { load, onTabActivated, onTabDeactivated, showEventDetail };

})();

// ── Wire Intelligence tab into existing view-tab click system ──────────────
document.addEventListener('DOMContentLoaded', () => {
    const intelBtn = document.getElementById('tab-intelligence');
    if (!intelBtn) return;
    intelBtn.addEventListener('click', () => {
        // hide all sections (same pattern as app uses)
        document.querySelectorAll('section[id]').forEach(s => {
            if (s.id.startsWith('view-')) s.style.display = 'none';
        });
        document.querySelectorAll('.view-tab').forEach(b => b.classList.remove('active'));
        intelBtn.classList.add('active');
        const panel = document.getElementById('view-intelligence');
        if (panel) panel.style.display = 'block';
        intelligenceTab.onTabActivated();
    });
    document.querySelectorAll('.view-tab:not(#tab-intelligence)').forEach(btn => {
        btn.addEventListener('click', () => intelligenceTab.onTabDeactivated());
    });
});

// ═══════════════════════════════════════════════════════════════════════════
// 📊  PSX CALIBRATION & LEARNING REPORT — FRONTEND MODULE
// ═══════════════════════════════════════════════════════════════════════════

const calibrationReport = (() => {

    let _loaded = false;

    function _setText(id, val) {
        const el = document.getElementById(id);
        if (el) el.textContent = val;
    }

    function _fmt(num, dec = 1) {
        if (num === null || num === undefined || isNaN(num)) return '—';
        return Number(num).toFixed(dec);
    }

    function _wrColor(wr) {
        if (wr >= 0.65) return 'wr-excellent';
        if (wr >= 0.55) return 'wr-good';
        if (wr >= 0.45) return 'wr-neutral';
        return 'wr-poor';
    }

    function _wColor(w) {
        if (w >= 1.5) return 'weight-high';
        if (w >= 1.1) return 'weight-med';
        if (w >= 0.9) return 'weight-neutral';
        return 'weight-low';
    }

    function _wrBar(wr) {
        const pct = Math.min(100, Math.max(0, wr * 100));
        const cls = _wrColor(wr);
        return `<div class="calib-wr-bar-wrap"><div class="calib-wr-bar ${cls}" style="width:${pct}%"></div></div>`;
    }

    function _timeAgo(iso) {
        if (!iso || iso === 'Never') return '—';
        const diff = Math.floor((Date.now() - new Date(iso)) / 1000);
        if (diff < 60)    return `${diff}s ago`;
        if (diff < 3600)  return `${Math.floor(diff/60)}m ago`;
        if (diff < 86400) return `${Math.floor(diff/3600)}h ago`;
        return `${Math.floor(diff/86400)}d ago`;
    }

    // ── Friendly factor label map ─────────────────────────────────────────
    const FACTOR_LABELS = {
        'GRADE'              : 'Grade',
        'TRIGGER_TYPE'       : 'Trigger Type',
        'TRIGGER_INDIVIDUAL' : 'Trigger Detail',
        'STOP_BASIS'         : 'Stop Method',
        'RR_BUCKET'          : 'R:R Range',
        'RAW_SCORE'          : 'Conviction Score',
        'INTEL_SIGNAL'       : 'Intelligence Signal',
        'SECTOR_INTEL'       : 'Sector (AI)',
        'CAUSAL_FACTOR'      : 'Causal Factor'
    };

    // ── Load Data ─────────────────────────────────────────────────────────
    async function load() {
        try {
            const [reportRes, histRes] = await Promise.allSettled([
                fetch('/api/calibration/report').then(r => r.json()),
                fetch('/api/calibration/history').then(r => r.json())
            ]);

            if (reportRes.status === 'fulfilled' && reportRes.value.success) {
                const data = reportRes.value;
                renderMeta(data.meta, data.performance);
                renderFactorMatrix(data.factor_weights || {});
                renderSectorTable(data.sector_stats || []);
                renderPatternEdge(data.pattern_edge || []);
            }
            if (histRes.status === 'fulfilled' && histRes.value.success) {
                renderChangelog(histRes.value.runs || [], histRes.value.recommendations || []);
            }
            _loaded = true;
        } catch (err) {
            console.error('[CalibReport] Load error:', err);
        }
    }

    // ── Meta / Overview ───────────────────────────────────────────────────
    function renderMeta(meta, perf) {
        if (!meta) return;
        _setText('calib-runs-pill',    (meta.calibration_runs_total ?? 0) + ' runs');
        _setText('calib-changes-pill', (meta.config_changes_applied ?? 0) + ' config changes');
        _setText('calib-last-pill',    'Last: ' + _timeAgo(meta.last_calibration_at));
        _setText('calib-ov-total',     (perf && perf.closed_outcomes != null) ? perf.closed_outcomes : '—');
        _setText('calib-ov-winrate',   (perf && perf.overall_win_rate_pct != null) ? perf.overall_win_rate_pct + '%' : '—');
        _setText('calib-ov-pf',        (perf && perf.profit_factor != null) ? _fmt(perf.profit_factor, 2) + 'x' : '—');
        _setText('calib-ov-factors',   (meta.factor_profiles ?? 0) + '');
    }

    // ── Factor Weight Matrix ──────────────────────────────────────────────
    function renderFactorMatrix(grouped) {
        const c = document.getElementById('calib-factor-matrix');
        if (!c) return;

        // Flatten, prioritise important factor types
        const priority = ['GRADE', 'TRIGGER_TYPE', 'STOP_BASIS', 'RR_BUCKET', 'RAW_SCORE', 'INTEL_SIGNAL', 'CAUSAL_FACTOR'];
        let rows = '';
        for (const ft of priority) {
            const items = (grouped[ft] || []).sort((a, b) => b.weight - a.weight);
            if (!items.length) continue;
            rows += `<div class="calib-factor-group-header">${FACTOR_LABELS[ft] || ft}</div>`;
            for (const fw of items) {
                const wr = fw.smoothed_win_rate || 0;
                const weight = fw.weight || 1.0;
                const samples = fw.sample_count || 0;
                const barPct = Math.min(100, Math.max(0, wr * 100));
                const wCls = _wColor(weight);
                rows += `
                    <div class="calib-factor-row">
                        <div class="calib-factor-name">${fw.factor_value}</div>
                        <div class="calib-factor-samples">${samples}×</div>
                        <div class="calib-factor-bar-wrap">
                            <div class="calib-wr-bar ${_wrColor(wr)}" style="width:${barPct}%"></div>
                        </div>
                        <div class="calib-factor-wr">${Math.round(wr * 100)}%</div>
                        <div class="calib-weight-badge ${wCls}">${weight.toFixed(2)}×</div>
                    </div>`;
            }
        }

        if (!rows) {
            c.innerHTML = `<div class="calib-empty"><div class="intel-spinner"></div><p>No factor data yet. Weights computed after ≥10 closed outcomes.</p></div>`;
        } else {
            c.innerHTML = `<div class="calib-factor-list">${rows}</div>`;
        }
    }

    // ── Sector Stats Table ────────────────────────────────────────────────
    function renderSectorTable(stats) {
        const c = document.getElementById('calib-sector-table');
        if (!c) return;
        if (!stats.length) {
            c.innerHTML = `<div class="calib-empty"><div class="intel-spinner"></div><p>Sector data builds from prediction outcomes.</p></div>`;
            return;
        }
        const rows = stats.map(s => {
            const wr = s.win_rate || 0;
            const wrPct = Math.round(wr * 100);
            const pf = s.profit_factor || 1.0;
            const wrCls = _wrColor(wr);
            return `
                <div class="calib-sector-row">
                    <div class="calib-sector-name">${s.sector}</div>
                    <div class="calib-sector-stats">
                        <span class="calib-sector-wr ${wrCls}">${wrPct}% win</span>
                        <span class="calib-sector-pf">PF ${_fmt(pf, 2)}</span>
                        <span class="calib-sector-samples">${s.sample_count}×</span>
                    </div>
                    ${_wrBar(wr)}
                    <div class="calib-sector-meta">
                        Avg ${_fmt(s.avg_days_to_outcome, 0)}d · +${_fmt(s.avg_winner_gain)}% win / -${_fmt(s.avg_loser_loss)}% loss
                    </div>
                </div>`;
        }).join('');
        c.innerHTML = `<div class="calib-sector-list">${rows}</div>`;
    }

    // ── Pattern Edge Table ────────────────────────────────────────────────
    function renderPatternEdge(patterns) {
        const c = document.getElementById('calib-pattern-edge');
        if (!c) return;
        const shown = patterns.filter(p => p.sample_count >= 1);
        if (!shown.length) {
            c.innerHTML = `<div class="calib-empty"><div class="intel-spinner"></div><p>Pattern edge builds overnight after events are detected.</p></div>`;
            return;
        }
        const rows = shown.map(p => {
            const edge = p.psxEdge || 0;
            const wr = p.raw_win_rate || 0;
            const edgeCls = edge >= 0.15 ? 'edge-positive' : edge >= 0.0 ? 'edge-neutral' : 'edge-negative';
            const edgeSign = edge >= 0 ? '+' : '';
            return `
                <div class="calib-pattern-row">
                    <div class="calib-pattern-name-col">${p.pattern_name}</div>
                    <div class="calib-pattern-stats">
                        <span class="calib-edge-badge ${edgeCls}">${edgeSign}${Math.round(edge * 100)}% edge</span>
                        <span class="calib-pattern-wr">${Math.round(wr * 100)}% win</span>
                        <span class="calib-pattern-samples">${p.sample_count}×</span>
                    </div>
                    ${_wrBar(wr)}
                    <div class="calib-pattern-conf-floor">Min confidence: ${p.recommended_confidence_floor}%</div>
                </div>`;
        }).join('');
        c.innerHTML = `<div class="calib-pattern-list">${rows}</div>`;
    }

    // ── Change Log ────────────────────────────────────────────────────────
    function renderChangelog(runs, recs) {
        const c = document.getElementById('calib-changelog');
        if (!c) return;

        let html = '';

        // Show applied config changes first
        const applied = recs.filter(r => r.applied);
        if (applied.length) {
            html += applied.map(r => `
                <div class="calib-change-entry applied">
                    <div class="calib-change-header">
                        <span class="calib-change-type applied-badge">✅ APPLIED</span>
                        <span class="calib-change-param">${r.param_name}</span>
                        <span class="calib-change-arrow">${r.old_value} → <strong>${r.new_value}</strong></span>
                        <span class="calib-change-time">${_timeAgo(r.recommended_at)}</span>
                    </div>
                    <div class="calib-change-reason">${r.reason}</div>
                    <div class="calib-change-proof">
                        Backtest: PF ${_fmt(r.backtest_pf_before, 2)} → ${_fmt(r.backtest_pf_after, 2)}
                        · ${r.sample_count} samples
                    </div>
                </div>`).join('');
        }

        // Show rejected / not-yet-applied recommendations
        const rejected = recs.filter(r => !r.applied);
        if (rejected.length) {
            html += rejected.map(r => `
                <div class="calib-change-entry rejected">
                    <div class="calib-change-header">
                        <span class="calib-change-type rejected-badge">❌ INSUFFICIENT IMPROVEMENT</span>
                        <span class="calib-change-param">${r.param_name}</span>
                        <span class="calib-change-arrow">${r.old_value} → ${r.new_value}</span>
                        <span class="calib-change-time">${_timeAgo(r.recommended_at)}</span>
                    </div>
                    <div class="calib-change-reason">${r.reason}</div>
                    <div class="calib-change-proof">
                        Backtest: PF ${_fmt(r.backtest_pf_before, 2)} → ${_fmt(r.backtest_pf_after, 2)}
                        (improvement ${_fmt((r.backtest_pf_after - r.backtest_pf_before), 3)} < 0.05 required)
                    </div>
                </div>`).join('');
        }

        // Show calibration run history (observation mode entries)
        if (runs.length) {
            html += `<div class="calib-runs-header">Calibration Runs</div>`;
            html += runs.map(r => `
                <div class="calib-run-entry">
                    <div class="calib-run-header">
                        <span class="calib-run-date">${r.run_at ? r.run_at.replace('T',' ').replace('Z','') + ' UTC' : '—'}</span>
                        <span class="calib-run-samples">${r.closed_samples} closed outcomes</span>
                        <span class="calib-run-wr">Win: ${Math.round((r.overall_win_rate || 0) * 100)}%</span>
                        <span class="calib-run-pf">PF: ${_fmt(r.profit_factor, 2)}</span>
                        <span class="calib-run-changes ${r.changes_applied > 0 ? 'has-changes' : ''}">${r.changes_applied} changes</span>
                    </div>
                    <div class="calib-run-summary">${r.summary || ''}</div>
                </div>`).join('');
        }

        if (!html) {
            c.innerHTML = `<div class="calib-empty"><div class="intel-spinner"></div>
                <p>No calibration data yet. First run every <strong>Sunday at 11 PM PKT</strong>.<br>
                The AI requires ≥10 closed outcomes before making any changes.</p></div>`;
        } else {
            c.innerHTML = html;
        }
    }

    // ── Public API ────────────────────────────────────────────────────────
    return { load };

})();

// ── Load calibration when intelligence tab is activated ────────────────────
(function patchCalibLoad() {
    document.addEventListener('DOMContentLoaded', () => {
        const intelBtn = document.getElementById('tab-intelligence');
        if (intelBtn) {
            intelBtn.addEventListener('click', () => {
                // Load calibration data slightly after main intel data
                setTimeout(() => calibrationReport.load(), 800);
            });
        }
    });
})();

// ═══════════════════════════════════════════════════════════════════════════
// 📈  PSX LONG-TERM INVESTING ENGINE — FRONTEND MODULE
// ═══════════════════════════════════════════════════════════════════════════

const longtermTab = (() => {

    let _currentGrade = 'B+';
    let _currentSector = '';
    let _currentMinDiv = 0;
    let _kse100Only = false;
    let _allRows = [];
    let _loaded = false;

    // ── Helpers ───────────────────────────────────────────────────────────
    const GRADE_ORDER = {'A+': 0, 'A': 1, 'A-': 2, 'B+': 3, 'B': 4, 'C': 5, 'D': 6};
    const GRADE_COLORS = {
        'A+': '#10b981', 'A': '#34d399', 'A-': '#6ee7b7',
        'B+': '#fbbf24', 'B': '#f97316', 'C': '#ef4444', 'D': '#6b7280'
    };

    function _fmt(n, dec=1) {
        if (n === null || n === undefined || isNaN(n)) return '—';
        return Number(n).toFixed(dec);
    }
    function _setText(id, v) { const el = document.getElementById(id); if (el) el.textContent = v; }
    function _timeAgo(iso) {
        if (!iso) return '—';
        const diff = Math.floor((Date.now() - new Date(iso)) / 1000);
        if (diff < 60) return `${diff}s ago`;
        if (diff < 3600) return `${Math.floor(diff/60)}m ago`;
        if (diff < 86400) return `${Math.floor(diff/3600)}h ago`;
        return `${Math.floor(diff/86400)}d ago`;
    }
    function _gradeClass(g) {
        return ({
            'A+': 'ap', 'A': 'a', 'A-': 'am', 'B+': 'bp',
            'B': 'b', 'C': 'c', 'D': 'd'
        })[g] || 'b';
    }
    function _scoreBar(score, max=25, color='#10b981') {
        const pct = Math.min(100, Math.max(0, (score / max) * 100));
        return `<div class="lt-score-bar-wrap"><div class="lt-score-bar" style="width:${pct}%;background:${color}"></div></div>`;
    }

    // ── Macro Banner ─────────────────────────────────────────────────────
    function _renderMacro(macro) {
        if (!macro) return;
        _setText('lt-macro-rate',     `SBP Rate: ${macro.sbp_rate_pct}%`);
        _setText('lt-macro-inflation', `Inflation: ${macro.inflation_pct}% YoY`);
        _setText('lt-macro-rating',    `Moody's ${macro.moodys_rating}${macro.moodys_upgraded ? ' ↑' : ''} · S&P ${macro.sp_rating}${macro.sp_upgraded ? ' ↑' : ''}`);
        _setText('lt-macro-imf',       `IMF: $${macro.imf_disbursed_bn}B of $${macro.imf_total_bn}B · Next $${macro.imf_next_tranche_bn}B pending`);
        _setText('lt-macro-fx',        `PKR/USD: ${macro.fx_usd_pkr}`);
        _setText('lt-macro-kse',       `KSE P/E: ${macro.kse100_pe}x · Yield: ${macro.kse100_div_yield}%`);
        _setText('lt-macro-cgt',       `CGT (ATL filer): ${macro.cgt_filer_pct}% · Div WHT: ${macro.dividend_wht_filer_pct}%`);
        _setText('lt-macro-debt',      `Circular Debt: Rs ${macro.circular_debt_trn_pkr}T ⚠️`);
        const noteEl = document.getElementById('lt-macro-note');
        if (noteEl) {
            noteEl.textContent = macro.iran_war_risk
                ? '⚠️ Iran conflict risk: Pakistan net oil importer — SBP rate, PKR, and market sentiment tied to oil price trajectory.'
                : '';
        }
    }

    // ── Sector Dropdown ───────────────────────────────────────────────────
    function _populateSectors(sectors) {
        const sel = document.getElementById('lt-sector-filter');
        if (!sel) return;
        const existing = Array.from(sel.options).map(o => o.value);
        (sectors || []).forEach(s => {
            if (!existing.includes(s)) {
                const opt = document.createElement('option');
                opt.value = s; opt.textContent = s;
                sel.appendChild(opt);
            }
        });
    }

    // ── Grade Summary Row ─────────────────────────────────────────────────
    function _renderGradeSummary(rows) {
        const counts = {'A+': 0, 'A': 0, 'A-': 0, 'B+': 0};
        rows.forEach(r => { if (r.grade in counts) counts[r.grade]++; });
        _setText('lt-count-ap', counts['A+']);
        _setText('lt-count-a',  counts['A']);
        _setText('lt-count-am', counts['A-']);
        _setText('lt-count-bp', counts['B+']);
    }

    // ── Stock Card ────────────────────────────────────────────────────────
    function _buildCard(r) {
        const gc = _gradeClass(r.grade);
        const gradColor = GRADE_COLORS[r.grade] || '#6b7280';
        const cdFlag = r.circular_debt_risk ? '<span class="lt-risk-tag cd">⚠️ Circular Debt</span>' : '';
        const rbFlag = r.rate_beneficiary   ? '<span class="lt-risk-tag rb">✅ Rate Beneficiary</span>' : '';
        const exFlag = r.exporter           ? '<span class="lt-risk-tag ex">📦 Exporter</span>' : '';

        const s2p = Math.round(r.stage2_score || 0);
        const s3p = Math.round(r.stage3_score || 0);
        const s4p = Math.round(r.stage4_score || 0);
        const s5p = Math.round(r.stage5_score || 0);

        return `
        <div class="lt-card" onclick="longtermTab.showDetail('${r.symbol}')">
            <div class="lt-card-header">
                <div>
                    <div class="lt-card-symbol">${r.symbol}</div>
                    <div class="lt-card-name">${(r.name || '').substring(0, 38)}</div>
                    <div class="lt-card-sector">${r.sector || '—'}</div>
                </div>
                <div class="lt-card-grade-wrap">
                    <div class="lt-grade-badge ${gc}">${r.grade}</div>
                    <div class="lt-card-score">${Math.round(r.total_score)}/100</div>
                </div>
            </div>

            <div class="lt-card-metrics">
                <div class="lt-metric">
                    <div class="lt-metric-val">${r.pe ? _fmt(r.pe, 1) + 'x' : '—'}</div>
                    <div class="lt-metric-label">P/E</div>
                </div>
                <div class="lt-metric">
                    <div class="lt-metric-val ${r.div_yield >= 11 ? 'highlight-green' : ''}">${r.div_yield ? _fmt(r.div_yield, 1) + '%' : '—'}</div>
                    <div class="lt-metric-label">Div Yield</div>
                </div>
                <div class="lt-metric">
                    <div class="lt-metric-val">${r.revenue_cagr !== null && r.revenue_cagr !== undefined ? _fmt(r.revenue_cagr, 1) + '%' : '—'}</div>
                    <div class="lt-metric-label">Rev CAGR</div>
                </div>
                <div class="lt-metric">
                    <div class="lt-metric-val">${r.market_cap ? 'Rs ' + _fmt(r.market_cap / 1e9, 1) + 'B' : '—'}</div>
                    <div class="lt-metric-label">Mkt Cap</div>
                </div>
            </div>

            <div class="lt-card-stages">
                <div class="lt-stage-row">
                    <span class="lt-stage-name">Financial Health</span>
                    ${_scoreBar(s2p, 25, '#60a5fa')}
                    <span class="lt-stage-pts">${s2p}/25</span>
                </div>
                <div class="lt-stage-row">
                    <span class="lt-stage-name">Profitability</span>
                    ${_scoreBar(s3p, 25, '#34d399')}
                    <span class="lt-stage-pts">${s3p}/25</span>
                </div>
                <div class="lt-stage-row">
                    <span class="lt-stage-name">Valuation</span>
                    ${_scoreBar(s4p, 25, '#fbbf24')}
                    <span class="lt-stage-pts">${s4p}/25</span>
                </div>
                <div class="lt-stage-row">
                    <span class="lt-stage-name">Governance/Macro</span>
                    ${_scoreBar(s5p, 25, '#f472b6')}
                    <span class="lt-stage-pts">${s5p}/25</span>
                </div>
            </div>

            <div class="lt-card-flags">${cdFlag}${rbFlag}${exFlag}</div>
            <div class="lt-card-cta">Click for AI analysis ›</div>
        </div>`;
    }

    // ── Render Cards ──────────────────────────────────────────────────────
    function _renderCards(rows) {
        const grid = document.getElementById('lt-cards-grid');
        if (!grid) return;
        _setText('lt-result-count', `${rows.length} result${rows.length !== 1 ? 's' : ''}`);
        if (!rows.length) {
            grid.innerHTML = `<div class="lt-empty"><p>No stocks match your current filters.</p><p>Try relaxing the grade or sector filter.</p></div>`;
            return;
        }
        grid.innerHTML = rows.map(_buildCard).join('');
    }

    // ── Apply Filters ─────────────────────────────────────────────────────
    function _applyFilters() {
        const minScore = {'A+': 80, 'A': 68, 'A-': 55, 'B+': 42, 'B': 28, 'C': 14, 'D': 0}[_currentGrade] || 42;
        let filtered = _allRows.filter(r => r.total_score >= minScore);
        if (_currentSector) filtered = filtered.filter(r => r.sector === _currentSector);
        if (_kse100Only)    filtered = filtered.filter(r => r.is_kse100);
        if (_currentMinDiv > 0) filtered = filtered.filter(r => (r.div_yield || 0) >= _currentMinDiv);
        _renderCards(filtered);
        _renderGradeSummary(filtered);
    }

    // ── Stock Detail Modal ─────────────────────────────────────────────────
    async function showDetail(symbol) {
        const modal = document.getElementById('lt-stock-modal');
        const content = document.getElementById('lt-modal-content');
        if (!modal || !content) return;
        content.innerHTML = `<div class="lt-modal-loading"><div class="intel-spinner"></div><p>Loading analysis for ${symbol}…</p></div>`;
        modal.style.display = 'flex';
        document.body.style.overflow = 'hidden';

        try {
            const res = await fetch(`/api/longterm/stock/${symbol}`);
            const data = await res.json();
            if (!data.success) { content.innerHTML = `<p class="lt-error">${data.error}</p>`; return; }

            const d = data.detail;
            const n = data.narrative || {};
            const gc = _gradeClass(d.grade);

            const s2bd = d.stage2_breakdown || {};
            const s3bd = d.stage3_breakdown || {};
            const s4bd = d.stage4_breakdown || {};
            const s5bd = d.stage5_breakdown || {};

            function _bdRow(label, bd) {
                if (!bd || bd.pts === undefined) return '';
                return `<div class="lt-bd-row">
                    <span class="lt-bd-label">${label}</span>
                    <span class="lt-bd-pts">${bd.pts}/10</span>
                    <span class="lt-bd-note">${bd.note || '—'}</span>
                </div>`;
            }

            const narrativeHtml = n.narrative
                ? `<div class="lt-narrative">${n.narrative.replace(/\n\n/g, '</p><p>').replace(/^/, '<p>').replace(/$/, '</p>')}</div>
                   <div class="lt-verdict-box"><strong>Verdict:</strong> ${n.one_line_verdict || '—'}</div>
                   <div class="lt-model-badge">AI: ${n.model_used || 'template'}</div>`
                : `<div class="lt-narrative-empty">AI analysis will be available after next Monday 9 AM scan.</div>`;

            content.innerHTML = `
                <div class="lt-modal-header">
                    <div>
                        <div class="lt-modal-symbol">${d.symbol}</div>
                        <div class="lt-modal-name">${d.name || ''}</div>
                        <div class="lt-modal-sector">${d.sector || ''}</div>
                    </div>
                    <div class="lt-modal-grade-wrap">
                        <div class="lt-grade-badge ${gc} large">${d.grade}</div>
                        <div class="lt-modal-score">${Math.round(d.total_score || 0)}/100</div>
                    </div>
                </div>

                <div class="lt-modal-metrics">
                    <div class="lt-modal-metric"><div class="lt-metric-val">${d.pe ? _fmt(d.pe,1)+'x' : '—'}</div><div class="lt-metric-label">P/E</div></div>
                    <div class="lt-modal-metric"><div class="lt-metric-val">${d.div_yield ? _fmt(d.div_yield,1)+'%' : '—'}</div><div class="lt-metric-label">Div Yield</div></div>
                    <div class="lt-modal-metric"><div class="lt-metric-val">${d.revenue_cagr !== null ? _fmt(d.revenue_cagr,1)+'%' : '—'}</div><div class="lt-metric-label">Rev CAGR</div></div>
                    <div class="lt-modal-metric"><div class="lt-metric-val">${d.price ? 'Rs '+_fmt(d.price,2) : '—'}</div><div class="lt-metric-label">Price</div></div>
                    <div class="lt-modal-metric"><div class="lt-metric-val">${d.market_cap ? 'Rs '+_fmt(d.market_cap/1e9,1)+'B' : '—'}</div><div class="lt-metric-label">Mkt Cap</div></div>
                </div>

                <div class="lt-modal-stages">
                    <div class="lt-modal-stage">
                        <div class="lt-stage-header"><span>🏦 Financial Health</span><span class="lt-stage-score-big">${Math.round(d.stage2_score||0)}/25</span></div>
                        ${_bdRow('Debt/Equity', s2bd.debt_equity)}
                        ${_bdRow('Current Ratio', s2bd.current_ratio)}
                        ${_bdRow('Revenue Stability', s2bd.revenue_stability)}
                    </div>
                    <div class="lt-modal-stage">
                        <div class="lt-stage-header"><span>📈 Profitability</span><span class="lt-stage-score-big">${Math.round(d.stage3_score||0)}/25</span></div>
                        ${_bdRow('Revenue CAGR', s3bd.revenue_cagr)}
                        ${_bdRow('EPS Growth', s3bd.eps_growth)}
                        ${_bdRow('Net Margin', s3bd.net_margin)}
                        ${_bdRow('Momentum', s3bd.momentum_proxy)}
                    </div>
                    <div class="lt-modal-stage">
                        <div class="lt-stage-header"><span>💰 Valuation</span><span class="lt-stage-score-big">${Math.round(d.stage4_score||0)}/25</span></div>
                        ${_bdRow('P/E vs Sector', s4bd.pe_vs_sector)}
                        ${_bdRow('Div Yield', s4bd.div_yield)}
                        ${_bdRow('Price/Book', s4bd.price_to_book)}
                    </div>
                    <div class="lt-modal-stage">
                        <div class="lt-stage-header"><span>🏛️ Governance & Macro</span><span class="lt-stage-score-big">${Math.round(d.stage5_score||0)}/25</span></div>
                        ${_bdRow('Free Float', s5bd.free_float)}
                        ${_bdRow('Sector/Macro', s5bd.macro_sector)}
                        ${_bdRow('Rate Sensitivity', s5bd.rate_sensitivity)}
                        ${_bdRow('Sponsor/Governance', s5bd.sponsor_governance)}
                    </div>
                </div>

                <div class="lt-modal-narrative-section">
                    <h4 class="lt-narrative-title">🤖 AI Investment Analysis</h4>
                    ${narrativeHtml}
                </div>`;
        } catch(err) {
            content.innerHTML = `<p class="lt-error">Failed to load: ${err.message}</p>`;
        }
    }

    function closeModal(e) {
        if (e && e.target !== document.getElementById('lt-stock-modal')) return;
        const modal = document.getElementById('lt-stock-modal');
        if (modal) modal.style.display = 'none';
        document.body.style.overflow = '';
    }

    // ── Main Load ─────────────────────────────────────────────────────────
    async function load() {
        const grid = document.getElementById('lt-cards-grid');
        if (grid && !_loaded) {
            grid.innerHTML = `<div class="lt-loading"><div class="intel-spinner"></div>
                <p>Running 7-stage pipeline on 410 eligible PSX stocks…</p>
                <p class="lt-loading-note">This takes ~20 seconds on first load. Results persist for the week.</p></div>`;
        }

        try {
            // Load macro context and shortlist in parallel
            const [macroRes, shortRes] = await Promise.all([
                fetch('/api/longterm/macro-context').then(r => r.json()),
                fetch(`/api/longterm/shortlist?grade=${encodeURIComponent(_currentGrade)}&sector=${encodeURIComponent(_currentSector)}&kse100=${_kse100Only?1:0}&min_div=${_currentMinDiv}`)
                    .then(r => r.json())
            ]);

            if (macroRes.success) {
                _renderMacro(macroRes.macro);
                _populateSectors(macroRes.sectors || []);
            }

            if (shortRes.success) {
                _allRows = shortRes.shortlist || [];
                const lastRun = shortRes.last_run || {};
                _setText('lt-last-scan', `Last scan: ${_timeAgo(lastRun.triggered_at)}`);
                _setText('lt-shortlist-count', `${_allRows.length} stocks in shortlist`);
                _applyFilters();
                _loaded = true;
            } else {
                if (grid) grid.innerHTML = `<div class="lt-error"><p>⚠️ ${shortRes.error || 'Load failed'}</p></div>`;
            }
        } catch(err) {
            if (grid) grid.innerHTML = `<div class="lt-error"><p>Connection error: ${err.message}</p></div>`;
        }
    }

    // ── Wire up filter controls ───────────────────────────────────────────
    function _initControls() {
        // Grade pills
        document.querySelectorAll('#lt-grade-filter .lt-grade-pill').forEach(btn => {
            btn.addEventListener('click', () => {
                document.querySelectorAll('#lt-grade-filter .lt-grade-pill').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                _currentGrade = btn.dataset.grade;
                _applyFilters();
            });
        });
        // Sector select
        const secSel = document.getElementById('lt-sector-filter');
        if (secSel) secSel.addEventListener('change', () => { _currentSector = secSel.value; _applyFilters(); });
        // Div yield
        const divSel = document.getElementById('lt-div-filter');
        if (divSel) divSel.addEventListener('change', () => { _currentMinDiv = parseFloat(divSel.value) || 0; _applyFilters(); });
        // KSE-100 toggle
        const kseChk = document.getElementById('lt-kse100-filter');
        if (kseChk) kseChk.addEventListener('change', () => { _kse100Only = kseChk.checked; _applyFilters(); });
        // Grade summary click-to-filter
        document.querySelectorAll('.lt-grade-card').forEach(card => {
            card.addEventListener('click', () => {
                const g = card.dataset.grade;
                document.querySelectorAll('#lt-grade-filter .lt-grade-pill').forEach(b => {
                    b.classList.toggle('active', b.dataset.grade === g);
                });
                _currentGrade = g;
                _applyFilters();
            });
        });
    }

    // ── Symbol Search & Institutional Deep-Dive ───────────────────────────
    function _initSearch() {
        const input = document.getElementById('lt-symbol-search-input');
        const dropdown = document.getElementById('lt-symbol-autocomplete');
        const clearBtn = document.getElementById('lt-search-clear-btn');
        const submitBtn = document.getElementById('lt-search-submit-btn');

        if (!input) return;

        let selectedIndex = -1;

        function getStockList() {
            if (typeof STOCKS !== 'undefined' && STOCKS.length) return STOCKS;
            return _allRows.map(r => ({ symbol: r.symbol, name: r.name, sector: r.sector, price: r.price }));
        }

        function closeDropdown() {
            if (dropdown) {
                dropdown.style.display = 'none';
                dropdown.innerHTML = '';
            }
            selectedIndex = -1;
        }

        input.addEventListener('input', () => {
            const q = input.value.trim().toUpperCase();
            if (clearBtn) clearBtn.style.display = q.length ? 'flex' : 'none';

            if (!q) {
                closeDropdown();
                return;
            }

            const list = getStockList();
            const matches = list.filter(s =>
                s.symbol.toUpperCase().includes(q) || (s.name && s.name.toUpperCase().includes(q))
            ).slice(0, 8);

            if (!matches.length || !dropdown) {
                closeDropdown();
                return;
            }

            dropdown.innerHTML = matches.map((m, idx) => `
                <div class="lt-autocomplete-item ${idx === selectedIndex ? 'selected' : ''}" data-symbol="${m.symbol}">
                    <div class="lt-ac-left">
                        <span class="lt-ac-sym">${m.symbol}</span>
                        <span class="lt-ac-name">${m.name || m.symbol}</span>
                    </div>
                    <div class="lt-ac-right">
                        <span class="lt-ac-sector">${m.sector || 'General'}</span>
                        <span class="lt-ac-price">₨${_fmt(m.price, 2)}</span>
                    </div>
                </div>
            `).join('');
            dropdown.style.display = 'block';

            dropdown.querySelectorAll('.lt-autocomplete-item').forEach(item => {
                item.addEventListener('click', () => {
                    const sym = item.dataset.symbol;
                    input.value = sym;
                    closeDropdown();
                    runDeepDive(sym);
                });
            });
        });

        input.addEventListener('keydown', (e) => {
            if (!dropdown || dropdown.style.display === 'none') {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    const val = input.value.trim().toUpperCase();
                    if (val) runDeepDive(val);
                }
                return;
            }

            const items = dropdown.querySelectorAll('.lt-autocomplete-item');
            if (!items.length) return;

            if (e.key === 'ArrowDown') {
                e.preventDefault();
                selectedIndex = (selectedIndex + 1) % items.length;
                items.forEach((it, i) => it.classList.toggle('selected', i === selectedIndex));
                items[selectedIndex].scrollIntoView({ block: 'nearest' });
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                selectedIndex = (selectedIndex - 1 + items.length) % items.length;
                items.forEach((it, i) => it.classList.toggle('selected', i === selectedIndex));
                items[selectedIndex].scrollIntoView({ block: 'nearest' });
            } else if (e.key === 'Enter') {
                e.preventDefault();
                if (selectedIndex >= 0 && items[selectedIndex]) {
                    const sym = items[selectedIndex].dataset.symbol;
                    input.value = sym;
                    closeDropdown();
                    runDeepDive(sym);
                } else {
                    const val = input.value.trim().toUpperCase();
                    closeDropdown();
                    if (val) runDeepDive(val);
                }
            } else if (e.key === 'Escape') {
                closeDropdown();
            }
        });

        if (clearBtn) {
            clearBtn.addEventListener('click', () => {
                input.value = '';
                clearBtn.style.display = 'none';
                closeDropdown();
                input.focus();
            });
        }

        if (submitBtn) {
            submitBtn.addEventListener('click', () => {
                const val = input.value.trim().toUpperCase();
                closeDropdown();
                if (val) runDeepDive(val);
            });
        }

        // Quick chips
        document.querySelectorAll('.lt-quick-chip').forEach(chip => {
            chip.addEventListener('click', () => {
                const sym = chip.dataset.symbol;
                if (input) input.value = sym;
                if (clearBtn) clearBtn.style.display = 'flex';
                closeDropdown();
                runDeepDive(sym);
            });
        });

        // Close dropdown on outside click
        document.addEventListener('click', (e) => {
            if (!e.target.closest('.lt-search-input-wrap')) {
                closeDropdown();
            }
        });
    }

    // ── Deep-Dive Execution & Rendering ───────────────────────────────────
    async function runDeepDive(symbol, force = false) {
        if (!symbol) return;
        symbol = symbol.trim().toUpperCase();

        const loading = document.getElementById('lt-deepdive-loading');
        const loadSym = document.getElementById('lt-loading-symbol');
        const resultContainer = document.getElementById('lt-deepdive-result');

        if (loadSym) loadSym.textContent = symbol;
        if (loading) loading.style.display = 'block';
        if (resultContainer) resultContainer.innerHTML = '';

        const section = document.getElementById('lt-deepdive-section');
        if (section) section.scrollIntoView({ behavior: 'smooth', block: 'start' });

        try {
            const url = `/api/longterm/deep-dive/${encodeURIComponent(symbol)}${force ? '?force=true' : ''}`;
            const res = await fetch(url);
            const json = await res.json();

            if (loading) loading.style.display = 'none';

            if (!json.success) {
                if (resultContainer) {
                    resultContainer.innerHTML = `
                        <div class="lt-dd-card" style="border-color:rgba(239,68,68,0.4);">
                            <p style="color:#f87171;font-weight:700;margin:0;">⚠️ Analysis failed: ${json.error || 'Unknown error'}</p>
                        </div>
                    `;
                }
                return;
            }

            _renderDeepDive(json.deep_dive);
        } catch (err) {
            if (loading) loading.style.display = 'none';
            if (resultContainer) {
                resultContainer.innerHTML = `
                    <div class="lt-dd-card" style="border-color:rgba(239,68,68,0.4);">
                        <p style="color:#f87171;font-weight:700;margin:0;">⚠️ Connection error: ${err.message}</p>
                    </div>
                `;
            }
        }
    }

    function _renderDeepDive(d) {
        const container = document.getElementById('lt-deepdive-result');
        if (!container || !d) return;

        const gc = _gradeClass(d.confidence_grade);
        const gradeColor = GRADE_COLORS[d.confidence_grade] || '#34d399';

        // Verdict class
        let vClass = 'verdict-hold';
        let vIcon = '⏸';
        let vLabel = d.verdict ? d.verdict.replace(/_/g, ' ') : 'HOLD';
        if (d.verdict === 'BUY') {
            vClass = 'verdict-buy'; vIcon = '▲';
        } else if (d.verdict === 'ACCUMULATE_ON_DIPS') {
            vClass = 'verdict-accumulate'; vIcon = '◆'; vLabel = 'ACCUMULATE ON DIPS';
        } else if (d.verdict === 'AVOID') {
            vClass = 'verdict-avoid'; vIcon = '▼';
        }

        // Layer tag class helper
        function _layerTagClass(layer) {
            const l = (layer || '').toLowerCase();
            if (l.includes('tech')) return 'tag-tech';
            if (l.includes('fund')) return 'tag-fund';
            if (l.includes('corp') || l.includes('macro')) return 'tag-corp';
            return 'tag-risk';
        }

        // Bull Case HTML
        const bullList = (d.bull_case || []).map((b, i) => `
            <div class="lt-dd-item">
                <span class="lt-dd-layer-tag ${_layerTagClass(b.layer)}">${b.layer || 'Fundamental'}</span>
                <div><strong>${i + 1}.</strong> ${b.point}</div>
            </div>
        `).join('');

        // Bear Case HTML
        const bearList = (d.bear_case || []).map((b, i) => `
            <div class="lt-dd-item">
                <span class="lt-dd-layer-tag ${_layerTagClass(b.layer)}">${b.layer || 'Risk'}</span>
                <div><strong>${i + 1}.</strong> ${b.point}</div>
            </div>
        `).join('');

        // Ranked Threats HTML
        const threatsList = (d.ranked_risks || []).map(t => {
            const sev = (t.severity || 'MEDIUM').toLowerCase();
            const sevClass = sev === 'high' ? 'sev-high' : sev === 'low' ? 'sev-low' : 'sev-medium';
            return `
                <div class="lt-dd-threat-item">
                    <div class="lt-dd-threat-header">
                        <span class="lt-dd-threat-name">${t.rank || 1}. ${t.title}</span>
                        <span class="lt-dd-severity-pill ${sevClass}">${t.severity}</span>
                    </div>
                    <div class="lt-dd-threat-desc">${t.description}</div>
                </div>
            `;
        }).join('');

        // 4-Layer Evidence Breakdown
        const ev = d.evidence || {};
        const tech = ev.technical || {};
        const fund = ev.fundamental || {};
        const corp = ev.corporate_macro || {};
        const risk = ev.risk || {};

        container.innerHTML = `
            <div class="lt-dd-card">
                <!-- Header -->
                <div class="lt-dd-top">
                    <div>
                        <div class="lt-dd-sym-row">
                            <span class="lt-dd-sym">${d.symbol}</span>
                            <span class="lt-dd-sector-badge">${d.sector}</span>
                        </div>
                        <div class="lt-dd-name">${d.name} · <strong style="color:#f1f5f9;font-family:var(--font-mono);">₨${_fmt(tech.price || d.raw_metrics?.price, 2)}</strong></div>
                    </div>
                    <div class="lt-dd-top-actions">
                        <span class="lt-dd-timestamp">Analyzed: ${_timeAgo(d.analyzed_at)}</span>
                        ${d.is_stale ? `<span class="lt-dd-stale-badge">⚠️ 7d+ Stale</span>` : ''}
                        <button class="lt-dd-rerun-btn" onclick="longtermTab.runDeepDive('${d.symbol}', true)">
                            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M23 4v6h-6M1 20v-6h6"/><path d="M20.49 9A9 9 0 005.64 5.64L1 10m22 4l-4.64 4.36A9 9 0 013.51 15"/></svg>
                            <span>Re-run Analysis</span>
                        </button>
                    </div>
                </div>

                <!-- Hero Verdict Banner -->
                <div class="lt-dd-hero-banner">
                    <div class="lt-dd-hero-block">
                        <span class="lt-dd-hero-label">Institutional Verdict</span>
                        <div class="lt-dd-verdict-pill ${vClass}">
                            <span>${vIcon}</span>
                            <span>${vLabel}</span>
                        </div>
                    </div>
                    <div class="lt-dd-hero-block">
                        <span class="lt-dd-hero-label">Suggested Holding Horizon</span>
                        <div class="lt-dd-horizon-text">⏱ ${d.holding_horizon}</div>
                    </div>
                    <div class="lt-dd-hero-block">
                        <span class="lt-dd-hero-label">Unified Confidence Grade</span>
                        <div class="lt-dd-grade-wrap">
                            <div class="lt-dd-grade-badge ${gc}" style="color:${gradeColor};">${d.confidence_grade}</div>
                            <div class="lt-dd-score-val">Score: <span>${d.composite_score}</span>/100</div>
                        </div>
                    </div>
                </div>

                <!-- Dual Column Bull & Bear Grid -->
                <div class="lt-dd-dual-grid">
                    <!-- Bull Case -->
                    <div class="lt-dd-case-card bull">
                        <div class="lt-dd-case-header">
                            <span class="lt-dd-case-title">
                                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/></svg>
                                Bull Case (Why choose this)
                            </span>
                            <span class="lt-dd-case-count">${d.bull_case?.length || 0} Factors</span>
                        </div>
                        <div class="lt-dd-items-list">
                            ${bullList}
                        </div>
                    </div>

                    <!-- Bear Case -->
                    <div class="lt-dd-case-card bear">
                        <div class="lt-dd-case-header">
                            <span class="lt-dd-case-title">
                                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="23 18 13.5 8.5 8.5 13.5 1 6"/><polyline points="17 18 23 18 23 12"/></svg>
                                Bear Case (Counter-signals & Cautions)
                            </span>
                            <span class="lt-dd-case-count">${d.bear_case?.length || 0} Watch-Items</span>
                        </div>
                        <div class="lt-dd-items-list">
                            ${bearList}
                        </div>
                    </div>
                </div>

                <!-- Model Reconciliation & Flip Triggers -->
                <div class="lt-dd-reconciliation">
                    <div class="lt-dd-rec-title">
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/></svg>
                        Model Synthesis & Flip Triggers
                    </div>
                    <div class="lt-dd-rec-body">
                        ${d.reconciliation}
                    </div>
                </div>

                <!-- Ranked Key Threats -->
                <div class="lt-dd-threats-card">
                    <div class="lt-dd-threats-title">
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
                        Ranked Key Threats & Risks
                    </div>
                    <div class="lt-dd-threats-list">
                        ${threatsList}
                    </div>
                </div>

                <!-- 4-Layer Evidence Breakdown -->
                <div class="lt-dd-evidence-card">
                    <div class="lt-dd-evidence-title">
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="9" y1="21" x2="9" y2="9"/></svg>
                        4-Layer Evidence Matrix
                    </div>
                    <div class="lt-dd-evidence-grid">
                        <!-- Layer 1: Technical -->
                        <div class="lt-dd-layer-col">
                            <h4>📈 Technical Layer</h4>
                            <div class="lt-dd-metric-row"><span class="lt-dd-metric-k">Trend</span><span class="lt-dd-metric-v" style="color:#38bdf8;">${tech.trend_status || '—'}</span></div>
                            <div class="lt-dd-metric-row"><span class="lt-dd-metric-k">4H MACD</span><span class="lt-dd-metric-v" style="color:${tech.macd_bullish ? '#34d399' : '#f87171'};">${tech.macd_bullish ? 'Bullish ▲' : 'Bearish ▼'}</span></div>
                            <div class="lt-dd-metric-row"><span class="lt-dd-metric-k">RSI (14)</span><span class="lt-dd-metric-v">${_fmt(tech.rsi, 1)}</span></div>
                            <div class="lt-dd-metric-row"><span class="lt-dd-metric-k">50-day EMA</span><span class="lt-dd-metric-v">₨${_fmt(tech.ema50, 2)}</span></div>
                            <div class="lt-dd-metric-row"><span class="lt-dd-metric-k">200-day EMA</span><span class="lt-dd-metric-v">₨${_fmt(tech.ema200, 2)}</span></div>
                            <div class="lt-dd-metric-row"><span class="lt-dd-metric-k">Support S1</span><span class="lt-dd-metric-v">₨${_fmt(tech.support_s1, 2)}</span></div>
                            <div class="lt-dd-metric-row"><span class="lt-dd-metric-k">Resistance R1</span><span class="lt-dd-metric-v">₨${_fmt(tech.resistance_r1, 2)}</span></div>
                        </div>

                        <!-- Layer 2: Fundamental -->
                        <div class="lt-dd-layer-col">
                            <h4>🏛 Fundamental Layer</h4>
                            <div class="lt-dd-metric-row"><span class="lt-dd-metric-k">Trailing P/E</span><span class="lt-dd-metric-v">${_fmt(fund.pe_ratio, 1)}x</span></div>
                            <div class="lt-dd-metric-row"><span class="lt-dd-metric-k">Dividend Yield</span><span class="lt-dd-metric-v" style="color:#34d399;">${_fmt(fund.div_yield, 1)}%</span></div>
                            <div class="lt-dd-metric-row"><span class="lt-dd-metric-k">Net Margin</span><span class="lt-dd-metric-v">${fund.net_profit_margin ? _fmt(fund.net_profit_margin > 1 ? fund.net_profit_margin : fund.net_profit_margin * 100, 1) + '%' : '—'}</span></div>
                            <div class="lt-dd-metric-row"><span class="lt-dd-metric-k">Debt / Equity</span><span class="lt-dd-metric-v">${_fmt(fund.debt_equity_ratio, 2)}x</span></div>
                            <div class="lt-dd-metric-row"><span class="lt-dd-metric-k">Current Ratio</span><span class="lt-dd-metric-v">${_fmt(fund.current_ratio, 2)}</span></div>
                            <div class="lt-dd-metric-row"><span class="lt-dd-metric-k">Book Value</span><span class="lt-dd-metric-v">₨${_fmt(fund.book_value_ps, 1)}</span></div>
                            <div class="lt-dd-metric-row"><span class="lt-dd-metric-k">Latest EPS</span><span class="lt-dd-metric-v">₨${_fmt(fund.eps_latest, 2)}</span></div>
                        </div>

                        <!-- Layer 3: Corporate & Macro -->
                        <div class="lt-dd-layer-col">
                            <h4>🇵🇰 Corporate & Macro</h4>
                            <div class="lt-dd-metric-row"><span class="lt-dd-metric-k">Sponsor Holding</span><span class="lt-dd-metric-v">${_fmt(corp.sponsor_holding_pct, 1)}%</span></div>
                            <div class="lt-dd-metric-row"><span class="lt-dd-metric-k">Div Consistency</span><span class="lt-dd-metric-v">${corp.dividend_years_paid || 0}/3 Yrs</span></div>
                            <div class="lt-dd-metric-row"><span class="lt-dd-metric-k">Circular Debt</span><span class="lt-dd-metric-v" style="color:${corp.is_circular_debt ? '#f87171' : '#34d399'};">${corp.is_circular_debt ? 'Exposed ⚠️' : 'None'}</span></div>
                            <div class="lt-dd-metric-row"><span class="lt-dd-metric-k">Rate Beneficiary</span><span class="lt-dd-metric-v">${corp.is_rate_beneficiary ? 'Yes (NIM+)' : 'Standard'}</span></div>
                            <div class="lt-dd-metric-row"><span class="lt-dd-metric-k">Exporter FX</span><span class="lt-dd-metric-v">${corp.is_exporter ? 'Yes ($ Hedged)' : 'Domestic'}</span></div>
                            <div class="lt-dd-metric-row"><span class="lt-dd-metric-k">KSE-100 Index</span><span class="lt-dd-metric-v">${corp.is_kse100 ? 'Constituent' : 'All-Share'}</span></div>
                        </div>

                        <!-- Layer 4: Risk & Liquidity -->
                        <div class="lt-dd-layer-col">
                            <h4>🛡 Risk & Liquidity</h4>
                            <div class="lt-dd-metric-row"><span class="lt-dd-metric-k">Liquidity Tier</span><span class="lt-dd-metric-v" style="font-size:0.7rem;">${risk.liquidity_tier || 'Moderate'}</span></div>
                            <div class="lt-dd-metric-row"><span class="lt-dd-metric-k">Daily Value</span><span class="lt-dd-metric-v">₨${_fmt(risk.daily_traded_val_m_pkr, 1)}M</span></div>
                            <div class="lt-dd-metric-row"><span class="lt-dd-metric-k">Free Float</span><span class="lt-dd-metric-v">₨${_fmt(risk.free_float_m_pkr, 0)}M</span></div>
                            <div class="lt-dd-metric-row"><span class="lt-dd-metric-k">Solvency</span><span class="lt-dd-metric-v" style="font-size:0.7rem;">${risk.solvency_risk || 'Normal'}</span></div>
                            <div class="lt-dd-metric-row"><span class="lt-dd-metric-k">Macro Volatility</span><span class="lt-dd-metric-v">SBP 11.5% Base</span></div>
                            <div class="lt-dd-metric-row"><span class="lt-dd-metric-k">Tax / CGT Rate</span><span class="lt-dd-metric-v">15% Filer</span></div>
                        </div>
                    </div>
                </div>

                <!-- Compliance Disclaimer -->
                <div class="lt-dd-disclaimer">
                    ⚠️ <strong>Compliance Notice:</strong> This analysis is synthesized algorithmically across multi-stage technical and fundamental datasets for research and educational purposes only. It does not constitute investment advice, financial planning, or a regulated solicitation. Always conduct independent due diligence before allocating capital.
                </div>
            </div>
        `;
    }

    // ── Safe initialization ──────────────────────────────────────────────
    function _init() {
        _initControls();
        _initSearch();
        const ltBtn = document.getElementById('tab-longterm');
        if (ltBtn) {
            ltBtn.addEventListener('click', () => {
                if (!_loaded) load();
            });
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', _init);
    } else {
        _init();
    }

    window.longtermTab = { load, showDetail, closeModal, runDeepDive };
    return window.longtermTab;

})();


