/**
 * Revenue Bucket Filter
 *
 * Shared client-side filter for revenue alerts across all pages.
 * Stores selected buckets in localStorage so the filter persists
 * across the dashboard, seller alerts, and homepage.
 *
 * Renders a searchable picklist with checkboxes (like MSXi filters).
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
    var _popoverInstance = null;
    var _btn = null;

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
        updateBadge();
        if (onFilterChange) onFilterChange(visibleCount);
    }

    function updateBadge() {
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
    }

    function buildContent() {
        var allSelected = allBuckets.every(function(b) { return selectedBuckets.has(b); });
        var c = '<div class="bucket-filter-panel" style="min-width:220px;">';
        // Search box
        c += '<div class="px-2 pt-2 pb-1">';
        c += '<input type="text" class="form-control form-control-sm" id="bucketSearchInput" placeholder="Search" autocomplete="off">';
        c += '</div>';
        // Select all row
        c += '<div class="px-2 py-1 border-bottom">';
        c += '<label class="form-check d-flex align-items-center gap-2 mb-0" style="cursor:pointer;">';
        c += '<input class="form-check-input mt-0" type="checkbox" id="bucketSelectAll"' + (allSelected ? ' checked' : '') + '>';
        c += '<span class="small fw-semibold">Select all</span>';
        c += '</label>';
        c += '</div>';
        // Bucket list
        c += '<div style="max-height:240px;overflow-y:auto;" id="bucketListContainer">';
        allBuckets.forEach(function(b) {
            var checked = selectedBuckets.has(b) ? ' checked' : '';
            var safeId = 'bf_' + b.replace(/[^a-zA-Z0-9]/g, '_');
            c += '<label class="bucket-filter-item form-check d-flex align-items-center gap-2 px-2 py-1 mb-0" data-bucket-name="' + b.toLowerCase() + '" style="cursor:pointer;">';
            c += '<input class="form-check-input mt-0 bucket-check" type="checkbox" value="' + b + '" id="' + safeId + '"' + checked + '>';
            c += '<span class="small">' + b + '</span>';
            c += '</label>';
        });
        c += '</div>';
        c += '</div>';
        return c;
    }

    function wirePopoverEvents() {
        // Find elements inside the active popover (not just anywhere in document)
        var popover = document.querySelector('.popover');
        if (!popover) return;
        var search = popover.querySelector('#bucketSearchInput');
        var selectAllCb = popover.querySelector('#bucketSelectAll');
        if (!search) return;

        // Search filtering
        search.addEventListener('input', function() {
            var term = this.value.toLowerCase().trim();
            popover.querySelectorAll('.bucket-filter-item').forEach(function(item) {
                var name = item.dataset.bucketName || '';
                if (!term || name.includes(term)) {
                    item.classList.remove('d-none');
                } else {
                    item.classList.add('d-none');
                }
            });
        });

        // Select all toggle
        if (selectAllCb) {
            selectAllCb.addEventListener('change', function() {
                if (this.checked) {
                    RevenueBucketFilter.selectAll();
                } else {
                    RevenueBucketFilter.selectNone();
                }
            });
        }

        // Individual checkbox changes
        popover.querySelectorAll('.bucket-check').forEach(function(cb) {
            cb.addEventListener('change', function() {
                if (this.checked) {
                    selectedBuckets.add(this.value);
                } else {
                    selectedBuckets.delete(this.value);
                }
                saveSelection();
                applyFilter();
                // Sync "Select all" checkbox
                var allCb = popover.querySelector('#bucketSelectAll');
                if (allCb) {
                    allCb.checked = allBuckets.every(function(b) { return selectedBuckets.has(b); });
                }
            });
        });

        // Auto-focus search
        setTimeout(function() { search.focus(); }, 50);
    }

    function renderFilterUI(containerId) {
        var container = document.getElementById(containerId);
        if (!container || allBuckets.length === 0) return;

        _btn = document.createElement('button');
        _btn.className = 'btn btn-sm btn-outline-secondary';
        _btn.type = 'button';
        _btn.setAttribute('data-bs-toggle', 'popover');
        _btn.setAttribute('data-bs-placement', 'bottom');
        _btn.setAttribute('data-bs-trigger', 'click');
        _btn.innerHTML = '<i class="bi bi-funnel"></i> Buckets <span id="bucketFilterCount" class="badge bg-info ms-1 d-none"></span>';
        container.appendChild(_btn);

        function initPopover() {
            if (typeof bootstrap !== 'undefined' && bootstrap.Popover) {
                _popoverInstance = new bootstrap.Popover(_btn, {
                    sanitize: false,
                    html: true,
                    content: buildContent
                });
                // Wire events after popover is shown
                _btn.addEventListener('shown.bs.popover', wirePopoverEvents);
                // Rebuild content each time the popover opens
                _btn.addEventListener('show.bs.popover', function() {
                    _popoverInstance._config.content = buildContent;
                });
                // Close on outside click
                document.addEventListener('click', function(e) {
                    if (!_btn.contains(e.target) && !document.querySelector('.popover')?.contains(e.target)) {
                        _popoverInstance.hide();
                    }
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
            var allCb = document.getElementById('bucketSelectAll');
            if (allCb) allCb.checked = true;
            applyFilter();
        },
        selectNone: function() {
            selectedBuckets.clear();
            saveSelection();
            document.querySelectorAll('.bucket-check').forEach(function(cb) { cb.checked = false; });
            var allCb = document.getElementById('bucketSelectAll');
            if (allCb) allCb.checked = false;
            applyFilter();
        },
        getSelected: function() {
            return Array.from(selectedBuckets);
        },
        isSelected: function(bucket) {
            return selectedBuckets.has(bucket);
        },
        getAllBuckets: function() {
            return allBuckets.slice();
        }
    };
})();
