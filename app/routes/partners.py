"""
Partner routes for Sales Buddy.
Handles partner management, contacts, and specialties.
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, g
from app.models import db, Partner, PartnerContact, Specialty, Note
from app.routes.admin import fetch_favicon_for_domain
from app.routes.msx import _extract_domain
from app.services.backup import backup_partner, delete_partner_backup

partners_bp = Blueprint('partners', __name__)


# =============================================================================
# Partner Routes
# =============================================================================

@partners_bp.route('/partners')
def partners_list():
    """List all partners."""
    partners = Partner.query.order_by(Partner.name).all()
    return render_template('partners_list.html', partners=partners)


@partners_bp.route('/partners/new', methods=['GET', 'POST'])
def partner_new():
    """Create a new partner."""
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        overview = request.form.get('overview', '').strip()
        rating_str = request.form.get('rating', '').strip()
        website = request.form.get('website', '').strip()
        specialty_ids = request.form.getlist('specialty_ids')
        
        # Parse rating (empty string = no rating)
        rating = int(rating_str) if rating_str else None
        if rating is not None and (rating < 0 or rating > 5):
            rating = None
        
        if not name:
            flash('Partner name is required', 'error')
            return redirect(request.url)
        
        # Normalize website to clean domain and fetch favicon
        website = _extract_domain(website) if website else None
        favicon_b64 = None
        if website:
            favicon_b64 = fetch_favicon_for_domain(website)
        
        partner = Partner(
            name=name,
            overview=overview or None,
            rating=rating,
            website=website,
            favicon_b64=favicon_b64,
        )
        
        # Add specialties
        if specialty_ids:
            specialties = Specialty.query.filter(Specialty.id.in_(specialty_ids)).all()
            partner.specialties = specialties
        
        db.session.add(partner)
        db.session.commit()
        
        backup_partner(partner.id)
        
        flash(f'Partner "{name}" created successfully', 'success')
        return redirect(url_for('partners.partner_view', id=partner.id))
    
    specialties = Specialty.query.order_by(Specialty.name).all()
    return render_template('partner_form.html', partner=None, specialties=specialties)


@partners_bp.route('/partners/<int:id>')
def partner_view(id):
    """View a partner's details."""
    partner = Partner.query.get_or_404(id)
    return render_template('partner_view.html', partner=partner)


@partners_bp.route('/partners/<int:id>/edit', methods=['GET', 'POST'])
def partner_edit(id):
    """Edit a partner."""
    partner = Partner.query.get_or_404(id)
    
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        overview = request.form.get('overview', '').strip()
        rating_str = request.form.get('rating', '').strip()
        website = request.form.get('website', '').strip()
        specialty_ids = request.form.getlist('specialty_ids')
        
        # Parse rating (empty string = no rating)
        rating = int(rating_str) if rating_str else None
        if rating is not None and (rating < 0 or rating > 5):
            rating = None
        
        if not name:
            flash('Partner name is required', 'error')
            return redirect(request.url)
        
        partner.name = name
        partner.overview = overview or None
        partner.rating = rating
        
        # Normalize website to clean domain and refetch favicon if changed
        website = _extract_domain(website) if website else None
        old_website = partner.website
        partner.website = website
        if website and website != old_website:
            partner.favicon_b64 = fetch_favicon_for_domain(website)
        elif not website:
            partner.favicon_b64 = None
        
        # Update specialties
        if specialty_ids:
            specialties = Specialty.query.filter(Specialty.id.in_(specialty_ids)).all()
            partner.specialties = specialties
        else:
            partner.specialties = []
        
        db.session.commit()
        
        backup_partner(partner.id)
        
        flash(f'Partner "{name}" updated successfully', 'success')
        return redirect(url_for('partners.partner_view', id=partner.id))
    
    specialties = Specialty.query.order_by(Specialty.name).all()
    return render_template('partner_form.html', partner=partner, specialties=specialties)


@partners_bp.route('/partners/<int:id>/delete', methods=['POST'])
def partner_delete(id):
    """Delete a partner."""
    partner = Partner.query.get_or_404(id)
    name = partner.name
    
    # Remove from all notes (unassociate)
    partner.notes = []
    
    db.session.delete(partner)
    db.session.commit()
    
    delete_partner_backup(id)
    
    flash(f'Partner "{name}" deleted successfully', 'success')
    return redirect(url_for('partners.partners_list'))


# =============================================================================
# Partner Contact Routes
# =============================================================================

@partners_bp.route('/partners/<int:partner_id>/contacts/new', methods=['POST'])
def contact_new(partner_id):
    """Add a new contact to a partner."""
    partner = Partner.query.get_or_404(partner_id)
    
    name = request.form.get('contact_name', '').strip()
    title = request.form.get('contact_title', '').strip()
    email = request.form.get('contact_email', '').strip()
    is_primary = request.form.get('is_primary') == 'on'
    
    if not name:
        flash('Contact name is required', 'error')
        return redirect(url_for('partners.partner_view', id=partner_id))
    
    # If marking as primary, unmark others
    if is_primary:
        for contact in partner.contacts:
            contact.is_primary = False
    
    contact = PartnerContact(
        partner_id=partner_id,
        name=name,
        title=title or None,
        email=email or None,
        is_primary=is_primary,
    )
    
    db.session.add(contact)
    db.session.commit()
    
    backup_partner(partner_id)
    
    flash(f'Contact "{name}" added successfully', 'success')
    return redirect(url_for('partners.partner_view', id=partner_id))


@partners_bp.route('/partners/<int:partner_id>/contacts/<int:contact_id>/primary', methods=['POST'])
def contact_set_primary(partner_id, contact_id):
    """Set a contact as the primary contact."""
    partner = Partner.query.get_or_404(partner_id)
    contact = PartnerContact.query.get_or_404(contact_id)
    
    if contact.partner_id != partner_id:
        flash('Contact does not belong to this partner', 'error')
        return redirect(url_for('partners.partner_view', id=partner_id))
    
    # Unmark all others
    for c in partner.contacts:
        c.is_primary = False
    
    contact.is_primary = True
    db.session.commit()
    
    backup_partner(partner_id)
    
    flash(f'"{contact.name}" set as primary contact', 'success')
    return redirect(url_for('partners.partner_view', id=partner_id))


@partners_bp.route('/partners/<int:partner_id>/contacts/<int:contact_id>/edit', methods=['POST'])
def contact_edit(partner_id, contact_id):
    """Edit a contact's name and email."""
    partner = Partner.query.get_or_404(partner_id)
    contact = PartnerContact.query.get_or_404(contact_id)

    if contact.partner_id != partner_id:
        flash('Contact does not belong to this partner', 'error')
        return redirect(url_for('partners.partner_view', id=partner_id))

    name = request.form.get('contact_name', '').strip()
    if not name:
        flash('Contact name is required', 'error')
        return redirect(url_for('partners.partner_view', id=partner_id))

    contact.name = name
    contact.title = request.form.get('contact_title', '').strip() or None
    contact.email = request.form.get('contact_email', '').strip() or None

    is_primary = request.form.get('is_primary') == 'on'
    if is_primary and not contact.is_primary:
        for c in partner.contacts:
            c.is_primary = False
        contact.is_primary = True

    db.session.commit()
    backup_partner(partner_id)

    flash(f'Contact "{name}" updated', 'success')
    return redirect(url_for('partners.partner_view', id=partner_id))


@partners_bp.route('/partners/<int:partner_id>/contacts/<int:contact_id>/delete', methods=['POST'])
def contact_delete(partner_id, contact_id):
    """Delete a contact."""
    contact = PartnerContact.query.get_or_404(contact_id)
    
    if contact.partner_id != partner_id:
        flash('Contact does not belong to this partner', 'error')
        return redirect(url_for('partners.partner_view', id=partner_id))
    
    name = contact.name
    db.session.delete(contact)
    db.session.commit()
    
    backup_partner(partner_id)
    
    flash(f'Contact "{name}" deleted', 'success')
    return redirect(url_for('partners.partner_view', id=partner_id))


@partners_bp.route('/api/partner/<int:partner_id>/contacts', methods=['POST'])
def api_partner_contact_create(partner_id):
    """Create a new contact for a partner (JSON API)."""
    Partner.query.get_or_404(partner_id)
    data = request.get_json()
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Name is required'}), 400

    contact = PartnerContact(
        partner_id=partner_id,
        name=name,
        email=(data.get('email') or '').strip() or None,
        title=(data.get('title') or '').strip() or None,
    )
    db.session.add(contact)
    db.session.commit()
    return jsonify({'id': contact.id, 'name': contact.name,
                    'email': contact.email or '', 'title': contact.title or ''}), 201


@partners_bp.route('/api/partner/contact/<int:contact_id>/photo', methods=['POST'])
def api_partner_contact_photo(contact_id):
    """Save a photo for a partner contact. Runs face detection server-side."""
    contact = PartnerContact.query.get_or_404(contact_id)
    data = request.get_json()
    raw_b64 = data.get('photo_b64')
    if raw_b64:
        from app.services.contact_photo import process_contact_photo
        cropped, full = process_contact_photo(raw_b64)
        contact.photo_b64 = cropped
        contact.photo_full_b64 = full
    else:
        contact.photo_b64 = None
        contact.photo_full_b64 = None
    db.session.commit()
    return jsonify({'success': True, 'photo_b64': contact.photo_b64})


# =============================================================================
# Specialty Routes
# =============================================================================

@partners_bp.route('/specialties')
def specialties_list():
    """List all specialties."""
    specialties = Specialty.query.order_by(Specialty.name).all()
    return render_template('specialties_list.html', specialties=specialties)


@partners_bp.route('/specialties/new', methods=['GET', 'POST'])
def specialty_new():
    """Create a new specialty."""
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip()
        
        if not name:
            flash('Specialty name is required', 'error')
            return redirect(request.url)
        
        # Check for duplicate
        existing = Specialty.query.filter(db.func.lower(Specialty.name) == name.lower()).first()
        if existing:
            flash(f'Specialty "{name}" already exists', 'error')
            return redirect(request.url)
        
        specialty = Specialty(
            name=name,
            description=description or None,
        )
        
        db.session.add(specialty)
        db.session.commit()
        
        flash(f'Specialty "{name}" created successfully', 'success')
        return redirect(url_for('partners.specialty_view', id=specialty.id))
    
    return render_template('specialty_form.html', specialty=None)


@partners_bp.route('/specialties/<int:id>')
def specialty_view(id):
    """View a specialty and its associated partners."""
    specialty = Specialty.query.get_or_404(id)
    return render_template('specialty_view.html', specialty=specialty)


@partners_bp.route('/specialties/<int:id>/edit', methods=['GET', 'POST'])
def specialty_edit(id):
    """Edit a specialty."""
    specialty = Specialty.query.get_or_404(id)
    
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip()
        
        if not name:
            flash('Specialty name is required', 'error')
            return redirect(request.url)
        
        # Check for duplicate (excluding self)
        existing = Specialty.query.filter(
            db.func.lower(Specialty.name) == name.lower(),
            Specialty.id != id
        ).first()
        if existing:
            flash(f'Specialty "{name}" already exists', 'error')
            return redirect(request.url)
        
        specialty.name = name
        specialty.description = description or None
        db.session.commit()
        
        flash(f'Specialty "{name}" updated successfully', 'success')
        return redirect(url_for('partners.specialty_view', id=specialty.id))
    
    return render_template('specialty_form.html', specialty=specialty)


@partners_bp.route('/specialties/<int:id>/delete', methods=['POST'])
def specialty_delete(id):
    """Delete a specialty."""
    specialty = Specialty.query.get_or_404(id)
    name = specialty.name
    
    # Remove from all partners (unassociate)
    specialty.partners = []
    
    db.session.delete(specialty)
    db.session.commit()
    
    flash(f'Specialty "{name}" deleted successfully', 'success')
    return redirect(url_for('partners.specialties_list'))


# =============================================================================
# API Routes
# =============================================================================

@partners_bp.route('/api/partners/search')
def api_partners_search():
    """Search partners by name for autocomplete."""
    query = request.args.get('q', '').strip()
    
    if len(query) < 1:
        return jsonify([])
    
    partners = Partner.query.filter(
        Partner.name.ilike(f'%{query}%')
    ).order_by(Partner.name).limit(10).all()
    
    return jsonify([
        {'id': p.id, 'name': p.name}
        for p in partners
    ])


@partners_bp.route('/api/partners/create', methods=['POST'])
def api_partner_create():
    """Create a partner via API (for inline creation from note form)."""
    data = request.get_json()
    name = data.get('name', '').strip()
    
    if not name:
        return jsonify({'error': 'Partner name is required'}), 400
    
    # Check if exists
    existing = Partner.query.filter(db.func.lower(Partner.name) == name.lower()).first()
    if existing:
        return jsonify({'id': existing.id, 'name': existing.name, 'existing': True})
    
    partner = Partner(
        name=name,
    )
    # Optional fields from flyout
    website = data.get('website', '').strip()
    if website:
        partner.website = _extract_domain(website)
        favicon_b64 = fetch_favicon_for_domain(partner.website)
        if favicon_b64:
            partner.favicon_b64 = favicon_b64
    overview = data.get('overview', '').strip()
    if overview:
        partner.overview = overview
    rating = data.get('rating')
    if rating is not None:
        partner.rating = int(rating)
    
    db.session.add(partner)
    db.session.flush()  # Get partner.id for relationships
    
    # Specialties
    specialty_ids = data.get('specialty_ids', [])
    if specialty_ids:
        specialties = Specialty.query.filter(Specialty.id.in_(specialty_ids)).all()
        partner.specialties = specialties
    
    # Contacts
    contacts = data.get('contacts', [])
    for i, c in enumerate(contacts):
        contact_name = c.get('name', '').strip()
        if not contact_name:
            continue
        contact = PartnerContact(
            partner_id=partner.id,
            name=contact_name,
            title=c.get('title', '').strip() or None,
            email=c.get('email', '').strip() or None,
            is_primary=(i == 0),
        )
        db.session.add(contact)
    
    db.session.commit()
    
    return jsonify({'id': partner.id, 'name': partner.name, 'existing': False})


@partners_bp.route('/api/partners/<int:id>')
def api_partner_get(id):
    """Get partner data for inline editing."""
    partner = Partner.query.get_or_404(id)
    return jsonify({
        'id': partner.id,
        'name': partner.name,
        'website': partner.website or '',
        'overview': partner.overview or '',
        'rating': partner.rating,
        'specialty_ids': [s.id for s in partner.specialties],
        'specialties': [{'id': s.id, 'name': s.name} for s in partner.specialties],
        'contacts': [
            {'id': c.id, 'name': c.name, 'title': c.title or '', 'email': c.email or '',
             'is_primary': c.is_primary, 'photo_b64': c.photo_b64 or ''}
            for c in partner.contacts
        ],
    })


@partners_bp.route('/api/partners/<int:id>', methods=['PUT'])
def api_partner_update(id):
    """Update a partner via API (for inline editing from note form flyout)."""
    partner = Partner.query.get_or_404(id)
    data = request.get_json()

    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Partner name is required'}), 400

    partner.name = name

    website = data.get('website', '').strip()
    if website:
        new_domain = _extract_domain(website)
        if new_domain != partner.website:
            partner.website = new_domain
            partner.favicon_b64 = fetch_favicon_for_domain(new_domain)
    else:
        partner.website = None
        partner.favicon_b64 = None

    partner.overview = (data.get('overview') or '').strip() or None

    rating = data.get('rating')
    partner.rating = int(rating) if rating is not None else None

    # Specialties
    specialty_ids = data.get('specialty_ids', [])
    if specialty_ids:
        partner.specialties = Specialty.query.filter(
            Specialty.id.in_(specialty_ids)
        ).all()
    else:
        partner.specialties = []

    # Contacts: full replace strategy - delete old, add new
    incoming_contacts = data.get('contacts', [])
    # Build set of incoming IDs that already exist
    incoming_ids = {c.get('id') for c in incoming_contacts if c.get('id')}
    # Delete contacts not in the incoming set
    for existing in list(partner.contacts):
        if existing.id not in incoming_ids:
            db.session.delete(existing)
    # Update existing and add new
    for i, c in enumerate(incoming_contacts):
        contact_name = c.get('name', '').strip()
        if not contact_name:
            continue
        cid = c.get('id')
        if cid:
            existing = PartnerContact.query.get(cid)
            if existing and existing.partner_id == partner.id:
                existing.name = contact_name
                existing.title = c.get('title', '').strip() or None
                existing.email = c.get('email', '').strip() or None
                existing.is_primary = c.get('is_primary', i == 0)
                continue
        # New contact
        contact = PartnerContact(
            partner_id=partner.id,
            name=contact_name,
            title=c.get('title', '').strip() or None,
            email=c.get('email', '').strip() or None,
            is_primary=c.get('is_primary', i == 0),
        )
        db.session.add(contact)

    db.session.commit()
    backup_partner(partner.id)

    return jsonify({'id': partner.id, 'name': partner.name})


@partners_bp.route('/api/specialties/search')
def api_specialties_search():
    """Search specialties by name for autocomplete."""
    query = request.args.get('q', '').strip()
    
    if len(query) < 1:
        return jsonify([])
    
    specialties = Specialty.query.filter(
        Specialty.name.ilike(f'%{query}%')
    ).order_by(Specialty.name).limit(10).all()
    
    return jsonify([
        {'id': s.id, 'name': s.name, 'description': s.description or ''}
        for s in specialties
    ])


@partners_bp.route('/api/specialties/create', methods=['POST'])
def api_specialty_create():
    """Create a specialty via API (for inline creation from partner form)."""
    data = request.get_json()
    name = data.get('name', '').strip()
    
    if not name:
        return jsonify({'error': 'Specialty name is required'}), 400
    
    # Check if exists
    existing = Specialty.query.filter(db.func.lower(Specialty.name) == name.lower()).first()
    if existing:
        return jsonify({'id': existing.id, 'name': existing.name, 'description': existing.description or '', 'existed': True})
    
    specialty = Specialty(
        name=name,
    )
    
    db.session.add(specialty)
    db.session.commit()
    
    return jsonify({'id': specialty.id, 'name': specialty.name, 'description': '', 'existed': False})


# =============================================================================
# Partner Sharing API
# =============================================================================

@partners_bp.route('/api/share/connection-info')
def api_share_connection_info():
    """Return Socket.IO gateway URL and JWT for partner sharing."""
    from app.services.partner_sharing import get_share_gateway_url, get_share_token
    token = get_share_token()
    if not token:
        return jsonify({'success': False, 'error': 'Not authenticated — sign in via Admin Panel'}), 401
    return jsonify({
        'success': True,
        'gateway_url': get_share_gateway_url(),
        'token': token,
    })


@partners_bp.route('/api/share/partner/<int:partner_id>')
def api_share_serialize_partner(partner_id):
    """Serialize a single partner for sharing."""
    from app.services.partner_sharing import serialize_partner
    partner = Partner.query.get_or_404(partner_id)
    return jsonify({'success': True, 'partner': serialize_partner(partner)})


@partners_bp.route('/api/share/directory')
def api_share_serialize_directory():
    """Serialize the entire partner directory for sharing."""
    from app.services.partner_sharing import serialize_all_partners
    return jsonify({'success': True, 'partners': serialize_all_partners()})


@partners_bp.route('/api/share/preview', methods=['POST'])
def api_share_preview():
    """Preview what would be created/updated without writing to DB."""
    from app.services.partner_sharing import preview_partners
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data provided'}), 400

    partners_data = data.get('partners', [])
    sender_name = data.get('sender_name', 'Unknown')

    if not partners_data:
        return jsonify({'success': False, 'error': 'No partners in payload'}), 400

    previews = preview_partners(partners_data, sender_name)
    return jsonify({'success': True, 'previews': previews})


@partners_bp.route('/api/share/receive', methods=['POST'])
def api_share_receive():
    """Receive and upsert partner data from a sharing peer."""
    from app.services.partner_sharing import upsert_partners
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data provided'}), 400

    partners_data = data.get('partners', [])
    sender_name = data.get('sender_name', 'Unknown')

    if not partners_data:
        return jsonify({'success': False, 'error': 'No partners in payload'}), 400

    results = upsert_partners(partners_data, sender_name)
    return jsonify({'success': True, **results})


# =============================================================================
# Partner Data Scraping (WorkIQ)
# =============================================================================

@partners_bp.route('/api/partners/<int:partner_id>/scrape', methods=['POST'])
def api_partner_scrape(partner_id):
    """Scrape partner data from WorkIQ meetings.

    Returns structured contacts, specialties, and overview for user review.
    """
    import logging
    logger = logging.getLogger(__name__)
    partner = Partner.query.get_or_404(partner_id)
    try:
        from app.services.partner_scrape import scrape_partner_data
        result = scrape_partner_data(partner)
        return jsonify({'success': True, **result})
    except TimeoutError:
        return jsonify({'success': False, 'error': 'WorkIQ query timed out. Try again.'}), 504
    except Exception as e:
        logger.exception(f"Partner scrape failed for {partner.name}")
        return jsonify({'success': False, 'error': str(e)}), 500


@partners_bp.route('/api/partners/<int:partner_id>/scrape/apply', methods=['POST'])
def api_partner_scrape_apply(partner_id):
    """Apply user-selected scrape results to the partner."""
    partner = Partner.query.get_or_404(partner_id)
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data'}), 400

    from app.services.partner_scrape import apply_scrape_results
    try:
        summary = apply_scrape_results(
            partner,
            contacts=data.get('contacts', []),
            specialties=data.get('specialties', []),
            overview=data.get('overview'),
        )
        return jsonify({'success': True, **summary})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
