"""Standalone installer for isolated, replaceable Connector environments."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import os
import json
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import venv

DEFAULT_SPEC = 'git+https://github.com/bourne015/botsdock-connector.git'


@contextmanager
def installation_lock():
    if os.name != 'posix':
        raise RuntimeError('Managed installation currently supports macOS and Linux.')
    import fcntl
    root = Path.home() / '.botsdock' / 'connector'
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'install.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('Another installation or rollback is in progress.') from exc
        yield


def resolve_spec(package_spec: str) -> str:
    if package_spec != DEFAULT_SPEC:
        return package_spec
    repository = DEFAULT_SPEC.removeprefix('git+')
    result = subprocess.run(['git', 'ls-remote', '--tags', repository],
                            check=True, capture_output=True, text=True)
    releases = {}
    for line in result.stdout.splitlines():
        commit, ref = line.split()
        match = re.fullmatch(r'refs/tags/v?(\d+)\.(\d+)\.(\d+)(\^\{\})?', ref)
        if match:
            version = tuple(int(match[i]) for i in range(1, 4))
            if version not in releases or match[4]:
                releases[version] = commit
    if not releases:
        raise RuntimeError('No stable release tags found. Use --package-spec for an explicit source.')
    return f'{DEFAULT_SPEC}@{releases[max(releases)]}'


def verify(environment: Path) -> None:
    python = environment / 'bin' / 'python'
    subprocess.run([str(python), '-m', 'pip', 'check'], check=True)
    subprocess.run([str(python), '-m', 'botsdock_connector', '--help'],
                   check=True, stdout=subprocess.DEVNULL)


def rollback() -> Path:
    with installation_lock():
        return _rollback()


def _rollback() -> Path:
    root = Path.home() / '.botsdock' / 'connector'
    previous = root / 'previous'
    if not previous.is_symlink() or not previous.resolve().is_dir():
        raise RuntimeError('No previous Connector installation is available.')
    target = previous.resolve()
    verify(target)
    temporary = root / 'current.tmp'
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    temporary.replace(root / 'current')
    return root / 'current' / 'bin' / 'botsdock-connector'


def cleanup_environments(root: Path) -> None:
    # Keep any environment still used by a foreground/background Connector.
    try:
        processes = subprocess.run(['ps', '-axo', 'command='], check=True,
                                   capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return
    keep = {(root / name).resolve() for name in ('current', 'previous')}
    for environment in root.glob('env-*'):
        if (environment.is_symlink() or environment in keep or
                str(environment) in processes or
                not (environment / 'botsdock-install.json').is_file()):
            continue
        shutil.rmtree(environment, ignore_errors=True)


def install(package_spec: str, pip_args: list[str] | None = None) -> Path:
    with installation_lock():
        return _install(package_spec, pip_args)


def _install(package_spec: str, pip_args: list[str] | None = None) -> Path:
    if os.name != 'posix':
        raise RuntimeError('Managed installation currently supports macOS and Linux.')
    package_spec = resolve_spec(package_spec)
    root = Path.home() / '.botsdock' / 'connector'
    root.mkdir(parents=True, exist_ok=True)
    environment = Path(tempfile.mkdtemp(prefix='env-', dir=root))
    try:
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / 'bin' / 'python'
        subprocess.run([str(python), '-m', 'pip', 'install', '--upgrade',
                        *(pip_args or []), package_spec], check=True)
        entry = environment / 'bin' / 'botsdock-connector'
        if not entry.is_file():
            raise RuntimeError('Installation did not create botsdock-connector.')
        verify(environment)
        (environment / 'botsdock-install.json').write_text(
            json.dumps({'package_spec': package_spec}) + '\n', encoding='utf-8')
        current = root / 'current'
        previous = root / 'previous'
        if current.is_symlink():
            temporary = root / 'previous.tmp'
            temporary.unlink(missing_ok=True)
            temporary.symlink_to(current.resolve())
            temporary.replace(previous)
        temporary = root / 'current.tmp'
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(environment)
        temporary.replace(current)
    except BaseException:
        shutil.rmtree(environment, ignore_errors=True)
        raise
    cleanup_environments(root)
    return current / 'bin' / 'botsdock-connector'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package-spec', default=DEFAULT_SPEC)
    parser.add_argument('--rollback', action='store_true')
    args = parser.parse_args()
    try:
        entry = rollback() if args.rollback else install(args.package_spec)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f'Connector installation failed: {exc}', file=sys.stderr)
        return 1
    print(f'Installed: {entry}')
    print('Add the connector bin directory to PATH (see installation documentation).')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
