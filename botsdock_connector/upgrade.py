"""Upgrade Connector through verified, isolated installations."""
from __future__ import annotations

import argparse
import shlex
import subprocess
import sys

PYPI_UPGRADE_SPEC = "botsdock-connector"
GITHUB_UPGRADE_SPEC = "git+https://github.com/bourne015/botsdock-connector.git"


def _upgrade_spec_with_version(spec: str, version: str | None) -> str:
    if not version:
        return spec
    if spec.startswith("git+"):
        base = spec.rsplit("@", 1)[0] if "@" in spec.rsplit("/", 1)[-1] else spec
        return f"{base}@{version}"
    return f"{spec}=={version}"


def build_upgrade_pip_args(args: argparse.Namespace) -> list[str]:
    package_spec = args.package_spec
    if not package_spec:
        package_spec = GITHUB_UPGRADE_SPEC if args.source == "github" else PYPI_UPGRADE_SPEC
    package_spec = _upgrade_spec_with_version(str(package_spec), args.version)
    command = [sys.executable, "-m", "pip", "install", "--upgrade"]
    if args.user:
        command.append("--user")
    if args.pre:
        command.append("--pre")
    if args.force_reinstall or package_spec.startswith("git+"):
        command.append("--force-reinstall")
    command.extend(str(item) for item in (args.pip_arg or []))
    command.append(package_spec)
    return command


def run_upgrade(args: argparse.Namespace) -> int:
    from .install import install

    if args.user:
        print('Managed upgrades use an isolated environment; omit --user.', file=sys.stderr)
        return 1
    command = build_upgrade_pip_args(args)
    package_spec = command[-1]
    print('Install into a new isolated environment: ' + shlex.join(command), file=sys.stderr)
    if args.dry_run:
        return 0
    try:
        entry = install(package_spec, command[5:-1])
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f'Connector upgrade failed; the previous installation remains active: {exc}',
              file=sys.stderr)
        return 1
    print(f'Connector installed and verified: {entry}', file=sys.stderr)
    print('Ensure $HOME/.botsdock/connector/current/bin is first on PATH.', file=sys.stderr)
    print(
        "Restart the connector to use the new version. For background mode, "
        "run 'botsdock-connector stop', then repeat your original start command "
        "(including any custom server, machine or runtime arguments). "
        "For foreground mode, stop it with Ctrl-C and repeat the original command.",
        file=sys.stderr,
    )
    return 0
