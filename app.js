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
async function fetchLiveData() {
    if (isLoading) return;
    isLoading = true;
    
    showLoading(true);
    setRefreshBtnSpinning(true);

    try {
        // Fetch stocks and indices in parallel
        const [stockRes, indexRes] = await Promise.all([
            fetch('/api/stocks'),
            fetch('/api/indices'),
        ]);

        const stockData = await stockRes.json();
        const indexData = await indexRes.json();

        if (stockData.success && stockData.data) {
            STOCKS = stockData.data;
            updateSectorFilter();
            updateMarketOverview(indexData, stockData);
            updateLastUpdated(stockData.fetchedAt);
            renderAll();

            // Show stale data banner if serving cached data
            if (stockData.stale) {
                showStaleBanner(stockData.fetchedAt);
            } else {
                hideStaleBanner();
            }
        } else {
            showError('Failed to load stock data. Please try again.');
        }
    } catch (error) {
        console.error('Error fetching data:', error);
        showError('Connection error. Make sure the server is running.');
    } finally {
        isLoading = false;
        showLoading(false);
        setRefreshBtnSpinning(false);
    }
}

function showLoading(show) {
    const overlay = document.getElementById('loading-overlay');
    if (overlay) {
        overlay.style.display = show ? 'flex' : 'none';
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

function renderScorecard(stocks) {
    const grid = document.getElementById("scorecard-grid");
    const sorted = [...stocks].sort((a, b) => b.score - a.score);

    if (sorted.length === 0) {
        grid.innerHTML = `<p style="text-align:center; padding:48px; color:var(--text-tertiary); grid-column:1/-1;">No stocks match your criteria.</p>`;
        return;
    }

    grid.innerHTML = sorted.slice(0, 50).map(stock => {
        const breakdown = getScoreBreakdown(stock);
        const scoreClass = getScoreClass(stock.score);
        const scoreColor = scoreClass === "excellent" ? "var(--gradient-score-excellent)" :
                          scoreClass === "good" ? "var(--gradient-score-good)" :
                          scoreClass === "average" ? "var(--gradient-score-average)" : "var(--gradient-score-weak)";

        const barColor = scoreClass === "excellent" ? "var(--accent-emerald)" :
                        scoreClass === "good" ? "var(--accent-cyan)" :
                        scoreClass === "average" ? "var(--accent-amber)" : "var(--accent-rose)";

        return `
        <div class="scorecard-item" data-symbol="${stock.symbol}">
            <div class="scorecard-top">
                <div>
                    <div class="scorecard-symbol">${stock.symbol}</div>
                    <div style="font-size:0.78rem;color:var(--text-tertiary)">${stock.name}</div>
                </div>
                <div class="scorecard-total" style="background:${scoreColor};-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text">${stock.score}</div>
            </div>
            <div class="scorecard-bars">
                <div class="score-row">
                    <span class="score-row-label">Valuation</span>
                    <div class="score-row-bar"><div class="score-row-fill" style="width:${(breakdown.pe / 25) * 100}%;background:${barColor}"></div></div>
                    <span class="score-row-value">${breakdown.pe}</span>
                </div>
                <div class="score-row">
                    <span class="score-row-label">Dividend</span>
                    <div class="score-row-bar"><div class="score-row-fill" style="width:${(breakdown.dividend / 25) * 100}%;background:${barColor}"></div></div>
                    <span class="score-row-value">${breakdown.dividend}</span>
                </div>
                <div class="score-row">
                    <span class="score-row-label">Growth</span>
                    <div class="score-row-bar"><div class="score-row-fill" style="width:${(breakdown.growth / 25) * 100}%;background:${barColor}"></div></div>
                    <span class="score-row-value">${breakdown.growth}</span>
                </div>
                <div class="score-row">
                    <span class="score-row-label">Liquidity</span>
                    <div class="score-row-bar"><div class="score-row-fill" style="width:${(breakdown.liquidity / 25) * 100}%;background:${barColor}"></div></div>
                    <span class="score-row-value">${breakdown.liquidity}</span>
                </div>
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
    renderScorecard(sorted);
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
                <div class="scorecard-bars" style="margin-top:16px">
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

// ─── View Switching ───
function switchView(view) {
    currentView = view;
    document.querySelectorAll(".view-tab").forEach(t => t.classList.remove("active"));
    document.querySelectorAll(".mobile-nav-item").forEach(t => t.classList.remove("active"));

    const activeTab = document.querySelector(`[data-view="${view}"]`);
    if (activeTab) activeTab.classList.add("active");

    const activeMobileTab = document.querySelector(`.mobile-nav-item[data-view="${view}"]`);
    if (activeMobileTab) activeMobileTab.classList.add("active");

    const views = ["table", "cards", "scorecard", "live-trading", "education", "simulator", "corporate", "financials"];
    views.forEach(v => {
        const el = document.getElementById(`view-${v}`);
        if (el) el.style.display = (view === v) ? (v === "cards" ? "grid" : "block") : "none";
    });

    // Auto collapse mobile search & options overlay when changing tabs
    document.getElementById("search-container")?.classList.remove("mobile-active");
    document.getElementById("screener-controls")?.classList.remove("expanded");

    // Show screener filters & market overview cards ONLY on main Dashboard screener views (table, cards, scorecard)
    const isScreenerView = ["table", "cards", "scorecard"].includes(view);
    const marketOverview = document.getElementById("market-overview");
    const screenerControls = document.getElementById("screener-controls");
    const searchContainer = document.getElementById("search-container");

    if (marketOverview) marketOverview.style.display = isScreenerView ? "grid" : "none";
    if (screenerControls) screenerControls.style.display = isScreenerView ? "block" : "none";
    if (searchContainer) searchContainer.style.display = isScreenerView ? "flex" : "none";

    // View specific initializations
    if (view === "live-trading") {
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

    // Refresh button
    document.getElementById("btn-refresh").addEventListener("click", async () => {
        // Clear server cache first
        try { await fetch('/api/refresh', { method: 'POST' }); } catch(e) {}
        fetchLiveData();
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
        const row = e.target.closest("tr[data-symbol], .stock-card[data-symbol], .scorecard-item[data-symbol]");
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
    document.getElementById("btn-upper-lock").addEventListener("click", openUpperLockAnalysis);
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

function openUpperLockAnalysis() {
    const modal = document.getElementById("upper-lock-modal");
    modal.classList.add("active");
    document.body.style.overflow = "hidden";

    // Show loading, hide results
    document.getElementById("upper-lock-loading").style.display = "flex";
    document.getElementById("upper-lock-results").style.display = "none";
    document.getElementById("upper-lock-sort").value = "probability";

    // Fetch analysis
    fetch("/api/upper-lock-analysis")
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                upperLockData = data;
                renderUpperLockResults(data, "probability");
            } else {
                showUpperLockError(data.error || "Failed to analyze stocks");
            }
        })
        .catch(err => {
            showUpperLockError("Network error: " + err.message);
        });
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
    const symbol = input.value.trim().toUpperCase();
    if (!symbol) return;
    
    const resultsDiv = document.getElementById('history-results');
    const loadingDiv = document.getElementById('history-loading');
    
    // Show loading
    loadingDiv.style.display = 'flex';
    resultsDiv.style.display = 'none';
    
    fetch(`/api/stock-history/${symbol}`)
        .then(r => r.json())
        .then(data => {
            loadingDiv.style.display = 'none';
            resultsDiv.style.display = 'block';
            if (data.success) {
                renderStockHistory(data);
            } else {
                resultsDiv.innerHTML = `<div class="upper-lock-empty">
                    <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                        <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>
                    </svg>
                    <p>${data.error || 'Stock not found'}</p>
                    <p style="font-size: 0.8rem; color: var(--text-tertiary)">Try symbols like OGDC, HBL, ENGRO, UNITY, LUCK</p>
                </div>`;
            }
        })
        .catch(err => {
            loadingDiv.style.display = 'none';
            resultsDiv.style.display = 'block';
            resultsDiv.innerHTML = `<div class="upper-lock-empty"><p>Network error: ${err.message}</p></div>`;
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

    // Show loading
    const loading = document.getElementById("live-trading-loading");
    const container = document.getElementById("live-trading-content");
    if (loading) loading.style.display = "flex";

    // Highlight active quick chip if matches
    document.querySelectorAll(".quick-chip").forEach(chip => {
        if (chip.dataset.symbol === currentLiveSymbol) chip.classList.add("active");
        else chip.classList.remove("active");
    });

    fetch(`/api/live-trading?symbol=${currentLiveSymbol}`)
        .then(r => r.json())
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
            if (container) {
                container.innerHTML = `<div class="upper-lock-empty"><p>Network Error: ${err.message}</p></div>`;
            }
        });
}

function setupLiveAutoRefresh(isOpen) {
    if (liveTradingTimer) clearInterval(liveTradingTimer);
    // Refresh every 10s if market OPEN, every 60s if CLOSED
    const intervalMs = isOpen ? 10000 : 60000;
    liveTradingTimer = setInterval(() => {
        if (currentView === "live-trading" && currentLiveSymbol) {
            fetch(`/api/live-trading?symbol=${currentLiveSymbol}`)
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

// ── Technical Indicator Calculations ──
function calcTechnicalRSI(closes, period = 14) {
    if (!closes || closes.length < period + 1) return 50.0;
    let gains = 0, losses = 0;
    for (let i = 0; i < period; i++) {
        const diff = closes[i] - closes[i + 1];
        if (diff >= 0) gains += diff;
        else losses -= diff;
    }
    let avgGain = gains / period;
    let avgLoss = losses / period;
    if (avgLoss === 0) return 100.0;
    const rs = avgGain / avgLoss;
    return 100 - (100 / (1 + rs));
}

function calcTechnicalMACD(closes) {
    if (!closes || closes.length < 26) return { macd: 0, signal: 0, histogram: 0 };
    const ema = (arr, p) => {
        const k = 2 / (p + 1);
        let val = arr.slice(-p).reduce((a, b) => a + b, 0) / p;
        for (let i = arr.length - p; i < arr.length; i++) {
            val = (arr[i] * k) + (val * (1 - k));
        }
        return val;
    };
    const ema12 = ema(closes, 12);
    const ema26 = ema(closes, 26);
    const macdLine = ema12 - ema26;
    const signalLine = macdLine * 0.8; // Approximation
    return {
        macd: macdLine,
        signal: signalLine,
        histogram: macdLine - signalLine
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

    const params = new URLSearchParams({
        symbol,
        buyPrice: pos.avgPrice,
        qty: pos.shares,
        purchaseDate: pos.purchaseDate || ""
    });

    fetch(`/api/position-analysis?${params}`)
        .then(r => r.json())
        .then(res => {
            if (res.success && res.data) {
                renderPositionAnalysisModal(res.data, pos);
            } else {
                modal.innerHTML = `<div class="pos-modal-box" style="text-align:center;padding:40px;">
                    <p style="color:#ef4444;">⚠ Could not load analysis for ${symbol}.</p>
                    <button class="btn btn-ghost" onclick="closePositionModal()">Close</button>
                </div>`;
            }
        })
        .catch(() => {
            modal.innerHTML = `<div class="pos-modal-box" style="text-align:center;padding:40px;">
                <p style="color:#ef4444;">Network error. Please check your connection.</p>
                <button class="btn btn-ghost" onclick="closePositionModal()">Close</button>
            </div>`;
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

    fetch("/api/dividends-corporate-actions")
        .then(r => r.json())
        .then(res => {
            if (res.success && res.data && res.data.dividendCalendar) {
                renderCorporateCalendar(res.data.dividendCalendar);
            }
        })
        .catch(err => console.error("Error fetching corporate actions:", err));
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
            <td><strong>${item.symbol}</strong></td>
            <td>${item.name}</td>
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

    fetch(`/api/financial-statements?symbol=${symbol}`)
        .then(r => r.json())
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
            if (workspace) workspace.innerHTML = `<div class="upper-lock-empty"><p>Error loading financials: ${err.message}</p></div>`;
        });
}

function calculate10FinancialRatios(fin) {
    const bs = fin.balanceSheet || {};
    const is = fin.incomeStatement || {};
    const cf = fin.cashFlowStatement || {};

    const curAssets = bs.currentAssets || 1;
    const curLiab = bs.currentLiabilities || 1;
    const inv = bs.inventory || 0;
    const cash = bs.cash || 0;
    const debt = bs.totalDebt || 0;
    const assets = bs.totalAssets || 1;
    const equity = bs.shareholderEquity || 1;
    const rev = is.revenue || 1;
    const ebit = is.ebit || 1;
    const interest = is.interestExpense || 1;
    const netInc = is.netIncome || 0;
    const cogs = is.cogs || 1;

    // 1. Current Ratio
    const currentRatio = curAssets / curLiab;
    // 2. Quick Ratio
    const quickRatio = (curAssets - inv) / curLiab;
    // 3. Cash Ratio
    const cashRatio = cash / curLiab;
    // 4. Debt-to-Equity Ratio
    const deRatio = debt / equity;
    // 5. Debt-to-Asset Ratio
    const daRatio = debt / assets;
    // 6. Return on Equity (ROE)
    const roe = (netInc / equity) * 100;
    // 7. Return on Assets (ROA)
    const roa = (netInc / assets) * 100;
    // 8. Return on Capital Employed (ROCE)
    const capEmployed = assets - curLiab;
    const roce = (ebit / capEmployed) * 100;
    // 9. Times Interest Earned (TIE)
    const tie = ebit / interest;
    // 10. Days Sales Inventory (DSI)
    const dsi = (inv / cogs) * 365;

    return { currentRatio, quickRatio, cashRatio, deRatio, daRatio, roe, roa, roce, tie, dsi };
}

function renderFinancialStatementsWorkspace(fin, ratios) {
    const workspace = document.getElementById("fin-workspace");
    if (!workspace) return;

    const bs = fin.balanceSheet || {};
    const is = fin.incomeStatement || {};
    const cf = fin.cashFlowStatement || {};

    let html = `
    <!-- Top Ratio Dashboard Cards (10 Financial Ratios) -->
    <div class="fin-ratios-section">
        <h3 class="fin-sec-title">Automated Financial Ratio Dashboard (10 Metrics)</h3>
        <div class="ratios-grid">
            <!-- Liquidity Ratios -->
            <div class="ratio-card">
                <div class="r-cat">Liquidity</div>
                <div class="r-name">Current Ratio</div>
                <div class="r-val ${ratios.currentRatio >= 1.5 ? 'positive' : 'negative'}">${ratios.currentRatio.toFixed(2)}x</div>
                <div class="r-desc">Current Assets / Liabilities (Target: >1.5x)</div>
            </div>
            <div class="ratio-card">
                <div class="r-cat">Liquidity</div>
                <div class="r-name">Quick Ratio (Acid-Test)</div>
                <div class="r-val ${ratios.quickRatio >= 1.0 ? 'positive' : 'negative'}">${ratios.quickRatio.toFixed(2)}x</div>
                <div class="r-desc">(Assets - Inventory) / Liabilities</div>
            </div>
            <div class="ratio-card">
                <div class="r-cat">Liquidity</div>
                <div class="r-name">Cash Ratio</div>
                <div class="r-val">${ratios.cashRatio.toFixed(2)}x</div>
                <div class="r-desc">Cash & Equivalents / Liabilities</div>
            </div>

            <!-- Solvency Ratios -->
            <div class="ratio-card">
                <div class="r-cat">Solvency</div>
                <div class="r-name">Debt-to-Equity (D/E)</div>
                <div class="r-val ${ratios.deRatio <= 1.0 ? 'positive' : 'negative'}">${ratios.deRatio.toFixed(2)}x</div>
                <div class="r-desc">Total Debt / Shareholder Equity</div>
            </div>
            <div class="ratio-card">
                <div class="r-cat">Solvency</div>
                <div class="r-name">Debt-to-Asset</div>
                <div class="r-val">${(ratios.daRatio * 100).toFixed(1)}%</div>
                <div class="r-desc">Total Debt / Total Assets</div>
            </div>

            <!-- Profitability & Efficiency Ratios -->
            <div class="ratio-card">
                <div class="r-cat">Profitability</div>
                <div class="r-name">Return on Equity (ROE)</div>
                <div class="r-val positive">${ratios.roe.toFixed(1)}%</div>
                <div class="r-desc">Net Income / Shareholder Equity</div>
            </div>
            <div class="ratio-card">
                <div class="r-cat">Profitability</div>
                <div class="r-name">Return on Assets (ROA)</div>
                <div class="r-val positive">${ratios.roa.toFixed(1)}%</div>
                <div class="r-desc">Net Income / Total Assets</div>
            </div>
            <div class="ratio-card">
                <div class="r-cat">Profitability</div>
                <div class="r-name">ROCE</div>
                <div class="r-val positive">${ratios.roce.toFixed(1)}%</div>
                <div class="r-desc">Return on Capital Employed</div>
            </div>
            <div class="ratio-card">
                <div class="r-cat">Efficiency</div>
                <div class="r-name">Times Interest Earned (TIE)</div>
                <div class="r-val ${ratios.tie >= 3.0 ? 'positive' : 'negative'}">${ratios.tie.toFixed(1)}x</div>
                <div class="r-desc">EBIT / Interest Expense Coverage</div>
            </div>
            <div class="ratio-card">
                <div class="r-cat">Efficiency</div>
                <div class="r-name">Days Sales Inventory (DSI)</div>
                <div class="r-val">${ratios.dsi.toFixed(0)} Days</div>
                <div class="r-desc">Avg Days to Turn Inventory into Sales</div>
            </div>
        </div>
    </div>

    <!-- Financial Statements Sub-Tabs -->
    <div class="fin-statements-section">
        <h3 class="fin-sec-title">${fin.symbol} — Financial Statements (${fin.companyName})</h3>
        
        <div class="stmt-grid">
            <!-- Balance Sheet Table -->
            <div class="stmt-card">
                <h4>Balance Sheet (${bs.period})</h4>
                <table class="stmt-table">
                    <tr><td>Cash & Cash Equivalents</td><td>₨${formatVolume(bs.cash)}</td></tr>
                    <tr><td>Receivables</td><td>₨${formatVolume(bs.receivables)}</td></tr>
                    <tr><td>Inventory</td><td>₨${formatVolume(bs.inventory)}</td></tr>
                    <tr class="row-bold"><td>Total Current Assets</td><td>₨${formatVolume(bs.currentAssets)}</td></tr>
                    <tr><td>Property, Plant & Equipment</td><td>₨${formatVolume(bs.nonCurrentAssets)}</td></tr>
                    <tr class="row-highlight"><td>TOTAL ASSETS</td><td>₨${formatVolume(bs.totalAssets)}</td></tr>
                    <tr><td>Current Liabilities</td><td>₨${formatVolume(bs.currentLiabilities)}</td></tr>
                    <tr><td>Total Debt</td><td>₨${formatVolume(bs.totalDebt)}</td></tr>
                    <tr class="row-bold"><td>Total Liabilities</td><td>₨${formatVolume(bs.totalLiabilities)}</td></tr>
                    <tr class="row-highlight"><td>SHAREHOLDER EQUITY</td><td>₨${formatVolume(bs.shareholderEquity)}</td></tr>
                </table>
            </div>

            <!-- Income Statement Table -->
            <div class="stmt-card">
                <h4>Income Statement (${is.period})</h4>
                <table class="stmt-table">
                    <tr class="row-bold"><td>Revenue (Sales)</td><td>₨${formatVolume(is.revenue)}</td></tr>
                    <tr><td>Cost of Goods Sold (COGS)</td><td>(₨${formatVolume(is.cogs)})</td></tr>
                    <tr class="row-bold"><td>Gross Profit</td><td>₨${formatVolume(is.grossProfit)}</td></tr>
                    <tr><td>Operating Expenses (OpEx)</td><td>(₨${formatVolume(is.operatingExpenses)})</td></tr>
                    <tr class="row-bold"><td>EBIT (Operating Income)</td><td>₨${formatVolume(is.ebit)}</td></tr>
                    <tr><td>Interest Expense</td><td>(₨${formatVolume(is.interestExpense)})</td></tr>
                    <tr><td>EBT (Pre-Tax Income)</td><td>₨${formatVolume(is.ebt)}</td></tr>
                    <tr><td>Tax Expense (29%)</td><td>(₨${formatVolume(is.tax)})</td></tr>
                    <tr class="row-highlight"><td>NET INCOME</td><td>₨${formatVolume(is.netIncome)}</td></tr>
                </table>
            </div>

            <!-- Cash Flow Statement Table -->
            <div class="stmt-card">
                <h4>Cash Flow Statement (${cf.period})</h4>
                <table class="stmt-table">
                    <tr class="row-bold"><td>Cash Flow from Operations (CFO)</td><td>₨${formatVolume(cf.operatingCashFlow)}</td></tr>
                    <tr><td>Capital Expenditures (CapEx)</td><td>(₨${formatVolume(cf.capex)})</td></tr>
                    <tr class="row-bold"><td>Cash Flow from Investing (CFI)</td><td>(₨${formatVolume(Math.abs(cf.investingCashFlow))})</td></tr>
                    <tr><td>Dividends Paid</td><td>(₨${formatVolume(cf.dividendsPaid)})</td></tr>
                    <tr class="row-bold"><td>Cash Flow from Financing (CFF)</td><td>(₨${formatVolume(Math.abs(cf.financingCashFlow))})</td></tr>
                    <tr class="row-highlight"><td>NET CHANGE IN CASH</td><td>₨${formatVolume(cf.netChangeInCash)}</td></tr>
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

let trialPollTimer = null;

function initTrialSystem() {
    const deviceId = getDeviceId();
    fetch(`/api/trial-status?deviceId=${deviceId}`)
        .then(r => r.json())
        .then(res => {
            if (res.success && res.data) {
                const info = res.data;
                const banner = document.getElementById("online-trial-banner");
                const bannerText = document.getElementById("online-trial-text");
                const paywallModal = document.getElementById("trial-paywall-modal");
                const emailModal = document.getElementById("trial-email-modal");

                if (info.isLocal) {
                    if (banner) banner.style.display = "none";
                    if (emailModal) emailModal.style.display = "none";
                    if (trialPollTimer) clearInterval(trialPollTimer);
                    return;
                }

                if (info.needsEmail) {
                    if (banner) banner.style.display = "none";
                    if (emailModal) emailModal.style.display = "flex";
                } else if (!info.trialActive) {
                    // Trial expired ➔ Lock app with paywall
                    if (banner) banner.style.display = "none";
                    if (emailModal) emailModal.style.display = "none";
                    if (paywallModal) paywallModal.style.display = "flex";
                    if (trialPollTimer) clearInterval(trialPollTimer);
                } else {
                    // Trial active ➔ Show countdown banner
                    if (emailModal) emailModal.style.display = "none";
                    if (paywallModal) paywallModal.style.display = "none";
                    if (banner) banner.style.display = "flex";
                    if (bannerText) {
                        let timeLabel = "";
                        if (info.isPaid) {
                            timeLabel = "🌟 PSX Screener Pro (Unlimited Online Access)";
                        } else if (info.hoursLeft < 0.1) {
                            const mins = Math.max(0, Math.floor(info.hoursLeft * 60));
                            timeLabel = `🚨 Free Trial Ending Soon: ~${mins} minute(s) remaining!`;
                        } else {
                            timeLabel = `⏳ Online 3-Day Free Trial: ${info.daysLeft} days (${info.hoursLeft} hrs) remaining`;
                        }
                        bannerText.textContent = timeLabel;
                    }

                    // Poll every 10 seconds to detect trial expiration in real-time
                    if (!trialPollTimer && !info.isPaid) {
                        trialPollTimer = setInterval(initTrialSystem, 10000);
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

    const deviceId = getDeviceId();
    const params = new URLSearchParams({ email, deviceId });

    fetch(`/api/start-trial?${params}`)
        .then(r => r.json())
        .then(res => {
            if (msgEl) {
                msgEl.style.display = "block";
                if (res.success) {
                    msgEl.className = "activation-msg msg-success";
                    msgEl.textContent = "✔ " + (res.message || "3-Day Free Trial Started!");
                    setTimeout(() => {
                        const emailModal = document.getElementById("trial-email-modal");
                        if (emailModal) emailModal.style.display = "none";
                        initTrialSystem();
                    }, 1200);
                } else {
                    msgEl.className = "activation-msg msg-error";
                    msgEl.textContent = "✖ " + (res.error || "Trial registration error.");
                }
            }
        })
        .catch(err => {
            console.error("submitTrialEmail error:", err);
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
}

function initTermsCheck() {
    const accepted = localStorage.getItem("psx_terms_accepted_v1");
    if (!accepted) {
        const modal = document.getElementById("terms-disclaimer-modal");
        if (modal) modal.style.display = "flex";
    }
}

// ─── Initialize ───
document.addEventListener("DOMContentLoaded", () => {
    document.getElementById("watchlist-count").textContent = watchlist.size;
    updateRangeLabels();
    initEventListeners();
    initPWAAndMobile();
    initTermsCheck();
    initTrialSystem();
    
    // Live Pakistan Clock Ticker
    updatePKTClock();
    setInterval(updatePKTClock, 1000);

    // Fetch live data on load
    fetchLiveData();
});
