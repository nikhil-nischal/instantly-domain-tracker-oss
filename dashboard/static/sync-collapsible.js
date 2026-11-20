/* ════════════════════════════════════════════════════════════
   COLLAPSIBLE SYNC BAR — Shrishti Design
   Toggle, auto-expand, status update.
   ════════════════════════════════════════════════════════════ */
(function () {
'use strict';

var _syncExpanded = false;

/* ── Toggle sync bar ─────────────────────────────────── */
window.toggleSyncBar = function () {
    var body = document.getElementById('sync-bar-body');
    var toggle = document.getElementById('sync-bar-toggle');
    if (!body || !toggle) return;

    if (_syncExpanded) {
        body.style.maxHeight = '0';
        toggle.textContent = '▾ Show Details';
        _syncExpanded = false;
    } else {
        // Measure content height
        var inner = body.querySelector('.sync-bar-body-inner');
        if (inner) {
            body.style.maxHeight = inner.scrollHeight + 40 + 'px';
        } else {
            body.style.maxHeight = '600px';
        }
        toggle.textContent = '▴ Hide Details';
        _syncExpanded = true;
    }

    // Save state
    try {
        localStorage.setItem('sync-bar-expanded', _syncExpanded ? '1' : '0');
    } catch(e) {}
};

/* ── Open (expand) sync bar ──────────────────────────── */
window.openSyncBar = function () {
    if (_syncExpanded) return;
    window.toggleSyncBar();
};

/* ─── Update collapsed bar status text/icon/dot ──────── */
window.syncBarUpdateStatus = function (pendingCount, latestDate) {
    var dot = document.getElementById('sync-bar-dot');
    var detail = document.getElementById('sync-bar-detail');
    var icon = document.getElementById('sync-bar-icon');
    if (!dot || !detail || !icon) return;

    if (pendingCount === undefined || pendingCount === null) {
        // Loading
        dot.className = 'sync-status-dot';
        dot.style.display = 'none';
        detail.textContent = 'Loading…';
        return;
    }

    if (pendingCount === 0) {
        // All caught up
        dot.className = 'sync-status-dot is-ok';
        dot.style.display = 'inline-block';
        detail.textContent = 'All caught up ✅';
        icon.textContent = '✅';
    } else {
        // Pending days
        var dayLabel = pendingCount === 1 ? 'day' : 'days';
        dot.className = 'sync-status-dot is-pending';
        dot.style.display = 'inline-block';
        detail.textContent = pendingCount + ' ' + dayLabel + ' pending';
        icon.textContent = '🔄';
    }

    // Also update the backfill panel badge indirectly — the backfill panel
    // will auto-update via its own loadBackfillPanel(). We just mirror.
};

/* ── Scroll to sync bar and auto-expand ─────────────── */
window.scrollToSync = function () {
    window.openSyncBar();
    var bar = document.getElementById('sync-collapsible-bar');
    if (bar) {
        bar.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
};

/* ── Watch backfill panel and mirror status to collapsed bar ── */
function _watchBackfillStatus() {
    // Poll periodically to mirror status from the backfill panel elements
    var check = function () {
        var badge = document.getElementById('bf-badge');
        if (!badge) return;

        var badgeText = badge.textContent || '';
        var dot = document.getElementById('sync-bar-dot');
        var detail = document.getElementById('sync-bar-detail');
        var icon = document.getElementById('sync-bar-icon');
        if (!dot || !detail || !icon) return;

        if (badgeText.indexOf('All caught up') !== -1 || badgeText.indexOf('✅') !== -1) {
            dot.className = 'sync-status-dot is-ok';
            detail.textContent = 'All caught up ✅';
            icon.textContent = '✅';
        } else if (badgeText.indexOf('days pending') !== -1) {
            dot.className = 'sync-status-dot is-pending';
            detail.textContent = badgeText.replace(/<[^>]*>/g, '').trim();
            icon.textContent = '🔄';
        } else if (badgeText.indexOf('Failed') !== -1 || badgeText.indexOf('Error') !== -1) {
            dot.className = 'sync-status-dot is-pending';
            detail.textContent = '⚠ Sync error';
            icon.textContent = '⚠️';
        } else if (badgeText.indexOf('Syncing') !== -1 || badgeText.indexOf('Checking') !== -1) {
            dot.className = 'sync-status-dot is-pending';
            detail.textContent = badgeText.replace(/<[^>]*>/g, '').trim();
            icon.textContent = '🔄';
        } else {
            // Pending badge with count
            var match = badgeText.match(/(\d+)\s*days?/);
            if (match) {
                var n = parseInt(match[1], 10);
                var dayLabel = n === 1 ? 'day' : 'days';
                dot.className = 'sync-status-dot is-pending';
                detail.textContent = n + ' ' + dayLabel + ' pending';
                icon.textContent = '🔄';
            }
        }
    };

    // Check on interval
    setInterval(check, 3000);
    // Also check after short delay
    setTimeout(check, 500);
    setTimeout(check, 1500);
}

/* ── Restore saved state on load ────────────────────── */
document.addEventListener('DOMContentLoaded', function () {
    var stored = null;
    try { stored = localStorage.getItem('sync-bar-expanded'); } catch(e) {}
    if (stored === '1') {
        // Auto-expand on load if previously expanded
        setTimeout(function () {
            window.openSyncBar();
        }, 300);
    }

    _watchBackfillStatus();
});

})();
