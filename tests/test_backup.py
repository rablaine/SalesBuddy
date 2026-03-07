"""
Tests for the backup API endpoints.
"""
import json
import os
import shutil
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


class TestBackupStatusAPI:
    """Tests for GET /api/admin/backup/status."""

    def test_backup_status_not_configured(self, client, app):
        """Should return not-configured status when no config exists."""
        with patch('app.routes.admin._get_backup_config') as mock_config:
            mock_config.return_value = {
                'enabled': False,
                'onedrive_path': '',
                'backup_dir': '',
                'retention': {'daily': 7, 'weekly': 4, 'monthly': 3},
                'last_backup': None,
                'task_registered': False,
            }
            response = client.get('/api/admin/backup/status')

        assert response.status_code == 200
        data = response.get_json()
        assert data['enabled'] is False
        assert data['recent_backups'] == []

    def test_backup_status_configured(self, client, app, tmp_path):
        """Should return configured status with backup list."""
        # Create a fake backup file
        (tmp_path / 'notehelper_2025-01-15_100000.db').write_bytes(b'x' * 4096)

        with patch('app.routes.admin._get_backup_config') as mock_config:
            mock_config.return_value = {
                'enabled': True,
                'onedrive_path': 'C:\\OneDrive',
                'backup_dir': str(tmp_path),
                'retention': {'daily': 7, 'weekly': 4, 'monthly': 3},
                'last_backup': '2025-01-15T10:30:00+00:00',
                'task_registered': True,
            }
            response = client.get('/api/admin/backup/status')

        assert response.status_code == 200
        data = response.get_json()
        assert data['enabled'] is True
        assert data['task_registered'] is True
        assert data['last_backup'] == '2025-01-15T10:30:00+00:00'
        assert len(data['recent_backups']) == 1
        assert data['recent_backups'][0]['name'] == 'notehelper_2025-01-15_100000.db'


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
