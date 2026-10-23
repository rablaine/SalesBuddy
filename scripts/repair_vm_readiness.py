"""Repair only the initial disposable-VM kit baseline and regenerate its local test updates."""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

if __package__:
    from scripts import vm_upgrade_smoke as smoke
else:
    import vm_upgrade_smoke as smoke


def validate_repair(repo: Path, kit: Path) -> tuple[dict, Path]:
    """Refuse changed installations, active updates, different kits and corrupt repair payloads."""
    smoke.validate_install(repo)
    smoke.validate_payload(kit)
    state = smoke.session(repo, kit)
    manifest = json.loads((kit / 'readiness-repair.json').read_text(encoding='utf-8'))
    patch = kit / 'readiness-repair' / 'supervisor.py'
    if manifest.get('schema_version') != 1 or manifest.get('kit_id') != state['kit_id']:
        raise RuntimeError('This repair is not for the installed VM test kit')
    if smoke.digest(patch) != manifest['new_supervisor_sha256']:
        raise RuntimeError('The readiness repair payload is damaged')
    if smoke.digest(repo / 'app' / 'supervisor.py') != manifest['old_supervisor_sha256']:
        raise RuntimeError('The VM supervisor has already changed; no files were overwritten')
    if smoke.run(repo, 'rev-parse', 'HEAD') != state['baseline'] or (
        smoke.run(repo, 'status', '--porcelain', '--untracked-files=no')
    ):
        raise RuntimeError('Apply this repair only to the initial, unchanged VM safety baseline')
    if (repo / 'data' / 'upgrade-state.json').exists():
        raise RuntimeError('An update was already attempted; preserve its recovery point instead')
    smoke.verify_records(state)
    return state, patch


def repair(repo: Path, kit: Path) -> None:
    """Preserve both test notes and rebuild local baseline/good/broken commits without publishing."""
    state, patch = validate_repair(repo, kit)
    work = Path(state['work']) / f'pid-fix-{uuid.uuid4().hex[:8]}'
    work.mkdir()
    (work / 'before-repair.json').write_text(json.dumps(state, indent=2), encoding='utf-8')
    (work / 'previous-supervisor.py').write_bytes((repo / 'app' / 'supervisor.py').read_bytes())
    smoke.run(repo, 'switch', '-c', f'fix/vm-readiness-{work.name}')
    (repo / 'app' / 'supervisor.py').write_bytes(patch.read_bytes())
    baseline = smoke.commit_files(
        repo, ['app/supervisor.py'], 'fix: Windows managed child PID identity',
        work / 'repair-paths.txt',
    )
    broken = smoke.create_target(repo, work, baseline, broken=True)
    good = smoke.create_target(repo, work, baseline, broken=False)
    smoke.run(
        repo, '--git-dir', state['remote'], 'fetch', '--no-tags', str(repo),
        f'{baseline}:refs/heads/main', f'{broken}:refs/heads/vm-broken',
        f'{good}:refs/heads/vm-good',
    )
    smoke.run(repo, 'fetch', 'origin', 'main')
    state.update(baseline=baseline, broken=broken, good=good, readiness_repair=str(work))
    smoke.write_json(repo / 'data' / smoke.MARKER, state)
    smoke.verify_records(state)
    print('Windows PID readiness fix installed. Both test notes are unchanged.')
    print('The local failed/successful update targets now use the corrected baseline.')
    print('Nothing was pushed, no database was replaced, and no recovery history was removed.')


def main() -> None:
    """Run from the extracted original kit using its VM-guarded repair launcher."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--kit', type=Path, required=True)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    repo, kit = args.repo.resolve(), args.kit.resolve()
    sys.path.insert(0, str(repo))
    if args.check:
        validate_repair(repo, kit)
    else:
        repair(repo, kit)


if __name__ == '__main__':
    main()
