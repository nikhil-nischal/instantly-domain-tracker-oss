/* ════════════════════════════════════════════════════════════
   SHARED TREND CHART  —  single source of truth
   Used by domain_detail.html (one domain) and overview.html (all domains).
   Edit here once; both pages update together.

   Public API:
     initTrendChart({ buildUrl: function(days){ return url; }, defaultDays })
       buildUrl   — returns the JSON data endpoint URL for N days
       defaultDays — initial range to load (default 30)

   Requires (loaded globally in base.html): Chart.js, and the chart markup
   from templates/_trend_chart.html present on the page.
   ════════════════════════════════════════════════════════════ */
(function () {
'use strict';

// Page-supplied config (URL builder etc.)
var _config = { buildUrl: function (days) { return '?days=' + days; }, defaultDays: 30 };

// ── Private helper (self-contained so the file works on any page) ──
function numFormat(n) {
    if (n == null) return '—';
    return n.toLocaleString();
}

/* ── Trend Chart ─────────────────────────────────────────────── */
let trendChart = null;
let yPctMax = 5;  // default until auto-compute runs
let yScaleMode = 'auto';  // 'auto' | '2' | '5' | '10' | '20'
const RANGE_MAP = {
    '7d': 7,
    '30d': 30,
    '90d': 90,
    '180d': 180,
    'all': 365,
};

const MONTH_NAMES = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

// ── Premium Color Palette (Shrishti) ───────────────────────
const CHART_COLORS = {
    sentBar: 'rgba(156, 163, 175, 0.2)',      // muted gray
    sentBarBorder: 'rgba(156, 163, 175, 0.4)',
    replyRate: '#fb923c',                     // orange-400
    burn: '#f87171',                          // softer red
    atRisk: '#facc15',                        // warmer yellow
    grid: 'rgba(255,255,255,0.03)',
    textMuted: '#6b7280',
};

function formatDateLabel(dateStr) {
    // Convert "2026-04-15" to "15 Apr"
    const parts = dateStr.split('-');
    if (parts.length !== 3) return dateStr;
    const month = MONTH_NAMES[parseInt(parts[1], 10) - 1];
    const day = parseInt(parts[2], 10);
    return day + ' ' + month;
}

function formatTooltipDate(dateStr) {
    // Convert "2026-04-15" to "15 Apr 2026"
    const parts = dateStr.split('-');
    if (parts.length !== 3) return dateStr;
    const month = MONTH_NAMES[parseInt(parts[1], 10) - 1];
    const day = parseInt(parts[2], 10);
    return day + ' ' + month + ' ' + parts[0];
}

function showChartLoading() {
    const loading = document.getElementById('chartLoading');
    const container = document.getElementById('chartContainer');
    const empty = document.getElementById('chartEmpty');
    const legend = document.getElementById('chartLegend');
    const summary = document.getElementById('chartSummary');
    if (loading) loading.style.display = 'flex';
    if (container) container.style.display = 'none';
    if (empty) empty.style.display = 'none';
    if (legend) legend.style.display = 'none';
    if (summary) summary.style.display = 'none';
}

function showChartReady() {
    const loading = document.getElementById('chartLoading');
    const container = document.getElementById('chartContainer');
    const legend = document.getElementById('chartLegend');
    if (loading) loading.style.display = 'none';
    if (container) container.style.display = 'block';
    if (legend) legend.style.display = 'flex';
}

function showChartEmpty() {
    const loading = document.getElementById('chartLoading');
    const container = document.getElementById('chartContainer');
    const empty = document.getElementById('chartEmpty');
    const legend = document.getElementById('chartLegend');
    const summary = document.getElementById('chartSummary');
    if (loading) loading.style.display = 'none';
    if (container) container.style.display = 'none';
    if (empty) empty.style.display = 'flex';
    if (legend) legend.style.display = 'none';
    if (summary) summary.style.display = 'none';
}

async function loadTrendChart(days) {
    const url = _config.buildUrl(days);

    showChartLoading();

    try {
        const res = await fetch(url);
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const rows = await res.json();

        if (!rows || rows.length === 0) {
            showChartEmpty();
            return;
        }

        updateChart(rows, days);
    } catch (err) {
        console.error('Failed to load trend data:', err);
        showChartEmpty();
    }
}

/* ── Tooltip: status dot HTML (kept for backward compat) ── */
function dotHtml(baseClass, val, isBounce) {
    let cls = baseClass;
    if (val != null) {
        if (isBounce) {
            if (val > 5) cls = 'dot-bad';
            else if (val > 3) cls = 'dot-warn';
            else cls = 'dot-good';
        } else {
            if (val < 1) cls = 'dot-bad';
            else if (val < 2) cls = 'dot-warn';
            else cls = 'dot-good';
        }
    }
    return '<span class="tt-status-dot ' + cls + '"></span>';
}

function updateChart(rows, days) {
    const ctx = document.getElementById('trendChart');
    if (!ctx) return;

    if (trendChart) {
        trendChart.destroy();
        trendChart = null;
    }

    // Compute low-volume threshold: < 10 sent = dimmed
    const labels = rows.map(function(r) { return r.date; });
    const sentValues = rows.map(function(r) { return r.sent; });
    const replyRateValues = rows.map(function(r) { return r.total_reply_rate != null ? r.total_reply_rate : 0; });
    const bounceRateValues = rows.map(function(r) { return r.bounce_rate != null ? r.bounce_rate : 0; });
    const humanReplyRateValues = rows.map(function(r) { return r.human_reply_rate != null ? r.human_reply_rate : 0; });

    // ── Y-axis max ── reactive to yScaleMode (Shrishti)
    const maxReplyRate = Math.max.apply(null, replyRateValues.filter(function(v) { return v != null && isFinite(v); }));
    if (yScaleMode === 'auto') {
        yPctMax = computeAutoYMax(replyRateValues);
    } else {
        yPctMax = parseFloat(yScaleMode);
    }

    // Max value for left Y-axis
    const maxSent = Math.max.apply(null, sentValues);
    const leftYMax = Math.max(maxSent, 10) * 1.15 || 100;

    // Tick callback for formatting thousands
    const countTickCallback = function(v) {
        if (v >= 1000) return (v / 1000).toFixed(v >= 10000 ? 0 : 1) + 'K';
        return v;
    };

    // X-axis tick callback with year-boundary detection
    let lastMonth = -1;
    function xTickCallback(value, index, ticks) {
        const dateStr = this.getLabelForValue(value);
        const parts = dateStr.split('-');
        if (parts.length !== 3) return dateStr;
        const month = parseInt(parts[1], 10);
        const day = parseInt(parts[2], 10);
        const label = day + ' ' + MONTH_NAMES[month - 1];
        // Add year on January boundary for long ranges
        if (month === 1 && lastMonth !== -1 && lastMonth === 12) {
            lastMonth = month;
            return label + ' ' + parts[0];
        }
        lastMonth = month;
        return label;
    }

    // Determine max ticks based on data length
    let maxTicks = 12;
    if (rows.length <= 30) maxTicks = 10;
    else if (rows.length <= 90) maxTicks = 10;
    else if (rows.length <= 180) maxTicks = 10;
    else maxTicks = 12;

    // Build the chart
    trendChart = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: labels,
            datasets: [
                {
                    // Dataset 1: Sent Volume (line, left Y)
                    label: 'Sent',
                    data: sentValues,
                    type: 'line',
                    order: 2,
                    borderColor: 'rgba(156, 163, 175, 0.6)',
                    borderWidth: 1.5,
                    pointRadius: 0,
                    pointHoverRadius: 4,
                    pointHoverBackgroundColor: 'rgba(156, 163, 175, 0.8)',
                    pointHoverBorderColor: '#fff',
                    pointHoverBorderWidth: 2,
                    fill: {
                        target: 'origin',
                        above: 'rgba(156, 163, 175, 0.1)',
                    },
                    tension: 0.3,
                    yAxisID: 'y-counts',
                },

                {
                    // Dataset 2: Reply Rate % (solid hero line, right Y)
                    label: 'Reply Rate',
                    data: replyRateValues,
                    type: 'line',
                    order: 1,
                    borderColor: CHART_COLORS.replyRate,
                    borderWidth: 3,
                    borderDash: [],
                    pointRadius: 0,
                    pointHoverRadius: 6,
                    pointHoverBackgroundColor: '#fb923c',
                    pointHoverBorderColor: '#fff',
                    pointHoverBorderWidth: 3,
                    fill: {
                        target: 'origin',
                        above: function(ctx) {
                            const gradient = ctx.chart.ctx.createLinearGradient(0, 0, 0, ctx.chart.height);
                            gradient.addColorStop(0, 'rgba(251, 146, 60, 0.18)');
                            gradient.addColorStop(0.5, 'rgba(251, 146, 60, 0.06)');
                            gradient.addColorStop(1, 'rgba(251, 146, 60, 0)');
                            return gradient;
                        },
                    },
                    tension: 0.35,
                    yAxisID: 'y-pct',
                },
                {
                    // Dataset 3: Bounce Rate % (red dotted, right Y)
                    label: 'Bounce Rate',
                    data: bounceRateValues,
                    type: 'line',
                    order: 1,
                    borderColor: '#ef4444',
                    borderWidth: 1.5,
                    borderDash: [2, 4],
                    pointRadius: 0,
                    pointHoverRadius: 4,
                    pointHoverBackgroundColor: '#ef4444',
                    pointHoverBorderColor: '#fff',
                    pointHoverBorderWidth: 2,
                    fill: false,
                    tension: 0.4,
                    yAxisID: 'y-pct',
                },
                {
                    // Dataset 4: Human Reply Rate % (solid green, right Y)
                    label: 'Human Reply Rate',
                    data: humanReplyRateValues,
                    type: 'line',
                    order: 1,
                    borderColor: '#22c55e',
                    borderWidth: 1.5,
                    borderDash: [],
                    pointRadius: 0,
                    pointHoverRadius: 4,
                    pointHoverBackgroundColor: '#22c55e',
                    pointHoverBorderColor: '#fff',
                    pointHoverBorderWidth: 2,
                    fill: false,
                    tension: 0.4,
                    yAxisID: 'y-pct',
                },
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: {
                duration: 800,
                easing: 'easeOutQuart',
            },
            transitions: {
                active: {
                    animation: {
                        duration: 200,
                    }
                }
            },
            interaction: {
                mode: 'index',
                intersect: false,
            },
            plugins: {
                legend: { display: false },
                tooltip: {
                    enabled: false,
                    mode: 'index',
                    intersect: false,
                    external: function(context) {
                        // ── Delegate to Data Monitor Panel ──
                        const tooltipModel = context.tooltip;
                        if (tooltipModel.opacity === 0 || !tooltipModel.dataPoints || tooltipModel.dataPoints.length === 0) {
                            return;  // monitor handles clear via mouseleave
                        }

                        const dataIndex = tooltipModel.dataPoints[0].dataIndex;
                        const row = rows[dataIndex];
                        if (!row) return;

                        // Call the monitor's update function
                        if (typeof window.updateDataMonitor === 'function') {
                            window.updateDataMonitor(row);
                        }
                    }
                }
            },
            scales: {
                x: {
                    grid: {
                        color: 'rgba(255,255,255,0.03)',
                        drawTicks: false,
                    },
                    border: {
                        display: false,
                    },
                    ticks: {
                        color: '#6b7280',
                        font: {
                            size: 11,
                            family: 'Inter, -apple-system, sans-serif',
                            weight: '500',
                        },
                        maxTicksLimit: maxTicks,
                        callback: xTickCallback,
                        padding: 8,
                    },
                },
                'y-counts': {
                    type: 'linear',
                    position: 'left',
                    beginAtZero: true,
                    max: leftYMax,
                    grid: {
                        color: 'rgba(255,255,255,0.03)',
                        drawTicks: false,
                    },
                    border: {
                        display: false,
                    },
                    ticks: {
                        color: '#6b7280',
                        font: {
                            size: 10,
                            family: 'Inter, -apple-system, sans-serif',
                            weight: '500',
                        },
                        callback: countTickCallback,
                        padding: 8,
                    },
                    title: {
                        display: true,
                        text: 'Volume',
                        color: '#6b7280',
                        font: {
                            size: 10,
                            family: 'Inter, -apple-system, sans-serif',
                            weight: '600',
                        },
                        padding: { bottom: 8 },
                    },
                },
                'y-pct': {
                    type: 'linear',
                    position: 'right',
                    beginAtZero: true,
                    max: yPctMax,  // Dynamic Y-axis max
                    title: {
                        display: true,
                        text: 'Rate %',
                        color: '#6b7280',
                        font: {
                            size: 10,
                            family: 'Inter, -apple-system, sans-serif',
                            weight: '600',
                        },
                        padding: { bottom: 8 },
                    },
                    ticks: {
                        color: '#6b7280',
                        font: {
                            size: 10,
                            family: 'Inter, -apple-system, sans-serif',
                            weight: '500',
                        },
                        callback: function(v) { return v.toFixed(1) + '%'; },
                        padding: 8,
                    },
                    grid: {
                        drawOnChartArea: false,
                    },
                    border: {
                        display: false,
                    },
                }
            }
        }
    });

    showChartReady();

    // Update summary bar
    updateSummaryBar(rows, days);

    // ── Chart canvas mouseleave → clear monitor ──
    ctx.addEventListener('mouseleave', function () {
        setTimeout(function () {
            var hoveredRow = document.querySelector('tbody tr:hover');
            if (!hoveredRow && typeof window.clearDataMonitor === 'function') {
                window.clearDataMonitor();
            }
        }, 120);
    });

    // ── Interactive Legend Toggles ──
    document.querySelectorAll('.legend-item[data-dataset-index]').forEach(function(item) {
        item.addEventListener('click', function() {
            const index = parseInt(item.getAttribute('data-dataset-index'));
            const meta = trendChart.getDatasetMeta(index);
            if (meta) {
                meta.hidden = !meta.hidden;
                trendChart.update();
                item.classList.toggle('legend-hidden');
            }
        });
    });
}

/* ── Summary Bar → redirected to Data Monitor idle state ── */
function updateSummaryBar(rows, days) {
    // Aggregate over the visible rows
    let totalSent = 0, totalHuman = 0, totalAuto = 0, totalBounced = 0;
    for (var i = 0; i < rows.length; i++) {
        totalSent += rows[i].sent || 0;
        totalHuman += rows[i].human_replies || 0;
        totalAuto += rows[i].auto_replies || 0;
        totalBounced += rows[i].bounced || 0;
    }
    const totalReplies = totalHuman + totalAuto;
    const avgSentPerDay = rows.length > 0 ? Math.round(totalSent / rows.length) : 0;
    const overallReplyRate = totalSent > 0 ? (totalReplies / totalSent * 100).toFixed(1) : 0;
    const humanPct = totalSent > 0 ? (totalHuman / totalSent * 100).toFixed(1) : 0;
    const overallBounceRate = totalSent > 0 ? (totalBounced / totalSent * 100).toFixed(1) : 0;

    // Store idle data for the monitor panel
    if (typeof window.setMonitorIdle === 'function') {
        window.setMonitorIdle({
            sentTotal: totalSent,
            replyRate: overallReplyRate + '%',
            totalReplies: totalReplies,
            humanPct: humanPct + '%',
            bounceRate: overallBounceRate + '%',
            humanTotal: totalHuman,
            bounceTotal: totalBounced
        });
    }

    // Also update the monitor's summary text directly
    var summaryText = document.getElementById('dm-summary-text');
    if (summaryText) {
        summaryText.innerHTML = '<span>' + numFormat(totalSent) + '</span> sent total · <span>' + overallReplyRate + '%</span> reply rate · <span>' + numFormat(totalReplies) + '</span> replies total · <span>' + humanPct + '%</span> human · <span>' + overallBounceRate + '%</span> bounce';
    }

    // Hide old summary bar (kept for backward compat)
    var oldSummary = document.getElementById('chartSummary');
    if (oldSummary) oldSummary.style.display = 'none';
}

/* ── Y-axis Scale Control (Shrishti) ──────────────────────────── */
function computeAutoYMax(replyRateValues) {
    var maxVal = Math.max.apply(null, replyRateValues.filter(function(v) { return v != null && isFinite(v); }));
    if (maxVal <= 0) return 5;
    // Pad 20% above the max, minimum 5%
    return Math.max(5, Math.ceil(maxVal * 1.2));
}

function initYAxisScaleControl() {
    var container = document.querySelector('.yaxis-scale-pills');
    if (!container) return;

    // Default: Auto
    yScaleMode = 'auto';

    container.querySelectorAll('.yaxis-scale-btn').forEach(function(btn) {
        btn.addEventListener('click', function() {
            // Deactivate all
            container.querySelectorAll('.yaxis-scale-btn').forEach(function(b) {
                b.classList.remove('active');
                b.setAttribute('aria-checked', 'false');
            });
            // Activate clicked
            btn.classList.add('active');
            btn.setAttribute('aria-checked', 'true');

            var scaleVal = btn.getAttribute('data-scale');
            yScaleMode = scaleVal;

            if (scaleVal === 'auto') {
                // Recompute from current data
                if (trendChart) {
                    var replyRateDataset = trendChart.data.datasets[1];
                    if (replyRateDataset && replyRateDataset.data) {
                        yPctMax = computeAutoYMax(replyRateDataset.data);
                        trendChart.options.scales['y-pct'].max = yPctMax;
                        trendChart.update('default');
                    }
                }
            } else {
                yPctMax = parseFloat(scaleVal);
                if (trendChart) {
                    trendChart.options.scales['y-pct'].max = yPctMax;
                    trendChart.update('default');
                }
            }
        });
    });
}


/* ── Public init — wires data load, range buttons, y-axis control ── */
window.initTrendChart = function (config) {
    _config = config || _config;
    var defaultDays = _config.defaultDays || 30;

    // Initial load
    loadTrendChart(defaultDays);

    // Range button bindings (7d / 30d / 90d / 180d / All)
    document.querySelectorAll('.chart-range-btn').forEach(function (btn) {
        btn.addEventListener('click', function () {
            document.querySelectorAll('.chart-range-btn').forEach(function (b) {
                b.classList.remove('active');
            });
            btn.classList.add('active');
            var range = btn.getAttribute('data-range');
            var days = RANGE_MAP[range] || 30;
            loadTrendChart(days);
        });
    });

    // Y-axis scale control
    initYAxisScaleControl();
};

})();
