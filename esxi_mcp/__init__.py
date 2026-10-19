"""
ESXi MCP Server für einen einzelnen VMware-ESXi-Host (vSphere Web Services API über pyVmomi).
Aufbau wie pbs-mcp/zammad-mcp: roher mcp.server.Server, Tool-Funktionen in TOOL_FUNCS.
Läuft ausschliesslich als Docker-Container mit Streamable-HTTP-Transport hinter einem
Reverse-Proxy und erfordert MCP_AUTH_TOKEN. Ein lokaler Betrieb (stdio/uvx) ist nicht vorgesehen.

Alle Werte kommen aus Umgebungsvariablen (ESXI_HOST, ESXI_USER, ESXI_PASSWORD, ESXI_PORT,
ESXI_VERIFY_SSL, ESXI_HTTP_PROXY, MCP_AUTH_TOKEN, MCP_HOST, MCP_PORT, MCP_READONLY,
MCP_PROTECTED_VMS, MCP_MAX_MEMORY_MB, MCP_MAX_CPU) - keine Secrets im Code.

Schutz für schreibende Tools:
- MCP_READONLY=true sperrt alle schreibenden Tools.
- Jedes schreibende Tool braucht confirm=true, ohne confirm kommt nur eine Vorschau.
- VMs in MCP_PROTECTED_VMS (kommagetrennt, Name oder MoID) sind für schreibende Tools gesperrt.
- MCP_MAX_MEMORY_MB / MCP_MAX_CPU begrenzen, wie gross eine VM per MCP werden darf.
- RAM/CPU-Änderungen legen vorher standardmässig einen Snapshot an.
- Löschen (VMs, Snapshots, Disks) ist bewusst nicht implementiert.
"""

import asyncio
import json
import os
import ssl
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from mcp.server import Server
from mcp.types import Tool, TextContent


# Version = <Major.Minor>.<Patch>, Patch wird aus der Git-Historie gezählt (wie pbs-mcp).
_REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VERSION_PATHS = ['Dockerfile', 'requirements.txt', 'pyproject.toml', 'esxi_mcp/', '.github/workflows/docker-publish.yml']


def _version_base() -> str:
    raw = ""
    try:
        import tomllib
        with open(os.path.join(_REPO_DIR, "pyproject.toml"), "rb") as f:
            raw = tomllib.load(f)["project"]["version"]
    except Exception:
        try:
            from importlib.metadata import version
            raw = version("esxi-mcp")
        except Exception:
            pass
    return ".".join(raw.split(".")[:2]) if raw else "0.0"


def _read_version() -> str:
    env_version = os.environ.get("APP_VERSION", "").strip()
    if env_version:
        return env_version
    base = _version_base()
    try:
        import subprocess
        count = subprocess.run(
            ["git", "rev-list", "--count", "HEAD", "--", *_VERSION_PATHS],
            cwd=_REPO_DIR, capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
        if count.isdigit():
            return f"{base}.{count}"
    except Exception:
        pass
    return f"{base}.0-dev"


__version__ = _read_version()


# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _flag(name: str) -> bool:
    return _env(name, "false").lower() in ("1", "true", "yes")


def _int_env(name: str) -> int | None:
    raw = _env(name)
    return int(raw) if raw.isdigit() else None


def _protected() -> set[str]:
    return {p.strip().lower() for p in _env("MCP_PROTECTED_VMS").split(",") if p.strip()}


def _ssl_context() -> ssl.SSLContext:
    raw = _env("ESXI_VERIFY_SSL", "true")
    if raw.lower() == "false":
        return ssl._create_unverified_context()
    if raw.lower() == "true":
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=raw)  # Pfad zu CA-Datei


# ---------------------------------------------------------------------------
# Verbindung (pro Aufruf eine Session, danach Logout - kein Session-Leak auf dem Host)
# ---------------------------------------------------------------------------

_connect_lock = threading.Lock()


@contextmanager
def esxi():
    from pyVim.connect import SmartConnect, Disconnect

    host, user, pwd = _env("ESXI_HOST"), _env("ESXI_USER"), os.environ.get("ESXI_PASSWORD", "")
    if not (host and user and pwd):
        raise RuntimeError("ESXI_HOST, ESXI_USER und ESXI_PASSWORD müssen gesetzt sein (Umgebungsvariablen)")
    kwargs = {"host": host, "user": user, "pwd": pwd, "port": int(_env("ESXI_PORT", "443")), "sslContext": _ssl_context()}
    proxy = _env("ESXI_HTTP_PROXY")
    if proxy:
        p_host, _, p_port = proxy.replace("http://", "").rstrip("/").rpartition(":")
        kwargs.update(httpProxyHost=p_host, httpProxyPort=int(p_port))
    with _connect_lock:
        si = SmartConnect(**kwargs)
    try:
        yield si
    finally:
        try:
            Disconnect(si)
        except Exception:
            pass


def _require_write() -> None:
    if _flag("MCP_READONLY"):
        raise RuntimeError("MCP_READONLY ist gesetzt - schreibende Aktionen sind gesperrt")


def _check_not_protected(vm) -> None:
    prot = _protected()
    if vm.name.lower() in prot or vm._moId.lower() in prot:
        raise RuntimeError(f"VM '{vm.name}' ist geschützt (MCP_PROTECTED_VMS) - keine schreibenden Aktionen erlaubt")


def _all_vms(si) -> list:
    from pyVmomi import vim
    content = si.RetrieveContent()
    view = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
    try:
        return list(view.view)
    finally:
        view.Destroy()


def _find_vm(si, vm: str):
    """VM per exaktem Namen (Gross/Klein egal) oder MoID suchen."""
    key = vm.strip().lower()
    hits = [v for v in _all_vms(si) if v.name.lower() == key or v._moId.lower() == key]
    if not hits:
        raise RuntimeError(f"VM '{vm}' nicht gefunden (Name oder MoID aus vm_list verwenden)")
    if len(hits) > 1:
        raise RuntimeError(f"Name '{vm}' ist nicht eindeutig - MoID verwenden: {[h._moId for h in hits]}")
    return hits[0]


def _host(si):
    from pyVmomi import vim
    content = si.RetrieveContent()
    view = content.viewManager.CreateContainerView(content.rootFolder, [vim.HostSystem], True)
    try:
        return view.view[0]
    finally:
        view.Destroy()


def _wait(task, timeout: int = 300) -> dict:
    """Task abwarten und Ergebnis zurückgeben."""
    deadline = time.time() + timeout
    while task.info.state in ("queued", "running"):
        if time.time() > deadline:
            return {"status": "läuft noch", "task": task._moId, "hinweis": f"Nach {timeout}s nicht fertig, mit task_list prüfen"}
        time.sleep(1)
    if task.info.state == "error":
        err = task.info.error
        raise RuntimeError(f"Task fehlgeschlagen: {getattr(err, 'msg', None) or err}")
    return {"status": "erfolgreich", "task": task._moId}


def _ts(value) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _gb(num_bytes) -> float | None:
    return round(num_bytes / 1024 ** 3, 1) if num_bytes is not None else None


# ---------------------------------------------------------------------------
# Lesende Tools
# ---------------------------------------------------------------------------

def get_version() -> dict:
    return {"version": __version__, "git_sha": _env("GIT_SHA", "unbekannt")}


def host_info() -> dict:
    """Version, Hardware, Auslastung und Uptime des ESXi-Hosts."""
    with esxi() as si:
        about = si.RetrieveContent().about
        h = _host(si)
        hw, qs, rt = h.summary.hardware, h.summary.quickStats, h.runtime
        cpu_total = hw.numCpuCores * hw.cpuMhz
        mem_total_mb = hw.memorySize // 1024 ** 2
        return {
            "name": h.name,
            "version": about.fullName,
            "hersteller": hw.vendor,
            "modell": hw.model,
            "cpu": {"modell": hw.cpuModel, "sockel": hw.numCpuPkgs, "kerne": hw.numCpuCores, "threads": hw.numCpuThreads,
                    "last_mhz": qs.overallCpuUsage, "total_mhz": cpu_total,
                    "last_prozent": round(100 * (qs.overallCpuUsage or 0) / cpu_total, 1) if cpu_total else None},
            "ram": {"total_gb": round(mem_total_mb / 1024, 1), "genutzt_gb": round((qs.overallMemoryUsage or 0) / 1024, 1),
                    "genutzt_prozent": round(100 * (qs.overallMemoryUsage or 0) / mem_total_mb, 1) if mem_total_mb else None},
            "uptime_stunden": round((qs.uptime or 0) / 3600, 1),
            "verbindungsstatus": rt.connectionState,
            "wartungsmodus": rt.inMaintenanceMode,
            "status": h.overallStatus,
        }


def datastore_list() -> list[dict]:
    """Datastores mit Kapazität und Belegung."""
    with esxi() as si:
        out = []
        for ds in _host(si).datastore:
            s = ds.summary
            used = (s.capacity or 0) - (s.freeSpace or 0)
            out.append({
                "name": s.name, "typ": s.type, "erreichbar": s.accessible,
                "kapazitaet_gb": _gb(s.capacity), "frei_gb": _gb(s.freeSpace),
                "belegt_prozent": round(100 * used / s.capacity, 1) if s.capacity else None,
                "vms": len(ds.vm),
            })
        return out


def vm_list(power_state: str = None, name_filter: str = None) -> list[dict]:
    """Alle VMs mit Power-Status, RAM, vCPU, Gast-OS, IP und aktueller Auslastung."""
    with esxi() as si:
        out = []
        for vm in _all_vms(si):
            s = vm.summary
            c, q, g, r = s.config, s.quickStats, s.guest, s.runtime
            if power_state and r.powerState != power_state:
                continue
            if name_filter and name_filter.lower() not in (c.name or "").lower():
                continue
            out.append({
                "name": c.name, "moid": vm._moId, "power": r.powerState,
                "ram_mb": c.memorySizeMB, "vcpu": c.numCpu,
                "gast_os": c.guestFullName, "ip": g.ipAddress if g else None,
                "vmware_tools": g.toolsRunningStatus if g else None,
                "cpu_last_mhz": q.overallCpuUsage, "ram_aktiv_mb": q.guestMemoryUsage,
                "uptime_stunden": round(q.uptimeSeconds / 3600, 1) if q.uptimeSeconds else None,
                "speicher_belegt_gb": _gb(s.storage.committed) if s.storage else None,
                "status": vm.overallStatus,
            })
        return sorted(out, key=lambda v: v["name"].lower())


def _snapshot_tree(nodes, depth: int = 0) -> list[dict]:
    out = []
    for n in nodes or []:
        out.append({"name": n.name, "beschreibung": n.description, "erstellt": _ts(n.createTime),
                    "zustand": n.state, "ebene": depth, "moid": n.snapshot._moId})
        out.extend(_snapshot_tree(n.childSnapshotList, depth + 1))
    return out


def vm_info(vm: str) -> dict:
    """Details einer VM: Hardware, Hot-Add, Disks, Netzwerkkarten, Snapshots, Auslastung."""
    from pyVmomi import vim
    with esxi() as si:
        v = _find_vm(si, vm)
        cfg, rt, q = v.config, v.runtime, v.summary.quickStats
        disks, nics = [], []
        for dev in cfg.hardware.device if cfg else []:
            if isinstance(dev, vim.vm.device.VirtualDisk):
                backing = dev.backing
                disks.append({
                    "label": dev.deviceInfo.label, "groesse_gb": round(dev.capacityInKB / 1024 ** 2, 1),
                    "datei": getattr(backing, "fileName", None),
                    "thin": getattr(backing, "thinProvisioned", None),
                })
            elif isinstance(dev, vim.vm.device.VirtualEthernetCard):
                nics.append({
                    "label": dev.deviceInfo.label, "netzwerk": dev.deviceInfo.summary, "mac": dev.macAddress,
                    "verbunden": dev.connectable.connected if dev.connectable else None, "typ": type(dev).__name__.split(".")[-1],
                })
        guest = v.guest
        return {
            "name": v.name, "moid": v._moId, "power": rt.powerState,
            "gast_os": cfg.guestFullName if cfg else None, "hardware_version": cfg.version if cfg else None,
            "ram_mb": cfg.hardware.memoryMB if cfg else None, "vcpu": cfg.hardware.numCPU if cfg else None,
            "kerne_pro_sockel": cfg.hardware.numCoresPerSocket if cfg else None,
            "ram_hot_add": cfg.memoryHotAddEnabled if cfg else None,
            "cpu_hot_add": cfg.cpuHotAddEnabled if cfg else None,
            "ram_reservation_mb": cfg.memoryAllocation.reservation if cfg and cfg.memoryAllocation else None,
            "disks": disks, "netzwerkkarten": nics,
            "ips": [ip for n in (guest.net or []) for ip in (n.ipAddress or [])] if guest else [],
            "hostname_gast": guest.hostName if guest else None,
            "vmware_tools": {"status": guest.toolsRunningStatus, "version": guest.toolsVersionStatus2} if guest else None,
            "auslastung": {"cpu_mhz": q.overallCpuUsage, "ram_aktiv_mb": q.guestMemoryUsage,
                           "ram_host_mb": q.hostMemoryUsage, "ballooned_mb": q.balloonedMemory,
                           "swapped_mb": q.swappedMemory},
            "snapshots": _snapshot_tree(v.snapshot.rootSnapshotList) if v.snapshot else [],
            "vmx_pfad": cfg.files.vmPathName if cfg else None,
            "notiz": cfg.annotation if cfg else None,
            "geschuetzt": v.name.lower() in _protected() or v._moId.lower() in _protected(),
        }


def snapshot_list(vm: str) -> list[dict]:
    """Snapshots einer VM (Baum flach, mit Ebene)."""
    with esxi() as si:
        v = _find_vm(si, vm)
        return _snapshot_tree(v.snapshot.rootSnapshotList) if v.snapshot else []


def task_list(limit: int = 20) -> list[dict]:
    """Zuletzt gelaufene Tasks auf dem Host (z.B. Power-Aktionen, Snapshots, Rekonfigurationen)."""
    with esxi() as si:
        out = []
        for t in si.RetrieveContent().taskManager.recentTask:
            i = t.info
            out.append({
                "task": i.key, "aktion": i.descriptionId, "objekt": i.entityName, "zustand": i.state,
                "fortschritt": i.progress, "gestartet": _ts(i.startTime), "beendet": _ts(i.completeTime),
                "user": i.reason.userName if hasattr(i.reason, "userName") else None,
                "fehler": getattr(i.error, "msg", None) if i.error else None,
            })
        out.sort(key=lambda x: x["gestartet"] or "", reverse=True)
        return out[:limit]


_SESSION_EVENTS = ("UserLoginSessionEvent", "UserLogoutSessionEvent", "BadUsernameSessionEvent")


def event_list(limit: int = 50, vm: str = None, since_hours: int = None, include_logins: bool = False) -> list[dict]:
    """Ereignisse des Hosts (neueste zuerst), optional nur einer VM oder der letzten N Stunden.
    An-/Abmeldungen (auch die des MCP selbst) sind standardmässig ausgeblendet."""
    from pyVmomi import vim
    from datetime import timedelta
    with esxi() as si:
        limit = max(1, min(int(limit), 1000))
        spec = vim.event.EventFilterSpec()
        if vm:
            spec.entity = vim.event.EventFilterSpec.ByEntity(entity=_find_vm(si, vm), recursion="self")
        if since_hours:
            spec.time = vim.event.EventFilterSpec.ByTime(beginTime=datetime.now(timezone.utc) - timedelta(hours=int(since_hours)))
        # Ein einzelner ESXi-Host kennt QueryEvents nicht (NotImplemented), nur den History-Collector.
        collector = si.RetrieveContent().eventManager.CreateCollectorForEvents(spec)
        try:
            collector.SetCollectorPageSize(limit if include_logins else 1000)
            events = list(collector.latestPage or [])
        finally:
            try:
                collector.DestroyCollector()
            except Exception:
                pass
        out = [{"zeit": _ts(e.createdTime), "typ": type(e).__name__.split(".")[-1],
                "meldung": e.fullFormattedMessage, "user": e.userName or None,
                "vm": e.vm.name if getattr(e, "vm", None) else None} for e in events
               if include_logins or type(e).__name__.split(".")[-1] not in _SESSION_EVENTS]
        out.sort(key=lambda x: x["zeit"] or "", reverse=True)
        return out[:limit]


# ---------------------------------------------------------------------------
# Schreibende Tools (alle mit confirm, Vorschau ohne confirm)
# ---------------------------------------------------------------------------

_POWER_ACTIONS = {
    "on": "PowerOnVM_Task",
    "off": "PowerOffVM_Task",          # hart ausschalten
    "reset": "ResetVM_Task",           # harter Reset
    "shutdown": "ShutdownGuest",       # sauber herunterfahren (VMware Tools nötig)
    "reboot": "RebootGuest",           # sauber neu starten (VMware Tools nötig)
}


def vm_power(vm: str, action: str, confirm: bool = False) -> dict:
    """Power-Aktion auf einer VM. Ohne confirm=true nur Vorschau."""
    if action not in _POWER_ACTIONS:
        raise RuntimeError(f"Unbekannte Aktion '{action}', erlaubt: {', '.join(_POWER_ACTIONS)}")
    _require_write()
    with esxi() as si:
        v = _find_vm(si, vm)
        _check_not_protected(v)
        state = v.runtime.powerState
        preview = {"vm": v.name, "moid": v._moId, "aktueller_zustand": state, "aktion": action}
        if action == "on" and state == "poweredOn":
            return {**preview, "hinweis": "VM läuft bereits, nichts zu tun"}
        if action in ("off", "reset", "shutdown", "reboot") and state != "poweredOn":
            return {**preview, "hinweis": f"VM ist nicht eingeschaltet ({state}), Aktion nicht möglich"}
        if action in ("shutdown", "reboot") and v.guest.toolsRunningStatus != "guestToolsRunning":
            return {**preview, "hinweis": "VMware Tools laufen nicht - sauberes Herunterfahren/Neustarten nicht möglich, nur off/reset"}
        if not confirm:
            return {**preview, "vorschau": True,
                    "hinweis": "Zum Ausführen mit confirm=true erneut aufrufen (nur nach ausdrücklicher Bestätigung)"}
        result = getattr(v, _POWER_ACTIONS[action])()
        if action in ("shutdown", "reboot"):
            return {**preview, "status": "an Gast gesendet", "hinweis": "Gast fährt selbst herunter, Zustand mit vm_info prüfen"}
        return {**preview, **_wait(result)}


def _create_snapshot(v, name: str, description: str, memory: bool = False, quiesce: bool = False) -> dict:
    task = v.CreateSnapshot_Task(name=name, description=description, memory=memory, quiesce=quiesce)
    return {"snapshot": name, **_wait(task, timeout=900)}


def vm_snapshot_create(vm: str, name: str, description: str = "", memory: bool = False,
                       quiesce: bool = False, confirm: bool = False) -> dict:
    """Snapshot einer VM erstellen. Ohne confirm=true nur Vorschau."""
    _require_write()
    with esxi() as si:
        v = _find_vm(si, vm)
        _check_not_protected(v)
        preview = {"vm": v.name, "moid": v._moId, "snapshot_name": name, "mit_ram": memory, "quiesce": quiesce,
                   "bestehende_snapshots": len(_snapshot_tree(v.snapshot.rootSnapshotList)) if v.snapshot else 0}
        if not confirm:
            return {**preview, "vorschau": True,
                    "hinweis": "Zum Ausführen mit confirm=true erneut aufrufen (nur nach ausdrücklicher Bestätigung)"}
        return {**preview, **_create_snapshot(v, name, description or "Erstellt über esxi-mcp", memory, quiesce)}


def vm_set_resources(vm: str, memory_mb: int = None, vcpu: int = None, snapshot: bool = True,
                     confirm: bool = False) -> dict:
    """RAM und/oder vCPU einer VM ändern. Bei laufender VM nur Erhöhen und nur mit aktivem Hot-Add,
    sonst muss die VM ausgeschaltet sein. Vorher standardmässig Snapshot. Ohne confirm=true nur Vorschau."""
    from pyVmomi import vim
    if memory_mb is None and vcpu is None:
        raise RuntimeError("memory_mb und/oder vcpu angeben")
    _require_write()
    max_mem, max_cpu = _int_env("MCP_MAX_MEMORY_MB"), _int_env("MCP_MAX_CPU")
    if memory_mb is not None:
        memory_mb = int(memory_mb)
        if memory_mb < 256 or memory_mb % 4:
            raise RuntimeError("memory_mb muss mindestens 256 und durch 4 teilbar sein (z.B. 16384 für 16 GB)")
        if max_mem and memory_mb > max_mem:
            raise RuntimeError(f"memory_mb {memory_mb} über dem Limit MCP_MAX_MEMORY_MB={max_mem}")
    if vcpu is not None:
        vcpu = int(vcpu)
        if vcpu < 1:
            raise RuntimeError("vcpu muss mindestens 1 sein")
        if max_cpu and vcpu > max_cpu:
            raise RuntimeError(f"vcpu {vcpu} über dem Limit MCP_MAX_CPU={max_cpu}")

    with esxi() as si:
        v = _find_vm(si, vm)
        _check_not_protected(v)
        hw, cfg = v.config.hardware, v.config
        running = v.runtime.powerState == "poweredOn"
        problems = []
        if memory_mb is not None and memory_mb != hw.memoryMB and running:
            if memory_mb < hw.memoryMB:
                problems.append("RAM verkleinern geht nur bei ausgeschalteter VM")
            elif not cfg.memoryHotAddEnabled:
                problems.append("RAM-Hot-Add ist bei dieser VM nicht aktiv - RAM erhöhen nur bei ausgeschalteter VM")
        if vcpu is not None and vcpu != hw.numCPU and running:
            if vcpu < hw.numCPU:
                problems.append("vCPU verkleinern geht nur bei ausgeschalteter VM")
            elif not cfg.cpuHotAddEnabled:
                problems.append("CPU-Hot-Add ist bei dieser VM nicht aktiv - vCPU erhöhen nur bei ausgeschalteter VM")
            elif vcpu % (hw.numCoresPerSocket or 1):
                problems.append(f"vCPU muss bei laufender VM ein Vielfaches von {hw.numCoresPerSocket} (Kerne pro Sockel) sein")
        if memory_mb is not None:
            host_mem_mb = _host(si).summary.hardware.memorySize // 1024 ** 2
            if memory_mb > host_mem_mb:
                problems.append(f"Mehr RAM als der Host hat ({host_mem_mb} MB)")

        preview = {
            "vm": v.name, "moid": v._moId, "power": v.runtime.powerState,
            "ram_mb": {"jetzt": hw.memoryMB, "neu": memory_mb if memory_mb is not None else hw.memoryMB},
            "vcpu": {"jetzt": hw.numCPU, "neu": vcpu if vcpu is not None else hw.numCPU},
            "ram_hot_add": cfg.memoryHotAddEnabled, "cpu_hot_add": cfg.cpuHotAddEnabled,
            "snapshot_vorher": snapshot,
        }
        if (memory_mb is None or memory_mb == hw.memoryMB) and (vcpu is None or vcpu == hw.numCPU):
            return {**preview, "hinweis": "Keine Änderung nötig, Werte sind bereits so gesetzt"}
        if problems:
            return {**preview, "nicht_moeglich": problems,
                    "hinweis": "VM zuerst herunterfahren (vm_power action=shutdown), dann erneut aufrufen"}
        if not confirm:
            return {**preview, "vorschau": True,
                    "hinweis": "Zum Ausführen mit confirm=true erneut aufrufen (nur nach ausdrücklicher Bestätigung)"}

        result = dict(preview)
        if snapshot:
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            result["snapshot"] = _create_snapshot(v, f"vor Ressourcen-Änderung {stamp}",
                                                  f"Automatisch durch esxi-mcp vor Änderung RAM {hw.memoryMB}->{preview['ram_mb']['neu']} MB, "
                                                  f"vCPU {hw.numCPU}->{preview['vcpu']['neu']}")
        spec = vim.vm.ConfigSpec()
        if memory_mb is not None:
            spec.memoryMB = memory_mb
        if vcpu is not None:
            spec.numCPUs = vcpu
        result["rekonfiguration"] = _wait(v.ReconfigVM_Task(spec=spec))
        v.Reload()
        result["ergebnis"] = {"ram_mb": v.config.hardware.memoryMB, "vcpu": v.config.hardware.numCPU}
        return result


TOOL_FUNCS = {
    "get_version": get_version,
    "host_info": host_info,
    "datastore_list": datastore_list,
    "vm_list": vm_list,
    "vm_info": vm_info,
    "snapshot_list": snapshot_list,
    "task_list": task_list,
    "event_list": event_list,
    "vm_power": vm_power,
    "vm_snapshot_create": vm_snapshot_create,
    "vm_set_resources": vm_set_resources,
}

WRITE_TOOLS = {"vm_power", "vm_snapshot_create", "vm_set_resources"}


# ---------------------------------------------------------------------------
# MCP-Server
# ---------------------------------------------------------------------------

server = Server("esxi-mcp")

_VM = {"type": "string", "description": "VM-Name (exakt, Gross/Klein egal) oder MoID aus vm_list"}
_CONFIRM = {"type": "boolean", "default": False,
            "description": "Erst nach ausdrücklicher Bestätigung durch den Benutzer auf true setzen, sonst nur Vorschau"}


@server.list_tools()
async def list_tools():
    return [
        Tool(name="get_version", description="Version des laufenden ESXi-MCP-Servers abfragen (Redeploy-Kontrolle).",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="host_info",
             description="ESXi-Host: Version, Hardware, CPU- und RAM-Auslastung, Uptime, Wartungsmodus (auch als Verbindungstest).",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="datastore_list", description="Datastores des Hosts mit Kapazität, freiem Platz, Belegung und Anzahl VMs.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="vm_list",
             description="Alle VMs mit Power-Status, RAM, vCPU, Gast-OS, IP, VMware-Tools-Status und aktueller Auslastung.",
             inputSchema={"type": "object", "properties": {
                 "power_state": {"type": "string", "enum": ["poweredOn", "poweredOff", "suspended"]},
                 "name_filter": {"type": "string", "description": "Teil des VM-Namens"},
             }}),
        Tool(name="vm_info",
             description="Details einer VM: Hardware, Hot-Add-Status, Disks, Netzwerkkarten, IPs, Snapshots, Auslastung (inkl. Ballooning/Swap).",
             inputSchema={"type": "object", "properties": {"vm": _VM}, "required": ["vm"]}),
        Tool(name="snapshot_list", description="Snapshots einer VM mit Erstellzeit und Ebene im Snapshot-Baum.",
             inputSchema={"type": "object", "properties": {"vm": _VM}, "required": ["vm"]}),
        Tool(name="task_list", description="Zuletzt gelaufene Tasks auf dem Host (Power-Aktionen, Snapshots, Rekonfigurationen) mit Status und Fehler.",
             inputSchema={"type": "object", "properties": {"limit": {"type": "integer", "default": 20}}}),
        Tool(name="event_list", description="Ereignisse des Hosts (neueste zuerst), optional nur einer VM und/oder der letzten N Stunden.",
             inputSchema={"type": "object", "properties": {
                 "limit": {"type": "integer", "default": 50},
                 "vm": _VM,
                 "since_hours": {"type": "integer"},
                 "include_logins": {"type": "boolean", "default": False, "description": "Auch An-/Abmeldungen und Fehllogins anzeigen"},
             }}),
        Tool(name="vm_power",
             description="SCHREIBEND: Power-Aktion auf einer VM. action: on, off (hart), reset (hart), shutdown (sauber, braucht VMware Tools), reboot (sauber, braucht VMware Tools). Ohne confirm=true nur Vorschau.",
             inputSchema={"type": "object", "properties": {
                 "vm": _VM, "action": {"type": "string", "enum": list(_POWER_ACTIONS)}, "confirm": _CONFIRM,
             }, "required": ["vm", "action"]}),
        Tool(name="vm_snapshot_create",
             description="SCHREIBEND: Snapshot einer VM erstellen (memory=true sichert auch den RAM-Zustand, quiesce=true friert das Dateisystem ein). Ohne confirm=true nur Vorschau.",
             inputSchema={"type": "object", "properties": {
                 "vm": _VM, "name": {"type": "string"}, "description": {"type": "string"},
                 "memory": {"type": "boolean", "default": False}, "quiesce": {"type": "boolean", "default": False},
                 "confirm": _CONFIRM,
             }, "required": ["vm", "name"]}),
        Tool(name="vm_set_resources",
             description="SCHREIBEND: RAM (memory_mb, z.B. 16384 = 16 GB) und/oder vCPU einer VM ändern. Bei laufender VM nur Erhöhen mit aktivem Hot-Add, sonst VM vorher herunterfahren. Legt vorher standardmässig einen Snapshot an. Ohne confirm=true nur Vorschau.",
             inputSchema={"type": "object", "properties": {
                 "vm": _VM, "memory_mb": {"type": "integer"}, "vcpu": {"type": "integer"},
                 "snapshot": {"type": "boolean", "default": True}, "confirm": _CONFIRM,
             }, "required": ["vm"]}),
    ]


def _format_error(e: Exception) -> str:
    msg = getattr(e, "msg", None)
    if msg:  # pyVmomi-Faults (z.B. vim.fault.NoPermission, InvalidLogin)
        name = type(e).__name__.split(".")[-1]
        extra = ""
        if name == "NoPermission":
            extra = f" (fehlendes Recht: {getattr(e, 'privilegeId', '?')})"
        return f"ESXi Fehler {name}: {msg}{extra}"
    return f"Fehler: {e}"


@server.call_tool()
async def call_tool(name, arguments):
    func = TOOL_FUNCS.get(name)
    if not func:
        return [TextContent(type="text", text=f"Unbekanntes Tool: {name}")]
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: func(**(arguments or {})))
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2, default=str)
        return [TextContent(type="text", text=text)]
    except Exception as e:
        return [TextContent(type="text", text=_format_error(e))]


# ---------------------------------------------------------------------------
# Cloud/HTTP-Betrieb (identisch zu pbs-mcp): statisches Bearer-Token schützt /mcp
# ---------------------------------------------------------------------------

MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")


class _StreamableHTTPASGIApp:
    def __init__(self, session_manager):
        self.session_manager = session_manager

    async def __call__(self, scope, receive, send):
        await self.session_manager.handle_request(scope, receive, send)


class BearerAuthMiddleware:
    """Prüft 'Authorization: Bearer <token>' (zeitkonstanter Vergleich)."""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        import hmac
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"").decode("latin-1")
        if not self.token or not hmac.compare_digest(auth_header.encode(), f"Bearer {self.token}".encode()):
            from starlette.responses import JSONResponse
            await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def with_healthz(app):
    """GET /healthz ohne Auth für den Docker-Healthcheck. Liefert nur {"status": "ok"},
    prüft nur den Prozess, nicht den ESXi-Host."""
    from starlette.responses import JSONResponse

    async def router(scope, receive, send):
        if scope["type"] == "http" and scope["path"] == "/healthz" and scope["method"] in ("GET", "HEAD"):
            await JSONResponse({"status": "ok"})(scope, receive, send)
            return
        await app(scope, receive, send)

    return router


def build_http_app(token: str):
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.middleware.cors import CORSMiddleware
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from mcp.server.transport_security import TransportSecuritySettings

    session_manager = StreamableHTTPSessionManager(
        app=server,
        json_response=True,
        security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    app = Starlette(
        routes=[Route("/mcp", endpoint=_StreamableHTTPASGIApp(session_manager))],
        lifespan=lambda app: session_manager.run(),
    )
    return CORSMiddleware(
        with_healthz(BearerAuthMiddleware(app, token)),
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["mcp-session-id"],
    )


async def run_http_server():
    import uvicorn

    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8000"))
    srv = uvicorn.Server(uvicorn.Config(build_http_app(MCP_AUTH_TOKEN), host=host, port=port, log_level="info"))
    print(f"ESXi MCP HTTP server {__version__} running on {host}:{port}", flush=True)
    await srv.serve()


def main():
    if len(MCP_AUTH_TOKEN) < 24:
        raise RuntimeError(
            "MCP_AUTH_TOKEN fehlt oder ist zu kurz (mind. 24 Zeichen) - aus Sicherheitsgründen kein Start."
        )
    asyncio.run(run_http_server())


if __name__ == "__main__":
    main()
