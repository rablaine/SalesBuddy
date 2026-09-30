"""Project selection, persistence, source removal, and shared detail rendering tests."""

from datetime import date

from bs4 import BeautifulSoup
import pytest

from app.models import ActionItem, Project, db
from app.services.salesiq_tools import execute_tool

API = '/api/reports/manager-one-on-one'
PAGE = '/reports/initiative-tracker'


@pytest.fixture
def projects(app) -> dict[str, int]:
    """Create internal projects across statuses and with no customer association."""
    with app.app_context():
        records = [
            Project(
                title=f'{status} learning plan', status=status, project_type='training',
                description='Prepare for a certification', due_date=date(2026, 12, 15),
            )
            for status in Project.STATUSES
        ]
        db.session.add_all(records)
        db.session.flush()
        db.session.add(ActionItem(
            project_id=records[0].id, title='Review exam topics', source='project',
        ))
        db.session.commit()
        return {project.status: project.id for project in records}


def create_initiative(client, name: str = 'Learning') -> int:
    """Create an initiative and return its persistent identifier."""
    response = client.post(f'{API}/sections', json={'name': name})
    assert response.status_code == 200
    return client.get(API).json['sections'][-1]['id']


def add_projects(client, section_id: int, ids: list[int]):
    """Add projects through the same endpoint as other work types."""
    return client.post(f'{API}/sections/{section_id}/items', json={
        'item_type': 'project', 'entity_ids': ids,
    })


def test_project_option_only_appears_when_projects_exist(client, app):
    """Avoid an empty work type while allowing even completed internal projects."""
    create_initiative(client)
    soup = BeautifulSoup(client.get(PAGE).data, 'html.parser')
    assert soup.select_one('option[value="project"]') is None
    with app.app_context():
        db.session.add(Project(title='Finished training', status='Completed'))
        db.session.commit()
    soup = BeautifulSoup(client.get(PAGE).data, 'html.parser')
    assert soup.select_one('option[value="project"]').get_text(strip=True) == 'Projects'


@pytest.mark.parametrize('project_type', ['general', 'training', 'achievement', 'custom'])
def test_system_projects_neither_enable_option_nor_appear_in_search(client, app, project_type):
    """System eligibility is based on type, not the project title or current status."""
    section_id = create_initiative(client)
    with app.app_context():
        system = Project(title='Renamed saved tasks', project_type='copilot_saved')
        db.session.add(system)
        db.session.commit()
        system_id = system.id
    soup = BeautifulSoup(client.get(PAGE).data, 'html.parser')
    assert soup.select_one('option[value="project"]') is None
    url = f'{API}/sections/{section_id}/candidates?type=project'
    assert client.get(url).json['results'] == []
    assert add_projects(client, section_id, [system_id]).status_code == 404
    with app.app_context():
        regular = Project(
            title='Copilot Saved Tasks', project_type=project_type, status='Completed',
        )
        db.session.add(regular)
        db.session.commit()
        regular_id = regular.id
    soup = BeautifulSoup(client.get(PAGE).data, 'html.parser')
    assert soup.select_one('option[value="project"]') is not None
    assert {row['id'] for row in client.get(url).json['results']} == {regular_id}
    assert add_projects(client, section_id, [regular_id, system_id]).status_code == 404
    assert client.get(API).json['sections'][0]['items'] == []
    assert add_projects(client, section_id, [regular_id]).status_code == 200


@pytest.mark.parametrize('search', ['', 'certification', 'training', 'learning'])
def test_project_candidates_include_all_statuses_without_customers(client, projects, search):
    """Search internal titles, descriptions, and types, not customer associations."""
    section_id = create_initiative(client)
    response = client.get(f'{API}/sections/{section_id}/candidates', query_string={
        'type': 'project', 'q': search,
    })
    assert response.status_code == 200
    rows = response.json['results']
    assert {row['id'] for row in rows} == set(projects.values())
    assert {row['status'] for row in rows} == set(Project.STATUSES)
    for row in rows:
        assert row['customer_id'] is None
        assert row['customer_name'] == 'Internal project'
        assert row['acr'] is None
        assert row['due_date'] == '2026-12-15'
        assert row['detail'] == 'Training'
    assert client.get(f'{API}/sections/{section_id}/candidates?type=project&q=absent').json[
        'results'
    ] == []
    assert client.get(
        f'{API}/sections/{section_id}/candidates?type=project&source=u2c',
    ).status_code == 400


def test_project_links_persist_allow_overlap_and_keep_live_details(client, app, projects):
    """Preserve per-initiative uniqueness and discussion state while using current details."""
    first = create_initiative(client)
    second = create_initiative(client, 'Development')
    project_id = projects['Active']
    assert add_projects(client, first, [project_id]).status_code == 200
    assert add_projects(client, first, [project_id]).status_code == 409
    assert add_projects(client, second, [project_id]).status_code == 200
    candidates = client.get(f'{API}/sections/{first}/candidates?type=project').json['results']
    assert project_id not in {row['id'] for row in candidates}
    item = client.get(API).json['sections'][0]['items'][0]
    assert item['available']
    assert item['seller_id'] is None
    assert item['entity_id'] == project_id
    assert item['item_type'] == 'project'
    assert client.patch(f"{API}/items/{item['id']}", json={
        'talking_points': 'Discuss certification budget',
    }).status_code == 200
    with app.app_context():
        project = db.session.get(Project, project_id)
        project.title = 'Updated learning plan'
        project.status = 'On Hold'
        db.session.commit()
    report = client.get(API).json
    assert report['sections'][0]['items'][0]['title'] == 'Updated learning plan'
    assert report['sections'][0]['items'][0]['status'] == 'On Hold'
    assert report['sections'][0]['items'][0]['talking_points'] == 'Discuss certification budget'
    with app.test_request_context():
        assert execute_tool('report_manager_one_on_one', {}) == {
            'sections': report['sections'],
        }
    soup = BeautifulSoup(client.get(PAGE).data, 'html.parser')
    row = soup.select_one(f'tr[data-entity-id="{project_id}"][data-item-type="project"]')
    assert row.select_one('.work-detail-link')['href'] == f'/project/{project_id}'
    assert len(row.select('td')) == 7
    assert row.select('td')[1].get_text(strip=True) == 'Internal project'
    assert row.select('td')[1].select_one('a') is None
    assert row.select('td')[4].get_text(strip=True) == '-'


def test_project_removal_retains_history_and_report_link_removal_keeps_source(
    client, app, projects,
):
    """Deleting source work keeps its snapshots and history; unlinking never deletes projects."""
    section_id = create_initiative(client)
    project_id = projects['Active']
    assert add_projects(client, section_id, [project_id]).status_code == 200
    item = client.get(API).json['sections'][0]['items'][0]
    saved = client.patch(f"{API}/items/{item['id']}", json={'talking_points': 'Budget agreed'})
    block = saved.json['item']
    assert client.post(f"{API}/items/{item['id']}/discuss", json={
        'talking_points': block['talking_points'],
        'points_created_at': block['points_created_at'],
    }).status_code == 200
    with app.app_context():
        db.session.delete(db.session.get(Project, project_id))
        db.session.commit()
    item = client.get(API).json['sections'][0]['items'][0]
    assert not item['available']
    assert item['title'] == 'Active learning plan'
    assert item['customer_name'] == 'Internal project'
    assert item['discussed_points'][0]['text'] == 'Budget agreed'
    assert client.get(PAGE).status_code == 200
    candidates = client.get(f'{API}/sections/{section_id}/candidates?type=project').json['results']
    assert {row['id'] for row in candidates} == {projects['On Hold'], projects['Completed']}
    assert add_projects(client, section_id, [projects['Completed']]).status_code == 200
    assert client.delete(f'{API}/sections/{section_id}').status_code == 200
    with app.app_context():
        assert db.session.get(Project, projects['Completed']) is not None


def test_project_add_rejects_missing_ids_atomically(client, projects):
    """Reject a mixed invalid selection without partial additions."""
    section_id = create_initiative(client)
    response = add_projects(client, section_id, [projects['Active'], 999999])
    assert response.status_code == 404
    assert client.get(API).json['sections'][0]['items'] == []


def test_project_search_keeps_picker_limit_and_escapes_titles(client, app):
    """Search at most 75 projects and render project titles as text, not markup."""
    section_id = create_initiative(client)
    with app.app_context():
        db.session.add_all(Project(title=f'Project {number:03}') for number in range(80))
        db.session.add_all(
            Project(title=f'Excluded {number:03}', project_type='copilot_saved')
            for number in range(80)
        )
        unsafe = Project(title='<script>alert(1)</script>')
        db.session.add(unsafe)
        db.session.commit()
        project_id = unsafe.id
    results = client.get(f'{API}/sections/{section_id}/candidates?type=project').json['results']
    assert len(results) == 75
    assert all(row['detail'] != 'Copilot Saved' for row in results)
    assert add_projects(client, section_id, [project_id]).status_code == 200
    html = client.get(PAGE).data.decode()
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in html
    assert '<script>alert(1)</script>' not in html


def test_project_detail_and_standalone_share_complete_content(client, projects):
    """The page shell and fragment expose the same existing project details and actions."""
    project_id = projects['Active']
    fragment = client.get(f'/api/project/{project_id}/detail')
    page = client.get(f'/project/{project_id}')
    assert fragment.status_code == page.status_code == 200
    content = BeautifulSoup(fragment.data, 'html.parser')
    standalone = BeautifulSoup(page.data, 'html.parser')
    for selector in ['h1', '.breadcrumb', '#addTaskForm', '#actionItemDetailModal']:
        assert str(content.select_one(selector)) == str(standalone.select_one(selector))
    assert b'Review exam topics' in fragment.data
    assert b'Prepare for a certification' in fragment.data
    assert b'addProjectTask' in fragment.data
    assert b'parentModal' in fragment.data
    assert content.select_one('html') is None
    assert client.get('/api/project/999999/detail').status_code == 404
