# ESXi MCP Server

MCP-Server für einen einzelnen VMware-ESXi-Host (ohne vCenter), über die vSphere Web Services API (pyVmomi). Host, Datastores, VMs, Snapshots, Tasks und Ereignisse lesen, dazu Power-Aktionen, Snapshots erstellen und RAM/vCPU ändern. Gleicher Aufbau wie pbs-mcp: läuft ausschliesslich als Docker-Container (Streamable HTTP) hinter einem Reverse-Proxy, Schutz per statischem Bearer-Token (`MCP_AUTH_TOKEN`), Image per GitHub Actions nach ghcr.io.

Schreibende Aktionen sind mehrfach gesichert:

- jedes schreibende Tool braucht `confirm=true`, ohne kommt nur eine Vorschau
- `MCP_READONLY=true` sperrt alle schreibenden Tools
- VMs in `MCP_PROTECTED_VMS` sind für schreibende Tools gesperrt
- `MCP_MAX_MEMORY_MB` / `MCP_MAX_CPU` begrenzen RAM/vCPU-Änderungen
- vor RAM/vCPU-Änderungen wird standardmässig ein Snapshot erstellt
- Löschen (VMs, Snapshots, Disks) und Snapshots zurücksetzen sind bewusst nicht implementiert
- zusätzlich begrenzt die ESXi-Rolle des Users, was überhaupt geht

Hinweis: Mit der kostenlosen ESXi-Lizenz ist die API nur lesend, schreibende Tools brauchen eine bezahlte Lizenz.

## Tools

Lesend:

| Tool | Beschreibung |
|------|--------------|
| `get_version` | Version des laufenden MCP-Servers (Redeploy-Kontrolle) |
| `host_info` | Version, Hardware, CPU-/RAM-Auslastung, Uptime, Wartungsmodus (auch Verbindungstest) |
| `datastore_list` | Datastores mit Kapazität, freiem Platz und Anzahl VMs |
| `vm_list` | Alle VMs mit Power-Status, RAM, vCPU, Gast-OS, IP, VMware Tools, Auslastung |
| `vm_info` | Details einer VM inkl. Hot-Add, Disks, Belegung der Dateisysteme im Gast, Netzwerkkarten, IPs, Snapshots, Ballooning/Swap |
| `snapshot_list` | Snapshots einer VM |
| `task_list` | Zuletzt gelaufene Tasks mit Status und Fehler |
| `event_list` | Ereignisse des Hosts, optional pro VM oder Zeitraum (An-/Abmeldungen standardmässig ausgeblendet) |
| `guest_os_list` | Gast-OS-Typen, die der Host für die Hardware-Version einer VM anbietet |

Schreibend (alle mit Vorschau ohne `confirm`):

| Tool | Beschreibung |
|------|--------------|
| `vm_power` | `on`, `off` (hart), `reset` (hart), `shutdown` / `reboot` (sauber, braucht VMware Tools) |
| `vm_snapshot_create` | Snapshot erstellen, optional mit RAM-Zustand oder Quiesce |
| `vm_set_resources` | RAM (`memory_mb`) und/oder vCPU ändern. Bei laufender VM nur Erhöhen mit aktivem Hot-Add, sonst vorher herunterfahren |
| `vm_set_guest_os` | Gast-OS-Typ ändern (nur bei ausgeschalteter VM), mögliche Werte über `guest_os_list` |

VMs werden per exaktem Namen (Gross/Klein egal) oder per MoID aus `vm_list` angegeben.

## Umgebungsvariablen

| Variable | Pflicht | Standard | Beschreibung |
|---|---|---|---|
| `ESXI_HOST` | ja | - | z.B. `esxi.example.com` |
| `ESXI_PORT` | nein | `443` | |
| `ESXI_USER` | ja | - | eigener User, nie root |
| `ESXI_PASSWORD` | ja | - | |
| `ESXI_VERIFY_SSL` | nein | `true` | `true`, `false` oder Pfad zu einer CA-Datei (ESXi hat oft ein selbstsigniertes Zertifikat) |
| `ESXI_HTTP_PROXY` | nein | - | `host:port`, nur falls der Host nur über einen HTTP-Proxy erreichbar ist |
| `MCP_AUTH_TOKEN` | ja | - | Statisches Bearer-Token (mind. 24 Zeichen), ohne startet der Server nicht |
| `MCP_READONLY` | nein | `false` | `true` sperrt alle schreibenden Tools |
| `MCP_PROTECTED_VMS` | nein | - | kommagetrennte VM-Namen oder MoIDs, die nicht verändert werden dürfen |
| `MCP_MAX_MEMORY_MB` / `MCP_MAX_CPU` | nein | - | Obergrenzen für `vm_set_resources` |
| `MCP_HOST` / `MCP_PORT` | nein | `0.0.0.0` / `8000` | Bind im Container |
| `MCP_BIND_ADDR` / `MCP_HOST_PORT` | nein | `0.0.0.0` / `8431` | Port-Mapping auf dem Docker-Host |

## 1. User und Rolle auf dem ESXi anlegen

User (z.B. `mcp`) im Host Client unter Manage > Security & users > Users anlegen. Dann per SSH als root eine eigene Rolle anlegen und zuweisen (`esxcli system permission set` kennt nur die eingebauten Rollen, darum `vim-cmd`):

```bash
vim-cmd vimsvc/auth/role_add MCP VirtualMachine.Config.Memory VirtualMachine.Config.CPUCount VirtualMachine.Interact.PowerOn VirtualMachine.Interact.PowerOff VirtualMachine.Interact.Reset VirtualMachine.State.CreateSnapshot VirtualMachine.Config.Settings
vim-cmd vimsvc/auth/entity_permission_add vim.Folder:ha-folder-root mcp false MCP true
esxcli system permission list
```

`System.View` und `System.Read` hat jede Rolle automatisch. Für reinen Lesebetrieb reicht die eingebaute Rolle `ReadOnly` (`esxcli system permission set --id mcp --role ReadOnly`). Fehlt ein Recht, meldet das Tool `NoPermission` mit dem Namen des fehlenden Rechts.

## 2. Cloud-Betrieb (Portainer)

Bei jedem Push auf `main` baut GitHub Actions das Image (`.github/workflows/docker-publish.yml`). Einmalig: Package-Sichtbarkeit auf GitHub auf **Public** stellen oder Portainer Zugang zur Registry geben.

1. Portainer > Stacks > Add stack > Repository, Branch `main`, Compose-Pfad `docker-compose.yml`
2. Environment variables setzen: `ESXI_HOST`, `ESXI_USER`, `ESXI_PASSWORD`, `ESXI_VERIFY_SSL`, `MCP_AUTH_TOKEN`, optional `MCP_PROTECTED_VMS` usw.
3. Deploy. Updates später per "Pull and redeploy".

Reverse-Proxy mit TLS auf `<Host-IP>:8431`, Endpoint-Pfad `/mcp`. `GET /healthz` ist ohne Token erreichbar und liefert nur `{"status": "ok"}` (Docker-Healthcheck, keine Abfrage des ESXi); alle anderen Pfade brauchen das Bearer-Token. In Claude als Remote-MCP mit `https://<subdomain>/mcp` und dem `MCP_AUTH_TOKEN` als Bearer-Token einbinden.

Der Container läuft als unprivilegierter User. Jeder Tool-Aufruf meldet sich am ESXi an und danach wieder ab, es bleiben keine Sessions offen.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

Die Unit-Tests prüfen Bearer-Auth, `/healthz` und alle Schutzschalter mit Fake-Objekten, ohne ESXi.
