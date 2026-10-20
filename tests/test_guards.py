"""Schutzschalter der schreibenden Tools, ohne echten ESXi-Host (Fake-Objekte)."""

import os
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import esxi_mcp as em  # noqa: E402


class FakeVM:
    def __init__(self, name="testvm", moid="1", power="poweredOn", mem=8192, cpu=4, cores=1,
                 mem_hot=False, cpu_hot=False, tools="guestToolsRunning"):
        self.name, self._moId = name, moid
        self.runtime = SimpleNamespace(powerState=power)
        self.guest = SimpleNamespace(toolsRunningStatus=tools)
        self.config = SimpleNamespace(
            hardware=SimpleNamespace(memoryMB=mem, numCPU=cpu, numCoresPerSocket=cores),
            memoryHotAddEnabled=mem_hot, cpuHotAddEnabled=cpu_hot,
        )
        self.snapshot = None
        self.calls = []

    def __getattr__(self, item):
        # jede API-Methode (PowerOffVM_Task, ReconfigVM_Task, ...) protokollieren statt ausführen
        def call(*a, **kw):
            self.calls.append(item)
            return SimpleNamespace(info=SimpleNamespace(state="success"), _moId="task-1")
        return call


@pytest.fixture
def vm(monkeypatch):
    v = FakeVM()

    @contextmanager
    def fake_esxi():
        yield object()

    monkeypatch.setattr(em, "esxi", fake_esxi)
    monkeypatch.setattr(em, "_find_vm", lambda si, name: v)
    monkeypatch.setattr(em, "_host", lambda si: SimpleNamespace(
        summary=SimpleNamespace(hardware=SimpleNamespace(memorySize=256 * 1024 ** 3))))
    for var in ("MCP_READONLY", "MCP_PROTECTED_VMS", "MCP_MAX_MEMORY_MB", "MCP_MAX_CPU"):
        monkeypatch.delenv(var, raising=False)
    return v


def test_power_without_confirm_is_preview(vm):
    r = em.vm_power("testvm", "off")
    assert r["vorschau"] is True and vm.calls == []


def test_power_with_confirm_runs(vm):
    r = em.vm_power("testvm", "off", confirm=True)
    assert vm.calls == ["PowerOffVM_Task"] and r["status"] == "erfolgreich"


def test_readonly_blocks(vm, monkeypatch):
    monkeypatch.setenv("MCP_READONLY", "true")
    with pytest.raises(RuntimeError, match="READONLY"):
        em.vm_power("testvm", "off", confirm=True)
    assert vm.calls == []


@pytest.mark.parametrize("entry", ["testvm", "TESTVM", "1", "other, testvm"])
def test_protected_vm_blocks(vm, monkeypatch, entry):
    monkeypatch.setenv("MCP_PROTECTED_VMS", entry)
    with pytest.raises(RuntimeError, match="geschützt"):
        em.vm_set_resources("testvm", memory_mb=16384, confirm=True)
    assert vm.calls == []


def test_unknown_power_action(vm):
    with pytest.raises(RuntimeError, match="Unbekannte Aktion"):
        em.vm_power("testvm", "destroy", confirm=True)


def test_shutdown_needs_tools(vm):
    vm.guest.toolsRunningStatus = "guestToolsNotRunning"
    r = em.vm_power("testvm", "shutdown", confirm=True)
    assert "VMware Tools" in r["hinweis"] and vm.calls == []


def test_ram_increase_running_without_hotadd_refused(vm):
    r = em.vm_set_resources("testvm", memory_mb=16384, confirm=True)
    assert r["nicht_moeglich"] and vm.calls == []


def test_ram_increase_powered_off_with_snapshot(vm):
    vm.runtime.powerState = "poweredOff"
    r = em.vm_set_resources("testvm", memory_mb=16384, confirm=True)
    assert vm.calls == ["CreateSnapshot_Task", "ReconfigVM_Task", "Reload"]
    assert r["ram_mb"] == {"jetzt": 8192, "neu": 16384}


def test_ram_preview_without_confirm(vm):
    vm.runtime.powerState = "poweredOff"
    r = em.vm_set_resources("testvm", memory_mb=16384)
    assert r["vorschau"] is True and vm.calls == []


def test_ram_hotadd_running(vm):
    vm.config.memoryHotAddEnabled = True
    em.vm_set_resources("testvm", memory_mb=16384, snapshot=False, confirm=True)
    assert vm.calls == ["ReconfigVM_Task", "Reload"]


def test_ram_shrink_running_refused(vm):
    vm.config.memoryHotAddEnabled = True
    r = em.vm_set_resources("testvm", memory_mb=4096, confirm=True)
    assert r["nicht_moeglich"] and vm.calls == []


@pytest.mark.parametrize("mem", [100, 16385])
def test_ram_invalid_values(vm, mem):
    with pytest.raises(RuntimeError, match="memory_mb"):
        em.vm_set_resources("testvm", memory_mb=mem, confirm=True)


def test_ram_limit(vm, monkeypatch):
    monkeypatch.setenv("MCP_MAX_MEMORY_MB", "12288")
    with pytest.raises(RuntimeError, match="Limit"):
        em.vm_set_resources("testvm", memory_mb=16384, confirm=True)


def test_more_ram_than_host_refused(vm):
    vm.runtime.powerState = "poweredOff"
    r = em.vm_set_resources("testvm", memory_mb=300 * 1024, confirm=True)
    assert r["nicht_moeglich"] and vm.calls == []


def test_no_change_needed(vm):
    r = em.vm_set_resources("testvm", memory_mb=8192, vcpu=4, confirm=True)
    assert "Keine Änderung" in r["hinweis"] and vm.calls == []


def test_snapshot_preview_and_run(vm):
    assert em.vm_snapshot_create("testvm", "test")["vorschau"] is True and vm.calls == []
    em.vm_snapshot_create("testvm", "test", confirm=True)
    assert vm.calls == ["CreateSnapshot_Task"]


def test_main_refuses_short_token(monkeypatch):
    monkeypatch.setattr(em, "MCP_AUTH_TOKEN", "kurz")
    with pytest.raises(RuntimeError, match="MCP_AUTH_TOKEN"):
        em.main()


def test_no_delete_tools():
    assert not [t for t in em.TOOL_FUNCS if any(w in t for w in ("delete", "remove", "destroy", "revert"))]


def test_guest_filesystems_merges_bind_mounts():
    gb = 1024 ** 3
    d = lambda p, cap, free: SimpleNamespace(diskPath=p, capacity=cap * gb, freeSpace=free * gb, filesystemType="xfs")
    fs = em._guest_filesystems([d("/usr/share/cagefs-skeleton", 1000, 600), d("/", 1000, 600),
                                d("/usr/share/cagefs-skeleton/lib", 1000, 600), d("/boot", 1, 0.5)])
    assert [f["pfad"] for f in fs] == ["/", "/boot"]
    assert fs[0]["weitere_mounts"] == 2 and fs[0]["belegt_prozent"] == 40.0
    assert "weitere_mounts" not in fs[1]
    assert em._guest_filesystems(None) == []


def test_guest_filesystems_tolerates_small_free_drift():
    gb = 1024 ** 3
    d = lambda p, free: SimpleNamespace(diskPath=p, capacity=1000 * gb, freeSpace=free, filesystemType="xfs")
    fs = em._guest_filesystems([d("/", 600 * gb), d("/a", 600 * gb - 4096), d("/b", 600 * gb + 8192), d("/data", 300 * gb)])
    assert [(f["pfad"], f.get("weitere_mounts")) for f in fs] == [("/", 2), ("/data", None)]


@pytest.fixture
def guest_vm(vm, monkeypatch):
    vm.config.guestId = "debian12Guest"
    vm.config.guestFullName = "Debian 12 (32-bit)"
    monkeypatch.setattr(em, "_guest_os_options", lambda v: {
        "debian12Guest": ("Debian 12 (32-bit)", "supported"),
        "debian12_64Guest": ("Debian 12 (64-bit)", "supported"),
        "debian6_64Guest": ("Debian 6 (64-bit)", "terminated"),
    })
    return vm


def test_guest_os_running_vm_refused(guest_vm):
    r = em.vm_set_guest_os("testvm", "debian12_64Guest", confirm=True)
    assert r["nicht_moeglich"] and guest_vm.calls == []


def test_guest_os_preview_and_run(guest_vm):
    guest_vm.runtime.powerState = "poweredOff"
    assert em.vm_set_guest_os("testvm", "debian12_64Guest")["vorschau"] is True and guest_vm.calls == []
    em.vm_set_guest_os("testvm", "debian12_64Guest", confirm=True)
    assert guest_vm.calls == ["ReconfigVM_Task", "Reload"]


@pytest.mark.parametrize("gid, msg", [("windows99Guest", "nicht angeboten"), ("debian6_64Guest", "terminated")])
def test_guest_os_invalid(guest_vm, gid, msg):
    guest_vm.runtime.powerState = "poweredOff"
    with pytest.raises(RuntimeError, match=msg):
        em.vm_set_guest_os("testvm", gid, confirm=True)
    assert guest_vm.calls == []


def test_guest_os_readonly_and_protected(guest_vm, monkeypatch):
    guest_vm.runtime.powerState = "poweredOff"
    monkeypatch.setenv("MCP_PROTECTED_VMS", "testvm")
    with pytest.raises(RuntimeError, match="geschützt"):
        em.vm_set_guest_os("testvm", "debian12_64Guest", confirm=True)
    monkeypatch.setenv("MCP_READONLY", "true")
    with pytest.raises(RuntimeError, match="READONLY"):
        em.vm_set_guest_os("testvm", "debian12_64Guest", confirm=True)
