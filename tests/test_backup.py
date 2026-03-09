"""
Tests for the backup API endpoints.
"""
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.backup import (
    _is_business_path,
    detect_onedrive_paths,
    is_business_onedrive_path,
)


# =========================================================================
# OneDrive for Business enforcement
# =========================================================================

class TestIsBusinessPath:
    """Unit tests for _is_business_path heuristic."""

    def test_onedrive_commercial_env_contoso_not_microsoft(self):
        """OneDriveCommercial pointing at non-Microsoft org is rejected."""
        assert _is_business_path(
            r"C:\Users\test\OneDrive - Contoso", "OneDriveCommercial env var"
        ) is False

    def test_registry_business1_contoso_not_microsoft(self):
        """Registry Business1 pointing at non-Microsoft org is rejected."""
        assert _is_business_path(
            r"C:\Users\test\OneDrive - Contoso", "Registry (Business1)"
        ) is False

    def test_onedrive_with_org_name_is_business(self):
        assert _is_business_path(
            r"C:\Users\test\OneDrive - Microsoft", "Folder scan"
        ) is True

    def test_onedrive_with_other_org_name_is_not_business(self):
        """Non-Microsoft org names should be rejected."""
        assert _is_business_path(
            r"C:\Users\test\OneDrive - Acme Corp", "OneDrive env var"
        ) is False

    def test_plain_onedrive_is_personal(self):
        assert _is_business_path(
            r"C:\Users\test\OneDrive", "Folder scan"
        ) is False

    def test_onedrive_personal_is_personal(self):
        assert _is_business_path(
            r"C:\Users\test\OneDrive - Personal", "Folder scan"
        ) is False

    def test_onedrive_personal_case_insensitive(self):
        assert _is_business_path(
            r"C:\Users\test\OneDrive - personal", "OneDrive env var"
        ) is False

    def test_plain_onedrive_via_env_is_personal(self):
        """Even if found via the OneDrive env var, bare 'OneDrive' is personal."""
        assert _is_business_path(
            r"C:\Users\test\OneDrive", "OneDrive env var"
        ) is False


class TestDetectOneDrivePathsFilter:
    """Tests for the business_only filter on detect_onedrive_paths."""

    def test_business_only_excludes_personal(self, tmp_path):
        """Personal OneDrive dirs should be excluded when business_only=True."""
        biz_dir = tmp_path / "OneDrive - Microsoft"
        personal_dir = tmp_path / "OneDrive"
        biz_dir.mkdir()
        personal_dir.mkdir()

        with patch.dict(os.environ, {
            "OneDriveCommercial": str(biz_dir),
            "OneDrive": str(personal_dir),
            "USERPROFILE": str(tmp_path),
        }), patch("app.services.backup.winreg") as mock_winreg:
            mock_winreg.OpenKey.side_effect = OSError("no key")

            results = detect_onedrive_paths(business_only=True)
            paths = [c["path"] for c in results]
            assert str(biz_dir) in paths
            assert str(personal_dir) not in paths

    def test_non_microsoft_business_excluded(self, tmp_path):
        """Non-Microsoft business OneDrive dirs should be excluded."""
        contoso_dir = tmp_path / "OneDrive - Contoso"
        contoso_dir.mkdir()

        with patch.dict(os.environ, {
            "OneDriveCommercial": str(contoso_dir),
            "OneDrive": "",
            "USERPROFILE": str(tmp_path),
        }), patch("app.services.backup.winreg") as mock_winreg:
            mock_winreg.OpenKey.side_effect = OSError("no key")

            results = detect_onedrive_paths(business_only=True)
            paths = [c["path"] for c in results]
            assert str(contoso_dir) not in paths

    def test_business_only_false_includes_all(self, tmp_path):
        """When business_only=False, personal dirs should be included."""
        biz_dir = tmp_path / "OneDrive - Microsoft"
        personal_dir = tmp_path / "OneDrive"
        biz_dir.mkdir()
        personal_dir.mkdir()

        with patch.dict(os.environ, {
            "OneDriveCommercial": str(biz_dir),
            "OneDrive": str(personal_dir),
            "USERPROFILE": str(tmp_path),
        }), patch("app.services.backup.winreg") as mock_winreg:
            mock_winreg.OpenKey.side_effect = OSError("no key")

            results = detect_onedrive_paths(business_only=False)
            paths = [c["path"] for c in results]
            assert str(biz_dir) in paths
            assert str(personal_dir) in paths

    def test_candidates_include_is_business_flag(self, tmp_path):
        """Each candidate should have an is_business flag."""
        biz_dir = tmp_path / "OneDrive - Microsoft"
        biz_dir.mkdir()

        with patch.dict(os.environ, {
            "OneDriveCommercial": str(biz_dir),
            "OneDrive": "",
            "USERPROFILE": str(tmp_path),
        }), patch("app.services.backup.winreg") as mock_winreg:
            mock_winreg.OpenKey.side_effect = OSError("no key")

            results = detect_onedrive_paths(business_only=False)
            for c in results:
                assert "is_business" in c


class TestIsBusinessOneDrivePath:
    """Tests for the is_business_onedrive_path validation helper."""

    def test_path_under_business_dir(self, tmp_path):
        biz_dir = tmp_path / "OneDrive - Microsoft"
        biz_dir.mkdir()
        backup_path = biz_dir / "Backups" / "NoteHelper"
        backup_path.mkdir(parents=True)

        with patch.dict(os.environ, {
            "OneDriveCommercial": str(biz_dir),
            "OneDrive": "",
            "USERPROFILE": str(tmp_path),
        }), patch("app.services.backup.winreg") as mock_winreg:
            mock_winreg.OpenKey.side_effect = OSError("no key")

            assert is_business_onedrive_path(str(backup_path)) is True

    def test_path_under_personal_dir(self, tmp_path):
        personal_dir = tmp_path / "OneDrive"
        personal_dir.mkdir()
        backup_path = personal_dir / "Backups" / "NoteHelper"
        backup_path.mkdir(parents=True)

        with patch.dict(os.environ, {
            "OneDriveCommercial": "",
            "OneDrive": str(personal_dir),
            "USERPROFILE": str(tmp_path),
        }), patch("app.services.backup.winreg") as mock_winreg:
            mock_winreg.OpenKey.side_effect = OSError("no key")

            assert is_business_onedrive_path(str(backup_path)) is False

    def test_random_local_path_rejected(self, tmp_path):
        local_dir = tmp_path / "MyBackups"
        local_dir.mkdir()

        with patch.dict(os.environ, {
            "OneDriveCommercial": "",
            "OneDrive": "",
            "USERPROFILE": str(tmp_path),
        }), patch("app.services.backup.winreg") as mock_winreg:
            mock_winreg.OpenKey.side_effect = OSError("no key")

            assert is_business_onedrive_path(str(local_dir)) is False


class TestBackupHelpers:
    """Tests for backup helper functions."""

    def test_get_backup_config_returns_defaults_when_no_file(self, app):
        """Should return default config when backup_config.json doesn't exist."""
        with app.app_context():
            from app.routes.admin import _get_backup_config
            with patch('app.routes.admin.Path.exists', return_value=False):
                config = _get_backup_config()
            assert config['enabled'] is False
            assert config['onedrive_path'] == ''
            assert config['backup_dir'] == ''
            assert config['last_backup'] is None
            assert config['task_registered'] is False
            assert config['retention'] == {'daily': 7, 'weekly': 4, 'monthly': 3}

    def test_get_backup_config_loads_from_file(self, app, tmp_path):
        """Should load config from existing backup_config.json."""
        with app.app_context():
            from app.routes.admin import _get_backup_config
            config_data = {
                'enabled': True,
                'onedrive_path': 'C:\\Users\\test\\OneDrive',
                'backup_dir': 'C:\\Users\\test\\OneDrive\\Backups\\NoteHelper',
                'retention': {'daily': 7, 'weekly': 4, 'monthly': 3},
                'last_backup': '2025-01-15T10:30:00+00:00',
                'task_registered': True,
            }
            config_file = tmp_path / 'backup_config.json'
            config_file.write_text(json.dumps(config_data))

            with patch(
                'app.routes.admin._get_backup_config'
            ) as mock_get:
                mock_get.return_value = config_data
                result = mock_get()

            assert result['enabled'] is True
            assert result['backup_dir'] == 'C:\\Users\\test\\OneDrive\\Backups\\NoteHelper'
            assert result['task_registered'] is True

    def test_get_backup_config_handles_corrupt_json(self, app):
        """Should return defaults when JSON is corrupt."""
        with app.app_context():
            from app.routes.admin import _get_backup_config
            with patch('app.routes.admin.Path.exists', return_value=True), \
                 patch('builtins.open', side_effect=json.JSONDecodeError('err', '', 0)):
                config = _get_backup_config()
            assert config['enabled'] is False

    def test_list_backup_files_empty_dir(self, app, tmp_path):
        """Should return empty list when backup dir has no matching files."""
        with app.app_context():
            from app.routes.admin import _list_backup_files
            result = _list_backup_files(str(tmp_path))
            assert result == []

    def test_list_backup_files_with_backups(self, app, tmp_path):
        """Should list backup files sorted newest first."""
        with app.app_context():
            from app.routes.admin import _list_backup_files
            # Create some fake backup files
            (tmp_path / 'notehelper_2025-01-10_100000.db').write_bytes(b'x' * 1024)
            (tmp_path / 'notehelper_2025-01-15_100000.db').write_bytes(b'x' * 2048)
            (tmp_path / 'other_file.txt').write_text('not a backup')

            result = _list_backup_files(str(tmp_path))
            assert len(result) == 2
            # Newest first (sorted by filename descending)
            assert result[0]['name'] == 'notehelper_2025-01-15_100000.db'
            assert result[1]['name'] == 'notehelper_2025-01-10_100000.db'
            assert 'size_display' in result[0]
            assert 'modified_iso' in result[0]

    def test_list_backup_files_respects_limit(self, app, tmp_path):
        """Should limit the number of returned backup files."""
        with app.app_context():
            from app.routes.admin import _list_backup_files
            for i in range(15):
                (tmp_path / f'notehelper_2025-01-{i+1:02d}_100000.db').write_bytes(b'x' * 512)

            result = _list_backup_files(str(tmp_path), limit=5)
            assert len(result) == 5

    def test_list_backup_files_nonexistent_dir(self, app):
        """Should return empty list for nonexistent directory."""
        with app.app_context():
            from app.routes.admin import _list_backup_files
            result = _list_backup_files('/path/that/does/not/exist')
            assert result == []

    def test_list_backup_files_empty_string(self, app):
        """Should return empty list for empty string dir."""
        with app.app_context():
            from app.routes.admin import _list_backup_files
            result = _list_backup_files('')
            assert result == []


class TestCheckScheduledTask:
    """Tests for _check_scheduled_task live Task Scheduler query."""

    def test_task_exists_parses_csv(self, app):
        """Should parse schtasks CSV output when task exists."""
        with app.app_context():
            from app.routes.admin import _check_scheduled_task
            csv_output = '"\\NoteHelper-DailyBackup","3/8/2026 11:00:00 AM","Ready"\n'
            with patch('app.routes.admin.subprocess.run') as mock_run:
                mock_run.return_value = type('R', (), {
                    'returncode': 0, 'stdout': csv_output, 'stderr': ''
                })()
                result = _check_scheduled_task()
            assert result['exists'] is True
            assert result['next_run'] == '3/8/2026 11:00:00 AM'
            assert result['status'] == 'Ready'

    def test_task_not_found(self, app):
        """Should return exists=False when schtasks returns non-zero."""
        with app.app_context():
            from app.routes.admin import _check_scheduled_task
            with patch('app.routes.admin.subprocess.run') as mock_run:
                mock_run.return_value = type('R', (), {
                    'returncode': 1, 'stdout': '', 'stderr': 'ERROR: not found'
                })()
                result = _check_scheduled_task()
            assert result['exists'] is False
            assert result['next_run'] is None

    def test_task_check_handles_timeout(self, app):
        """Should gracefully handle subprocess timeout."""
        with app.app_context():
            from app.routes.admin import _check_scheduled_task
            with patch('app.routes.admin.subprocess.run',
                       side_effect=subprocess.TimeoutExpired('schtasks', 5)):
                result = _check_scheduled_task()
            assert result['exists'] is False

    def test_task_check_non_windows(self, app):
        """Should return exists=False on non-Windows platforms."""
        with app.app_context():
            from app.routes.admin import _check_scheduled_task
            with patch('app.routes.admin.sys') as mock_sys:
                mock_sys.platform = 'linux'
                result = _check_scheduled_task()
            assert result['exists'] is False


class TestBackupStatusAPI:
    """Tests for GET /api/admin/backup/status."""

    def test_backup_status_not_configured(self, client, app):
        """Should return not-configured status when no config exists."""
        with patch('app.routes.admin._get_backup_config') as mock_config, \
             patch('app.routes.admin._check_scheduled_task') as mock_task:
            mock_config.return_value = {
                'enabled': False,
                'onedrive_path': '',
                'backup_dir': '',
                'retention': {'daily': 7, 'weekly': 4, 'monthly': 3},
                'last_backup': None,
                'task_registered': False,
            }
            mock_task.return_value = {'exists': False, 'next_run': None, 'status': None}
            response = client.get('/api/admin/backup/status')

        assert response.status_code == 200
        data = response.get_json()
        assert data['enabled'] is False
        assert data['recent_backups'] == []
        assert data['task_exists'] is False

    def test_backup_status_configured(self, client, app, tmp_path):
        """Should return configured status with backup list."""
        # Create a fake backup file
        (tmp_path / 'notehelper_2025-01-15_100000.db').write_bytes(b'x' * 4096)

        with patch('app.routes.admin._get_backup_config') as mock_config, \
             patch('app.routes.admin._check_scheduled_task') as mock_task:
            mock_config.return_value = {
                'enabled': True,
                'onedrive_path': 'C:\\OneDrive',
                'backup_dir': str(tmp_path),
                'retention': {'daily': 7, 'weekly': 4, 'monthly': 3},
                'last_backup': '2025-01-15T10:30:00+00:00',
                'task_registered': True,
            }
            mock_task.return_value = {'exists': True, 'next_run': '3/8/2026 11:00:00 AM', 'status': 'Ready'}
            response = client.get('/api/admin/backup/status')

        assert response.status_code == 200
        data = response.get_json()
        assert data['enabled'] is True
        assert data['task_registered'] is True
        assert data['task_exists'] is True
        assert data['task_next_run'] == '3/8/2026 11:00:00 AM'
        assert data['task_status'] == 'Ready'
        assert data['last_backup'] == '2025-01-15T10:30:00+00:00'
        assert len(data['recent_backups']) == 1
        assert data['recent_backups'][0]['name'] == 'notehelper_2025-01-15_100000.db'

    def test_backup_status_task_missing_but_config_says_registered(self, client, app):
        """task_exists should be False even when task_registered is True."""
        with patch('app.routes.admin._get_backup_config') as mock_config, \
             patch('app.routes.admin._check_scheduled_task') as mock_task:
            mock_config.return_value = {
                'enabled': True,
                'onedrive_path': 'C:\\OneDrive',
                'backup_dir': 'C:\\fake',
                'retention': {'daily': 7, 'weekly': 4, 'monthly': 3},
                'last_backup': None,
                'task_registered': True,
            }
            mock_task.return_value = {'exists': False, 'next_run': None, 'status': None}
            response = client.get('/api/admin/backup/status')

        assert response.status_code == 200
        data = response.get_json()
        assert data['task_registered'] is True
        assert data['task_exists'] is False
        assert data['task_next_run'] is None


class TestBackupRunAPI:
    """Tests for POST /api/admin/backup/run."""

    def test_backup_run_not_configured(self, client, app):
        """Should return 400 when backups are not configured."""
        with patch('app.routes.admin._get_backup_config') as mock_config:
            mock_config.return_value = {
                'enabled': False,
                'backup_dir': '',
            }
            response = client.post('/api/admin/backup/run')

        assert response.status_code == 400
        data = response.get_json()
        assert data['success'] is False
        assert 'not configured' in data['error'].lower()

    def test_backup_run_no_database(self, client, app, tmp_path):
        """Should return 404 when database file doesn't exist."""
        with patch('app.routes.admin._get_backup_config') as mock_config, \
             patch('app.routes.admin.Path.exists', return_value=False):
            mock_config.return_value = {
                'enabled': True,
                'backup_dir': str(tmp_path),
            }
            response = client.post('/api/admin/backup/run')

        assert response.status_code == 404

    def test_backup_run_success(self, client, app, tmp_path):
        """Should successfully create a backup copy."""
        backup_dir = tmp_path / 'backups'
        backup_dir.mkdir()

        # Create a fake DB file
        fake_db = tmp_path / 'notehelper.db'
        fake_db.write_bytes(b'SQLite DB content' * 100)

        config = {
            'enabled': True,
            'backup_dir': str(backup_dir),
            'last_backup': None,
        }

        with patch('app.routes.admin._get_backup_config', return_value=config), \
             patch('app.routes.admin._save_backup_config') as mock_save, \
             patch('app.routes.admin.Path', wraps=Path) as mock_path_cls:
            # Make the db_path check return our fake db
            original_truediv = Path.__truediv__

            def patched_truediv(self, other):
                result = original_truediv(self, other)
                if str(other) == 'notehelper.db' and 'data' in str(self):
                    return fake_db
                return result

            with patch.object(Path, '__truediv__', patched_truediv):
                response = client.post('/api/admin/backup/run')

        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        assert 'Backup created' in data['message']

    def test_backup_run_creates_file_in_backup_dir(self, client, app, tmp_path):
        """Should create an actual .db file in the backup directory."""
        backup_dir = tmp_path / 'backups'
        backup_dir.mkdir()

        fake_db = tmp_path / 'notehelper.db'
        fake_db.write_bytes(b'test database content')

        config = {
            'enabled': True,
            'backup_dir': str(backup_dir),
            'last_backup': None,
        }

        with patch('app.routes.admin._get_backup_config', return_value=config), \
             patch('app.routes.admin._save_backup_config'):
            # Patch the db_path construction
            original_init = Path.__truediv__

            def patched(self, other):
                result = original_init(self, other)
                if str(other) == 'notehelper.db' and 'data' in str(self):
                    return fake_db
                return result

            with patch.object(Path, '__truediv__', patched):
                response = client.post('/api/admin/backup/run')

        assert response.status_code == 200
        backup_files = list(backup_dir.glob('notehelper_*.db'))
        assert len(backup_files) == 1
        assert backup_files[0].read_bytes() == b'test database content'

    def test_backup_run_enabled_but_empty_dir(self, client, app):
        """Should return 400 when enabled but backup_dir is empty."""
        with patch('app.routes.admin._get_backup_config') as mock_config:
            mock_config.return_value = {
                'enabled': True,
                'backup_dir': '',
            }
            response = client.post('/api/admin/backup/run')

        assert response.status_code == 400


# =========================================================================
# Export / Import round-trip tests
# =========================================================================

class TestCustomerToDict:
    """Tests for _customer_to_dict serialization."""

    def test_export_version_3(self, app):
        """Exported dict should have _version 3."""
        from app.models import db, Customer, Seller, Territory
        from app.services.backup import _customer_to_dict

        with app.app_context():
            seller = Seller(name='Test Seller', alias='ts', seller_type='Growth')
            territory = Territory(name='Test Territory')
            db.session.add_all([seller, territory])
            db.session.flush()
            customer = Customer(
                name='Export Test', tpid=5000,
                seller_id=seller.id, territory_id=territory.id,
            )
            db.session.add(customer)
            db.session.commit()

            data = _customer_to_dict(customer)
            assert data["_version"] == 3
            assert data["_notehelper_backup"] is True
            assert "engagements" in data
            assert "notes" in data

    def test_export_includes_note_milestones(self, app):
        """Notes should include linked milestone IDs."""
        from app.models import (
            db, Customer, Note, Milestone, Opportunity, Seller, Territory,
        )
        from app.services.backup import _customer_to_dict

        with app.app_context():
            seller = Seller(name='S', alias='s', seller_type='Growth')
            territory = Territory(name='T')
            db.session.add_all([seller, territory])
            db.session.flush()

            customer = Customer(
                name='MS Test', tpid=5001,
                seller_id=seller.id, territory_id=territory.id,
            )
            db.session.add(customer)
            db.session.flush()

            opp = Opportunity(
                msx_opportunity_id='OPP-AAA', name='Opp A',
                customer_id=customer.id,
            )
            db.session.add(opp)
            db.session.flush()

            ms = Milestone(
                msx_milestone_id='MS-111', url='https://example.com/ms1',
                title='Milestone 1', customer_id=customer.id,
                opportunity_id=opp.id,
            )
            db.session.add(ms)
            db.session.flush()

            note = Note(
                customer_id=customer.id,
                call_date=datetime(2025, 1, 15, 10, 0, tzinfo=timezone.utc),
                content='Test note with milestone',
            )
            note.milestones.append(ms)
            db.session.add(note)
            db.session.commit()

            data = _customer_to_dict(customer)
            assert len(data["notes"]) == 1
            assert data["notes"][0]["milestones"] == ["MS-111"]

    def test_export_includes_engagements_with_story_fields(self, app):
        """Engagements should include all story fields and relationships."""
        from app.models import (
            db, Customer, Note, Engagement, Milestone, Opportunity,
            Seller, Territory,
        )
        from app.services.backup import _customer_to_dict

        with app.app_context():
            seller = Seller(name='S', alias='s', seller_type='Growth')
            territory = Territory(name='T')
            db.session.add_all([seller, territory])
            db.session.flush()

            customer = Customer(
                name='Eng Test', tpid=5002,
                seller_id=seller.id, territory_id=territory.id,
            )
            db.session.add(customer)
            db.session.flush()

            opp = Opportunity(
                msx_opportunity_id='OPP-BBB', name='Opp B',
                customer_id=customer.id,
            )
            db.session.add(opp)
            db.session.flush()

            ms = Milestone(
                msx_milestone_id='MS-222', url='https://example.com/ms2',
                title='Milestone 2', customer_id=customer.id,
                opportunity_id=opp.id,
            )
            db.session.add(ms)
            db.session.flush()

            note = Note(
                customer_id=customer.id,
                call_date=datetime(2025, 2, 1, 14, 0, tzinfo=timezone.utc),
                content='Engagement linked note',
            )
            db.session.add(note)
            db.session.flush()

            from datetime import date as date_type
            eng = Engagement(
                customer_id=customer.id,
                title='Cloud Migration',
                status='Active',
                key_individuals='CTO John',
                technical_problem='Legacy on-prem',
                business_impact='$2M annual savings',
                solution_resources='Azure Migrate',
                estimated_acr='$500K',
                target_date=date_type(2025, 6, 30),
            )
            eng.notes.append(note)
            eng.opportunities.append(opp)
            eng.milestones.append(ms)
            db.session.add(eng)
            db.session.commit()

            data = _customer_to_dict(customer)
            assert len(data["engagements"]) == 1

            eng_data = data["engagements"][0]
            assert eng_data["title"] == "Cloud Migration"
            assert eng_data["status"] == "Active"
            assert eng_data["key_individuals"] == "CTO John"
            assert eng_data["technical_problem"] == "Legacy on-prem"
            assert eng_data["business_impact"] == "$2M annual savings"
            assert eng_data["solution_resources"] == "Azure Migrate"
            assert eng_data["estimated_acr"] == "$500K"
            assert eng_data["target_date"] == "2025-06-30"
            assert len(eng_data["linked_notes"]) == 1
            assert eng_data["linked_opportunities"] == ["OPP-BBB"]
            assert eng_data["linked_milestones"] == ["MS-222"]

    def test_export_customer_without_engagements(self, app):
        """Customers with no engagements should have empty engagements list."""
        from app.models import db, Customer, Seller, Territory
        from app.services.backup import _customer_to_dict

        with app.app_context():
            seller = Seller(name='S', alias='s', seller_type='Growth')
            territory = Territory(name='T')
            db.session.add_all([seller, territory])
            db.session.flush()

            customer = Customer(
                name='No Eng Customer', tpid=5003,
                seller_id=seller.id, territory_id=territory.id,
            )
            db.session.add(customer)
            db.session.commit()

            data = _customer_to_dict(customer)
            assert data["engagements"] == []


class TestRestoreFromBackup:
    """Tests for restore_from_backup with v3 format."""

    def _make_v3_backup(
        self,
        tpid: int = 5010,
        notes: list = None,
        engagements: list = None,
        account_context: str = None,
    ) -> dict:
        """Helper to build a v3 backup payload."""
        return {
            "_notehelper_backup": True,
            "_version": 3,
            "_exported_at": datetime.now(timezone.utc).isoformat(),
            "customer": {
                "name": "Restore Test",
                "nickname": "RT",
                "tpid": tpid,
                "tpid_url": "https://example.com/rt",
                "account_context": account_context,
                "seller_name": "Seller",
                "territory_name": "Territory",
                "verticals": [],
            },
            "notes": notes or [],
            "engagements": engagements or [],
        }

    def test_restore_invalid_payload(self, app):
        """Non-backup payload should be rejected."""
        from app.services.backup import restore_from_backup

        with app.app_context():
            result = restore_from_backup({"foo": "bar"})
            assert result["success"] is False
            assert "Invalid" in result["error"]

    def test_restore_missing_tpid(self, app):
        """Backup without TPID should be rejected."""
        from app.services.backup import restore_from_backup

        with app.app_context():
            data = {"_notehelper_backup": True, "customer": {}}
            result = restore_from_backup(data)
            assert result["success"] is False
            assert "TPID" in result["error"]

    def test_restore_customer_not_found(self, app):
        """Should fail if customer with TPID doesn't exist."""
        from app.services.backup import restore_from_backup

        with app.app_context():
            data = self._make_v3_backup(tpid=99999)
            result = restore_from_backup(data)
            assert result["success"] is False
            assert "not found" in result["error"]

    def test_restore_creates_notes(self, app):
        """Restore should create notes that don't exist yet."""
        from app.models import db, Customer, Note
        from app.services.backup import restore_from_backup

        with app.app_context():
            customer = Customer(name='Restore Notes', tpid=5010)
            db.session.add(customer)
            db.session.commit()

            data = self._make_v3_backup(notes=[
                {
                    "call_date": "2025-03-01T10:00:00+00:00",
                    "content": "First note",
                    "topics": ["Azure VM"],
                    "partners": ["Contoso"],
                    "milestones": [],
                },
                {
                    "call_date": "2025-03-02T10:00:00+00:00",
                    "content": "Second note",
                    "topics": [],
                    "partners": [],
                    "milestones": [],
                },
            ])

            result = restore_from_backup(data)
            assert result["success"] is True
            assert result["logs_created"] == 2
            assert result["logs_skipped"] == 0

            notes = Note.query.filter_by(customer_id=customer.id).all()
            assert len(notes) == 2

    def test_restore_deduplicates_notes_by_date(self, app):
        """Existing notes with same call_date should be skipped."""
        from app.models import db, Customer, Note
        from app.services.backup import restore_from_backup

        with app.app_context():
            customer = Customer(name='Dedup Test', tpid=5011)
            db.session.add(customer)
            db.session.flush()

            existing = Note(
                customer_id=customer.id,
                call_date=datetime(2025, 3, 1, 10, 0, tzinfo=timezone.utc),
                content='Already exists',
            )
            db.session.add(existing)
            db.session.commit()

            data = self._make_v3_backup(tpid=5011, notes=[
                {
                    "call_date": "2025-03-01T10:00:00+00:00",
                    "content": "Duplicate",
                    "topics": [],
                    "partners": [],
                    "milestones": [],
                },
                {
                    "call_date": "2025-03-05T10:00:00+00:00",
                    "content": "New one",
                    "topics": [],
                    "partners": [],
                    "milestones": [],
                },
            ])

            result = restore_from_backup(data)
            assert result["logs_created"] == 1
            assert result["logs_skipped"] == 1

    def test_restore_links_note_milestones(self, app):
        """Restored notes should be linked to milestones by msx_milestone_id."""
        from app.models import db, Customer, Note, Milestone, Opportunity
        from app.services.backup import restore_from_backup

        with app.app_context():
            customer = Customer(name='MS Link Test', tpid=5012)
            db.session.add(customer)
            db.session.flush()

            opp = Opportunity(
                msx_opportunity_id='OPP-RESTORE', name='Restore Opp',
                customer_id=customer.id,
            )
            db.session.add(opp)
            db.session.flush()

            ms = Milestone(
                msx_milestone_id='MS-RESTORE-1',
                url='https://example.com/ms',
                title='Restore MS',
                customer_id=customer.id,
                opportunity_id=opp.id,
            )
            db.session.add(ms)
            db.session.commit()

            data = self._make_v3_backup(tpid=5012, notes=[
                {
                    "call_date": "2025-04-01T10:00:00+00:00",
                    "content": "Note with milestone",
                    "topics": [],
                    "partners": [],
                    "milestones": ["MS-RESTORE-1"],
                },
            ])

            result = restore_from_backup(data)
            assert result["success"] is True
            assert result["logs_created"] == 1

            note = Note.query.filter_by(customer_id=customer.id).first()
            assert len(note.milestones) == 1
            assert note.milestones[0].msx_milestone_id == "MS-RESTORE-1"

    def test_restore_relinks_milestones_on_existing_notes(self, app):
        """Skipped (existing) notes should still get missing milestone links."""
        from app.models import db, Customer, Note, Milestone, Opportunity
        from app.services.backup import restore_from_backup

        with app.app_context():
            customer = Customer(name='Relink Test', tpid=5013)
            db.session.add(customer)
            db.session.flush()

            opp = Opportunity(
                msx_opportunity_id='OPP-RL', name='Relink Opp',
                customer_id=customer.id,
            )
            db.session.add(opp)
            db.session.flush()

            ms = Milestone(
                msx_milestone_id='MS-RELINK',
                url='https://example.com/rl',
                title='Relink MS',
                customer_id=customer.id,
                opportunity_id=opp.id,
            )
            db.session.add(ms)
            db.session.flush()

            # Existing note WITHOUT the milestone link
            existing = Note(
                customer_id=customer.id,
                call_date=datetime(2025, 4, 10, 10, 0, tzinfo=timezone.utc),
                content='Existing without milestone',
            )
            db.session.add(existing)
            db.session.commit()

            assert len(existing.milestones) == 0

            data = self._make_v3_backup(tpid=5013, notes=[
                {
                    "call_date": "2025-04-10T10:00:00+00:00",
                    "content": "Same note",
                    "topics": [],
                    "partners": [],
                    "milestones": ["MS-RELINK"],
                },
            ])

            result = restore_from_backup(data)
            assert result["logs_skipped"] == 1  # Note existed

            # Milestone link should now be present
            db.session.refresh(existing)
            assert len(existing.milestones) == 1
            assert existing.milestones[0].msx_milestone_id == "MS-RELINK"

    def test_restore_creates_engagements(self, app):
        """Restore should create engagements with all story fields."""
        from app.models import db, Customer, Engagement
        from app.services.backup import restore_from_backup

        with app.app_context():
            customer = Customer(name='Eng Restore', tpid=5020)
            db.session.add(customer)
            db.session.commit()

            data = self._make_v3_backup(tpid=5020, engagements=[
                {
                    "title": "Cloud Migration",
                    "status": "Active",
                    "key_individuals": "CTO Jane",
                    "technical_problem": "Legacy systems",
                    "business_impact": "Cost savings",
                    "solution_resources": "Azure Migrate",
                    "estimated_acr": "$300K",
                    "target_date": "2025-12-31",
                    "created_at": "2025-01-01T00:00:00+00:00",
                    "updated_at": "2025-01-15T00:00:00+00:00",
                    "linked_notes": [],
                    "linked_opportunities": [],
                    "linked_milestones": [],
                },
            ])

            result = restore_from_backup(data)
            assert result["success"] is True
            assert result["engagements_created"] == 1
            assert result["engagements_skipped"] == 0

            eng = Engagement.query.filter_by(customer_id=customer.id).first()
            assert eng.title == "Cloud Migration"
            assert eng.status == "Active"
            assert eng.key_individuals == "CTO Jane"
            assert eng.technical_problem == "Legacy systems"
            assert eng.business_impact == "Cost savings"
            assert eng.solution_resources == "Azure Migrate"
            assert eng.estimated_acr == "$300K"
            assert eng.target_date.isoformat() == "2025-12-31"

    def test_restore_deduplicates_engagements_by_title(self, app):
        """Existing engagement with same title should be skipped."""
        from app.models import db, Customer, Engagement
        from app.services.backup import restore_from_backup

        with app.app_context():
            customer = Customer(name='Eng Dedup', tpid=5021)
            db.session.add(customer)
            db.session.flush()

            existing = Engagement(
                customer_id=customer.id,
                title='Already Exists',
                status='Active',
            )
            db.session.add(existing)
            db.session.commit()

            data = self._make_v3_backup(tpid=5021, engagements=[
                {
                    "title": "Already Exists",
                    "status": "Won",
                    "linked_notes": [],
                    "linked_opportunities": [],
                    "linked_milestones": [],
                },
                {
                    "title": "Brand New",
                    "status": "Active",
                    "linked_notes": [],
                    "linked_opportunities": [],
                    "linked_milestones": [],
                },
            ])

            result = restore_from_backup(data)
            assert result["engagements_created"] == 1
            assert result["engagements_skipped"] == 1

    def test_restore_links_engagement_to_notes(self, app):
        """Restored engagement should be linked to notes by call_date."""
        from app.models import db, Customer, Note, Engagement
        from app.services.backup import restore_from_backup

        with app.app_context():
            customer = Customer(name='Eng Note Link', tpid=5022)
            db.session.add(customer)
            db.session.flush()

            # Pre-existing note
            note = Note(
                customer_id=customer.id,
                call_date=datetime(2025, 5, 1, 10, 0, tzinfo=timezone.utc),
                content='Existing note',
            )
            db.session.add(note)
            db.session.commit()

            data = self._make_v3_backup(tpid=5022, engagements=[
                {
                    "title": "Linked Engagement",
                    "status": "Active",
                    "linked_notes": ["2025-05-01T10:00:00+00:00"],
                    "linked_opportunities": [],
                    "linked_milestones": [],
                },
            ])

            result = restore_from_backup(data)
            assert result["engagements_created"] == 1

            eng = Engagement.query.filter_by(
                customer_id=customer.id, title="Linked Engagement"
            ).first()
            assert len(eng.notes) == 1
            assert eng.notes[0].content == "Existing note"

    def test_restore_links_engagement_to_opportunities_and_milestones(self, app):
        """Restored engagement should be linked to opps and milestones."""
        from app.models import (
            db, Customer, Engagement, Milestone, Opportunity,
        )
        from app.services.backup import restore_from_backup

        with app.app_context():
            customer = Customer(name='Eng Full Link', tpid=5023)
            db.session.add(customer)
            db.session.flush()

            opp = Opportunity(
                msx_opportunity_id='OPP-ENG',
                name='Eng Opp',
                customer_id=customer.id,
            )
            db.session.add(opp)
            db.session.flush()

            ms = Milestone(
                msx_milestone_id='MS-ENG',
                url='https://example.com/eng-ms',
                title='Eng Milestone',
                customer_id=customer.id,
                opportunity_id=opp.id,
            )
            db.session.add(ms)
            db.session.commit()

            data = self._make_v3_backup(tpid=5023, engagements=[
                {
                    "title": "Full Links",
                    "status": "Active",
                    "linked_notes": [],
                    "linked_opportunities": ["OPP-ENG"],
                    "linked_milestones": ["MS-ENG"],
                },
            ])

            result = restore_from_backup(data)
            assert result["engagements_created"] == 1

            eng = Engagement.query.filter_by(
                customer_id=customer.id, title="Full Links"
            ).first()
            assert len(eng.opportunities) == 1
            assert eng.opportunities[0].msx_opportunity_id == "OPP-ENG"
            assert len(eng.milestones) == 1
            assert eng.milestones[0].msx_milestone_id == "MS-ENG"

    def test_restore_v2_backup_no_engagements(self, app):
        """v2 backups without engagements key should restore notes only."""
        from app.models import db, Customer, Note
        from app.services.backup import restore_from_backup

        with app.app_context():
            customer = Customer(name='V2 Compat', tpid=5030)
            db.session.add(customer)
            db.session.commit()

            data = {
                "_notehelper_backup": True,
                "_version": 2,
                "customer": {
                    "name": "V2 Compat",
                    "tpid": 5030,
                },
                "notes": [
                    {
                        "call_date": "2025-01-10T09:00:00+00:00",
                        "content": "V2 note",
                        "topics": [],
                        "partners": [],
                    },
                ],
            }

            result = restore_from_backup(data)
            assert result["success"] is True
            assert result["logs_created"] == 1
            assert result["engagements_created"] == 0
            assert result["engagements_skipped"] == 0

            notes = Note.query.filter_by(customer_id=customer.id).all()
            assert len(notes) == 1

    def test_restore_account_context(self, app):
        """Should restore account_context if customer has none."""
        from app.models import db, Customer
        from app.services.backup import restore_from_backup

        with app.app_context():
            customer = Customer(name='Context Test', tpid=5040)
            db.session.add(customer)
            db.session.commit()

            data = self._make_v3_backup(
                tpid=5040, account_context="Important context"
            )

            result = restore_from_backup(data)
            assert result["success"] is True

            db.session.refresh(customer)
            assert customer.account_context == "Important context"

    def test_restore_does_not_overwrite_existing_context(self, app):
        """Should NOT overwrite existing account_context."""
        from app.models import db, Customer
        from app.services.backup import restore_from_backup

        with app.app_context():
            customer = Customer(
                name='Context Keep', tpid=5041,
                account_context="Original context",
            )
            db.session.add(customer)
            db.session.commit()

            data = self._make_v3_backup(
                tpid=5041, account_context="Backup context trying to overwrite"
            )

            result = restore_from_backup(data)
            assert result["success"] is True

            db.session.refresh(customer)
            assert customer.account_context == "Original context"

    def test_full_round_trip(self, app):
        """Export a customer then restore into empty state — full round trip."""
        from app.models import (
            db, Customer, Note, Engagement, Milestone, Opportunity,
            Seller, Territory, Topic, Partner,
        )
        from app.services.backup import _customer_to_dict, restore_from_backup

        with app.app_context():
            # --- Build rich customer data ---
            seller = Seller(name='RT Seller', alias='rts', seller_type='Growth')
            territory = Territory(name='RT Territory')
            db.session.add_all([seller, territory])
            db.session.flush()

            customer = Customer(
                name='Round Trip Co', tpid=6000, nickname='RTC',
                account_context='Strategic customer',
                seller_id=seller.id, territory_id=territory.id,
            )
            db.session.add(customer)
            db.session.flush()

            topic = Topic(name='Kubernetes')
            partner = Partner(name='Partner Corp')
            db.session.add_all([topic, partner])
            db.session.flush()

            opp = Opportunity(
                msx_opportunity_id='OPP-RT', name='RT Opp',
                customer_id=customer.id,
            )
            db.session.add(opp)
            db.session.flush()

            ms = Milestone(
                msx_milestone_id='MS-RT',
                url='https://example.com/rt-ms',
                title='RT Milestone',
                customer_id=customer.id,
                opportunity_id=opp.id,
            )
            db.session.add(ms)
            db.session.flush()

            note1 = Note(
                customer_id=customer.id,
                call_date=datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc),
                content='<p>Note with <img src="data:image/png;base64,abc123"> image</p>',
            )
            note1.topics.append(topic)
            note1.partners.append(partner)
            note1.milestones.append(ms)
            db.session.add(note1)

            note2 = Note(
                customer_id=customer.id,
                call_date=datetime(2025, 6, 15, 14, 0, tzinfo=timezone.utc),
                content='<p>Second note</p>',
            )
            db.session.add(note2)
            db.session.flush()

            from datetime import date as date_type
            eng = Engagement(
                customer_id=customer.id,
                title='AKS Deployment',
                status='Active',
                key_individuals='CTO, VP Eng',
                technical_problem='Container orchestration',
                business_impact='Faster releases',
                solution_resources='AKS + Arc',
                estimated_acr='$200K',
                target_date=date_type(2025, 9, 30),
            )
            eng.notes.append(note1)
            eng.opportunities.append(opp)
            eng.milestones.append(ms)
            db.session.add(eng)
            db.session.commit()

            # --- Export ---
            exported = _customer_to_dict(customer)

            # --- Verify export structure ---
            assert exported["_version"] == 3
            assert len(exported["notes"]) == 2
            assert len(exported["engagements"]) == 1
            assert exported["engagements"][0]["title"] == "AKS Deployment"
            assert "data:image/png;base64,abc123" in exported["notes"][0]["content"] or \
                   "data:image/png;base64,abc123" in exported["notes"][1]["content"]

            # --- Wipe customer-specific data (simulate disaster) ---
            for n in customer.notes:
                n.topics.clear()
                n.partners.clear()
                n.milestones.clear()
            for e in customer.engagements:
                e.notes.clear()
                e.opportunities.clear()
                e.milestones.clear()
            db.session.flush()

            Engagement.query.filter_by(customer_id=customer.id).delete()
            Note.query.filter_by(customer_id=customer.id).delete()
            customer.account_context = None
            db.session.commit()

            assert Note.query.filter_by(customer_id=customer.id).count() == 0
            assert Engagement.query.filter_by(customer_id=customer.id).count() == 0

            # --- Restore ---
            result = restore_from_backup(exported)
            assert result["success"] is True
            assert result["logs_created"] == 2
            assert result["engagements_created"] == 1

            # Verify notes restored
            notes = Note.query.filter_by(customer_id=customer.id).order_by(
                Note.call_date
            ).all()
            assert len(notes) == 2

            # Verify inline image survived round trip
            image_note = [n for n in notes if "base64" in n.content]
            assert len(image_note) == 1

            # Verify topics and partners restored
            topics_restored = [t.name for n in notes for t in n.topics]
            assert "Kubernetes" in topics_restored
            partners_restored = [p.name for n in notes for p in n.partners]
            assert "Partner Corp" in partners_restored

            # Verify note→milestone link restored
            ms_note = [n for n in notes if len(n.milestones) > 0]
            assert len(ms_note) == 1
            assert ms_note[0].milestones[0].msx_milestone_id == "MS-RT"

            # Verify engagement restored with story fields
            eng = Engagement.query.filter_by(customer_id=customer.id).first()
            assert eng.title == "AKS Deployment"
            assert eng.key_individuals == "CTO, VP Eng"
            assert eng.estimated_acr == "$200K"

            # Verify engagement links
            assert len(eng.notes) == 1
            assert len(eng.opportunities) == 1
            assert eng.opportunities[0].msx_opportunity_id == "OPP-RT"
            assert len(eng.milestones) == 1
            assert eng.milestones[0].msx_milestone_id == "MS-RT"

            # Verify account context restored
            db.session.refresh(customer)
            assert customer.account_context == "Strategic customer"
