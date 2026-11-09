"""Tests for the platform-aware peak-memory helper used by `ties bench`."""

from __future__ import annotations

import sys
import types

import pytest

from ties import cli


def _fake_resource(ru_maxrss: int) -> types.ModuleType:
    """A stand-in for the Unix-only `resource` module reporting a fixed ru_maxrss."""
    module = types.ModuleType("resource")
    module.RUSAGE_SELF = 0
    module.getrusage = lambda who: types.SimpleNamespace(ru_maxrss=ru_maxrss)
    return module


def test_linux_reports_kilobytes_as_megabytes(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setitem(sys.modules, "resource", _fake_resource(300 * 1024))
    assert cli._peak_memory_mb() == 300


def test_macos_reports_bytes_as_megabytes(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "resource", _fake_resource(300 * 1024 * 1024))
    assert cli._peak_memory_mb() == 300


def test_missing_resource_module_gives_none(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setitem(sys.modules, "resource", None)  # makes `import resource` fail
    assert cli._peak_memory_mb() is None


@pytest.mark.skipif(sys.platform != "win32", reason="reads the Windows process memory counters")
def test_windows_reads_a_positive_peak_working_set():
    peak = cli._peak_memory_mb()
    assert isinstance(peak, int)
    assert peak > 0
