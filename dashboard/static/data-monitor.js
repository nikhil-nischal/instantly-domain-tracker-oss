/* ════════════════════════════════════════════════════════════
   DATA MONITOR — replaces chartjs floating tooltip
   Public API:
     updateDataMonitor(row)  — populate from a data row
     clearDataMonitor()      — reset to idle (rolling averages)
     initDataMonitor()       — wire table row hover events
   ════════════════════════════════════════════════════════════ */
(function () {
'use strict';

/* ── Helpers ─────────────────────────────────────────── */
function numFormat(n) {
    if (n == null) return '—';
    return n.toLocaleString();
}

function rateColor(val) {
    if (val == null) return '#6b7280';
    if (val >= 2.0) return '#22c55e';
    if (val >= 1.0) return '#eab308';
    return '#ef4444';
}

function bounceColor(val) {
    if (val == null) return '#6b7280';
    if (val <= 3) return '#22c55e';
    if (val <= 5) return '#eab308';
    return '#ef4444';
}

function statusIcon(val, isBounce) {
    var colorFn = isBounce ? bounceColor : rateColor;
    var c = colorFn(val);
    if (c === '#22c55e') return '🟢 Good';
    if (c === '#eab308') return '🟡 Warn';
    return '🔴 Bad';
}

/* ── Global idle data (set by chart summary update) ──── */
var _idleData = {
    sentTotal: '—',
    replyRate: '—',
    totalReplies: '—',
    humanPct: '—',
    bounceRate: '—',
    humanTotal: '—',
    bounceTotal: '—'
};

/* ── Set idle rolling averages ───────────────────────── */
window.setMonitorIdle = function (data) {
    _idleData = data;
};

/* ── updateDataMonitor(row) — populates from data row ──── */
window.updateDataMonitor = function (row) {
    var panel = document.getElementById('data-monitor-panel');
    if (!panel) return;

    if (!row || row === undefined || row.sent === undefined) {
        clearDataMonitor();
        return;
    }

    var sent = row.sent || 0;
    var totalReplies = row.total_replies || 0;
    var humanReplies = row.human_replies || 0;
    var autoReplies = row.auto_replies || 0;
    var bounced = row.bounced || 0;
    var replyRate = row.total_reply_rate;
    var humanRate = row.human_reply_rate;
    var bounceRate = row.bounce_rate;
    // Activate panel
    panel.classList.add('is-active');

    // Badge
    var badge = document.getElementById('dm-badge');
    if (badge) {
        badge.className = 'dm-head-badge';
        if (replyRate != null) {
            var rrColor = rateColor(replyRate);
            badge.textContent = replyRate.toFixed(1) + '%';
            badge.style.background = rrColor + '18';
            badge.style.color = rrColor;
        } else {
            badge.textContent = '—';
            badge.className = 'dm-head-badge is-idle';
        }
    }

    // Date
    var dateEl = document.getElementById('dm-date');
    if (dateEl) {
        if (row.date) {
            // Format "15 May 2026" from "2026-05-15"
            var parts = row.date.split('-');
            if (parts.length === 3) {
                var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
                var monthIdx = parseInt(parts[1], 10) - 1;
                dateEl.textContent = parseInt(parts[2], 10) + ' ' + months[monthIdx] + ' ' + parts[0];
            } else {
                dateEl.textContent = row.date;
            }
        } else {
            dateEl.textContent = row.domain || 'Selected';
        }
        dateEl.classList.remove('is-idle');
    }

    // ── Sent value ──
    var sentVal = document.getElementById('dm-sent-val');
    if (sentVal) {
        sentVal.textContent = numFormat(sent);
        sentVal.classList.remove('is-idle');
    }

    // ── Reply Rate ──
    var rrVal = document.getElementById('dm-replyrate-val');
    if (rrVal) {
        rrVal.textContent = replyRate != null ? replyRate.toFixed(1) + '% (' + numFormat(totalReplies) + ')' : '—';
        rrVal.style.color = replyRate != null ? rateColor(replyRate) : '';
        rrVal.classList.remove('is-idle');
    }
    // ── Human Reply ──
    var hmVal = document.getElementById('dm-human-val');
    if (hmVal) {
        hmVal.textContent = humanRate != null ? humanRate.toFixed(1) + '% (' + numFormat(humanReplies) + ')' : '—';
        hmVal.style.color = humanRate != null ? rateColor(humanRate) : '';
        hmVal.classList.remove('is-idle');
    }

    // ── Bounce Rate ──
    var brVal = document.getElementById('dm-bounce-val');
    if (brVal) {
        brVal.textContent = bounceRate != null ? bounceRate.toFixed(1) + '% (' + numFormat(bounced) + ')' : '—';
        brVal.style.color = bounceRate != null ? bounceColor(bounceRate) : '';
        brVal.classList.remove('is-idle');
    }


};

/* ── clearDataMonitor() — reset to idle state ─────────── */
window.clearDataMonitor = function () {
    var panel = document.getElementById('data-monitor-panel');
    if (!panel) return;

    panel.classList.remove('is-active');

    var d = _idleData;

    // Badge — show idle reply rate with color (same design as hover)
    var badge = document.getElementById('dm-badge');
    if (badge) {
        var rateVal = parseFloat(d.replyRate);
        if (d.replyRate !== '—' && !isNaN(rateVal)) {
            var rrColor = rateColor(rateVal);
            badge.className = 'dm-head-badge';
            badge.textContent = d.replyRate;
            badge.style.background = rrColor + '18';
            badge.style.color = rrColor;
        } else {
            badge.className = 'dm-head-badge';
            badge.textContent = '—';
            badge.style.background = '';
            badge.style.color = '';
        }
    }

    // Date — neutral
    var dateEl = document.getElementById('dm-date');
    if (dateEl) {
        dateEl.textContent = 'Selected Range';
        dateEl.classList.remove('is-idle');
    }

    // Values — rolling averages from idle data (same visual design as hover)
    var sentVal = document.getElementById('dm-sent-val');
    if (sentVal) {
        sentVal.textContent = d.sentTotal !== '—' ? numFormat(d.sentTotal) : '—';
        sentVal.style.color = '';
    }

    var rrVal = document.getElementById('dm-replyrate-val');
    if (rrVal) {
        rrVal.textContent = d.replyRate !== '—' ? d.replyRate + ' (' + numFormat(d.totalReplies) + ')' : '—';
        rrVal.style.color = '';
    }

    var hmVal = document.getElementById('dm-human-val');
    if (hmVal) {
        hmVal.textContent = d.humanPct !== '—' ? d.humanPct + ' (' + numFormat(d.humanTotal) + ')' : '—';
        hmVal.style.color = '';
    }

    var brVal = document.getElementById('dm-bounce-val');
    if (brVal) {
        brVal.textContent = d.bounceRate !== '—' ? d.bounceRate + ' (' + numFormat(d.bounceTotal) + ')' : '—';
        brVal.style.color = '';
    }



};

/* ── initDataMonitor() — wire table row hover events ──── */
function initDataMonitor() {
    // Wire table row hover
    wireTableRowHover();

    // Listen for chart mouse leave
    var chartContainer = document.getElementById('chartContainer');
    if (chartContainer) {
        chartContainer.addEventListener('mouseleave', function () {
            // Debounce: only if no table row is hovered
            setTimeout(function () {
                var hoveredRow = document.querySelector('tbody tr:hover');
                if (!hoveredRow) {
                    clearDataMonitor();
                }
            }, 50);
        });
    }

    // Also wire the trend chart canvas directly
    var canvas = document.getElementById('trendChart');
    if (canvas) {
        canvas.addEventListener('mouseleave', function () {
            setTimeout(function () {
                var hoveredRow = document.querySelector('tbody tr:hover');
                if (!hoveredRow) {
                    clearDataMonitor();
                }
            }, 100);
        });
    }
}

/* ── Table row hover wiring ──────────────────────────── */
function wireTableRowHover() {
    var rows = document.querySelectorAll('tbody tr');
    rows.forEach(function (row) {
        row.addEventListener('mouseenter', function () {
            // Extract data from data-* attributes
            var data = {};
            data.sent = parseInt(this.getAttribute('data-sent')) || 0;
            data.total_replies = parseInt(this.getAttribute('data-replies')) || 0;
            data.human_replies = parseInt(this.getAttribute('data-human-replies')) || 0;
            data.auto_replies = parseInt(this.getAttribute('data-auto-replies')) || 0;
            data.bounced = parseInt(this.getAttribute('data-bounced')) || 0;
            data.total_reply_rate = parseFloat(this.getAttribute('data-reply-rate'));
            data.human_reply_rate = parseFloat(this.getAttribute('data-human-reply-rate'));
            data.bounce_rate = parseFloat(this.getAttribute('data-bounce-rate'));

            var domain = this.getAttribute('data-domain');
            data.date = domain;  // show domain name as "date" field
            data.domain = domain;


            window.updateDataMonitor(data);
        });

        row.addEventListener('mouseleave', function () {
            // Debounce: check if mouse is now over chart
            setTimeout(function () {
                var chartCanvas = document.getElementById('trendChart');
                if (chartCanvas) {
                    // Check if chart has a hovered point
                    // If chart.js tooltip opacity > 0, keep monitor active
                }
                var hoveredRow = document.querySelector('tbody tr:hover');
                if (!hoveredRow) {
                    clearDataMonitor();
                }
            }, 80);
        });
    });
}

// Export initDataMonitor globally
window.initDataMonitor = initDataMonitor;
window.wireTableRowHover = wireTableRowHover;

})();
