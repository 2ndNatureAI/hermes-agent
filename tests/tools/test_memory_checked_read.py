"""Memory updates must not treat an unreliable existence probe as absence."""
from pathlib import Path

import pytest

from tools import memory_tool
from tools.memory_tool import MemoryStore


@pytest.fixture
def memory_dir(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    root = tmp_path / 'memory'; root.mkdir()
    monkeypatch.setattr(memory_tool, 'get_memory_dir', lambda: root)
    return root


@pytest.mark.parametrize(('filename', 'target'), [('MEMORY.md', 'memory'), ('USER.md', 'user')])
def test_false_existence_probe_does_not_discard_readable_memory(memory_dir, monkeypatch, filename, target):
    path = memory_dir / filename
    path.write_text('original fact', encoding='utf-8')
    original_exists = Path.exists
    monkeypatch.setattr(Path, 'exists', lambda self: False if self == path else original_exists(self))
    result = MemoryStore().add(target, 'additional fact')
    assert result['success'] is True
    assert path.read_text(encoding='utf-8') == 'original fact\n§\nadditional fact'


@pytest.mark.parametrize(('filename', 'target'), [('MEMORY.md', 'memory'), ('USER.md', 'user')])
def test_unreadable_file_with_false_probe_is_not_overwritten(memory_dir, monkeypatch, filename, target):
    path = memory_dir / filename
    path.write_text('original fact', encoding='utf-8')
    exists = Path.exists; read = Path.read_text
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'exists', lambda self: False if self == path else exists(self))
        def read_denied(self, *args, **kwargs):
            if self == path:
                raise PermissionError('synthetic denied read')
            return read(self, *args, **kwargs)
        patch.setattr(Path, 'read_text', read_denied)
        result = MemoryStore().add(target, 'additional fact')
    assert result['success'] is False
    assert path.read_text(encoding='utf-8') == 'original fact'


def test_missing_file_still_creates_memory(memory_dir):
    result = MemoryStore().add('memory', 'new fact')
    assert result['success'] is True
    assert (memory_dir / 'MEMORY.md').read_text(encoding='utf-8') == 'new fact'


def test_unresolved_existing_path_is_not_assumed_missing(memory_dir, monkeypatch):
    path = memory_dir / 'MEMORY.md'
    path.write_text('original fact', encoding='utf-8')
    read = Path.read_text
    def synthetic_missing(self, *args, **kwargs):
        if self == path:
            raise FileNotFoundError('synthetic disappearing target')
        return read(self, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'read_text', synthetic_missing)
        result = MemoryStore().add('memory', 'additional fact')
    assert result['success'] is False
    assert path.read_text(encoding='utf-8') == 'original fact'


def test_failed_link_metadata_is_not_assumed_missing(memory_dir, monkeypatch):
    path = memory_dir / 'MEMORY.md'
    path.write_text('original fact', encoding='utf-8')
    read = Path.read_text; lstat = Path.lstat
    def synthetic_missing(self, *args, **kwargs):
        if self == path:
            raise FileNotFoundError('synthetic disappearing target')
        return read(self, *args, **kwargs)
    def synthetic_lstat_failure(self, *args, **kwargs):
        if self == path:
            raise PermissionError('synthetic unavailable link metadata')
        return lstat(self, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'read_text', synthetic_missing)
        patch.setattr(Path, 'lstat', synthetic_lstat_failure)
        result = MemoryStore().add('memory', 'additional fact')
    assert result['success'] is False
    assert path.read_text(encoding='utf-8') == 'original fact'


def test_real_profile_resolver_preserves_unreadable_memory(tmp_path, monkeypatch):
    home = tmp_path / 'profile-home'
    monkeypatch.setenv('HERMES_HOME', str(home))
    path = home / 'memories' / 'MEMORY.md'
    path.parent.mkdir(parents=True)
    path.write_text('profile fact', encoding='utf-8')
    assert memory_tool.get_memory_dir() == path.parent
    exists = Path.exists; read = Path.read_text
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'exists', lambda self: False if self == path else exists(self))
        def deny(self, *args, **kwargs):
            if self == path:
                raise PermissionError('synthetic denied read')
            return read(self, *args, **kwargs)
        patch.setattr(Path, 'read_text', deny)
        result = MemoryStore().add('memory', 'new fact')
    assert result['success'] is False
    assert path.read_text(encoding='utf-8') == 'profile fact'


def test_dangling_memory_link_is_not_overwritten(memory_dir):
    link = memory_dir / 'MEMORY.md'
    try:
        link.symlink_to(memory_dir / 'missing-target')
    except OSError:
        pytest.skip('host cannot create file symlinks')
    result = MemoryStore().add('memory', 'new fact')
    assert result['success'] is False
    assert link.is_symlink()
