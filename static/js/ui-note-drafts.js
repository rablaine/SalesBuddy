/** Recovery copies are independent of saved notes and never overwrite another window. */
(function(root) {
    'use strict';
    const PREFIX = 'salesbuddy_draft_';
    function fingerprint(data, normalizeContent = value => value) {
        const ids = values => (values || []).map(value => String(value.id ?? value)).sort();
        return JSON.stringify({
            customer: String(data.customer_id || ''), project: String(data.project_id || ''),
            meeting: String(data.from_meeting || ''),
            date: data.call_date || '', time: data.call_time || '',
            content: normalizeContent(data.content || ''),
            topics: ids(data.topics), pendingTopics: [...(data.pendingTopics || [])].sort(),
            partners: ids(data.partners),
            attendees: (data.attendees || []).map(a => JSON.stringify([
                a.type, String(a.id || ''), a.type === 'external' ? a.name || '' : '',
                a.type === 'external' ? a.email || '' : '',
            ])).sort(),
            milestones: ids(data.milestones), opportunities: ids(data.opportunities),
            engagements: ids(data.engagementIds), task: data.task || {},
            inline: (data.inlineEngagements || []).filter(e => e.dirty).map(e => ({
                id: e.id, status: e.status, technical_problem: e.technical_problem,
                business_impact: e.business_impact, solution_resources: e.solution_resources,
                estimated_acr: e.estimated_acr, target_date: e.target_date,
            })).sort((a, b) => String(a.id).localeCompare(String(b.id))),
            actionItems: ids((data.engTaskBadges || []).map(b => b.taskId || b.href)),
            pendingTask: data.pendingTaskSuggestion || null,
            taskOutcome: data.taskOutcome || '',
        });
    }
    function createStore({storage, key, uuid, onFork = () => {}}) {
        let expected = null;
        let owned = false;
        let lastSignature = null;
        let requestToken = null;
        function read() {
            const raw = storage.getItem(key);
            return raw ? JSON.parse(raw) : null;
        }
        function claim() { expected = storage.getItem(key); }
        function write(data) {
            const current = storage.getItem(key);
            const previousKey = key;
            const previousRaw = expected;
            if (current !== expected || (current !== null && !owned)) {
                key = PREFIX + 'recovery_' + uuid();
                expected = null;
                onFork(key, current !== previousRaw ? 'conflict' : 'resume');
            }
            const record = {...data, revision: uuid()};
            const raw = JSON.stringify(record);
            storage.setItem(key, raw);
            expected = raw;
            owned = true;
            if (previousKey !== key && previousRaw !== null &&
                storage.getItem(previousKey) === previousRaw) {
                storage.removeItem(previousKey);
            }
            return record;
        }
        function remove(revision) {
            const raw = storage.getItem(key);
            if (!raw) return true;
            if (raw !== expected || (revision && JSON.parse(raw).revision !== revision)) return false;
            storage.removeItem(key);
            expected = null;
            return true;
        }
        function token(signature, stable = false) {
            if (!requestToken || (!stable && lastSignature !== signature)) {
                requestToken = uuid();
            }
            lastSignature = signature;
            return requestToken;
        }
        function restoreToken(record) {
            lastSignature = record.saveSignature || null;
            requestToken = record.saveToken || null;
        }
        return {read, claim, write, remove, token, restoreToken, get key() { return key; }};
    }
    const api = {PREFIX, fingerprint, createStore};
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    else root.SBNoteDrafts = Object.freeze(api);
    if (typeof document !== 'undefined') document.addEventListener('DOMContentLoaded', () => {
        const notice = document.querySelector('[data-note-recovery]');
        if (!notice) return;
        try {
            const drafts = Object.keys(localStorage).filter(key => key.startsWith(PREFIX))
                .map(key => ({key, ...JSON.parse(localStorage.getItem(key))}))
                .filter(draft => String(draft.note_id) === notice.dataset.noteRecovery)
                .sort((a, b) => b.timestamp - a.timestamp);
            if (!drafts.length) return;
            notice.hidden = false;
            notice.querySelector('span').textContent =
                'Unfinished edits from ' + new Date(drafts[0].timestamp).toLocaleString() + '.';
            notice.querySelector('a').href += '?draft=' + encodeURIComponent(drafts[0].key);
        } catch (error) {
            console.error('Unable to inspect note recovery:', error);
            notice.hidden = false;
            notice.querySelector('span').textContent = 'Draft recovery could not be checked. Open Home > Drafts.';
        }
    });
})(typeof window === 'undefined' ? globalThis : window);
