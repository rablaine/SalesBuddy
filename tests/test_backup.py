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
        backup_path = biz_dir / "Backups" / "SalesBuddy"
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
        backup_path = personal_dir / "Backups" / "SalesBuddy"
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

    def test_get_onedrive_path_empty_when_not_set(self, app):
        """Should return empty string when no UserPreference onedrive_path set."""
        with app.app_context():
            from app.routes.admin import _get_onedrive_path
            from app.models import UserPreference, db
            prefs = UserPreference.query.first()
            if prefs:
                prefs.onedrive_path = None
                db.session.commit()
            result = _get_onedrive_path()
            assert result == ''

    def test_get_onedrive_path_returns_stored_value(self, app):
        """Should return the stored OneDrive path from UserPreference."""
        with app.app_context():
            from app.routes.admin import _get_onedrive_path
            from app.models import UserPreference, db
            prefs = UserPreference.query.first()
            prefs.onedrive_path = 'C:\\Users\\test\\OneDrive'
            db.session.commit()
            result = _get_onedrive_path()
            assert result == 'C:\\Users\\test\\OneDrive'
            # Clean up
            prefs.onedrive_path = None
            db.session.commit()

    def test_get_backup_dir_empty_when_no_path(self, app):
        """Should return empty string when no OneDrive path configured."""
        with app.app_context():
            from app.routes.admin import _get_backup_dir
            from app.models import UserPreference, db
            prefs = UserPreference.query.first()
            prefs.onedrive_path = None
            db.session.commit()
            result = _get_backup_dir()
            assert result == ''
            prefs.onedrive_path = None
            db.session.commit()

    def test_get_backup_dir_derives_path(self, app):
        """Should derive backup dir as {onedrive_path}/Backups/SalesBuddy."""
        with app.app_context():
            from app.routes.admin import _get_backup_dir
            from app.models import UserPreference, db
            prefs = UserPreference.query.first()
            prefs.onedrive_path = 'C:\\Users\\test\\OneDrive'
            db.session.commit()
            result = _get_backup_dir()
            assert result == os.path.join('C:\\Users\\test\\OneDrive', 'Backups', 'SalesBuddy')
            prefs.onedrive_path = None
            db.session.commit()

    def test_get_retention_defaults(self, app):
        """Should return default retention values from UserPreference."""
        with app.app_context():
            from app.routes.admin import _get_retention
            result = _get_retention()
            assert result == {'daily': 7, 'weekly': 4, 'monthly': 3}

    def test_get_last_backup_from_files(self, app, tmp_path):
        """Should deduce last backup from newest file timestamp."""
        with app.app_context():
            from app.routes.admin import _get_last_backup
            (tmp_path / 'salesbuddy_2025-01-15_100000.db').write_bytes(b'x')
            result = _get_last_backup(str(tmp_path))
            assert result is not None

    def test_get_last_backup_empty_dir(self, app, tmp_path):
        """Should return None for empty backup directory."""
        with app.app_context():
            from app.routes.admin import _get_last_backup
            result = _get_last_backup(str(tmp_path))
            assert result is None

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
            (tmp_path / 'salesbuddy_2025-01-10_100000.db').write_bytes(b'x' * 1024)
            (tmp_path / 'salesbuddy_2025-01-15_100000.db').write_bytes(b'x' * 2048)
            (tmp_path / 'other_file.txt').write_text('not a backup')

            result = _list_backup_files(str(tmp_path))
            assert len(result) == 2
            # Newest first (sorted by filename descending)
            assert result[0]['name'] == 'salesbuddy_2025-01-15_100000.db'
            assert result[1]['name'] == 'salesbuddy_2025-01-10_100000.db'
            assert 'size_display' in result[0]
            assert 'modified_iso' in result[0]

    def test_list_backup_files_respects_limit(self, app, tmp_path):
        """Should limit the number of returned backup files."""
        with app.app_context():
            from app.routes.admin import _list_backup_files
            for i in range(15):
                (tmp_path / f'salesbuddy_2025-01-{i+1:02d}_100000.db').write_bytes(b'x' * 512)

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
            csv_output = '"\\SalesBuddy-DailyBackup","3/8/2026 11:00:00 AM","Ready"\n'
            with patch('app.routes.admin.subprocess.run') as mock_run:
                mock_run.return_value = type('R', (), {
                    'returncode': 0, 'stdout': csv_output, 'stderr': ''
                })()
                result = _check_scheduled_task('SalesBuddy-DailyBackup')
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
                result = _check_scheduled_task('SalesBuddy-DailyBackup')
            assert result['exists'] is False
            assert result['next_run'] is None

    def test_task_check_handles_timeout(self, app):
        """Should gracefully handle subprocess timeout."""
        with app.app_context():
            from app.routes.admin import _check_scheduled_task
            with patch('app.routes.admin.subprocess.run',
                       side_effect=subprocess.TimeoutExpired('schtasks', 5)):
                result = _check_scheduled_task('SalesBuddy-DailyBackup')
            assert result['exists'] is False

    def test_task_check_non_windows(self, app):
        """Should return exists=False on non-Windows platforms."""
        with app.app_context():
            from app.routes.admin import _check_scheduled_task
            with patch('app.routes.admin.sys') as mock_sys:
                mock_sys.platform = 'linux'
                result = _check_scheduled_task('SalesBuddy-DailyBackup')
            assert result['exists'] is False


class TestBackupStatusAPI:
    """Tests for GET /api/admin/backup/status."""

    def test_backup_status_not_configured(self, client, app):
        """Should return not-configured status when no OneDrive path set."""
        with app.app_context():
            from app.models import UserPreference, db
            prefs = UserPreference.query.first()
            prefs.onedrive_path = None
            db.session.commit()

        with patch('app.routes.admin._check_scheduled_task') as mock_task:
            mock_task.return_value = {'exists': False, 'next_run': None, 'status': None}
            response = client.get('/api/admin/backup/status')

        assert response.status_code == 200
        data = response.get_json()
        assert data['configured'] is False
        assert data['recent_backups'] == []
        assert data['task_exists'] is False

    def test_backup_status_configured(self, client, app, tmp_path):
        """Should return configured status with backup list."""
        # Create backup subdir and a fake backup file
        backup_dir = tmp_path / 'Backups' / 'SalesBuddy'
        backup_dir.mkdir(parents=True)
        (backup_dir / 'salesbuddy_2025-01-15_100000.db').write_bytes(b'x' * 4096)

        with app.app_context():
            from app.models import UserPreference, db
            prefs = UserPreference.query.first()
            prefs.onedrive_path = str(tmp_path)
            db.session.commit()

        with patch('app.routes.admin._check_scheduled_task') as mock_task:
            mock_task.return_value = {'exists': True, 'next_run': '3/8/2026 11:00:00 AM', 'status': 'Ready'}
            response = client.get('/api/admin/backup/status')

        assert response.status_code == 200
        data = response.get_json()
        assert data['configured'] is True
        assert data['task_exists'] is True
        assert data['task_next_run'] == '3/8/2026 11:00:00 AM'
        assert data['task_status'] == 'Ready'
        assert data['last_backup'] is not None
        assert len(data['recent_backups']) == 1
        assert data['recent_backups'][0]['name'] == 'salesbuddy_2025-01-15_100000.db'

        # Clean up
        with app.app_context():
            prefs = UserPreference.query.first()
            prefs.onedrive_path = None
            db.session.commit()

    def test_backup_status_task_not_found(self, client, app):
        """task_exists should be False when task is not in Task Scheduler."""
        with app.app_context():
            from app.models import UserPreference, db
            prefs = UserPreference.query.first()
            prefs.onedrive_path = 'C:\\OneDrive'
            db.session.commit()

        with patch('app.routes.admin._check_scheduled_task') as mock_task:
            mock_task.return_value = {'exists': False, 'next_run': None, 'status': None}
            response = client.get('/api/admin/backup/status')

        assert response.status_code == 200
        data = response.get_json()
        assert data['task_exists'] is False
        assert data['task_next_run'] is None

        # Clean up
        with app.app_context():
            prefs = UserPreference.query.first()
            prefs.onedrive_path = None
            db.session.commit()


class TestBackupRunAPI:
    """Tests for POST /api/admin/backup/run."""

    def test_backup_run_not_configured(self, client, app):
        """Should return 400 when backups are not configured."""
        with app.app_context():
            from app.models import UserPreference, db
            prefs = UserPreference.query.first()
            prefs.onedrive_path = None
            db.session.commit()

        response = client.post('/api/admin/backup/run')

        assert response.status_code == 400
        data = response.get_json()
        assert data['success'] is False
        assert 'not configured' in data['error'].lower()

    def test_backup_run_no_database(self, client, app, tmp_path):
        """Should return 404 when database file doesn't exist."""
        with app.app_context():
            from app.models import UserPreference, db
            prefs = UserPreference.query.first()
            prefs.onedrive_path = str(tmp_path)
            db.session.commit()

        with patch('app.routes.admin.Path.exists', return_value=False):
            response = client.post('/api/admin/backup/run')

        assert response.status_code == 404

        # Clean up
        with app.app_context():
            prefs = UserPreference.query.first()
            prefs.onedrive_path = None
            db.session.commit()

    def test_backup_run_success(self, client, app, tmp_path):
        """Should successfully create a backup copy."""
        backup_dir = tmp_path / 'Backups' / 'SalesBuddy'
        backup_dir.mkdir(parents=True)

        # Create a fake DB file
        fake_db = tmp_path / 'salesbuddy.db'
        fake_db.write_bytes(b'SQLite DB content' * 100)

        with app.app_context():
            from app.models import UserPreference, db
            prefs = UserPreference.query.first()
            prefs.onedrive_path = str(tmp_path)
            db.session.commit()

        original_truediv = Path.__truediv__

        def patched_truediv(self, other):
            result = original_truediv(self, other)
            if str(other) == 'salesbuddy.db' and 'data' in str(self):
                return fake_db
            return result

        with patch.object(Path, '__truediv__', patched_truediv):
            response = client.post('/api/admin/backup/run')

        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        assert 'Backup created' in data['message']

        # Clean up
        with app.app_context():
            prefs = UserPreference.query.first()
            prefs.onedrive_path = None
            db.session.commit()

    def test_backup_run_creates_file_in_backup_dir(self, client, app, tmp_path):
        """Should create an actual .db file in the backup directory."""
        backup_dir = tmp_path / 'Backups' / 'SalesBuddy'
        backup_dir.mkdir(parents=True)

        fake_db = tmp_path / 'salesbuddy.db'
        fake_db.write_bytes(b'test database content')

        with app.app_context():
            from app.models import UserPreference, db
            prefs = UserPreference.query.first()
            prefs.onedrive_path = str(tmp_path)
            db.session.commit()

        original_init = Path.__truediv__

        def patched(self, other):
            result = original_init(self, other)
            if str(other) == 'salesbuddy.db' and 'data' in str(self):
                return fake_db
            return result

        with patch.object(Path, '__truediv__', patched):
            response = client.post('/api/admin/backup/run')

        assert response.status_code == 200
        backup_files = list(backup_dir.glob('salesbuddy_*.db'))
        assert len(backup_files) == 1
        assert backup_files[0].read_bytes() == b'test database content'

        # Clean up
        with app.app_context():
            prefs = UserPreference.query.first()
            prefs.onedrive_path = None
            db.session.commit()

    def test_backup_run_no_onedrive_path(self, client, app):
        """Should return 400 when onedrive_path is empty."""
        with app.app_context():
            from app.models import UserPreference, db
            prefs = UserPreference.query.first()
            prefs.onedrive_path = None
            db.session.commit()

        response = client.post('/api/admin/backup/run')
        assert response.status_code == 400


class TestTaskToggleAPI:
    """Tests for POST /api/admin/tasks/<task_key>/toggle."""

    def test_toggle_unknown_task_key(self, client, app):
        """Should return 400 for an unknown task key."""
        response = client.post(
            '/api/admin/tasks/bogus/toggle',
            json={'enabled': True},
        )
        assert response.status_code == 400
        data = response.get_json()
        assert data['success'] is False

    def test_toggle_missing_enabled_field(self, client, app):
        """Should return 400 when enabled field is missing."""
        response = client.post(
            '/api/admin/tasks/backup/toggle',
            json={},
        )
        assert response.status_code == 400

    def test_toggle_backup_enable_success(self, client, app):
        """Should enable the backup task successfully."""
        with patch('app.routes.admin._set_scheduled_task_enabled') as mock_set, \
             patch('app.routes.admin._check_scheduled_task') as mock_check:
            mock_set.return_value = {'success': True}
            mock_check.return_value = {
                'exists': True, 'next_run': '3/8/2026 11:00:00 AM', 'status': 'Ready'
            }
            response = client.post(
                '/api/admin/tasks/backup/toggle',
                json={'enabled': True},
            )

        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        assert data['task_status'] == 'Ready'

    def test_toggle_backup_disable_success(self, client, app):
        """Should disable the backup task successfully."""
        with patch('app.routes.admin._set_scheduled_task_enabled') as mock_set, \
             patch('app.routes.admin._check_scheduled_task') as mock_check:
            mock_set.return_value = {'success': True}
            mock_check.return_value = {
                'exists': True, 'next_run': None, 'status': 'Disabled'
            }
            response = client.post(
                '/api/admin/tasks/backup/toggle',
                json={'enabled': False},
            )

        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        assert data['task_status'] == 'Disabled'

    def test_toggle_autostart_success(self, client, app):
        """Should toggle the autostart task."""
        with patch('app.routes.admin._set_scheduled_task_enabled') as mock_set, \
             patch('app.routes.admin._check_scheduled_task') as mock_check:
            mock_set.return_value = {'success': True}
            mock_check.return_value = {
                'exists': True, 'next_run': None, 'status': 'Disabled'
            }
            response = client.post(
                '/api/admin/tasks/autostart/toggle',
                json={'enabled': False},
            )

        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True

    def test_toggle_fails_returns_500(self, client, app):
        """Should return 500 when toggle operation fails."""
        with patch('app.routes.admin._set_scheduled_task_enabled') as mock_set:
            mock_set.return_value = {'success': False, 'error': 'Access denied'}
            response = client.post(
                '/api/admin/tasks/backup/toggle',
                json={'enabled': True},
            )

        assert response.status_code == 500
        data = response.get_json()
        assert data['success'] is False


class TestAutoStartStatusAPI:
    """Tests for GET /api/admin/tasks/autostart/status."""

    def test_autostart_status_exists(self, client, app):
        """Should return status when autostart task exists."""
        with patch('app.routes.admin._check_scheduled_task') as mock_check:
            mock_check.return_value = {
                'exists': True, 'next_run': None, 'status': 'Ready'
            }
            response = client.get('/api/admin/tasks/autostart/status')

        assert response.status_code == 200
        data = response.get_json()
        assert data['task_exists'] is True
        assert data['task_status'] == 'Ready'

    def test_autostart_status_not_found(self, client, app):
        """Should return exists=False when task not found."""
        with patch('app.routes.admin._check_scheduled_task') as mock_check:
            mock_check.return_value = {
                'exists': False, 'next_run': None, 'status': None
            }
            response = client.get('/api/admin/tasks/autostart/status')

        assert response.status_code == 200
        data = response.get_json()
        assert data['task_exists'] is False


# =========================================================================
# Export / Import round-trip tests
# =========================================================================

class TestCustomerToDict:
    """Tests for _customer_to_dict serialization."""

    def test_export_version_4(self, app):
        """Exported dict should have _version 4."""
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
            assert data["_version"] == 4
            assert data["_salesbuddy_backup"] is True
            assert "engagements" in data
            assert "notes" in data
            assert "partners" in data
            assert "topics" in data

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
                estimated_acr=500000,
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
            assert eng_data["estimated_acr"] == 500000
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

    def test_export_includes_partner_details(self, app):
        """Partners should include contacts, specialties, notes, and rating."""
        from app.models import (
            db, Customer, Note, Partner, PartnerContact, Specialty,
            Seller, Territory,
        )
        from app.services.backup import _customer_to_dict

        with app.app_context():
            # Ensure the 'notes' text column exists (shadows relationship in model)
            db.session.execute(db.text(
                "ALTER TABLE partners ADD COLUMN notes TEXT"
            )) if 'notes' not in [
                row[1] for row in db.session.execute(
                    db.text("PRAGMA table_info(partners)")
                ).fetchall()
            ] else None
            db.session.commit()

            seller = Seller(name='S', alias='s', seller_type='Growth')
            territory = Territory(name='T')
            db.session.add_all([seller, territory])
            db.session.flush()

            customer = Customer(
                name='Partner Export', tpid=5004,
                seller_id=seller.id, territory_id=territory.id,
            )
            db.session.add(customer)
            db.session.flush()

            spec = Specialty(name='Azure AI', description='AI/ML on Azure')
            db.session.add(spec)
            db.session.flush()

            partner = Partner(name='Fabrikam')
            partner.rating = 4
            partner.specialties.append(spec)
            db.session.add(partner)
            db.session.flush()

            # Set the text notes column via raw SQL (shadowed by relationship)
            db.session.execute(
                db.text("UPDATE partners SET notes = :notes WHERE id = :id"),
                {"notes": "Great partner for AI workloads", "id": partner.id},
            )

            contact = PartnerContact(
                partner_id=partner.id, name='Jane Doe',
                email='jane@fabrikam.com', is_primary=True,
            )
            db.session.add(contact)
            db.session.flush()

            note = Note(
                customer_id=customer.id,
                call_date=datetime(2025, 5, 1, 10, 0, tzinfo=timezone.utc),
                content='Partner test note',
            )
            note.partners.append(partner)
            db.session.add(note)
            db.session.commit()

            data = _customer_to_dict(customer)
            assert len(data["partners"]) == 1
            p = data["partners"][0]
            assert p["name"] == "Fabrikam"
            assert p["rating"] == 4
            assert p["notes"] == "Great partner for AI workloads"
            assert len(p["contacts"]) == 1
            assert p["contacts"][0]["name"] == "Jane Doe"
            assert p["contacts"][0]["email"] == "jane@fabrikam.com"
            assert p["contacts"][0]["is_primary"] is True
            assert "Azure AI" in p["specialties"]

    def test_export_includes_topic_descriptions(self, app):
        """Topics should include descriptions."""
        from app.models import db, Customer, Note, Topic, Seller, Territory
        from app.services.backup import _customer_to_dict

        with app.app_context():
            seller = Seller(name='S', alias='s', seller_type='Growth')
            territory = Territory(name='T')
            db.session.add_all([seller, territory])
            db.session.flush()

            customer = Customer(
                name='Topic Export', tpid=5005,
                seller_id=seller.id, territory_id=territory.id,
            )
            db.session.add(customer)
            db.session.flush()

            topic = Topic(name='AKS', description='Azure Kubernetes Service')
            db.session.add(topic)
            db.session.flush()

            note = Note(
                customer_id=customer.id,
                call_date=datetime(2025, 5, 1, 10, 0, tzinfo=timezone.utc),
                content='Topic test note',
            )
            note.topics.append(topic)
            db.session.add(note)
            db.session.commit()

            data = _customer_to_dict(customer)
            assert len(data["topics"]) == 1
            assert data["topics"][0]["name"] == "AKS"
            assert data["topics"][0]["description"] == "Azure Kubernetes Service"

    def test_export_includes_customer_website_and_favicon(self, app):
        """Customer website and favicon_b64 should be exported."""
        from app.models import db, Customer, Seller, Territory
        from app.services.backup import _customer_to_dict

        with app.app_context():
            seller = Seller(name='S', alias='s', seller_type='Growth')
            territory = Territory(name='T')
            db.session.add_all([seller, territory])
            db.session.flush()

            customer = Customer(
                name='Web Export', tpid=5006,
                seller_id=seller.id, territory_id=territory.id,
                website='https://example.com',
                favicon_b64='data:image/png;base64,abc123',
            )
            db.session.add(customer)
            db.session.commit()

            data = _customer_to_dict(customer)
            assert data["customer"]["website"] == "https://example.com"
            assert data["customer"]["favicon_b64"] == "data:image/png;base64,abc123"


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
            "_salesbuddy_backup": True,
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
            data = {"_salesbuddy_backup": True, "customer": {}}
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
                    "estimated_acr": 300000,
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
            assert eng.estimated_acr == 300000
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
                "_salesbuddy_backup": True,
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
            # Ensure the 'notes' text column exists on partners
            if 'notes' not in [
                row[1] for row in db.session.execute(
                    db.text("PRAGMA table_info(partners)")
                ).fetchall()
            ]:
                db.session.execute(db.text(
                    "ALTER TABLE partners ADD COLUMN notes TEXT"
                ))
                db.session.commit()

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
                estimated_acr=200000,
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
            assert exported["_version"] == 4
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
            assert eng.estimated_acr == 200000

            # Verify engagement links
            assert len(eng.notes) == 1
            assert len(eng.opportunities) == 1
            assert eng.opportunities[0].msx_opportunity_id == "OPP-RT"
            assert len(eng.milestones) == 1
            assert eng.milestones[0].msx_milestone_id == "MS-RT"

            # Verify account context restored
            db.session.refresh(customer)
            assert customer.account_context == "Strategic customer"


class TestRestoreV4PerCustomer:
    """Tests for v4-specific per-customer restore fields."""

    def _make_v4_backup(
        self,
        tpid: int = 5050,
        notes: list = None,
        engagements: list = None,
        partners: list = None,
        topics: list = None,
        account_context: str = None,
        website: str = None,
        favicon_b64: str = None,
    ) -> dict:
        """Helper to build a v4 backup payload."""
        cust = {
            "name": "V4 Restore Test",
            "nickname": "V4",
            "tpid": tpid,
            "tpid_url": "https://example.com/v4",
            "account_context": account_context,
            "website": website,
            "favicon_b64": favicon_b64,
            "seller_name": "Seller",
            "territory_name": "Territory",
            "verticals": [],
        }
        return {
            "_salesbuddy_backup": True,
            "_version": 4,
            "_exported_at": datetime.now(timezone.utc).isoformat(),
            "customer": cust,
            "notes": notes or [],
            "engagements": engagements or [],
            "partners": partners or [],
            "topics": topics or [],
        }

    def test_restore_customer_website_and_favicon(self, app):
        """Should restore website and favicon_b64 when customer has none."""
        from app.models import db, Customer
        from app.services.backup import restore_from_backup

        with app.app_context():
            customer = Customer(name='V4 Web', tpid=5050)
            db.session.add(customer)
            db.session.commit()

            data = self._make_v4_backup(
                tpid=5050,
                website='https://v4test.com',
                favicon_b64='data:image/png;base64,xyz',
            )

            result = restore_from_backup(data)
            assert result["success"] is True

            db.session.refresh(customer)
            assert customer.website == 'https://v4test.com'
            assert customer.favicon_b64 == 'data:image/png;base64,xyz'

    def test_restore_does_not_overwrite_existing_website(self, app):
        """Should NOT overwrite existing website/favicon."""
        from app.models import db, Customer
        from app.services.backup import restore_from_backup

        with app.app_context():
            customer = Customer(
                name='V4 Keep Web', tpid=5051,
                website='https://original.com',
                favicon_b64='original-favicon',
            )
            db.session.add(customer)
            db.session.commit()

            data = self._make_v4_backup(
                tpid=5051,
                website='https://backup.com',
                favicon_b64='backup-favicon',
            )

            result = restore_from_backup(data)
            assert result["success"] is True

            db.session.refresh(customer)
            assert customer.website == 'https://original.com'
            assert customer.favicon_b64 == 'original-favicon'

    def test_restore_partner_details(self, app):
        """Should create partners with contacts and specialties from v4 backup."""
        from app.models import db, Customer, Partner, PartnerContact, Specialty
        from app.services.backup import restore_from_backup

        with app.app_context():
            # Ensure the 'notes' text column exists (shadows relationship in model)
            if 'notes' not in [
                row[1] for row in db.session.execute(
                    db.text("PRAGMA table_info(partners)")
                ).fetchall()
            ]:
                db.session.execute(db.text(
                    "ALTER TABLE partners ADD COLUMN notes TEXT"
                ))
                db.session.commit()

            customer = Customer(name='V4 Partner', tpid=5052)
            db.session.add(customer)
            db.session.commit()

            data = self._make_v4_backup(
                tpid=5052,
                partners=[
                    {
                        "name": "NewPartner Inc",
                        "notes": "Excellent at cloud migrations",
                        "rating": 5,
                        "contacts": [
                            {
                                "name": "Alice Smith",
                                "email": "alice@newpartner.com",
                                "is_primary": True,
                            },
                            {
                                "name": "Bob Jones",
                                "email": "bob@newpartner.com",
                                "is_primary": False,
                            },
                        ],
                        "specialties": ["Azure Migrate", "Networking"],
                    },
                ],
            )

            result = restore_from_backup(data)
            assert result["success"] is True

            partner = Partner.query.filter_by(name="NewPartner Inc").first()
            assert partner is not None
            assert partner.rating == 5

            # Check text notes via raw SQL (shadowed column)
            text_notes = db.session.execute(
                db.text("SELECT notes FROM partners WHERE id = :id"),
                {"id": partner.id},
            ).scalar()
            assert text_notes == "Excellent at cloud migrations"

            contacts = PartnerContact.query.filter_by(partner_id=partner.id).all()
            assert len(contacts) == 2
            primary_contacts = [c for c in contacts if c.is_primary]
            assert len(primary_contacts) == 1
            assert primary_contacts[0].name == "Alice Smith"

            specs = [s.name for s in partner.specialties]
            assert "Azure Migrate" in specs
            assert "Networking" in specs

    def test_restore_partner_does_not_overwrite_existing(self, app):
        """Should not overwrite rating or notes on existing partners."""
        from app.models import db, Customer, Partner
        from app.services.backup import restore_from_backup

        with app.app_context():
            # Ensure the 'notes' text column exists (shadows relationship in model)
            if 'notes' not in [
                row[1] for row in db.session.execute(
                    db.text("PRAGMA table_info(partners)")
                ).fetchall()
            ]:
                db.session.execute(db.text(
                    "ALTER TABLE partners ADD COLUMN notes TEXT"
                ))
                db.session.commit()

            customer = Customer(name='V4 Existing Partner', tpid=5053)
            db.session.add(customer)

            partner = Partner(name='ExistingCorp')
            partner.rating = 3
            db.session.add(partner)
            db.session.flush()
            db.session.execute(
                db.text("UPDATE partners SET notes = :notes WHERE id = :id"),
                {"notes": "Original notes", "id": partner.id},
            )
            db.session.commit()

            data = self._make_v4_backup(
                tpid=5053,
                partners=[
                    {
                        "name": "ExistingCorp",
                        "notes": "Backup notes trying to overwrite",
                        "rating": 5,
                        "contacts": [],
                        "specialties": [],
                    },
                ],
            )

            result = restore_from_backup(data)
            assert result["success"] is True

            db.session.refresh(partner)
            # Rating should NOT be overwritten (partner already had one)
            assert partner.rating == 3
            # Notes should NOT be overwritten (non-empty)
            text_notes = db.session.execute(
                db.text("SELECT notes FROM partners WHERE id = :id"),
                {"id": partner.id},
            ).scalar()
            assert text_notes == "Original notes"

    def test_restore_topic_descriptions(self, app):
        """Should set description on existing topics that have none."""
        from app.models import db, Customer, Topic
        from app.services.backup import restore_from_backup

        with app.app_context():
            customer = Customer(name='V4 Topics', tpid=5054)
            db.session.add(customer)

            # Existing topic without description
            topic = Topic(name='AKS')
            db.session.add(topic)
            db.session.commit()

            data = self._make_v4_backup(
                tpid=5054,
                topics=[
                    {"name": "AKS", "description": "Azure Kubernetes Service"},
                    {"name": "CosmosDB", "description": "Globally distributed DB"},
                ],
            )

            result = restore_from_backup(data)
            assert result["success"] is True

            db.session.refresh(topic)
            assert topic.description == "Azure Kubernetes Service"

            # CosmosDB topic should NOT be created by per-customer restore
            # (topics in the per-customer section only update existing ones)
            cosmos = Topic.query.filter_by(name="CosmosDB").first()
            assert cosmos is None

    def test_restore_does_not_overwrite_existing_topic_description(self, app):
        """Should NOT overwrite existing topic descriptions."""
        from app.models import db, Customer, Topic
        from app.services.backup import restore_from_backup

        with app.app_context():
            customer = Customer(name='V4 Keep Topics', tpid=5055)
            db.session.add(customer)

            topic = Topic(name='AKS', description='Original description')
            db.session.add(topic)
            db.session.commit()

            data = self._make_v4_backup(
                tpid=5055,
                topics=[
                    {"name": "AKS", "description": "Trying to overwrite"},
                ],
            )

            result = restore_from_backup(data)
            assert result["success"] is True

            db.session.refresh(topic)
            assert topic.description == "Original description"

    def test_v3_backup_still_works(self, app):
        """v3 backups without partners/topics/website should restore fine."""
        from app.models import db, Customer, Note
        from app.services.backup import restore_from_backup

        with app.app_context():
            customer = Customer(name='V3 Compat', tpid=5056)
            db.session.add(customer)
            db.session.commit()

            data = {
                "_salesbuddy_backup": True,
                "_version": 3,
                "customer": {
                    "name": "V3 Compat",
                    "tpid": 5056,
                },
                "notes": [
                    {
                        "call_date": "2025-01-10T09:00:00+00:00",
                        "content": "V3 note",
                        "topics": [],
                        "partners": [],
                        "milestones": [],
                    },
                ],
                "engagements": [],
            }

            result = restore_from_backup(data)
            assert result["success"] is True
            assert result["logs_created"] == 1

            notes = Note.query.filter_by(customer_id=customer.id).all()
            assert len(notes) == 1


class TestGlobalDataExport:
    """Tests for _global_data_to_dict serialization."""

    def test_global_data_structure(self, app):
        """Global export should have all expected top-level keys."""
        from app.services.backup import _global_data_to_dict

        with app.app_context():
            data = _global_data_to_dict()
            assert data["_salesbuddy_global_backup"] is True
            assert data["_version"] == 5
            assert "note_templates" in data
            assert "user_preferences" in data
            assert "specialties" in data
            assert "revenue_config" in data
            assert "connect_exports" in data
            assert "partners" in data

    def test_global_data_includes_templates(self, app):
        """Should include note templates."""
        from app.models import db, NoteTemplate
        from app.services.backup import _global_data_to_dict

        with app.app_context():
            tmpl = NoteTemplate(
                name='Call Summary',
                content='<h2>Summary</h2><p>Details here</p>',
                is_builtin=False,
            )
            db.session.add(tmpl)
            db.session.commit()

            data = _global_data_to_dict()
            templates = [t for t in data["note_templates"] if t["name"] == "Call Summary"]
            assert len(templates) == 1
            assert templates[0]["content"] == '<h2>Summary</h2><p>Details here</p>'
            assert templates[0]["is_builtin"] is False

    def test_global_data_includes_preferences(self, app):
        """Should include user preferences with template names."""
        from app.models import db, UserPreference, NoteTemplate
        from app.services.backup import _global_data_to_dict

        with app.app_context():
            tmpl = NoteTemplate(name='My Template', content='<p>Test</p>')
            db.session.add(tmpl)
            db.session.flush()

            # Update the existing UserPreference (conftest creates one)
            pref = UserPreference.query.first()
            pref.dark_mode = True
            pref.customer_view_grouped = True
            pref.default_template_customer_id = tmpl.id
            db.session.commit()

            data = _global_data_to_dict()
            assert data["user_preferences"] is not None
            assert data["user_preferences"]["dark_mode"] is True
            assert data["user_preferences"]["customer_view_grouped"] is True
            assert data["user_preferences"]["default_template_customer_name"] == "My Template"

    def test_global_data_includes_specialties(self, app):
        """Should include specialties with descriptions."""
        from app.models import db, Specialty
        from app.services.backup import _global_data_to_dict

        with app.app_context():
            spec = Specialty(name='Azure AI', description='AI services on Azure')
            db.session.add(spec)
            db.session.commit()

            data = _global_data_to_dict()
            specs = [s for s in data["specialties"] if s["name"] == "Azure AI"]
            assert len(specs) == 1
            assert specs[0]["description"] == "AI services on Azure"

    def test_global_data_includes_revenue_config(self, app):
        """Should include revenue config thresholds."""
        from app.models import db, RevenueConfig
        from app.services.backup import _global_data_to_dict

        with app.app_context():
            rc = RevenueConfig(
                min_revenue_for_outreach=5000,
                high_value_threshold=30000,
            )
            db.session.add(rc)
            db.session.commit()

            data = _global_data_to_dict()
            assert data["revenue_config"] is not None
            assert data["revenue_config"]["min_revenue_for_outreach"] == 5000
            assert data["revenue_config"]["high_value_threshold"] == 30000

    def test_global_data_includes_connect_exports(self, app):
        """Should include connect exports with AI summaries."""
        from datetime import date as date_type
        from app.models import db, ConnectExport
        from app.services.backup import _global_data_to_dict

        with app.app_context():
            ce = ConnectExport(
                name='Q1 Export',
                start_date=date_type(2025, 1, 1),
                end_date=date_type(2025, 3, 31),
                note_count=42,
                customer_count=10,
                ai_summary='Strong Q1 performance',
            )
            db.session.add(ce)
            db.session.commit()

            data = _global_data_to_dict()
            exports = [e for e in data["connect_exports"] if e["name"] == "Q1 Export"]
            assert len(exports) == 1
            assert exports[0]["note_count"] == 42
            assert exports[0]["ai_summary"] == "Strong Q1 performance"

    def test_global_data_includes_partners(self, app):
        """Should include partners with contacts and specialties."""
        from app.models import db, Partner, PartnerContact, Specialty
        from app.services.backup import _global_data_to_dict

        with app.app_context():
            spec = Specialty(name="Azure AI")
            db.session.add(spec)
            db.session.flush()

            partner = Partner(
                name="Contoso Partners",
                overview="Cloud consultancy",
                rating=4,
                website="https://contoso.example.com",
            )
            db.session.add(partner)
            db.session.flush()

            contact = PartnerContact(
                partner_id=partner.id,
                name="Alice",
                email="alice@contoso.example.com",
                is_primary=True,
            )
            db.session.add(contact)
            partner.specialties.append(spec)
            db.session.commit()

            data = _global_data_to_dict()
            partners = [p for p in data["partners"] if p["name"] == "Contoso Partners"]
            assert len(partners) == 1
            p = partners[0]
            assert p["overview"] == "Cloud consultancy"
            assert p["rating"] == 4
            assert p["website"] == "https://contoso.example.com"
            assert len(p["contacts"]) == 1
            assert p["contacts"][0]["name"] == "Alice"
            assert p["contacts"][0]["email"] == "alice@contoso.example.com"
            assert p["contacts"][0]["is_primary"] is True
            assert "Azure AI" in p["specialties"]


class TestRestoreGlobalData:
    """Tests for restore_global_data."""

    def _make_global_backup(self, **overrides) -> dict:
        """Helper to build a global backup payload."""
        base = {
            "_salesbuddy_global_backup": True,
            "_version": 5,
            "_exported_at": datetime.now(timezone.utc).isoformat(),
            "note_templates": [],
            "user_preferences": None,
            "specialties": [],
            "revenue_config": None,
            "connect_exports": [],
            "partners": [],
        }
        base.update(overrides)
        return base

    def test_restore_creates_templates(self, app):
        """Should create note templates that don't exist."""
        from app.models import db, NoteTemplate
        from app.services.backup import restore_global_data

        with app.app_context():
            data = self._make_global_backup(
                note_templates=[
                    {"name": "New Template", "content": "<p>Hello</p>", "is_builtin": False},
                    {"name": "Built-in", "content": "<p>Default</p>", "is_builtin": True},
                ],
            )

            result = restore_global_data(data)
            assert result["note_templates"]["created"] == 2
            assert result["note_templates"]["skipped"] == 0

            tmpl = NoteTemplate.query.filter_by(name="New Template").first()
            assert tmpl is not None
            assert tmpl.content == "<p>Hello</p>"

    def test_restore_enriches_existing_templates(self, app):
        """Should update content of existing templates from backup."""
        from app.models import db, NoteTemplate
        from app.services.backup import restore_global_data

        with app.app_context():
            existing = NoteTemplate(name="Existing", content="<p>Original</p>")
            db.session.add(existing)
            db.session.commit()

            data = self._make_global_backup(
                note_templates=[
                    {"name": "Existing", "content": "<p>Backup</p>", "is_builtin": False},
                    {"name": "Brand New", "content": "<p>New</p>", "is_builtin": False},
                ],
            )

            result = restore_global_data(data)
            assert result["note_templates"]["created"] == 1
            assert result["note_templates"]["skipped"] == 1

            # Content should be updated from backup
            db.session.refresh(existing)
            assert existing.content == "<p>Backup</p>"

    def test_restore_creates_specialties(self, app):
        """Should create specialties that don't exist."""
        from app.models import db, Specialty
        from app.services.backup import restore_global_data

        with app.app_context():
            data = self._make_global_backup(
                specialties=[
                    {"name": "Azure AI", "description": "AI on Azure"},
                    {"name": "Networking", "description": "Azure Networking"},
                ],
            )

            result = restore_global_data(data)
            assert result["specialties"]["created"] == 2

            spec = Specialty.query.filter_by(name="Azure AI").first()
            assert spec.description == "AI on Azure"

    def test_restore_enriches_existing_specialty_description(self, app):
        """Should add description to existing specialty if missing."""
        from app.models import db, Specialty
        from app.services.backup import restore_global_data

        with app.app_context():
            spec = Specialty(name="Azure AI")
            db.session.add(spec)
            db.session.commit()

            data = self._make_global_backup(
                specialties=[
                    {"name": "Azure AI", "description": "AI services on Azure"},
                ],
            )

            result = restore_global_data(data)
            assert result["specialties"]["skipped"] == 1

            db.session.refresh(spec)
            assert spec.description == "AI services on Azure"

    def test_restore_preferences(self, app):
        """Should restore user preferences."""
        from app.models import db, UserPreference
        from app.services.backup import restore_global_data

        with app.app_context():
            data = self._make_global_backup(
                user_preferences={
                    "dark_mode": False,
                    "customer_view_grouped": True,
                    "customer_sort_by": "recent",
                    "topic_sort_by_calls": True,
                    "territory_view_accounts": True,
                    "show_customers_without_calls": False,
                    "workiq_summary_prompt": "Summarize the key points",
                    "workiq_connect_impact": False,
                    "ai_enabled": True,
                    "default_template_customer_name": None,
                    "default_template_noncustomer_name": None,
                },
            )

            result = restore_global_data(data)
            assert result["user_preference"] == "restored"

            pref = UserPreference.query.first()
            assert pref is not None
            assert pref.dark_mode is False
            assert pref.customer_view_grouped is True
            assert pref.customer_sort_by == "recent"
            assert pref.workiq_summary_prompt == "Summarize the key points"

    def test_restore_preferences_with_template_links(self, app):
        """Should resolve default template FKs by name."""
        from app.models import db, NoteTemplate, UserPreference
        from app.services.backup import restore_global_data

        with app.app_context():
            tmpl = NoteTemplate(name="My Call Template", content="<p>Template</p>")
            db.session.add(tmpl)
            db.session.commit()

            data = self._make_global_backup(
                user_preferences={
                    "dark_mode": True,
                    "default_template_customer_name": "My Call Template",
                    "default_template_noncustomer_name": None,
                },
            )

            result = restore_global_data(data)
            assert result["user_preference"] == "restored"

            pref = UserPreference.query.first()
            assert pref.default_template_customer_id == tmpl.id

    def test_restore_revenue_config(self, app):
        """Should restore revenue config thresholds."""
        from app.models import db, RevenueConfig
        from app.services.backup import restore_global_data

        with app.app_context():
            data = self._make_global_backup(
                revenue_config={
                    "min_revenue_for_outreach": 5000,
                    "min_dollar_impact": 2000,
                    "dollar_at_risk_override": 3000,
                    "dollar_opportunity_override": 2500,
                    "high_value_threshold": 30000,
                    "strategic_threshold": 60000,
                    "volatile_min_revenue": 7000,
                    "recent_drop_threshold": -0.20,
                    "expansion_growth_threshold": 0.10,
                },
            )

            result = restore_global_data(data)
            assert result["revenue_config"] == "restored"

            rc = RevenueConfig.query.first()
            assert rc is not None
            assert rc.min_revenue_for_outreach == 5000
            assert rc.high_value_threshold == 30000
            assert rc.recent_drop_threshold == -0.20

    def test_restore_connect_exports(self, app):
        """Should create connect exports that don't exist."""
        from app.models import db, ConnectExport
        from app.services.backup import restore_global_data

        with app.app_context():
            data = self._make_global_backup(
                connect_exports=[
                    {
                        "name": "Q1 Export",
                        "start_date": "2025-01-01",
                        "end_date": "2025-03-31",
                        "note_count": 42,
                        "customer_count": 10,
                        "ai_summary": "Strong Q1",
                    },
                ],
            )

            result = restore_global_data(data)
            assert result["connect_exports"]["created"] == 1

            ce = ConnectExport.query.filter_by(name="Q1 Export").first()
            assert ce is not None
            assert ce.note_count == 42
            assert ce.ai_summary == "Strong Q1"

    def test_restore_deduplicates_connect_exports(self, app):
        """Should skip connect exports that already exist (by name+start_date)."""
        from datetime import date as date_type
        from app.models import db, ConnectExport
        from app.services.backup import restore_global_data

        with app.app_context():
            existing = ConnectExport(
                name="Q1 Export",
                start_date=date_type(2025, 1, 1),
                end_date=date_type(2025, 3, 31),
                note_count=42,
                customer_count=10,
            )
            db.session.add(existing)
            db.session.commit()

            data = self._make_global_backup(
                connect_exports=[
                    {
                        "name": "Q1 Export",
                        "start_date": "2025-01-01",
                        "end_date": "2025-03-31",
                        "note_count": 100,
                        "customer_count": 20,
                        "ai_summary": "Overwrite attempt",
                    },
                    {
                        "name": "Q2 Export",
                        "start_date": "2025-04-01",
                        "end_date": "2025-06-30",
                        "note_count": 50,
                        "customer_count": 15,
                        "ai_summary": "New export",
                    },
                ],
            )

            result = restore_global_data(data)
            assert result["connect_exports"]["created"] == 1
            assert result["connect_exports"]["skipped"] == 1

            # Original should be preserved
            db.session.refresh(existing)
            assert existing.note_count == 42

    def test_restore_empty_global_data(self, app):
        """Should handle empty global backup gracefully."""
        from app.services.backup import restore_global_data

        with app.app_context():
            data = self._make_global_backup()
            result = restore_global_data(data)
            assert result["note_templates"]["created"] == 0
            assert result["specialties"]["created"] == 0
            assert result["user_preference"] == "not_in_backup"
            assert result["revenue_config"] == "not_in_backup"
            assert result["connect_exports"]["created"] == 0
            assert result["partners"]["created"] == 0

    def test_restore_creates_partners(self, app):
        """Should create partners with contacts and specialties."""
        from app.models import db, Partner, PartnerContact, Specialty
        from app.services.backup import restore_global_data

        with app.app_context():
            spec = Specialty(name="Azure AI")
            db.session.add(spec)
            db.session.commit()

            data = self._make_global_backup(
                partners=[
                    {
                        "name": "Contoso Partners",
                        "overview": "Cloud consultancy",
                        "rating": 4,
                        "website": "https://contoso.example.com",
                        "contacts": [
                            {"name": "Alice", "email": "alice@contoso.example.com", "is_primary": True},
                        ],
                        "specialties": ["Azure AI", "Nonexistent Specialty"],
                    },
                ],
            )

            result = restore_global_data(data)
            assert result["partners"]["created"] == 1
            assert result["partners"]["skipped"] == 0

            partner = Partner.query.filter_by(name="Contoso Partners").first()
            assert partner is not None
            assert partner.overview == "Cloud consultancy"
            assert partner.rating == 4
            assert partner.website == "https://contoso.example.com"
            assert len(partner.contacts) == 1
            assert partner.contacts[0].name == "Alice"
            assert partner.contacts[0].is_primary is True
            # Only existing specialties should be linked
            assert len(partner.specialties) == 1
            assert partner.specialties[0].name == "Azure AI"

    def test_restore_enriches_existing_partner(self, app):
        """Should add missing fields and contacts to existing partners."""
        from app.models import db, Partner, PartnerContact, Specialty
        from app.services.backup import restore_global_data

        with app.app_context():
            spec = Specialty(name="Networking")
            db.session.add(spec)
            db.session.flush()

            partner = Partner(name="Contoso Partners")
            db.session.add(partner)
            db.session.flush()

            existing_contact = PartnerContact(
                partner_id=partner.id,
                name="Bob",
                email="bob@contoso.example.com",
                is_primary=False,
            )
            db.session.add(existing_contact)
            db.session.commit()

            data = self._make_global_backup(
                partners=[
                    {
                        "name": "Contoso Partners",
                        "overview": "Cloud consultancy",
                        "rating": 4,
                        "website": "https://contoso.example.com",
                        "contacts": [
                            {"name": "Bob", "email": "bob@contoso.example.com", "is_primary": False},
                            {"name": "Alice", "email": "alice@contoso.example.com", "is_primary": True},
                        ],
                        "specialties": ["Networking"],
                    },
                ],
            )

            result = restore_global_data(data)
            assert result["partners"]["created"] == 0
            assert result["partners"]["skipped"] == 1

            db.session.refresh(partner)
            # Enriched fields
            assert partner.overview == "Cloud consultancy"
            assert partner.rating == 4
            assert partner.website == "https://contoso.example.com"
            # Bob skipped (exists), Alice added
            assert len(partner.contacts) == 2
            contact_names = {c.name for c in partner.contacts}
            assert contact_names == {"Alice", "Bob"}
            # Specialty linked
            assert len(partner.specialties) == 1
            assert partner.specialties[0].name == "Networking"


class TestPartnerBackup:
    """Tests for individual partner backup/delete."""

    def test_partner_to_dict_serialization(self, app):
        """Should serialize a partner to a standalone backup dict."""
        from app.models import db, Partner, PartnerContact, Specialty
        from app.services.backup import _partner_to_dict

        with app.app_context():
            spec = Specialty(name="Azure AI")
            db.session.add(spec)
            db.session.flush()

            partner = Partner(
                name="Contoso",
                overview="Cloud partner",
                rating=4,
                website="contoso.com",
            )
            db.session.add(partner)
            db.session.flush()

            contact = PartnerContact(
                partner_id=partner.id,
                name="Alice",
                email="alice@contoso.com",
                is_primary=True,
            )
            db.session.add(contact)
            partner.specialties.append(spec)
            db.session.commit()

            data = _partner_to_dict(partner)
            assert data["_salesbuddy_partner_backup"] is True
            assert data["_version"] == 1
            p = data["partner"]
            assert p["name"] == "Contoso"
            assert p["overview"] == "Cloud partner"
            assert p["rating"] == 4
            assert p["website"] == "contoso.com"
            assert len(p["contacts"]) == 1
            assert p["contacts"][0]["name"] == "Alice"
            assert "Azure AI" in p["specialties"]

    def test_backup_partner_writes_file(self, app, tmp_path, monkeypatch):
        """Should write a partner .json file to the backup directory."""
        from app.models import db, Partner
        from app.services import backup as backup_mod

        monkeypatch.setattr(backup_mod, "_get_backup_root", lambda: str(tmp_path))

        with app.app_context():
            partner = Partner(name="Acme")
            db.session.add(partner)
            db.session.commit()

            result = backup_mod.backup_partner(partner.id)
            assert result is True

            filepath = tmp_path / "notes" / "partners" / f"{partner.id}.json"
            assert filepath.exists()
            data = json.loads(filepath.read_text(encoding="utf-8"))
            assert data["partner"]["name"] == "Acme"

    def test_backup_partner_overwrites_on_edit(self, app, tmp_path, monkeypatch):
        """Re-backing up a partner should overwrite the existing file."""
        from app.models import db, Partner
        from app.services import backup as backup_mod

        monkeypatch.setattr(backup_mod, "_get_backup_root", lambda: str(tmp_path))

        with app.app_context():
            partner = Partner(name="Acme", overview="v1")
            db.session.add(partner)
            db.session.commit()

            backup_mod.backup_partner(partner.id)

            partner.overview = "v2"
            db.session.commit()
            backup_mod.backup_partner(partner.id)

            filepath = tmp_path / "notes" / "partners" / f"{partner.id}.json"
            data = json.loads(filepath.read_text(encoding="utf-8"))
            assert data["partner"]["overview"] == "v2"

    def test_delete_partner_backup_removes_file(self, app, tmp_path, monkeypatch):
        """Should remove the partner .json file."""
        from app.models import db, Partner
        from app.services import backup as backup_mod

        monkeypatch.setattr(backup_mod, "_get_backup_root", lambda: str(tmp_path))

        with app.app_context():
            partner = Partner(name="Acme")
            db.session.add(partner)
            db.session.commit()
            pid = partner.id

            backup_mod.backup_partner(pid)
            filepath = tmp_path / "notes" / "partners" / f"{pid}.json"
            assert filepath.exists()

            result = backup_mod.delete_partner_backup(pid)
            assert result is True
            assert not filepath.exists()

    def test_delete_partner_backup_nonexistent_ok(self, app, tmp_path, monkeypatch):
        """Deleting backup for nonexistent file should return True."""
        from app.services import backup as backup_mod

        monkeypatch.setattr(backup_mod, "_get_backup_root", lambda: str(tmp_path))

        with app.app_context():
            result = backup_mod.delete_partner_backup(9999)
            assert result is True


class TestTemplateBackup:
    """Tests for individual template backup/delete."""

    def test_template_to_dict_serialization(self, app):
        """Should serialize a template to a standalone backup dict."""
        from app.models import db, NoteTemplate
        from app.services.backup import _template_to_dict

        with app.app_context():
            tmpl = NoteTemplate(name="Call Summary", content="<p>Summary</p>")
            db.session.add(tmpl)
            db.session.commit()

            data = _template_to_dict(tmpl)
            assert data["_salesbuddy_template_backup"] is True
            assert data["_version"] == 1
            t = data["template"]
            assert t["name"] == "Call Summary"
            assert t["content"] == "<p>Summary</p>"
            assert t["is_builtin"] is False

    def test_backup_template_writes_file(self, app, tmp_path, monkeypatch):
        """Should write a template .json file to the backup directory."""
        from app.models import db, NoteTemplate
        from app.services import backup as backup_mod

        monkeypatch.setattr(backup_mod, "_get_backup_root", lambda: str(tmp_path))

        with app.app_context():
            tmpl = NoteTemplate(name="Quick Note", content="<p>Hello</p>")
            db.session.add(tmpl)
            db.session.commit()

            result = backup_mod.backup_template(tmpl.id)
            assert result is True

            filepath = tmp_path / "notes" / "templates" / f"{tmpl.id}.json"
            assert filepath.exists()
            data = json.loads(filepath.read_text(encoding="utf-8"))
            assert data["template"]["name"] == "Quick Note"

    def test_backup_template_overwrites_on_edit(self, app, tmp_path, monkeypatch):
        """Re-backing up a template should overwrite the existing file."""
        from app.models import db, NoteTemplate
        from app.services import backup as backup_mod

        monkeypatch.setattr(backup_mod, "_get_backup_root", lambda: str(tmp_path))

        with app.app_context():
            tmpl = NoteTemplate(name="Quick Note", content="<p>v1</p>")
            db.session.add(tmpl)
            db.session.commit()

            backup_mod.backup_template(tmpl.id)

            tmpl.content = "<p>v2</p>"
            db.session.commit()
            backup_mod.backup_template(tmpl.id)

            filepath = tmp_path / "notes" / "templates" / f"{tmpl.id}.json"
            data = json.loads(filepath.read_text(encoding="utf-8"))
            assert data["template"]["content"] == "<p>v2</p>"

    def test_delete_template_backup_removes_file(self, app, tmp_path, monkeypatch):
        """Should remove the template .json file."""
        from app.models import db, NoteTemplate
        from app.services import backup as backup_mod

        monkeypatch.setattr(backup_mod, "_get_backup_root", lambda: str(tmp_path))

        with app.app_context():
            tmpl = NoteTemplate(name="Quick Note", content="<p>Hello</p>")
            db.session.add(tmpl)
            db.session.commit()
            tid = tmpl.id

            backup_mod.backup_template(tid)
            filepath = tmp_path / "notes" / "templates" / f"{tid}.json"
            assert filepath.exists()

            result = backup_mod.delete_template_backup(tid)
            assert result is True
            assert not filepath.exists()


class TestClearBackupNotes:
    """Tests for clear_backup_notes file-by-file deletion."""

    def test_deletes_all_files_and_counts(self, app, tmp_path, monkeypatch):
        """Should delete individual files and return correct count."""
        from app.services import backup as backup_mod

        monkeypatch.setattr(backup_mod, "_get_backup_root", lambda: str(tmp_path))

        # Create a notes/ tree with some files
        notes_dir = tmp_path / "notes"
        seller_dir = notes_dir / "SellerA"
        seller_dir.mkdir(parents=True)
        (seller_dir / "1.json").write_text("{}")
        (seller_dir / "2.json").write_text("{}")
        partners_dir = notes_dir / "partners"
        partners_dir.mkdir()
        (partners_dir / "10.json").write_text("{}")

        result = backup_mod.clear_backup_notes()
        assert result["deleted"] == 3
        assert result["errors"] == 0
        # notes/ should be recreated empty
        assert notes_dir.is_dir()
        assert list(notes_dir.iterdir()) == []

    def test_no_backup_root_returns_skipped(self, app, monkeypatch):
        """Should skip when no backup location configured."""
        from app.services import backup as backup_mod

        monkeypatch.setattr(backup_mod, "_get_backup_root", lambda: None)
        result = backup_mod.clear_backup_notes()
        assert result["skipped"] is True
        assert result["deleted"] == 0

    def test_empty_notes_dir_returns_zero(self, app, tmp_path, monkeypatch):
        """Should return 0 deleted when notes/ exists but is empty."""
        from app.services import backup as backup_mod

        monkeypatch.setattr(backup_mod, "_get_backup_root", lambda: str(tmp_path))
        (tmp_path / "notes").mkdir()

        result = backup_mod.clear_backup_notes()
        assert result["deleted"] == 0
        assert result["errors"] == 0
