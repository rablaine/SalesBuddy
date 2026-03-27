"""
Project routes for Sales Buddy.
Handles internal project CRUD - non-customer work like training, copilot saved tasks, etc.
"""
import logging
from datetime import datetime, timezone

from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from app.models import db, Project, ActionItem, Note, notes_projects

logger = logging.getLogger(__name__)

projects_bp = Blueprint('projects', __name__)


@projects_bp.route('/project/new', methods=['GET', 'POST'])
def project_create():
    """Create a new internal project."""
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        project_type = request.form.get('project_type', 'general').strip()
        due_date_str = request.form.get('due_date', '').strip()

        if not title:
            flash('Project title is required.', 'danger')
            return redirect(url_for('projects.project_create'))

        project = Project(
            title=title,
            description=description or None,
            project_type=project_type,
        )
        if due_date_str:
            try:
                project.due_date = datetime.strptime(due_date_str, '%Y-%m-%d').date()
            except ValueError:
                pass

        db.session.add(project)
        db.session.flush()

        # Attach selected notes
        note_ids = request.form.getlist('note_ids')
        if note_ids:
            notes = Note.query.filter(Note.id.in_([int(nid) for nid in note_ids])).all()
            project.notes.extend(notes)

        db.session.commit()
        flash(f'Project "{title}" created!', 'success')
        return redirect(url_for('projects.project_view', id=project.id))

    # Get unattached general notes (no customer, not linked to any project)
    unattached_notes = (
        Note.query
        .filter(Note.customer_id.is_(None))
        .filter(~Note.projects.any())
        .order_by(Note.call_date.desc())
        .all()
    )

    return render_template('project_form.html', project=None,
                         project_types=Project.BUILT_IN_TYPES,
                         unattached_notes=unattached_notes)


@projects_bp.route('/project/<int:id>')
def project_view(id):
    """View project details with notes and action items."""
    project = Project.query.get_or_404(id)
    return render_template('project_view.html', project=project)


@projects_bp.route('/project/<int:id>/edit', methods=['GET', 'POST'])
def project_edit(id):
    """Edit an existing project."""
    project = Project.query.get_or_404(id)

    if request.method == 'POST':
        project.title = request.form.get('title', '').strip() or project.title
        project.description = request.form.get('description', '').strip() or None
        project.status = request.form.get('status', project.status)
        project.project_type = request.form.get('project_type', project.project_type)
        due_date_str = request.form.get('due_date', '').strip()
        if due_date_str:
            try:
                project.due_date = datetime.strptime(due_date_str, '%Y-%m-%d').date()
            except ValueError:
                pass
        else:
            project.due_date = None

        # Update attached notes
        note_ids = request.form.getlist('note_ids')
        current_note_ids = {n.id for n in project.notes}
        selected_ids = {int(nid) for nid in note_ids} if note_ids else set()
        # Add newly selected
        to_add = selected_ids - current_note_ids
        if to_add:
            new_notes = Note.query.filter(Note.id.in_(to_add)).all()
            project.notes.extend(new_notes)
        # Remove deselected
        to_remove = current_note_ids - selected_ids
        if to_remove:
            project.notes = [n for n in project.notes if n.id not in to_remove]

        db.session.commit()
        flash('Project updated.', 'success')
        return redirect(url_for('projects.project_view', id=project.id))

    # Get unattached general notes + notes already on this project
    unattached_notes = (
        Note.query
        .filter(Note.customer_id.is_(None))
        .filter(db.or_(
            ~Note.projects.any(),
            Note.projects.any(Project.id == project.id),
        ))
        .order_by(Note.call_date.desc())
        .all()
    )

    return render_template('project_form.html', project=project,
                         project_types=Project.BUILT_IN_TYPES,
                         unattached_notes=unattached_notes)


@projects_bp.route('/project/<int:id>/delete', methods=['POST'])
def project_delete(id):
    """Delete a project."""
    project = Project.query.get_or_404(id)
    title = project.title
    db.session.delete(project)
    db.session.commit()
    flash(f'Project "{title}" deleted.', 'success')
    return redirect(url_for('engagements.engagements_hub'))


@projects_bp.route('/api/project/<int:id>/action-item', methods=['POST'])
def project_add_action_item(id):
    """Add an action item to a project."""
    project = Project.query.get_or_404(id)
    data = request.get_json(silent=True) or {}
    title = (data.get('title') or '').strip()
    if not title:
        return jsonify(success=False, error='Title is required'), 400

    item = ActionItem(
        project_id=project.id,
        title=title,
        description=(data.get('description') or '').strip() or None,
        source='project',
        status='open',
        priority=data.get('priority', 'normal'),
    )
    due_str = (data.get('due_date') or '').strip()
    if due_str:
        try:
            item.due_date = datetime.strptime(due_str, '%Y-%m-%d').date()
        except ValueError:
            pass

    db.session.add(item)
    db.session.commit()
    return jsonify(success=True, id=item.id)


@projects_bp.route('/api/project/create-inline', methods=['POST'])
def project_create_inline():
    """Create a project with just a title (inline from note form).

    Accepts JSON with: title, [project_type, description, due_date]
    Returns: {success: true, id: X, title: "...", project_type: "..."}
    """
    data = request.get_json(silent=True) or {}
    title = (data.get('title') or '').strip()
    if not title:
        return jsonify(success=False, error='Title is required'), 400

    project_type = (data.get('project_type') or 'general').strip()
    allowed_types = [t for t in Project.BUILT_IN_TYPES if t != 'copilot_saved']
    if project_type not in allowed_types:
        project_type = 'general'

    project = Project(
        title=title,
        description=(data.get('description') or '').strip() or None,
        project_type=project_type,
    )
    due_str = (data.get('due_date') or '').strip()
    if due_str:
        try:
            project.due_date = datetime.strptime(due_str, '%Y-%m-%d').date()
        except ValueError:
            pass

    db.session.add(project)
    db.session.commit()

    return jsonify(
        success=True,
        id=project.id,
        title=project.title,
        project_type=project.project_type,
    )


@projects_bp.route('/api/project/<int:id>', methods=['GET'])
def project_get_json(id: int):
    """Return project details as JSON (for inline flyout editing)."""
    project = Project.query.get_or_404(id)
    return jsonify(
        id=project.id,
        title=project.title,
        description=project.description or '',
        project_type=project.project_type,
        status=project.status,
        due_date=project.due_date.strftime('%Y-%m-%d') if project.due_date else '',
    )


@projects_bp.route('/api/project/<int:id>', methods=['PUT'])
def project_update_json(id: int):
    """Update a project from the inline flyout."""
    project = Project.query.get_or_404(id)
    data = request.get_json(silent=True) or {}

    title = (data.get('title') or '').strip()
    if not title:
        return jsonify(success=False, error='Title is required'), 400

    project.title = title
    project.description = (data.get('description') or '').strip() or None

    project_type = (data.get('project_type') or project.project_type).strip()
    allowed_types = [t for t in Project.BUILT_IN_TYPES if t != 'copilot_saved']
    if project_type in allowed_types:
        project.project_type = project_type

    status = (data.get('status') or '').strip()
    if status in Project.STATUSES:
        project.status = status

    due_str = (data.get('due_date') or '').strip()
    if due_str:
        try:
            project.due_date = datetime.strptime(due_str, '%Y-%m-%d').date()
        except ValueError:
            pass
    else:
        project.due_date = None

    db.session.commit()
    return jsonify(
        success=True,
        id=project.id,
        title=project.title,
        project_type=project.project_type,
        status=project.status,
    )
