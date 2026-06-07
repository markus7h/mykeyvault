#!/usr/bin/env node
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { z } from "zod";
import { spawnSync } from "child_process";
import { createHash, timingSafeEqual } from "crypto";
import express from "express";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";

const BASE = process.env.VAULT_API_URL ?? "http://localhost:8223";
const TOKEN = process.env.VAULT_API_TOKEN ?? "";

// Transport: stdio (Default, lokal mit vollem Funktionsumfang) oder http
// (zentral als Container — dann sieht der Server NUR das eigene Dateisystem,
// daher werden die lokal-ausführenden Tools im http-Modus nicht registriert).
const TRANSPORT = (process.env.MCP_TRANSPORT ?? "stdio").toLowerCase();
const HTTP_MODE = TRANSPORT === "http";
const PORT = Number(process.env.PORT ?? 3458);
const HOST = process.env.HOST ?? "0.0.0.0";
// Bearer für den http-Modus. Bewusst derselbe Token wie ai-rem (Vault-Item
// ai-rem-api-token), den jeder Client ohnehin in ~/.claude.json trägt.
const AUTH_TOKEN = process.env.MCP_AUTH_TOKEN ?? "";

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

/** SSH-SHA256-Fingerprint aus einem OpenSSH-Public-Key (ohne ssh-keygen-Abhängigkeit). */
function sshFingerprint(publicKey: string): string {
  const blob = publicKey.trim().split(/\s+/)[1] ?? "";
  const hash = createHash("sha256").update(Buffer.from(blob, "base64")).digest("base64").replace(/=+$/, "");
  return `SHA256:${hash}`;
}

// ---------------------------------------------------------------------------

/**
 * Baut eine McpServer-Instanz. Im http-Modus werden nur die reinen Vault-API-Tools
 * registriert (list/create_item/get_ssh_public_key/create_ssh_key); die lokal
 * ausführenden Tools (write_secret, run_with_secret, run_with_secret_file) bleiben
 * dem stdio-Modus vorbehalten, da sie das Dateisystem des aufrufenden Rechners
 * brauchen — über http liefe das auf dem Server.
 */
function buildServer(): McpServer {
  const server = new McpServer({ name: "mykeyvault", version: "2.1.0" });

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
    "vault_create_ssh_key",
    "Legt einen SSH-Key als nativen SSH-Key-Eintrag (Bitwarden-Typ 5) im Vault an. " +
      "Der private Key kann als Inhalt (private_key) ODER als lokaler Dateipfad (private_key_path) " +
      "übergeben werden — über http wird der Inhalt benötigt, da der Server kein lokales Dateisystem " +
      "des Clients sieht. Public-Key analog (public_key / public_key_path); der Fingerprint wird, " +
      "falls nicht angegeben, aus dem Public-Key abgeleitet.",
    {
      name: z.string().describe("Name des Eintrags"),
      private_key: z.string().optional().describe("Privater SSH-Key als Inhalt (Alternative zu private_key_path)"),
      private_key_path: z.string().optional().describe("Lokaler Pfad zum privaten SSH-Key (nur stdio/lokal)"),
      public_key: z.string().optional().describe("Public-Key als Inhalt (Alternative zu public_key_path)"),
      public_key_path: z.string().optional().describe(
        "Pfad zum Public-Key; Standard: <private_key_path>.pub (nur stdio/lokal)"
      ),
      fingerprint: z.string().optional().describe("SHA256-Fingerprint; wird sonst aus dem Public-Key abgeleitet"),
      notes: z.string().optional().describe("Notizen (optional)"),
    },
    async ({ name, private_key, private_key_path, public_key, public_key_path, fingerprint, notes }) => {
      let privateKey = private_key;
      if (!privateKey) {
        if (!private_key_path) {
          throw new Error("private_key oder private_key_path angeben.");
        }
        privateKey = fs.readFileSync(private_key_path, "utf8");
      }

      let publicKey = public_key?.trim();
      if (!publicKey) {
        const pubPath = public_key_path ?? (private_key_path ? `${private_key_path}.pub` : undefined);
        if (!pubPath) {
          throw new Error("public_key oder public_key_path (bzw. private_key_path) angeben.");
        }
        if (!fs.existsSync(pubPath)) {
          throw new Error(`Public-Key nicht gefunden: ${pubPath} — public_key/public_key_path angeben.`);
        }
        publicKey = fs.readFileSync(pubPath, "utf8").trim();
      }

      const fp = fingerprint ?? sshFingerprint(publicKey);

      const result = await api<{ success: boolean; name: string }>("/ssh-keys", {
        method: "POST",
        body: JSON.stringify({ name, private_key: privateKey, public_key: publicKey, fingerprint: fp, notes }),
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

  // ── Lokal ausführende Tools: nur im stdio-Modus (brauchen das Client-FS) ──
  if (!HTTP_MODE) {
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

        const result = spawnSync("sh", ["-c", command], { encoding: "utf8", env, timeout: 60_000 });

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
  }

  return server;
}

// ---------------------------------------------------------------------------

function bearerOk(header: string | undefined): boolean {
  if (!AUTH_TOKEN) return false;
  const got = (header ?? "").replace(/^Bearer\s+/i, "");
  const a = Buffer.from(got);
  const b = Buffer.from(AUTH_TOKEN);
  return a.length === b.length && timingSafeEqual(a, b);
}

async function main() {
  if (!HTTP_MODE) {
    const server = buildServer();
    await server.connect(new StdioServerTransport());
    return;
  }

  if (!AUTH_TOKEN) {
    process.stderr.write("[mykeyvault-mcp] MCP_AUTH_TOKEN nicht gesetzt — fail-closed, Abbruch.\n");
    process.exit(1);
  }

  const app = express();
  app.use(express.json({ limit: "4mb" }));

  // Healthcheck (kein Token — bewusst, damit der Docker-Healthcheck ohne Secret auskommt).
  app.get("/health", (_req, res) => res.status(200).send("ok"));

  app.post("/mcp", async (req, res) => {
    if (!bearerOk(req.headers.authorization)) {
      res.status(401).json({ jsonrpc: "2.0", error: { code: -32001, message: "unauthorized" }, id: null });
      return;
    }
    // Stateless: pro Request frische Server- + Transport-Instanz.
    const server = buildServer();
    const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined });
    res.on("close", () => { transport.close(); server.close(); });
    try {
      await server.connect(transport);
      await transport.handleRequest(req, res, req.body);
    } catch (err) {
      process.stderr.write(`[mykeyvault-mcp] error: ${String(err)}\n`);
      if (!res.headersSent) {
        res.status(500).json({ jsonrpc: "2.0", error: { code: -32603, message: "internal error" }, id: null });
      }
    }
  });

  // Stateless-Server unterstützt keine GET-SSE/DELETE-Session.
  const methodNotAllowed = (_req: express.Request, res: express.Response) =>
    res.status(405).json({ jsonrpc: "2.0", error: { code: -32000, message: "method not allowed" }, id: null });
  app.get("/mcp", methodNotAllowed);
  app.delete("/mcp", methodNotAllowed);

  app.listen(PORT, HOST, () => {
    process.stderr.write(`[mykeyvault-mcp] http on http://${HOST}:${PORT}/mcp (vault-api ${BASE})\n`);
  });
}

main().catch((err) => {
  process.stderr.write(`[mykeyvault-mcp] fatal: ${String(err)}\n`);
  process.exit(1);
});
