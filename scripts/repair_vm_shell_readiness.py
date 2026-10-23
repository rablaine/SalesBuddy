"""Repair the exact VM kit's restored baseline without removing its recovery evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

if __package__:
    from scripts import vm_upgrade_smoke as smoke
else:
    import vm_upgrade_smoke as smoke


def validate_repair(repo: Path, kit: Path) -> tuple[dict, dict]:
    """Require the original kit, exact source, preserved notes and dismissed early rollback."""
    from scripts.upgrade import load_state

    smoke.validate_install(repo)
    smoke.validate_payload(kit)
    state = smoke.session(repo, kit)
    manifest = json.loads((kit / 'shell-readiness-repair.json').read_text(encoding='utf-8'))
    if manifest.get('schema_version') != 1 or manifest.get('kit_id') != state['kit_id']:
        raise RuntimeError('This patch is not for the installed VM test kit')
    head = smoke.run(repo, 'rev-parse', 'HEAD')
    if smoke.run(repo, 'status', '--porcelain', '--untracked-files=no'):
        raise RuntimeError('The VM has tracked local edits; no repair files were overwritten')
    resume = None
    if head != state['baseline']:
        for candidate in Path(state['work']).glob('shell-fix-*'):
            backup = candidate / 'before-repair.json'
            if backup.is_file() and json.loads(backup.read_text(encoding='utf-8')) == state:
                if resume is not None:
                    raise RuntimeError('Multiple partial repairs found; retain their backups')
                resume = candidate
        if resume is None:
            raise RuntimeError('The VM revision changed without a matching partial repair backup')
        try:
            smoke.run(repo, 'merge-base', '--is-ancestor', state['baseline'], head)
        except subprocess.CalledProcessError as error:
            raise RuntimeError('The partial repair is not based on this VM baseline') from error
        changed = set(smoke.run(repo, 'diff', '--name-only', state['baseline'], head).splitlines())
        if not changed or not changed.issubset(manifest['files']):
            raise RuntimeError('The partial repair contains unexpected source changes')
        manifest['resume_work'] = str(resume)
    upgrade = load_state(repo)
    if (not upgrade or upgrade.get('phase') not in {'restored', 'failed'}
            or upgrade.get('notification_pending')
            or not upgrade.get('restored_at') or not upgrade.get('acknowledged_at')
            or upgrade.get('target_commit') != state['broken']):
        raise RuntimeError('Finish recovery and click Continue working before applying this patch')
    if not resume and smoke.digest(repo / 'electron-dist' / 'resources' / 'app.asar') != (
        state['baseline_shell_sha256']
    ):
        raise RuntimeError('The previous desktop payload is not restored; retain the recovery files')
    if (Path(state['work']) / 'fault-reached.json').exists():
        raise RuntimeError('The intended startup fault ran; this patch is for the earlier shell failure')
    remote_tip = smoke.run(repo, '--git-dir', state['remote'], 'rev-parse', 'main')
    if remote_tip not in {state['baseline'], state['broken'], head}:
        raise RuntimeError('The local test update target changed; no repair files were written')
    manifest['remote_tip'] = remote_tip
    for relative, hashes in manifest['files'].items():
        if relative not in {
            'electron/main.js', 'electron/upgrade-safety.js',
            'scripts/migrate-to-electron.ps1', 'scripts/upgrade.py', 'scripts/server.ps1',
        }:
            raise RuntimeError('Unexpected VM source patch')
        payload = kit / 'shell-readiness-repair' / relative
        if smoke.digest(payload) != hashes['new_sha256']:
            raise RuntimeError(f'The repair payload is damaged: {relative}')
        current = (repo / relative).read_bytes().replace(b'\r\n', b'\n')
        allowed = {hashes['old_sha256']}
        if resume:
            allowed.add(hashlib.sha256(payload.read_bytes().replace(b'\r\n', b'\n')).hexdigest())
        if hashlib.sha256(current).hexdigest() not in allowed:
            raise RuntimeError(f'The original VM source has changed: {relative}')
    helper = kit / 'shell-readiness-repair' / 'vm_upgrade_smoke.py'
    if smoke.digest(helper) != manifest['helper_sha256']:
        raise RuntimeError('The repaired VM checker payload is damaged')
    smoke.verify_records(state)
    return state, manifest


def repair(repo: Path, kit: Path) -> None:
    """Commit a fixed baseline and regenerate local update targets while retaining old attempts."""
    from scripts.upgrade import load_state, save_state

    state, manifest = validate_repair(repo, kit)
    previous_upgrade = load_state(repo)
    resume = manifest.get('resume_work')
    work = Path(resume) if resume else Path(state['work']) / f'shell-fix-{uuid.uuid4().hex[:8]}'
    if resume:
        print('Resuming the interrupted repair; preserving its original backups.', flush=True)
    else:
        work.mkdir()
        smoke.write_json(work / 'before-repair.json', state)
        smoke.write_json(work / 'previous-upgrade-state.json', previous_upgrade)
        shutil.copy2(kit / 'kit-manifest.json', work / 'previous-kit-manifest.json')
        shutil.copy2(kit / 'vm_upgrade_smoke.py', work / 'previous-vm-upgrade-smoke.py')
        smoke.run(repo, 'switch', '-c', f'fix/vm-shell-readiness-{work.name}')
    for relative in manifest['files']:
        backup = work / 'previous-source' / relative
        backup.parent.mkdir(parents=True, exist_ok=True)
        if not backup.exists():
            shutil.copy2(repo / relative, backup)
        shutil.copy2(kit / 'shell-readiness-repair' / relative, repo / relative)
    baseline = smoke.run(repo, 'rev-parse', 'HEAD')
    if smoke.run(repo, 'diff', '--name-only'):
        baseline = smoke.commit_files(
            repo, list(manifest['files']), 'fix: packaged VM shell startup and recovery ownership',
            work / 'repair-paths.txt',
        )
    print('Building the repaired desktop...', flush=True)
    built = subprocess.run(
        ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
         str(repo / 'electron' / 'build.ps1'), '-SkipInstall', '-SkipSign'],
        cwd=repo, check=False,
    )
    if built.returncode:
        raise RuntimeError('The patched VM desktop build failed; preserve its output and repair backup')
    print('Staging the repaired desktop; the repair resumes afterward.', flush=True)
    staged = subprocess.run(
        ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
         str(repo / 'scripts' / 'migrate-to-electron.ps1'), '-SkipPull', '-NoLaunch',
         '-NoAutoStart'],
        cwd=repo, check=False,
    )
    if staged.returncode:
        raise RuntimeError('The patched VM desktop could not be staged; preserve the repair backup')
    print('Regenerating the failed and successful local test versions...', flush=True)
    targets = work / f'targets-{uuid.uuid4().hex[:8]}'
    targets.mkdir()
    broken = smoke.create_target(repo, targets, baseline, broken=True)
    good = smoke.create_target(repo, targets, baseline, broken=False)
    smoke.run(
        repo, '--git-dir', state['remote'], 'fetch', '--no-tags', str(repo),
        f'{baseline}:refs/heads/vm-shell-fix-baseline-{targets.name}',
        f'{broken}:refs/heads/vm-shell-fix-broken-{targets.name}',
        f'{good}:refs/heads/vm-shell-fix-good-{targets.name}',
    )
    remote_tip = smoke.run(repo, '--git-dir', state['remote'], 'rev-parse', 'main')
    if remote_tip != manifest['remote_tip']:
        raise RuntimeError('The local test update target changed during repair; preserve the backup')
    smoke.run(repo, '--git-dir', state['remote'], 'update-ref', 'refs/heads/main',
              baseline, remote_tip)
    smoke.run(repo, 'fetch', 'origin', 'main')
    state.update(
        baseline=baseline, broken=broken, good=good, shell_readiness_repair=str(work),
        baseline_shell_sha256=smoke.digest(repo / 'electron-dist' / 'resources' / 'app.asar'),
    )
    shutil.copy2(kit / 'shell-readiness-repair' / 'vm_upgrade_smoke.py',
                 kit / 'vm_upgrade_smoke.py')
    kit_manifest = json.loads((kit / 'kit-manifest.json').read_text(encoding='utf-8'))
    kit_manifest['files']['vm_upgrade_smoke.py'] = manifest['helper_sha256']
    smoke.write_json(kit / 'kit-manifest.json', kit_manifest)
    smoke.write_json(repo / 'data' / smoke.MARKER, state)
    if previous_upgrade['phase'] == 'failed':
        save_state(repo, previous_upgrade, 'restored', notification_pending=False)
    smoke.verify_records(state)
    print('Fixed the VM shell and regenerated both local test updates.')
    print('Both notes, the previous failed attempt and its queued diagnostic report are retained.')
    print('After Sales Buddy opens, run 2-TEST-FAILED-UPDATE.cmd again, then step 3.')


def main() -> None:
    """Run only through the VM-guarded patch launcher."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--kit', type=Path, required=True)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    repo, kit = args.repo.resolve(), args.kit.resolve()
    sys.path.insert(0, str(repo))
    from scripts.upgrade import upgrade_lock

    with upgrade_lock(repo, name='recovery-notice.lock', timeout=0):
        if args.check:
            validate_repair(repo, kit)
        else:
            repair(repo, kit)


if __name__ == '__main__':
    main()
