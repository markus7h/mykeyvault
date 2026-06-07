# mykeyvault

Self-hosted Passwort- / Secrets-Vault auf Basis von **Vaultwarden** (Bitwarden-kompatibler Server). Läuft als Docker-Container und ist im LAN via Caddy-Reverse-Proxy unter einer eigenen Domain (z. B. `https://mykeyvault.lan`) erreichbar.

Dazu zwei eigene Komponenten, damit Automatisierung (Scripts, Claude/MCP) Secrets nutzen kann, **ohne dass das Secret jemals in einen Prompt-/Chat-Kontext gelangt**:

- **`vault-api`** — schlanke FastAPI vor der `bw`-CLI (REST, Token-geschützt).
- **`mcp`** — MCP-Server (stdio **oder** StreamableHTTP), der die vault-api als Tools für Claude bereitstellt.

> Hostnamen, IPs und Pfade in dieser README sind **Platzhalter** (`<your-server>`, `https://mykeyvault.lan`, …). An die eigene Umgebung anpassen bzw. per Env-Variablen überschreiben.

## Repo-Layout

| Pfad | Zweck |
|---|---|
| `docker-compose.yml` | Compose für `mykeyvault` (Vaultwarden) + `vault-api` |
| `setup.sh` | One-Shot-Deploy-Script: legt Verzeichnisse an, kopiert Compose + vault-api-Quelle per scp, erzeugt `.env`, baut & startet die Container (Zielhost via `DEPLOY_HOST`) |
| `.env.example` | Template für `.env` (`VAULT_API_TOKEN`, `BW_CLIENTID/SECRET`, `BW_PASSWORD`) |
| `vault-api/` | FastAPI-Service: `main.py`, `Dockerfile`, `requirements.txt` |
| `mcp/` | MCP-Server (TypeScript): `src/index.ts` → `dist/index.js` |
| `requirements.md` | Anforderungs-Notizen |
| `.gitignore` | schließt `.env`, `node_modules/`, `dist/` aus |
| `mykeyvault.code-workspace` | VSCode-Workspace |

## Überblick

| Eigenschaft | Wert |
|---|---|
| Container | `mykeyvault` |
| Image | `vaultwarden/server:latest` |
| Port (Host → Container) | `8222 → 80` |
| Docker-Network | `mykeyvault-net` |
| Restart-Policy | `unless-stopped` |
| Compose-Deployment | `$REMOTE_COMPOSE_DIR` (Default `/var/local/mydocker/compose-files/mykeyvault/`) |
| Daten-Volume | `$REMOTE_DATA_DIR/data` → `/data` |
| LAN-URL | `https://mykeyvault.lan` (Caddy, `tls internal`) |
| Direkt-URL | `http://<your-server>:8222` |
| Signups | deaktiviert (`SIGNUPS_ALLOWED=false`) |
| Admin-Panel | `https://mykeyvault.lan/admin` |
| vault-api Container | `vault-api` |
| vault-api Port | `8223 → 8000` (`http://<your-server>:8223`) |

## Architektur

```
Bitwarden-Client (Desktop/Browser/Mobile)        Claude / Scripts
   │  https://mykeyvault.lan                          │  stdio
   ▼                                                  ▼
Caddy (Reverse-Proxy)  ── reverse_proxy ──►      mcp (TypeScript)
   │                  <server>:8222                   │  HTTP + Bearer-Token
   │                                                  ▼
   │                                          vault-api (FastAPI, :8223)
   │                                                  │  ruft bw-CLI auf
   ▼                                                  ▼
Vaultwarden Container "mykeyvault"  (intern Port 80) ◄┘
   │
   ▼
Bind-Mount $REMOTE_DATA_DIR/data
   ├── db.sqlite3        (Vault-DB)
   ├── attachments/
   ├── sends/
   ├── rsa_key.*         (JWT-Signing)
   └── config.json
```

> Beide Container hängen im Docker-Network `mykeyvault-net`. `vault-api` erreicht Vaultwarden über Caddy (`VAULT_URL=https://mykeyvault.lan`, via `extra_hosts: host-gateway`) und vertraut dessen internem Root-CA über `NODE_EXTRA_CA_CERTS`.

## Deploy

Im Repo-Verzeichnis (auf einem Host mit SSH-Zugang zum Zielserver). Zielhost und Pfade sind über Env-Variablen konfigurierbar:

```bash
DEPLOY_HOST=your-server bash setup.sh
```

Das Script:

1. legt das Compose- und Daten-Verzeichnis (`$REMOTE_COMPOSE_DIR`, `$REMOTE_DATA_DIR`) auf dem Zielhost an,
2. kopiert `docker-compose.yml` sowie die vault-api-Quelle (`main.py`, `Dockerfile`, `requirements.txt`) per scp ins Compose-Verzeichnis,
3. erzeugt — falls noch nicht vorhanden — eine `.env` (`chmod 600`) mit frisch generiertem `VAULT_API_TOKEN` und leeren `BW_*`-Platzhaltern und gibt den Token einmalig aus,
4. baut und startet beide Container via `docker compose up -d --build`.

**Wichtig**:

- Bei Erst-Deploy `BW_CLIENTID` / `BW_CLIENTSECRET` (aus den Vaultwarden-Account-Einstellungen) und `BW_PASSWORD` (Master-Passwort) in die `.env` auf dem Zielhost eintragen — sonst kann `vault-api` den Vault nicht entsperren.
- Den ausgegebenen `VAULT_API_TOKEN` auf dem Claude-Host in `~/.claude.json` unter `mcpServers.mykeyvault.env.VAULT_API_TOKEN` setzen (zusammen mit `VAULT_API_URL=http://<your-server>:8223`).
- Re-Deploy nach Code-Änderung an `vault-api`/Compose: einfach erneut `bash setup.sh` (idempotent; vorhandene `.env` bleibt unangetastet).

## Caddy-Reverse-Proxy

Beispiel-`Caddyfile` (nicht Teil dieses Repos):

```
mykeyvault.lan {
    reverse_proxy <server>:8222 {
        header_up Host {host}
    }
    tls internal
}
```

- `tls internal` → Caddy generiert eigenes Root-CA. Client-Geräte müssen das Caddy-Root-Cert importieren.
- Nach Caddyfile-Änderung den Caddy-Container neu laden.

## Account-Anlage

Signups sind aus → Erstanmeldung läuft über das Admin-Panel:

1. `https://mykeyvault.lan/admin` öffnen → Admin-Token eingeben.
2. *Invite User* → eigene E-Mail eintragen.
3. Im Bitwarden-Client (Desktop/Browser-Extension/Mobile):
   - Settings → Server-URL: `https://mykeyvault.lan`
   - Registrierungslink aus dem Invite folgen, Master-Passwort vergeben.

## Client-CLI (`bw`)

```bash
bw config server https://mykeyvault.lan
bw login
export BW_SESSION=$(bw unlock --raw)
```

## vault-api (REST)

FastAPI-Wrapper um die `bw`-CLI, lauscht auf `http://<your-server>:8223`. Jeder Aufruf außer `/health` braucht den Header `Authorization: Bearer $VAULT_API_TOKEN`. Login/Unlock passieren intern via `BW_CLIENTID/SECRET` + `BW_PASSWORD`; die Session wird im Prozess gehalten.

| Methode & Pfad | Zweck |
|---|---|
| `GET /health` | Liveness (ohne Token) |
| `GET /items` | alle Einträge (Name + Username; **kein** Secret) |
| `GET /item/{name}` | Name + Username eines Eintrags |
| `GET /secret/{name}` | Passwort/Secret byte-genau; bei SSH-Keys (Typ 5) der `privateKey` |
| `POST /items` | Login-Eintrag anlegen (`name`, `username`, `password`, `url?`, `notes?`) |
| `POST /ssh-keys` | **SSH-Key-Eintrag** (Bitwarden-Typ 5) anlegen (`name`, `private_key`, `public_key`, `fingerprint`, `notes?`) |
| `GET /ssh-key/{name}` | Public-Key + Fingerprint eines SSH-Key-Eintrags (**kein** privater Key) |
| `PUT /items/{name}` | `password` / `username` / `notes` aktualisieren |
| `DELETE /items/{name}` | Eintrag löschen |

> **Namensauflösung:** `bw get` matcht Namen per Teilstring. Bei mehrdeutigem Treffer kommt „More than one result" — Item-Namen also eindeutig wählen.

## MCP-Tools

Der `mcp`-Server (`mcp/dist/index.js`, stdio) macht die vault-api für Claude nutzbar. Leitprinzip: **Secrets erscheinen nie im Claude-Kontext** — sie laufen nur durch den MCP-/API-Prozess.

| Tool | Zweck |
|---|---|
| `vault_list_items` | Einträge auflisten (kein Secret) |
| `vault_create_item` | Login-Eintrag anlegen |
| `vault_create_ssh_key` | SSH-Key anlegen — privater Key als **Inhalt** (`private_key`) oder **lokaler Pfad** (`private_key_path`); Public-Key analog (`public_key`/`public_key_path`), Fingerprint wird sonst aus dem Public-Key abgeleitet |
| `vault_get_ssh_public_key` | Public-Key + Fingerprint eines SSH-Key-Eintrags |
| `vault_write_secret` | Secret in eine Datei (`chmod 600`) schreiben, gibt nur den Pfad zurück |
| `vault_run_with_secret` | Secret als Env-Variable in einen Shell-Befehl injizieren |
| `vault_run_with_secret_file` | Secret in eine Temp-Datei (`chmod 600`) schreiben, Befehl ausführen (`{}` / `$SECRET_FILE` = Pfad), Datei danach garantiert wieder löschen — für SSH-Keys/PEM, die einen Dateipfad verlangen |

**Datei vs. Env:** Für Konsumenten, die ein Secret aus einer Env-Variable lesen (Tokens, API-Keys), ist `vault_run_with_secret` vorzuziehen — nichts landet auf der Platte. Eine Datei ist nur nötig, wenn ein Tool zwingend einen Pfad will (SSH `-i`, PEM, kubeconfig); dafür ist `vault_run_with_secret_file` (mit Auto-Cleanup) der saubere Weg. `vault_write_secret` legt die Datei dauerhaft an und räumt **nicht** selbst auf.

> Nach Änderungen am MCP-Code: `cd mcp && npm run build`. Neue Tools werden erst nach Neustart des MCP-Servers (neue Claude-Session) sichtbar.

### Transport: stdio vs. HTTP

Der MCP-Server läuft in zwei Modi (per `MCP_TRANSPORT`, Default `stdio`):

- **`stdio`** (lokal): voller Funktionsumfang inkl. der lokal ausführenden Tools `vault_write_secret`, `vault_run_with_secret`, `vault_run_with_secret_file` (brauchen das Dateisystem des aufrufenden Rechners).
- **`http`** (zentraler Container, StreamableHTTP unter `/mcp`): macht die vault-Tools auf **jedem** Client verfügbar — **ohne** dass der MCP-Code lokal/per Mount vorliegen muss. Es werden nur die reinen vault-api-Tools registriert (`vault_list_items`, `vault_create_item`, `vault_create_ssh_key`, `vault_get_ssh_public_key`); die lokal ausführenden Tools entfallen, da der Server sonst sein **eigenes** Dateisystem sähe. Bearer-Auth via `MCP_AUTH_TOKEN` (fail-closed), `/health` ohne Token.

Env (http): `MCP_TRANSPORT=http`, `PORT` (Default `3458`), `HOST`, `VAULT_API_URL`, `VAULT_API_TOKEN`, `MCP_AUTH_TOKEN`. Hinter Caddy (`tls internal`) als eigene Domain (z. B. `https://keyvault-mcp.lan/mcp`) erreichbar; Client-Registrierung als `{"type":"http","url":…,"headers":{"Authorization":"Bearer …"}}`.

## Secrets

- `VAULT_API_TOKEN` und die `BW_*`-Credentials leben ausschließlich in der `.env` im Compose-Verzeichnis (`chmod 600`, nicht im Repo).
- `VAULTWARDEN_ADMIN_TOKEN` (Admin-Panel) wird separat gesetzt; bei Rotation manuell ersetzen und `docker compose up -d` neu fahren.
- Anwendungs-Secrets (API-Tokens, SSH-Keys, PINs) liegen *im* Vault, nicht in `.env`.

## Backup

Datenbestand: `$REMOTE_DATA_DIR/data` (Bind-Mount, kein Docker-Volume).

Minimal-Backup:

```bash
ssh "$DEPLOY_HOST" 'sudo tar czf ~/mykeyvault-backup-$(date +%Y%m%d).tar.gz \
  -C /var/local/mydocker/mykeyvault data'
```

Für konsistentes SQLite vorher Container stoppen oder mit `sqlite3 .backup` arbeiten.

## Integrationen

- Persönliche Login-Credentials → mykeyvault (Bitwarden-Client).
- Tool-/Script-Secrets & SSH-Keys → vault-api (REST) bzw. mykeyvault-MCP (siehe oben).

## Verwandte Projekte

- [ai-rem](https://github.com/markus7h/ai-rem) — Langzeit-Gedächtnis als Knowledge-Graph-MCP. Hält Kontext/Preferences, **aber keine Secrets** — die liegen hier in mykeyvault.
- [tools-mcp](https://github.com/markus7h/tools-mcp) — Tool-/Script-Registry als MCP. Script-Secrets werden über die mykeyvault-vault-api bezogen, statt sie in Scripts oder Configs abzulegen.

## Troubleshooting

| Symptom | Check |
|---|---|
| `ERR_CERT_AUTHORITY_INVALID` im Browser | Caddy-Root-Cert importieren |
| Container restartet permanent | `docker logs mykeyvault` — meist fehlende/leere `.env` |
| `mykeyvault.lan` löst nicht auf | LAN-DNS / Router prüfen, ggf. `/etc/hosts`-Eintrag |
| Bitwarden-Client „Login attempt failed" | Server-URL exakt `https://mykeyvault.lan` (kein Trailing-Slash, kein `/api`) |

## Lizenz & Upstream

Der **eigene Code** dieses Repos (`vault-api`, `mcp`, `setup.sh`, Compose) steht unter der **MIT-Lizenz** (siehe [`LICENSE`](LICENSE)).

mykeyvault **orchestriert** zwei separate Upstream-Komponenten, ohne deren Quellcode zu verändern, einzubinden oder mitzuliefern — sie werden zur Laufzeit/Build-Zeit vom Nutzer selbst bezogen:

| Komponente | Rolle | Lizenz | Einbindung |
|---|---|---|---|
| [Vaultwarden](https://github.com/dani-garcia/vaultwarden) | Vault-Server | AGPL-3.0 | unverändertes Docker-Image (`vaultwarden/server:latest`), eigener Container, Kommunikation über HTTP |
| [Bitwarden CLI](https://github.com/bitwarden/clients) (`@bitwarden/cli`) | Vault-Client | GPL-3.0 | per `npm install` ins vault-api-Image, Aufruf als Subprozess (`bw …`) |

Da beide als **eigenständige Programme** (eigener Prozess/Container, Aufruf über Netzwerk bzw. fork/exec) genutzt und nicht modifiziert oder als Quellcode weiterverteilt werden, propagiert deren Copyleft (AGPL/GPL) nicht auf den MIT-lizenzierten Code dieses Repos. Wer ein **kombiniertes** Image baut und weiterverteilt, übernimmt für die enthaltenen AGPL/GPL-Komponenten die jeweiligen Pflichten (Quelltext-Angebot; AGPL §13 für *modifizierte* Vaultwarden-Instanzen).

## Referenzen

- Vaultwarden Wiki: https://github.com/dani-garcia/vaultwarden/wiki
- Bitwarden Clients: https://bitwarden.com/download/
