/**
 * Revenue Bucket Filter
 *
 * Shared client-side filter for revenue alerts across all pages.
 * Stores selected buckets in localStorage so the filter persists
 * across the dashboard, seller alerts, and homepage.
 *
 * Usage:
 *   1. Include this script on any page with revenue alerts
 *   2. Add a container with id="bucketFilterContainer" where the filter button should appear
 *   3. Add data-bucket="BucketName" to each filterable element (row or card)
 *   4. Call RevenueBucketFilter.init() after the page loads
 */
var RevenueBucketFilter = (function() {
    var STORAGE_KEY = 'salesbuddy_revenue_bucket_filter';
    var allBuckets = [];
    var selectedBuckets = new Set();
    var onFilterChange = null;

    function loadSelection() {
        try {
            var saved = JSON.parse(localStorage.getItem(STORAGE_KEY));
            if (saved && Array.isArray(saved) && saved.length > 0) {
                selectedBuckets = new Set(saved);
                return;
            }
        } catch(e) {}
        // Default: all buckets selected
        selectedBuckets = new Set(allBuckets);
    }

    function saveSelection() {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(Array.from(selectedBuckets)));
    }

    function discoverBuckets() {
        var bucketSet = new Set();
        document.querySelectorAll('[data-bucket]').forEach(function(el) {
            var b = el.dataset.bucket;
            if (b) bucketSet.add(b);
        });
        allBuckets = Array.from(bucketSet).sort();
    }

    function applyFilter() {
        var visibleCount = 0;
        document.querySelectorAll('[data-bucket]').forEach(function(el) {
            var show = selectedBuckets.has(el.dataset.bucket);
            el.style.display = show ? '' : 'none';
            if (show) visibleCount++;
        });
        // Update any badge counts
        var countEl = document.getElementById('bucketFilterCount');
        if (countEl) {
            var total = allBuckets.length;
            if (selectedBuckets.size < total) {
                countEl.textContent = selectedBuckets.size + '/' + total;
                countEl.classList.remove('d-none');
            } else {
                countEl.classList.add('d-none');
            }
        }
        if (onFilterChange) onFilterChange(visibleCount);
    }

    function renderFilterUI(containerId) {
        var container = document.getElementById(containerId);
        if (!container || allBuckets.length === 0) return;

        var btn = document.createElement('button');
        btn.className = 'btn btn-sm btn-outline-secondary';
        btn.type = 'button';
        btn.setAttribute('data-bs-toggle', 'popover');
        btn.setAttribute('data-bs-placement', 'bottom');
        btn.setAttribute('data-bs-trigger', 'click');
        btn.innerHTML = '<i class="bi bi-funnel"></i> Buckets <span id="bucketFilterCount" class="badge bg-info ms-1 d-none"></span>';
        container.appendChild(btn);

        function buildContent() {
            var c = '<div style="max-height:300px;overflow-y:auto;min-width:200px;">';
            c += '<div class="mb-2"><button class="btn btn-sm btn-link p-0 me-2" onclick="RevenueBucketFilter.selectAll()">All</button>';
            c += '<button class="btn btn-sm btn-link p-0" onclick="RevenueBucketFilter.selectNone()">None</button></div>';
            allBuckets.forEach(function(b) {
                var checked = selectedBuckets.has(b) ? ' checked' : '';
                c += '<div class="form-check">';
                c += '<input class="form-check-input bucket-check" type="checkbox" value="' + b + '" id="bf_' + b.replace(/\s/g, '_') + '"' + checked + ' onchange="RevenueBucketFilter.toggle(\'' + b.replace(/'/g, "\\'") + '\')">';
                c += '<label class="form-check-label small" for="bf_' + b.replace(/\s/g, '_') + '">' + b + '</label>';
                c += '</div>';
            });
            c += '</div>';
            return c;
        }

        // Defer popover init until Bootstrap is available
        function initPopover() {
            if (typeof bootstrap !== 'undefined' && bootstrap.Popover) {
                var pop = new bootstrap.Popover(btn, {
                    sanitize: false,
                    html: true,
                    content: buildContent
                });
                // Close on outside click
                document.addEventListener('click', function(e) {
                    if (!btn.contains(e.target) && !document.querySelector('.popover')?.contains(e.target)) {
                        pop.hide();
                    }
                });
                // Rebuild content each time the popover opens
                btn.addEventListener('show.bs.popover', function() {
                    pop._config.content = buildContent;
                });
            } else {
                setTimeout(initPopover, 50);
            }
        }
        initPopover();
    }

    return {
        init: function(opts) {
            opts = opts || {};
            onFilterChange = opts.onFilterChange || null;
            discoverBuckets();
            loadSelection();
            // Remove buckets from selection that no longer exist in data
            selectedBuckets.forEach(function(b) {
                if (allBuckets.indexOf(b) === -1) selectedBuckets.delete(b);
            });
            if (opts.containerId) renderFilterUI(opts.containerId);
            applyFilter();
        },
        toggle: function(bucket) {
            if (selectedBuckets.has(bucket)) {
                selectedBuckets.delete(bucket);
            } else {
                selectedBuckets.add(bucket);
            }
            saveSelection();
            applyFilter();
        },
        selectAll: function() {
            selectedBuckets = new Set(allBuckets);
            saveSelection();
            document.querySelectorAll('.bucket-check').forEach(function(cb) { cb.checked = true; });
            applyFilter();
        },
        selectNone: function() {
            selectedBuckets.clear();
            saveSelection();
            document.querySelectorAll('.bucket-check').forEach(function(cb) { cb.checked = false; });
            applyFilter();
        },
        getSelected: function() {
            return Array.from(selectedBuckets);
        },
        isSelected: function(bucket) {
            return selectedBuckets.has(bucket);
        }
    };
})();
