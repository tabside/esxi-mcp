# Changelog

## 0.1

- Erste Version: Host, Datastores, VMs, Snapshots, Tasks und Ereignisse lesen; Power-Aktionen, Snapshot erstellen und RAM/vCPU ändern mit `confirm`, Vorschau, Schutzliste, Limits und automatischem Snapshot vor Ressourcen-Änderungen.
- Ereignisse über den History-Collector, weil ein einzelner ESXi-Host `QueryEvents` nicht unterstützt.
- `vm_info` zeigt die Belegung der Dateisysteme im Gast (über VMware Tools), Bind-Mounts derselben Partition (z.B. CageFS) werden zusammengefasst.
- Neue Tools `guest_os_list` und `vm_set_guest_os` (Gast-OS-Typ ändern, nur bei ausgeschalteter VM, braucht `VirtualMachine.Config.Settings`).
- Neues Tool `vm_snapshot_remove`: einzelnen Snapshot löschen, nur mit `MCP_ALLOW_DELETE=true` und `confirm`, braucht `VirtualMachine.State.RemoveSnapshot`.
