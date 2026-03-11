"""
Partner routes for NoteHelper.
Handles partner management, contacts, and specialties.
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, g
from app.models import db, Partner, PartnerContact, Specialty, Note
from app.routes.admin import fetch_favicon_for_domain
from app.routes.msx import _extract_domain

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
        
        flash(f'Partner "{name}" updated successfully', 'success')
        return redirect(url_for('partners.partner_view', id=partner.id))
    
    specialties = Specialty.query.order_by(Specialty.name).all()
    return render_template('partner_form.html', partner=partner, specialties=specialties)


@partners_bp.route('/partners/<int:id>/delete', methods=['POST'])
def partner_delete(id):
    """Delete a partner."""
    partner = Partner.query.get_or_404(id)
    name = partner.name
    
    # Remove from all call logs (unassociate)
    partner.notes = []
    
    db.session.delete(partner)
    db.session.commit()
    
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
        email=email or None,
        is_primary=is_primary,
    )
    
    db.session.add(contact)
    db.session.commit()
    
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
    
    flash(f'"{contact.name}" set as primary contact', 'success')
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
    
    flash(f'Contact "{name}" deleted', 'success')
    return redirect(url_for('partners.partner_view', id=partner_id))


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
    """Create a partner via API (for inline creation from call log form)."""
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
    
    db.session.add(partner)
    db.session.commit()
    
    return jsonify({'id': partner.id, 'name': partner.name, 'existing': False})


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
