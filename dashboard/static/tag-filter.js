/**
 * Multi-Select Tag Filter — Reusable Component
 *
 * Features:
 *   - Draft/applied state separation: tag toggles update local draft only.
 *   - Apply button commits the draft to the URL (navigates once).
 *   - Closing the dropdown reverts the draft back to the last applied state.
 *   - Pills show draft state; ✕ removes from draft; clicking Apply commits.
 *   - Clear All clears draft and auto-applies (explicit reset action).
 *
 * Usage:
 *   1. Add <div class="tag-filter" data-tag-filter> to your template
 *   2. Call TagFilter.init(element) or TagFilter.initAll()
 */

const TAG_COLORS = [
    'tag-color-0', 'tag-color-1', 'tag-color-2', 'tag-color-3',
    'tag-color-4', 'tag-color-5', 'tag-color-6', 'tag-color-7',
];

const TagFilter = {
    instances: new Map(),

    /**
     * Initialize a tag filter on a container element.
     * @param {HTMLElement} el - Container with data-tag-filter attribute
     */
    init(el) {
        const key = el.dataset.tagFilter || 'default';
        const filter = new TagFilterInstance(el, key);
        this.instances.set(key, filter);
        return filter;
    },

    /** Initialize all tag filters on the page. */
    initAll() {
        document.querySelectorAll('[data-tag-filter]').forEach(el => this.init(el));
    },
};

class TagFilterInstance {
    constructor(container, key) {
        this.container = container;
        this.key = key;
        this.selected = new Set();     // draft — user's in-progress selection
        this.appliedTags = new Set();  // last committed state (from URL)
        this.allTags = [];
        this.filteredTags = [];
        this.open = false;
        this.highlightedIndex = -1;
        this._build();
        this._loadFromUrl();
        this._fetchTags();
        this._bindEvents();
    }

    // ── Build DOM ─────────────────────────────────────────

    _build() {
        this.container.innerHTML = `
            <div class="tag-filter-trigger" tabindex="0" role="button" aria-haspopup="listbox">
                <span style="color: var(--text-muted); font-size: 14px; line-height: 1;">🏷️</span>
                <span class="label">Tags:</span>
                <div class="tag-pills" data-pills>
                    <span class="tag-filter-all">All</span>
                </div>
                <span class="chevron">▼</span>
            </div>
            <div class="tag-filter-dropdown" data-dropdown>
                <div class="tag-filter-search-wrap">
                    <span class="search-icon">🔍</span>
                    <input class="tag-filter-search" type="text" placeholder="Search tags…" data-search>
                </div>
                <div class="tag-filter-options" data-options>
                    <div class="tag-filter-no-results">⏳ Loading tags…</div>
                </div>
                <div class="tag-filter-actions">
                    <span class="tag-count-badge" data-count>0 selected</span>
                    <div style="display: flex; gap: 6px;">
                        <button class="action-btn clear" data-clear>Clear All</button>
                        <button class="action-btn apply" data-apply>Apply</button>
                    </div>
                </div>
            </div>
        `;

        this.trigger = this.container.querySelector('.tag-filter-trigger');
        this.dropdown = this.container.querySelector('[data-dropdown]');
        this.searchInput = this.container.querySelector('[data-search]');
        this.optionsEl = this.container.querySelector('[data-options]');
        this.pillsEl = this.container.querySelector('[data-pills]');
        this.countEl = this.container.querySelector('[data-count]');
        this.clearBtn = this.container.querySelector('[data-clear]');
        this.applyBtn = this.container.querySelector('[data-apply]');
    }

    // ── Fetch tags from API ───────────────────────────────

    async _fetchTags() {
        try {
            const res = await fetch('/tags/data' + window.location.search);
            const data = await res.json();
            this.allTags = data.tags || [];

            // Remove any tags that are no longer in the API response
            const validTags = new Set(this.allTags.map(t => t.tag));
            for (const t of this.selected) {
                if (!validTags.has(t)) {
                    this.selected.delete(t);
                }
            }
            for (const t of this.appliedTags) {
                if (!validTags.has(t)) {
                    this.appliedTags.delete(t);
                }
            }

            this._renderOptions();
            this._updatePills();
        } catch (err) {
            console.error('TagFilter: failed to fetch tags', err);
            this.optionsEl.innerHTML = '<div class="tag-filter-no-results">❌ Could not load tags</div>';
        }
    }

    // ── Render options ────────────────────────────────────

    _renderOptions() {
        const query = (this.searchInput.value || '').toLowerCase().trim();
        this.filteredTags = this.allTags.filter(t =>
            t.tag.toLowerCase().includes(query)
        );

        if (this.filteredTags.length === 0) {
            this.optionsEl.innerHTML = `<div class="tag-filter-no-results"><div class="icon">🔍</div>No tags match</div>`;
            return;
        }

        this.optionsEl.innerHTML = this.filteredTags.map((t, i) => {
            const checked = this.selected.has(t.tag) ? 'checked' : '';
            const isUnlabeled = t.tag.toLowerCase() === 'unlabeled';
            const colorIdx = isUnlabeled ? 7 : this._colorIndex(t.tag);
            const colorClass = TAG_COLORS[colorIdx];
            const countClass = isUnlabeled ? ' tag-count-muted' : '';
            return `<div class="tag-filter-option" data-tag="${escapeHtml(t.tag)}" data-index="${i}">
                <div class="checkbox-wrap">
                    <input type="checkbox" ${checked}>
                </div>
                <span class="tag-name"><span class="tag-pill ${colorClass}" style="padding:0 6px;font-size:11px;">${escapeHtml(t.tag)}</span></span>
                <span class="tag-count${countClass}">${t.count}</span>
            </div>`;
        }).join('');

        this._renderHighlighted();
    }

    // ── Update pills in trigger ───────────────────────────

    _updatePills() {
        if (this.selected.size === 0) {
            this.pillsEl.innerHTML = '<span class="tag-filter-all">All</span>';
            this.countEl.textContent = '0 selected';
            return;
        }

        const arr = Array.from(this.selected);
        let html = '';
        const maxVisible = 3;

        for (let i = 0; i < Math.min(arr.length, maxVisible); i++) {
            const colorIdx = this._colorIndex(arr[i]);
            const colorClass = TAG_COLORS[colorIdx];
            html += `<span class="tag-pill ${colorClass}" data-tag-pill="${escapeHtml(arr[i])}">
                ${escapeHtml(arr[i])}
                <span class="tag-pill-remove" data-remove="${escapeHtml(arr[i])}">✕</span>
            </span>`;
        }

        if (arr.length > maxVisible) {
            html += `<span class="tag-pill-more">+${arr.length - maxVisible} more</span>`;
        }

        if (arr.length > 0) {
            html += `<span class="tag-filter-count-badge-pill">${arr.length}</span>`;
        }

        this.pillsEl.innerHTML = html;
        if (this.allTags.length > 0) {
            this.countEl.textContent = `${arr.length} of ${this.allTags.length} selected`;
        } else {
            this.countEl.textContent = `${arr.length > 0 ? arr.length : '0'} selected`;
        }

        // Bind remove buttons on pills — ✕ click immediately removes and refreshes
        this.pillsEl.querySelectorAll('[data-remove]').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                this._applyRemove(btn.dataset.remove);
            });
        });
    }

    // ── Toggle a tag in the draft set ──────────────────────
    // No page navigation — only updates local state.
    _toggle(tag) {
        if (this.selected.has(tag)) {
            this.selected.delete(tag);
        } else {
            this.selected.add(tag);
        }
        this._renderOptions();
        this._updatePills();
    }

    // ── ApplyRemove — remove tag and immediately refresh ─

    _applyRemove(tag) {
        this.selected.delete(tag);
        this.appliedTags.delete(tag);
        this._renderOptions();
        this._updatePills();
        // Navigate / reload immediately
        try {
            const url = this._buildUrl();
            const currentHref = window.location.href;
            const builtHref = url ? new URL(url, window.location.origin).href : null;
            if (!url || (builtHref && builtHref === new URL(currentHref).href)) {
                window.location.reload();
                return;
            }
            window.location.href = url;
        } catch (e) {
            window.location.reload();
        }
    }

    // ── Apply — commit draft to applied state and navigate ─

    _apply() {
        // Commit the draft as the new applied state
        this.appliedTags = new Set(this.selected);

        // Try to close the dropdown cleanly, but don't let errors block navigation
        try {
            this._syncUrl();
            this._close();
        } catch (e) {
            console && console.warn('[TagFilter] Close error (non-fatal):', e);
        }

        // Always build URL and navigate, even if close/sync failed
        try {
            const url = this._buildUrl();
            const currentHref = window.location.href;
            // Compare full absolute URLs to detect real changes
            const builtHref = url ? new URL(url, window.location.origin).href : null;
            if (!url || (builtHref && builtHref === new URL(currentHref).href)) {
                // URL hasn't changed meaningfully — force reload
                console && console.log('[TagFilter] URL unchanged, forcing reload');
                window.location.reload();
                return;
            }
            console && console.log('[TagFilter] Apply →', url, 'applied=', Array.from(this.appliedTags));
            window.location.href = url;
        } catch (e2) {
            console && console.error('[TagFilter] BuildUrl/nav error:', e2);
            // Last resort: hard reload
            window.location.reload();
        }
    }

    // ── Clear all ─────────────────────────────────────────

    _clear() {
        this.selected.clear();
        this.highlightedIndex = 0;
        this._renderOptions();
        this._updatePills();
        // Clear All is an explicit reset action — apply immediately
        this._apply();
    }

    // ── URL sync ──────────────────────────────────────────

    _loadFromUrl() {
        // Strip leading '?' if present — URLSearchParams handles it but be safe
        const searchStr = (window.location.search || '').replace(/^\?/, '');
        const params = new URLSearchParams(searchStr);
        const tags = params.get('tags');
        if (tags) {
            tags.split(',').forEach(t => {
                const trimmed = t.trim();
                if (trimmed) this.selected.add(trimmed);
            });
            // Both draft and applied start from the URL
            this.appliedTags = new Set(this.selected);
            console && console.log('[TagFilter] Loaded tags from URL:', Array.from(this.selected));
            // Update pills immediately so they show selected tags even before
            // the async _fetchTags() completes.
            this._updatePills();
        }
    }

    _syncUrl() {
        // Just update the pill/count rendering — actual navigation happens on Apply
    }

    _buildUrl() {
        // Build URL manually to avoid any URLSearchParams encoding quirks
        const currentParams = new URLSearchParams(window.location.search);
        const newParams = new URLSearchParams();

        // Copy all current params except 'tags'
        for (const [k, v] of currentParams.entries()) {
            if (k !== 'tags') {
                newParams.append(k, v);
            }
        }

        // Set tags param if any are selected
        if (this.selected.size > 0) {
            // Join tags with comma — URLSearchParams will encode commas as %2C
            // Flask splits on comma after decoding, so this round-trips correctly
            newParams.set('tags', Array.from(this.selected).join(','));
        }

        const qs = newParams.toString();
        const base = window.location.pathname;
        return qs ? (base + '?' + qs) : base;
    }

    // ── Color system ──────────────────────────────────────

    _colorIndex(tag) {
        if (tag.toLowerCase() === 'unlabeled') return 7; // always gray
        let hash = 0;
        for (let i = 0; i < tag.length; i++) {
            hash = ((hash << 5) - hash) + tag.charCodeAt(i);
            hash |= 0;
        }
        return Math.abs(hash) % TAG_COLORS.length;
    }

    // ── Open / Close ──────────────────────────────────────

    _open() {
        this.open = true;
        this.highlightedIndex = 0;
        this.dropdown.classList.add('open');
        this.trigger.classList.add('open');
        this.searchInput.focus();
    }

    _close() {
        // Revert draft back to the last applied state.
        // This means closing without clicking Apply discards in-progress changes.
        this.selected = new Set(this.appliedTags);
        this.open = false;
        this.dropdown.classList.remove('open');
        this.trigger.classList.remove('open');
        this.searchInput.value = '';
        this._renderOptions();
        this._updatePills();
    }

    _toggleOpen() {
        if (this.open) this._close();
        else this._open();
    }

    // ── Update count badge ────────────────────────────────

    _renderHighlighted() {
        if (this.highlightedIndex < 0 || this.filteredTags.length === 0) return;
        const maxIndex = this.filteredTags.length - 1;
        if (this.highlightedIndex > maxIndex) {
            this.highlightedIndex = maxIndex;
        }
        this.optionsEl.querySelectorAll('.tag-filter-option').forEach((el, i) => {
            el.classList.toggle('highlighted', i === this.highlightedIndex);
        });
    }

    _scrollIntoView() {
        const el = this.optionsEl.querySelector(`.tag-filter-option[data-index="${this.highlightedIndex}"]`);
        if (el) {
            el.scrollIntoView({ block: 'nearest' });
        }
    }

    // ── Events ────────────────────────────────────────────

    _bindEvents() {
        // Toggle on trigger click
        this.trigger.addEventListener('click', (e) => {
            if (e.target.closest('[data-remove]')) return;
            this._toggleOpen();
        });

        // Search
        this.searchInput.addEventListener('input', () => {
            this.highlightedIndex = 0;
            this._renderOptions();
        });

        // Option clicks (delegated) — toggle in draft only, no navigation
        this.optionsEl.addEventListener('click', (e) => {
            const option = e.target.closest('.tag-filter-option');
            if (!option) return;
            const tag = option.dataset.tag;
            if (tag) this._toggle(tag);
        });

        // Clear button — resets draft and applies immediately
        this.clearBtn.addEventListener('click', () => this._clear());

        // Apply button — commits draft and navigates
        this.applyBtn.addEventListener('click', () => this._apply());

        // Close on click outside — reverts in-progress changes back to applied state.
        // Use composedPath so it works even if _renderOptions() detaches the
        // click-target from the DOM mid-propagation.
        document.addEventListener('click', (e) => {
            if (this.open) {
                const clickWasInside = e.composedPath
                    ? e.composedPath().includes(this.container)
                    : this.container.contains(e.target);
                if (!clickWasInside) {
                    this._close();
                }
            }
        });

        // Keyboard
        this.container.addEventListener('keydown', (e) => {
            if (!this.open) return;

            if (e.key === 'Escape') {
                // Escape = cancel — reverts to last applied state
                this._close();
                e.preventDefault();
                return;
            }

            if (e.key === 'Enter') {
                // Enter = apply — commits draft and navigates
                this._apply();
                e.preventDefault();
                return;
            }

            if (e.key === 'ArrowDown') {
                e.preventDefault();
                if (this.filteredTags.length === 0) return;
                this.highlightedIndex = Math.min(this.highlightedIndex + 1, this.filteredTags.length - 1);
                this._renderHighlighted();
                this._scrollIntoView();
                return;
            }

            if (e.key === 'ArrowUp') {
                e.preventDefault();
                if (this.filteredTags.length === 0) return;
                this.highlightedIndex = Math.max(this.highlightedIndex - 1, 0);
                this._renderHighlighted();
                this._scrollIntoView();
                return;
            }

            if (e.key === ' ' || e.key === 'Spacebar') {
                e.preventDefault();
                if (this.highlightedIndex < 0 || this.highlightedIndex >= this.filteredTags.length) return;
                const tag = this.filteredTags[this.highlightedIndex].tag;
                if (tag) this._toggle(tag);
                return;
            }
        });
    }
}

// ── Utility ──────────────────────────────────────────────

function escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

// Auto-init on DOM ready
document.addEventListener('DOMContentLoaded', () => TagFilter.initAll());
