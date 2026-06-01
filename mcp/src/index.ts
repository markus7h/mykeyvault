#!/usr/bin/env node
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { spawnSync } from "child_process";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";

const BASE = process.env.VAULT_API_URL ?? "http://localhost:8223";
const TOKEN = process.env.VAULT_API_TOKEN ?? "";

async function api<T>(apiPath: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${apiPath}`, {
    ...opts,
    headers: {
      Authorization: `Bearer ${TOKEN}`,
      "Content-Type": "application/json",
      ...(opts?.headers ?? {}),
    },
  });
  if (!res.ok) {
    throw new Error(`vault-api ${apiPath}: ${res.status} ${await res.text()}`);
  }
  return res.json() as Promise<T>;
}

// ---------------------------------------------------------------------------

const server = new McpServer({ name: "mykeyvault", version: "2.0.0" });

server.tool(
  "vault_list_items",
  "Listet alle Vault-Einträge mit Name und Benutzername. Kein Passwort / Secret im Output.",
  {},
  async () => {
    const items = await api<{ name: string; username: string | null }[]>("/items");
    return { content: [{ type: "text" as const, text: JSON.stringify(items, null, 2) }] };
  }
);

server.tool(
  "vault_create_item",
  "Legt einen neuen Login-Eintrag im Vault an. " +
    "Das Passwort erscheint als Eingabe-Parameter — es wird aber nicht in der Antwort zurückgegeben.",
  {
    name: z.string().describe("Name des Eintrags"),
    username: z.string().describe("Benutzername"),
    password: z.string().describe("Passwort / Secret"),
    notes: z.string().optional().describe("Notizen (optional)"),
    url: z.string().optional().describe("URL (optional)"),
  },
  async ({ name, username, password, notes, url }) => {
    const result = await api<{ success: boolean; name: string }>("/items", {
      method: "POST",
      body: JSON.stringify({ name, username, password, notes, url }),
    });
    return { content: [{ type: "text" as const, text: JSON.stringify(result) }] };
  }
);

server.tool(
  "vault_write_secret",
  "Schreibt das Passwort eines Vault-Eintrags in eine lokale Datei (chmod 600). " +
    "Gibt nur den Dateipfad zurück — das Secret selbst erscheint nie im Kontext.",
  {
    item_name: z.string().describe("Name des Vault-Eintrags"),
    output_path: z.string().optional().describe("Zieldatei; Standard: /tmp/vault-secret-<ts>"),
  },
  async ({ item_name, output_path }) => {
    const { password } = await api<{ password: string }>(`/secret/${encodeURIComponent(item_name)}`);
    const filePath = output_path ?? path.join(os.tmpdir(), `vault-secret-${Date.now()}`);
    fs.writeFileSync(filePath, password, { mode: 0o600 });
    return { content: [{ type: "text" as const, text: JSON.stringify({ path: filePath }) }] };
  }
);

server.tool(
  "vault_run_with_secret",
  "Führt einen Shell-Befehl aus und injiziert dabei das Passwort eines Vault-Eintrags als Env-Variable. " +
    "Das Secret erscheint nicht im Rückgabewert — solange der Befehl es nicht selbst ausgibt.",
  {
    item_name: z.string().describe("Name des Vault-Eintrags"),
    env_var: z.string().describe("Env-Variablen-Name für das Passwort (z.B. MY_SECRET)"),
    command: z.string().describe("Shell-Befehl (wird via sh -c ausgeführt)"),
    username_env_var: z.string().optional().describe(
      "Falls gesetzt: Benutzername des Eintrags als diese Env-Variable mitgeben"
    ),
  },
  async ({ item_name, env_var, command, username_env_var }) => {
    const { password } = await api<{ password: string }>(`/secret/${encodeURIComponent(item_name)}`);
    const env: Record<string, string> = { ...(process.env as Record<string, string>), [env_var]: password };

    if (username_env_var) {
      const item = await api<{ username: string | null }>(`/item/${encodeURIComponent(item_name)}`);
      env[username_env_var] = item.username ?? "";
    }

    const result = spawnSync("sh", ["-c", command], {
      encoding: "utf8",
      env,
      timeout: 60_000,
    });

    return {
      content: [{
        type: "text" as const,
        text: JSON.stringify(
          { exit_code: result.status, stdout: result.stdout, stderr: result.stderr },
          null,
          2
        ),
      }],
    };
  }
);

server.tool(
  "vault_create_ssh_key",
  "Legt einen SSH-Key als nativen SSH-Key-Eintrag (Bitwarden-Typ 5) im Vault an. " +
    "Der private Key wird lokal aus der Datei gelesen und erscheint NICHT als Parameter / im Kontext. " +
    "Public-Key und Fingerprint werden lokal via ssh-keygen abgeleitet.",
  {
    name: z.string().describe("Name des Eintrags"),
    private_key_path: z.string().describe("Lokaler Pfad zum privaten SSH-Key"),
    public_key_path: z.string().optional().describe(
      "Pfad zum Public-Key; Standard: <private_key_path>.pub"
    ),
    notes: z.string().optional().describe("Notizen (optional)"),
  },
  async ({ name, private_key_path, public_key_path, notes }) => {
    const privateKey = fs.readFileSync(private_key_path, "utf8");
    const pubPath = public_key_path ?? `${private_key_path}.pub`;
    if (!fs.existsSync(pubPath)) {
      throw new Error(`Public-Key nicht gefunden: ${pubPath} — public_key_path angeben.`);
    }
    const publicKey = fs.readFileSync(pubPath, "utf8").trim();
    const fp = spawnSync("ssh-keygen", ["-lf", pubPath], { encoding: "utf8" });
    if (fp.status !== 0) {
      throw new Error(`ssh-keygen -lf: ${fp.stderr}`);
    }
    // Ausgabe: "<bits> SHA256:<hash> <comment> (<type>)"
    const fingerprint = fp.stdout.trim().split(/\s+/)[1] ?? "";
    const result = await api<{ success: boolean; name: string }>("/ssh-keys", {
      method: "POST",
      body: JSON.stringify({
        name,
        private_key: privateKey,
        public_key: publicKey,
        fingerprint,
        notes,
      }),
    });
    return { content: [{ type: "text" as const, text: JSON.stringify(result) }] };
  }
);

server.tool(
  "vault_get_ssh_public_key",
  "Gibt Public-Key und Fingerprint eines SSH-Key-Eintrags zurück (kein privater Key).",
  { item_name: z.string().describe("Name des Vault-Eintrags") },
  async ({ item_name }) => {
    const data = await api<{ name: string; public_key: string | null; fingerprint: string | null }>(
      `/ssh-key/${encodeURIComponent(item_name)}`
    );
    return { content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }] };
  }
);

server.tool(
  "vault_run_with_secret_file",
  "Schreibt das Secret eines Vault-Eintrags in eine temporäre Datei (chmod 600), führt einen Shell-Befehl aus " +
    "und löscht die Datei danach garantiert wieder (auch bei Fehler). Im Befehl wird '{}' durch den Temp-Pfad " +
    "ersetzt, zusätzlich steht er als $SECRET_FILE bereit. Für SSH-Keys / PEM, die einen Dateipfad verlangen. " +
    "Das Secret erscheint nicht im Rückgabewert — solange der Befehl es nicht selbst ausgibt.",
  {
    item_name: z.string().describe("Name des Vault-Eintrags"),
    command: z.string().describe(
      "Shell-Befehl (sh -c); '{}' und $SECRET_FILE = Pfad der temporären Secret-Datei"
    ),
  },
  async ({ item_name, command }) => {
    const { password } = await api<{ password: string }>(`/secret/${encodeURIComponent(item_name)}`);
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "vault-"));
    const filePath = path.join(dir, "secret");
    try {
      fs.writeFileSync(filePath, password, { mode: 0o600 });
      const cmd = command.split("{}").join(filePath);
      const result = spawnSync("sh", ["-c", cmd], {
        encoding: "utf8",
        env: { ...(process.env as Record<string, string>), SECRET_FILE: filePath },
        timeout: 60_000,
      });
      return {
        content: [{
          type: "text" as const,
          text: JSON.stringify(
            { exit_code: result.status, stdout: result.stdout, stderr: result.stderr },
            null,
            2
          ),
        }],
      };
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  }
);

// ---------------------------------------------------------------------------

const transport = new StdioServerTransport();
await server.connect(transport);
