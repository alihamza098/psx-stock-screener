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
        tbody.innerHTML = `<tr><td colspan="11" style="text-align:center; padding:48px; color:var(--accent-rose);">${message}</td></tr>`;
    }
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
            <td><span class="cell-symbol">${stock.symbol}<span class="cell-name">${stock.name}</span></span></td>
            <td><span class="cell-sector">${stock.sector}</span></td>
            <td class="cell-price">${formatPrice(stock.price)}</td>
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

    updateRangeLabels();
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
    document.querySelector(`[data-view="${view}"]`).classList.add("active");

    document.getElementById("view-table").style.display = view === "table" ? "block" : "none";
    document.getElementById("view-cards").style.display = view === "cards" ? "grid" : "none";
    document.getElementById("view-scorecard").style.display = view === "scorecard" ? "block" : "none";
}

// ─── Event Listeners ───
function initEventListeners() {
    // Filters
    const filterElements = ["filter-sector", "filter-index", "filter-mcap", "filter-pe", "filter-div", "filter-change", "filter-year-change", "filter-volume"];
    filterElements.forEach(id => {
        document.getElementById(id).addEventListener("input", () => {
            updateRangeLabels();
            renderAll();
            document.querySelectorAll(".preset-chip").forEach(c => c.classList.remove("active"));
            activePreset = null;
        });
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
        }
    });

    // Export
    document.getElementById("btn-export").addEventListener("click", exportCSV);
}

// ─── Initialize ───
document.addEventListener("DOMContentLoaded", () => {
    document.getElementById("watchlist-count").textContent = watchlist.size;
    updateRangeLabels();
    initEventListeners();
    
    // Fetch live data on load
    fetchLiveData();
});
