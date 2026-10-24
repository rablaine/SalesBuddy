/** Native Partner detail, contact drafts, photos and explicit meeting review writes. */
((window) => {
    'use strict';
    function normalizeDomain(value) {
        return value.trim().toLowerCase().replace(/^https?:\/\//, '')
            .replace(/^www\./, '').split('/')[0];
    }
    function selectedResults(data, overview, specialties, contacts) {
        return {overview: overview?.trim() || null,
            specialties: specialties.map(index => data.specialties[index].name),
            contacts: contacts.map(index => data.contacts[index])};
    }
    async function request(fetch, url, method, payload) {
        const response = await fetch(url, {method,
            ...(payload === undefined ? {} : {headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)})});
        const data = await response.json();
        if (!response.ok || data.error || data.success === false) {
            throw new Error(data.error || `Request failed (${response.status}).`);
        }
        return data;
    }
    function validateHistory(value, id) {
        if (!value || value.id !== id || !Array.isArray(value.scroll)
            || value.scroll.length !== 3 || value.scroll.some(pair =>
                !Array.isArray(pair) || pair.length !== 2
                || pair.some(n => !Number.isFinite(n) || n < 0))
            || (value.draft && (typeof value.draft.action !== 'string'
                || !new RegExp(`^/partners/${id}/contacts/(new|\\d+/edit)$`)
                    .test(value.draft.action)
                || ['contact_name', 'contact_title', 'contact_email'].some(
                    name => typeof value.draft.values[name] !== 'string')))) {
            throw new Error('Invalid Partner detail context.');
        }
        return value;
    }
    if (typeof module !== 'undefined') module.exports = {
        normalizeDomain, selectedResults, request, validateHistory};
    if (!window.document) return;
    function mount() {
    const document = window.document;
    const root = document.querySelector('[data-partner-detail]');
    if (!root) return;
    const partner = JSON.parse(document.getElementById('partnerDetailData').textContent);
    const id = partner.id;
    const get = name => document.getElementById(name);
    const contactForm = get('contactForm');
    const contactDialog = get('addContactModal');
    const panes = ['.part-overview', '.part-contacts', '.part-notes']
        .map(selector => root.querySelector(selector));
    let pending = false, submitting = false, baseline = '', scrapeData = null;
    let photoButton = null, discardAction = null, restoring = false;
    let notePreview = null;
    function values() {
        return {contact_name: get('contact_name').value, contact_title: get('contact_title').value,
            contact_email: get('contact_email').value,
            is_primary: get('is_primary').checked ? 'on' : ''};
    }
    function dirty() { return contactDialog.open && JSON.stringify(values()) !== baseline; }
    function notice(error, target = 'partnerNotice') {
        window.console.error(error);
        get(target).textContent = error.message;
        get(target).hidden = false;
    }
    function lock(value, label = 'Saving...') {
        pending = value;
        root.querySelectorAll('button,input,textarea').forEach(node => {
            if (submitting && node.matches('input,textarea')) return;
            if (value) { node.dataset.wasDisabled = String(node.disabled); node.disabled = true; }
            else if (node.dataset.wasDisabled !== undefined) {
                node.disabled = node.dataset.wasDisabled === 'true';
                delete node.dataset.wasDisabled;
            }
        });
        get('partnerDetailState').textContent = value ? label : 'All linked notes · Manual contact save';
    }
    function save() {
        if (restoring) return;
        try {
            window.history.replaceState({...window.history.state, sbPartnerDetail: {
                id, scroll: panes.map(pane => [pane.scrollTop, pane.scrollLeft]),
                draft: contactDialog.open ? {action: contactForm.getAttribute('action'),
                    title: get('contactModalTitle').textContent, values: values(), baseline} : null,
            }}, '');
        } catch (error) { notice(error); }
    }
    function openContact(contact, draft) {
        contactForm.action = draft?.action || `/partners/${id}/contacts/${contact ? contact.id + '/edit' : 'new'}`;
        get('contactModalTitle').textContent = draft?.title || (contact ? 'Edit Contact' : 'Add Contact');
        get('contactSubmitBtn').textContent = contact || draft?.action.endsWith('/edit')
            ? 'Save Changes' : 'Add Contact';
        const input = draft?.values || {contact_name: contact?.name || '',
            contact_title: contact?.title || '', contact_email: contact?.email || '',
            is_primary: contact?.is_primary ? 'on' : ''};
        ['contact_name', 'contact_title', 'contact_email'].forEach(name => {
            get(name).value = input[name];
        });
        get('is_primary').checked = input.is_primary === 'on';
        baseline = draft?.baseline || JSON.stringify(values());
        get('contactFailure').hidden = true;
        window.SBDialog.open(contactDialog);
    }
    function restore() {
        restoring = true;
        try {
            window.SBTableSort.mount(root);
            const saved = window.history.state?.sbPartnerDetail;
            if (saved?.id === id) {
                validateHistory(saved, id);
                panes.forEach((pane, index) => {
                    [pane.scrollTop, pane.scrollLeft] = saved.scroll[index];
                });
                if (saved.draft && !partner.failure) openContact(null, saved.draft);
            }
        } catch (error) { notice(error); }
        restoring = false;
    }
    function discard(action) {
        discardAction = action;
        window.SBDialog.open('partnerDiscardDialog');
    }
    contactDialog.addEventListener('sb:dialog-before-close', event => {
        if (pending) event.preventDefault();
        else if (dirty() && event.detail.reason !== 'discard') {
            event.preventDefault();
            discard(() => window.SBDialog.close(contactDialog, 'discard'));
        }
    });
    get('partnerDiscardConfirm').addEventListener('click', () => {
        window.SBDialog.close('partnerDiscardDialog');
        const action = discardAction;
        discardAction = null;
        action?.();
        save();
    });
    root.querySelectorAll('dialog').forEach(dialog => {
        dialog.addEventListener('sb:dialog-before-close', event => {
            if (pending) event.preventDefault();
        });
    });
    contactDialog.addEventListener('sb:dialog-closed', save);
    contactForm.addEventListener('input', save);
    contactForm.addEventListener('change', save);
    panes.forEach(pane => pane.addEventListener('scroll', save, {passive: true}));
    function openNotePreview(link) {
        try {
            if (!notePreview) {
                notePreview = window.SBRecordDialog.create('partnerNotePreview', {
                    statusText: 'Open the full page to edit this note.',
                    errorText: 'Unable to load this note. Retry or open the full page.',
                    loadUrl: noteId => `/api/note/${noteId}/detail`,
                    fullUrl: noteId => `/note/${noteId}`,
                    fragmentSelector: '[data-sb-note-detail]',
                    identityAttribute: 'data-sb-note-id',
                    mount: content => window.SBNoteActions.mount(content),
                });
            }
            notePreview.open({
                id: Number(link.closest('[data-note-id]').dataset.noteId),
                title: link.dataset.recordTitle,
            });
        } catch (error) {
            notice(new Error(
                'Could not open the note preview. Try again or open the note in a new tab.',
                {cause: error}));
        }
    }
    root.addEventListener('click', event => {
        const link = event.target.closest('a[href]');
        if (link) {
            if (pending) event.preventDefault();
            else {
                save();
                if (link.dataset.partnerNotePreview !== undefined && !event.defaultPrevented
                    && event.button === 0 && !event.ctrlKey && !event.metaKey
                    && !event.shiftKey && !event.altKey) {
                    event.preventDefault();
                    if (dirty()) {
                        discard(() => {
                            window.SBDialog.close(contactDialog, 'discard');
                            save();
                            openNotePreview(link);
                        });
                    } else openNotePreview(link);
                }
            }
            return;
        }
        if (pending) return;
        const note = event.target.closest('[data-note-id]');
        if (note && !event.defaultPrevented && !link && event.button === 0
            && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey
            && !event.target.closest('button,input,select,textarea')) {
            note.querySelector('a').click();
            return;
        }
        const edit = event.target.closest('[data-edit-contact]');
        if (edit) openContact(partner.contacts.find(c => c.id === Number(edit.dataset.editContact)));
        const remove = event.target.closest('[data-delete-contact]');
        if (remove) {
            const contact = partner.contacts.find(c => c.id === Number(remove.dataset.deleteContact));
            get('deleteContactDialog').querySelector('form').action =
                `/partners/${id}/contacts/${contact.id}/delete`;
            get('deleteContactText').textContent = `Delete ${contact.name}?`;
            window.SBDialog.open('deleteContactDialog');
        }
        const photo = event.target.closest('.contact-photo-wrap');
        if (photo) {
            photoButton = photo;
            const src = photo.dataset.fullSrc || photo.querySelector('img')?.src;
            get('contactPhotoFull').hidden = !src;
            if (src) get('contactPhotoFull').src = src;
            get('contactPhotoPaste').hidden = !!src;
            get('contactPhotoRemoveBtn').hidden = !src;
            get('contactPhotoStatus').textContent = '';
            window.SBDialog.open('contactPhotoModal');
            get('contactPhotoPaste').focus();
        }
    });
    document.addEventListener('click', event => {
        const link = event.target.closest('a[href]');
        if (!link || event.defaultPrevented || event.button > 0 || event.ctrlKey
            || event.metaKey || event.shiftKey
            || event.altKey || link.target === '_blank' || link.hasAttribute('download')) return;
        if (pending) event.preventDefault();
        else if (dirty()) {
            event.preventDefault();
            discard(() => {
                window.SBDialog.close(contactDialog, 'discard');
                save();
                window.location.assign(link.href);
            });
        }
    });
    get('addContactBtn').addEventListener('click', () => openContact(null));
    root.querySelector('[data-partner-share]').addEventListener('click', () => {
        if (!pending) Share.openShareModal('partner', id);
    });
    root.querySelectorAll('[data-partner-write]').forEach(form => {
        form.addEventListener('submit', event => {
            if (pending) { event.preventDefault(); return; }
            if (!form.checkValidity()) return;
            save();
            submitting = true;
            lock(true);
        });
    });
    async function writePhoto(b64) {
        const button = photoButton;
        const data = await request(window.fetch.bind(window),
            `/api/partner/contact/${button.dataset.contactId}/photo`, 'POST', {photo_b64: b64});
        if (data.success !== true) throw new Error('Could not save the photo.');
        button.replaceChildren();
        if (b64) {
            const img = document.createElement('img');
            img.alt = '';
            img.src = `data:image/png;base64,${data.photo_b64}`;
            button.append(img);
            button.dataset.fullSrc = `data:image/png;base64,${b64}`;
            get('contactPhotoFull').src = button.dataset.fullSrc;
            get('contactPhotoFull').hidden = false;
            get('contactPhotoPaste').hidden = true;
            get('contactPhotoRemoveBtn').hidden = false;
            get('contactPhotoStatus').textContent = 'Saved!';
        } else {
            delete button.dataset.fullSrc;
            const icon = document.createElement('i');
            icon.className = 'bi bi-person-circle';
            button.append(icon);
        }
    }
    document.addEventListener('paste', async event => {
        if (!get('contactPhotoModal').open || pending) return;
        const item = [...(event.clipboardData?.items || [])].find(
            entry => entry.type.startsWith('image/'));
        if (!item) return;
        event.preventDefault();
        lock(true, 'Processing photo...');
        try {
            const b64 = await new Promise((resolve, reject) => {
                const reader = new window.FileReader();
                reader.onload = () => resolve(reader.result.split(',')[1]);
                reader.onerror = () => reject(new Error('Could not read the photo. Please try again.'));
                reader.readAsDataURL(item.getAsFile());
            });
            await writePhoto(b64);
        } catch (error) { notice(error, 'contactPhotoStatus'); }
        finally { lock(false); }
    });
    get('contactPhotoRemoveBtn').addEventListener('click', async () => {
        if (pending) return;
        lock(true, 'Removing photo...');
        try {
            await writePhoto(null);
            lock(false);
            window.SBDialog.close('contactPhotoModal');
        } catch (error) { notice(error, 'contactPhotoStatus'); }
        finally { if (pending) lock(false); }
    });
    function section(title) {
        const section = document.createElement('section');
        const heading = document.createElement('h3');
        heading.textContent = title;
        section.append(heading);
        get('scrapeReviewBody').append(section);
        return section;
    }
    function choice(parent, className, index, checked, disabled, text, detail) {
        const label = document.createElement('label');
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.className = className;
        checkbox.dataset.idx = String(index);
        checkbox.checked = checked;
        checkbox.disabled = disabled;
        const content = document.createElement('span');
        content.textContent = text;
        if (detail) {
            const small = document.createElement('small');
            small.textContent = detail;
            content.append(small);
        }
        label.append(checkbox, content);
        parent.append(label);
        return checkbox;
    }
    function review(data) {
        scrapeData = data;
        const body = get('scrapeReviewBody');
        body.replaceChildren();
        const summary = document.createElement('p');
        summary.textContent = `Found data from ${data.meetings_found || 0} meeting(s). Uncheck anything you don't want to import.`;
        body.append(summary);
        if (data.overview) {
            const notes = section('Notes');
            const checkbox = choice(notes, '', 0, true, false, 'Import notes',
                partner.overview ? 'Your existing notes are preserved above the separator. Edit as needed.' : '');
            checkbox.id = 'scrapeOverview';
            const textarea = document.createElement('textarea');
            textarea.id = 'scrapeOverviewText';
            textarea.setAttribute('aria-label', 'Notes to import');
            textarea.rows = 6;
            textarea.value = partner.overview
                ? `${partner.overview.trim()}\n\n---\n\n${data.overview}` : data.overview;
            notes.append(textarea);
        }
        if (data.specialties?.length) {
            const specs = section('Specialties');
            data.specialties.forEach((s, index) => choice(specs, 'scrape-specialty', index,
                !!s.is_new, !s.is_new, s.name, s.is_new ? 'New' : 'Already added'));
        }
        if (data.contacts?.length) {
            const contacts = section('Contacts');
            data.contacts.forEach((c, index) => choice(contacts, 'scrape-contact', index,
                !!(c.is_new || c.has_updates), !c.is_new && !c.has_updates, c.name,
                [c.title, c.email, c.is_new ? 'New contact' : c.has_updates
                    ? (c.updates || []).join(', ') : 'Already exists, no new data']
                    .filter(Boolean).join(' · ')));
        }
        if (!data.overview && !data.specialties?.length && !data.contacts?.length) {
            const empty = document.createElement('p');
            empty.textContent = 'No results came back this time. WorkIQ can be inconsistent - it may return results on a second attempt.';
            body.append(empty);
        }
        get('scrapeReviewError').hidden = true;
        window.SBDialog.open('scrapeReviewModal');
    }
    async function scan(target = 'partnerNotice') {
        if (pending) return;
        get(target).hidden = true;
        lock(true, 'Scanning meetings...');
        try {
            const data = await request(window.fetch.bind(window), `/api/partners/${id}/scrape`, 'POST');
            if (data.success !== true) throw new Error('Invalid meeting results. Please try again.');
            lock(false);
            if (get('scrapeDomainModal').open) window.SBDialog.close('scrapeDomainModal');
            review(data);
        } catch (error) { notice(error, target); }
        finally { if (pending) lock(false); }
    }
    get('scrapeBtn').addEventListener('click', () => {
        if (partner.website) { scan(); return; }
        get('scrapeDomainInput').value = partner.contacts.map(c => c.email.split('@')[1])
            .find(Boolean) || '';
        get('scrapeDomainHint').textContent = get('scrapeDomainInput').value
            ? 'Suggested from existing contact email. Edit if needed.' : '';
        get('scrapeDomainError').hidden = true;
        window.SBDialog.open('scrapeDomainModal');
    });
    get('scrapeDomainForm').addEventListener('submit', async event => {
        event.preventDefault();
        if (pending) return;
        const domain = normalizeDomain(get('scrapeDomainInput').value);
        if (!domain) {
            notice(new Error('Please enter a domain (e.g. xoriant.com)'), 'scrapeDomainError');
            return;
        }
        if (domain !== partner.website) {
            lock(true, 'Saving domain...');
            try {
                await request(window.fetch.bind(window), `/api/partners/${id}`, 'PUT',
                    {name: partner.name, website: domain, overview: partner.overview,
                        rating: partner.rating, specialty_ids: partner.specialty_ids,
                        contacts: partner.contacts});
                partner.website = domain;
            } catch (error) {
                notice(error, 'scrapeDomainError');
                lock(false);
                return;
            }
            lock(false);
        }
        await scan('scrapeDomainError');
    });
    get('scrapeRetryBtn').addEventListener('click', () => scan('scrapeReviewError'));
    get('scrapeApplyBtn').addEventListener('click', async () => {
        if (pending || !scrapeData) return;
        const indexes = selector => [...get('scrapeReviewBody').querySelectorAll(selector)]
            .map(node => Number(node.dataset.idx));
        const payload = selectedResults(scrapeData,
            get('scrapeOverview')?.checked ? get('scrapeOverviewText').value : null,
            indexes('.scrape-specialty:checked:not(:disabled)'),
            indexes('.scrape-contact:checked:not(:disabled)'));
        get('scrapeReviewError').hidden = true;
        lock(true);
        try {
            const data = await request(window.fetch.bind(window),
                `/api/partners/${id}/scrape/apply`, 'POST', payload);
            if (data.success !== true) throw new Error('Could not import the selected results.');
            lock(false);
            window.SBDialog.close('scrapeReviewModal');
            window.location.reload();
        } catch (error) { notice(error, 'scrapeReviewError'); }
        finally { if (pending) lock(false); }
    });
    window.addEventListener('beforeunload', event => {
        if (!submitting && (pending || dirty())) {
            event.preventDefault();
            event.returnValue = '';
        }
    });
    window.addEventListener('pagehide', save);
    window.addEventListener('pageshow', () => {
        submitting = false;
        if (pending) lock(false);
        restore();
    });
    restore();
    if (partner.failure) {
        const failure = partner.failure;
        openContact(null, {action: failure.path,
            title: failure.path.endsWith('/edit') ? 'Edit Contact' : 'Add Contact',
            values: failure.values});
        baseline = '';
        get('contactFailure').textContent = 'The contact was not saved. Your entries are still here.';
        get('contactFailure').hidden = false;
    }
    }
    if (window.document.readyState === 'loading') {
        window.document.addEventListener('DOMContentLoaded', mount, {once: true});
    } else mount();
})(typeof window === 'undefined' ? {} : window);
