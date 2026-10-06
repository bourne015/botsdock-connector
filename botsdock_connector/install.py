"""Standalone installer for isolated, replaceable Connector environments."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import venv

DEFAULT_SPEC = 'git+https://github.com/bourne015/botsdock-connector.git'


def install(package_spec: str, pip_args: list[str] | None = None) -> Path:
    if os.name != 'posix':
        raise RuntimeError('Managed installation currently supports macOS and Linux.')
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
        return current / 'bin' / 'botsdock-connector'
    except BaseException:
        shutil.rmtree(environment, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package-spec', default=DEFAULT_SPEC)
    args = parser.parse_args()
    try:
        entry = install(args.package_spec)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f'Connector installation failed: {exc}', file=sys.stderr)
        return 1
    print(f'Installed: {entry}')
    print('Add the connector bin directory to PATH (see installation documentation).')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
