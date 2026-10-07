from pathlib import Path
import subprocess

import pytest

from botsdock_connector import install as installer


def prepare(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    root = tmp_path / '.botsdock' / 'connector'
    old = root / 'old'
    (old / 'bin').mkdir(parents=True)
    (old / 'bin' / 'botsdock-connector').touch()
    (root / 'current').symlink_to(old)
    monkeypatch.setattr(installer, 'resolve_spec', lambda spec: spec)
    def create(self, path):
        (path / 'bin').mkdir()
        (path / 'bin' / 'botsdock-connector').touch()
    monkeypatch.setattr(installer.venv.EnvBuilder, 'create', create)
    return root, old


def test_switches_only_after_validation_and_preserves_previous(monkeypatch, tmp_path):
    root, old = prepare(monkeypatch, tmp_path)
    commands = []
    def run(command, **kwargs):
        if command[0] == 'ps':
            return subprocess.CompletedProcess(command, 0, stdout='')
        assert (root / 'current').resolve() == old
        commands.append(command)
    monkeypatch.setattr(installer.subprocess, 'run', run)
    entry = installer.install('example.whl')
    assert entry.is_file()
    assert (root / 'current').resolve() != old
    assert (root / 'previous').resolve() == old
    assert any(command[-2:] == ['pip', 'check'] for command in commands)
    assert commands[-1][-2:] == ['botsdock_connector', '--help']
    assert 'example.whl' in ((root / 'current').resolve() / 'botsdock-install.json').read_text()


@pytest.mark.parametrize('failure_at', [1, 2, 3])
def test_failed_install_or_validation_keeps_old_entry(monkeypatch, tmp_path, failure_at):
    root, old = prepare(monkeypatch, tmp_path)
    count = 0
    def run(command, **kwargs):
        nonlocal count
        count += 1
        if count == failure_at:
            raise subprocess.CalledProcessError(1, command)
    monkeypatch.setattr(installer.subprocess, 'run', run)
    with pytest.raises(subprocess.CalledProcessError):
        installer.install('example.whl')
    assert (root / 'current').resolve() == old
    assert list(root.glob('env-*')) == []


def test_rollback_validates_previous_before_switching(monkeypatch, tmp_path):
    root, old = prepare(monkeypatch, tmp_path)
    previous = root / 'older'
    previous.mkdir()
    (root / 'previous').symlink_to(previous)
    def fail(path):
        assert path == previous
        raise RuntimeError('broken previous installation')
    monkeypatch.setattr(installer, 'verify', fail)
    with pytest.raises(RuntimeError):
        installer.rollback()
    assert (root / 'current').resolve() == old
    monkeypatch.setattr(installer, 'verify', lambda path: None)
    installer.rollback()
    assert (root / 'current').resolve() == previous


def test_resolves_highest_stable_tag_to_immutable_commit(monkeypatch):
    monkeypatch.setattr(installer.subprocess, 'run', lambda *a, **kw:
        subprocess.CompletedProcess(a, 0, stdout='aaa refs/tags/v0.1.9\nbbb refs/tags/v0.1.15\nccc refs/tags/v0.1.15^{}\nddd refs/tags/v0.2.0rc1\n'))
    assert installer.resolve_spec(installer.DEFAULT_SPEC) == installer.DEFAULT_SPEC + '@ccc'
    assert installer.resolve_spec('custom.whl') == 'custom.whl'


def test_missing_stable_release_fails_before_install(monkeypatch):
    monkeypatch.setattr(installer.subprocess, 'run', lambda *a, **kw:
        subprocess.CompletedProcess(a, 0, stdout='aaa refs/tags/v1.0.0rc1\n'))
    with pytest.raises(RuntimeError, match='No stable release'):
        installer.resolve_spec(installer.DEFAULT_SPEC)


def test_concurrent_installation_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    with installer.installation_lock():
        with pytest.raises(RuntimeError, match='Another installation'):
            with installer.installation_lock():
                pass


def test_cleanup_keeps_previous_and_running_environments(monkeypatch, tmp_path):
    for name in ['env-current', 'env-previous', 'env-running', 'env-unused']:
        directory = tmp_path / name
        directory.mkdir()
        (directory / 'botsdock-install.json').touch()
    (tmp_path / 'current').symlink_to(tmp_path / 'env-current')
    (tmp_path / 'previous').symlink_to(tmp_path / 'env-previous')
    monkeypatch.setattr(installer.subprocess, 'run', lambda *a, **kw:
        subprocess.CompletedProcess(a, 0, stdout=str(tmp_path / 'env-running' / 'bin' / 'python')))
    installer.cleanup_environments(tmp_path)
    assert not (tmp_path / 'env-unused').exists()
    assert (tmp_path / 'env-running').exists()
    assert (tmp_path / 'env-previous').exists()
