"""Upgrade subcommand for botsdock-connector."""

from __future__ import annotations

import argparse
import importlib.metadata
import shlex
import subprocess
import sys

from .log import get_logger

logger = get_logger(__name__)

PYPI_UPGRADE_SPEC = "botsdock-connector"
GITHUB_UPGRADE_SPEC = "git+https://github.com/bourne015/botsdock-connector.git"


def _parse_pip_version(version_string: str) -> tuple[int, ...] | None:
    """Parse pip version to a comparable tuple.

    Handles non-standard version strings like ``22.0.2+dev`` or ``22.0.2rc1``
    by keeping only the leading numeric prefix of each dot-separated component.
    """
    try:
        parts = []
        for part in version_string.split(".")[:3]:
            i = 0
            while i < len(part) and part[i].isdigit():
                i += 1
            if i == 0:
                break
            parts.append(int(part[:i]))
        return tuple(parts) if parts else None
    except (ValueError, TypeError):
        return None


def _check_pip_version() -> bool:
    """Check that pip > 22.0.2 is available (older versions produce UNKNOWN wheels)."""
    try:
        pip_version = importlib.metadata.version("pip")
    except importlib.metadata.PackageNotFoundError:
        print("botsdock connector upgrade: pip is not installed.", file=sys.stderr)
        return False
    parsed = _parse_pip_version(pip_version)
    if parsed is None:
        print(
            f"botsdock connector upgrade: could not determine pip version ({pip_version}). "
            "Run 'python3 -m pip install --upgrade pip' first.",
            file=sys.stderr,
        )
        return False
    if parsed <= (22, 0, 2):
        print(
            f"botsdock connector upgrade: pip > 22.0.2 is required, found {pip_version}. "
            "Run 'python3 -m pip install --upgrade pip' first.",
            file=sys.stderr,
        )
        return False
    return True


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
    if args.force_reinstall:
        command.append("--force-reinstall")
    command.extend(str(item) for item in (args.pip_arg or []))
    command.append(package_spec)
    return command


def _get_installed_version() -> str | None:
    try:
        return importlib.metadata.version("botsdock-connector")
    except importlib.metadata.PackageNotFoundError:
        return None


def _get_package_version_via_subprocess() -> str | None:
    """Read installed version via a fresh subprocess, avoiding importlib caches."""
    try:
        result = subprocess.run(
            [sys.executable, "-c",
             "from importlib.metadata import version; print(version('botsdock-connector'))"],
            capture_output=True, text=True,
        )
        return result.stdout.strip() or None
    except (subprocess.SubprocessError, OSError):
        return None


def _parse_successfully_installed_version(output: str) -> str | None:
    """Parse pip's confirmed installed botsdock-connector version."""
    prefix = "botsdock-connector-"
    for line in output.splitlines():
        marker = "Successfully installed "
        if marker not in line:
            continue
        installed = line.split(marker, 1)[1]
        for token in installed.split():
            normalized = token.lower().replace("_", "-")
            if normalized.startswith(prefix):
                return token[len(prefix):]
    return None


def _write_process_output(output: str, *, file: object) -> None:
    if not output:
        return
    print(output, end="" if output.endswith("\n") else "\n", file=file)


def run_upgrade(args: argparse.Namespace) -> int:
    if not _check_pip_version():
        return 1
    if args.user and sys.prefix != sys.base_prefix:
        print(
            "botsdock connector upgrade: warning: --user is set but running inside "
            "a virtual environment. The package may install to the user site instead "
            "of the venv, which is rarely what you want. Consider dropping --user.",
            file=sys.stderr,
        )

    old_version = _get_installed_version()
    if old_version:
        print(f"Current botsdock-connector version: {old_version}", file=sys.stderr)
    else:
        print("botsdock-connector is not currently installed.", file=sys.stderr)

    command = build_upgrade_pip_args(args)
    printable = " ".join(shlex.quote(part) for part in command)
    print(f"botsdock connector upgrade command: {printable}", file=sys.stderr)
    if args.dry_run:
        return 0
    result = subprocess.run(command, capture_output=True, text=True)
    _write_process_output(result.stdout, file=sys.stdout)
    _write_process_output(result.stderr, file=sys.stderr)
    if result.returncode == 0:
        new_version = _parse_successfully_installed_version(
            f"{result.stdout}\n{result.stderr}"
        ) or _get_package_version_via_subprocess()
        if new_version:
            if old_version and old_version != new_version:
                print(
                    f"botsdock connector upgraded: {old_version} -> {new_version}",
                    file=sys.stderr,
                )
            else:
                print(
                    f"botsdock connector version {new_version} installed.",
                    file=sys.stderr,
                )
        else:
            print(
                "botsdock connector upgrade finished. Restart botsdock-connector to use the new version.",
                file=sys.stderr,
            )
    return result.returncode
