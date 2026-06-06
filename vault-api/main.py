import os
import time
import subprocess
import json
import urllib.request
import urllib.error
import urllib.parse
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Security, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

VAULT_URL = os.environ.get("VAULT_URL", "http://mykeyvault:80")
VAULT_API_TOKEN = os.environ.get("VAULT_API_TOKEN", "")
BW_CLIENTID = os.environ.get("BW_CLIENTID", "")
BW_CLIENTSECRET = os.environ.get("BW_CLIENTSECRET", "")
BW_PASSWORD = os.environ.get("BW_PASSWORD", "")

# `bw serve` laeuft dauerhaft entsperrt im Container und bindet localhost:8087.
# Alle Vault-Operationen gehen ueber dessen lokale REST-API statt pro Request
# einen `bw`-CLI-Subprozess zu spawnen (Cold-Start ~1,3-2,3s je Aufruf).
SERVE_BASE = "http://localhost:8087"
_serve_proc: subprocess.Popen | None = None

_session: str | None = None
_configured: bool = False

security = HTTPBearer()


def verify_token(creds: HTTPAuthorizationCredentials = Security(security)) -> None:
    if not VAULT_API_TOKEN or creds.credentials != VAULT_API_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _run(args: list[str], input_text: str | None = None) -> tuple[str, str, int]:
    tls_verify = "1" if os.environ.get("NODE_EXTRA_CA_CERTS") else "0"
    env = {**os.environ, "NODE_TLS_REJECT_UNAUTHORIZED": tls_verify}
    result = subprocess.run(
        ["bw"] + args,
        capture_output=True,
        text=True,
        input=input_text,
        env=env,
        timeout=15,
    )
    return result.stdout, result.stderr, result.returncode


def _ensure_ready() -> str:
    """Einmaliger CLI-Login/Config/Unlock beim Startup. `bw serve` hat keinen
    Login-Endpoint, daher muss der Account vorab per CLI eingerichtet und einmal
    entsperrt werden; der Session-Token startet dann den serve-Daemon entsperrt."""
    global _session, _configured

    if _session:
        return _session

    stdout, _, _ = _run(["status"])
    try:
        status_obj = json.loads(stdout)
    except Exception:
        status_obj = {}
    status = status_obj.get("status", "unauthenticated")

    if not _configured:
        # `bw config server` ist nur erlaubt UND nur noetig, solange kein Account
        # eingerichtet ist — bw lehnt es mit "Logout required before server config
        # update" ab, sobald ein User eingeloggt ist. Nach Container-Neustart wird
        # _configured zurueckgesetzt, der persistente bw-Stand bleibt aber ggf.
        # eingeloggt → daher auf den Live-Status pruefen, nicht nur auf das Flag.
        if status == "unauthenticated" and status_obj.get("serverUrl") != VAULT_URL:
            _, stderr, rc = _run(["config", "server", VAULT_URL])
            if rc != 0:
                raise RuntimeError(f"bw config: {stderr.strip()}")
        _configured = True

    if status == "unauthenticated":
        if not BW_CLIENTID or not BW_CLIENTSECRET:
            raise RuntimeError("BW_CLIENTID / BW_CLIENTSECRET not set")
        _, stderr, rc = _run(["login", "--apikey"])
        if rc != 0:
            raise RuntimeError(f"bw login: {stderr.strip()}")

    if not BW_PASSWORD:
        raise RuntimeError("BW_PASSWORD not set")

    stdout, stderr, rc = _run(["unlock", "--raw"], BW_PASSWORD + "\n")
    token = stdout.strip()
    if rc != 0 or not token:
        raise RuntimeError(f"bw unlock: {stderr.strip() or 'empty session'}")

    _session = token
    return _session


def _request(method: str, path: str, json_body: dict | None = None) -> tuple[int, dict]:
    """Roh-HTTP-Call gegen den serve-Daemon. Gibt (status_code, parsed_json) zurueck,
    auch bei non-2xx (urllib.HTTPError wird abgefangen)."""
    data = json.dumps(json_body).encode() if json_body is not None else None
    req = urllib.request.Request(SERVE_BASE + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode()
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            parsed = json.loads(body) if body else {}
        except Exception:
            parsed = {"message": body}
        return e.code, parsed


def _unlock() -> None:
    if not BW_PASSWORD:
        raise RuntimeError("BW_PASSWORD not set")
    _, payload = _request("POST", "/unlock", {"password": BW_PASSWORD})
    if not payload.get("success", False):
        raise RuntimeError(f"serve unlock: {payload.get('message')}")


def _api(method: str, path: str, json_body: dict | None = None):
    """serve-API-Call. Parst {success, data}, gibt data zurueck, wirft bei Fehler.
    Bei gesperrtem Vault (seltenes Auto-Lock) einmal entsperren und neu versuchen."""
    status, payload = _request(method, path, json_body)
    if status < 200 or status >= 300 or not payload.get("success", False):
        if "locked" in (payload.get("message") or "").lower():
            _unlock()
            status, payload = _request(method, path, json_body)
    if status < 200 or status >= 300 or not payload.get("success", False):
        raise RuntimeError(payload.get("message") or f"serve {method} {path}: HTTP {status}")
    return payload.get("data")


def _start_serve() -> None:
    """Startet den entsperrten serve-Daemon und wartet auf Bereitschaft."""
    global _serve_proc
    session = _ensure_ready()
    tls_verify = "1" if os.environ.get("NODE_EXTRA_CA_CERTS") else "0"
    env = {**os.environ, "NODE_TLS_REJECT_UNAUTHORIZED": tls_verify, "BW_SESSION": session}
    _serve_proc = subprocess.Popen(
        ["bw", "serve", "--hostname", "localhost", "--port", "8087"],
        env=env,
    )
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            _, payload = _request("GET", "/status")
            if payload.get("success"):
                st = payload.get("data", {}).get("template", {}).get("status")
                if st == "unlocked":
                    return
                if st == "locked":
                    _unlock()
                    return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("bw serve did not become ready within 30s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _start_serve()
    yield
    global _session, _configured, _serve_proc
    if _serve_proc is not None:
        _serve_proc.terminate()
        _serve_proc = None
    # reset session on shutdown so next startup re-authenticates
    _session = None
    _configured = False


app = FastAPI(title="vault-api", lifespan=lifespan)


def _get_object(item_name: str) -> dict:
    return _api("GET", f"/object/item/{urllib.parse.quote(item_name)}")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/items", dependencies=[Depends(verify_token)])
def list_items():
    try:
        data = _api("GET", "/list/object/items")
        return [
            {"name": i["name"], "username": i.get("login", {}).get("username")}
            for i in data.get("data", [])
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/secret/{item_name}", dependencies=[Depends(verify_token)])
def get_secret(item_name: str):
    try:
        # SSH-Keys (type 5) haben kein password-Feld; der private Key liegt in
        # sshKey.privateKey. Fuer alle anderen Typen aus login.password.
        item = _get_object(item_name)
        if item.get("type") == 5:
            password = item.get("sshKey", {}).get("privateKey", "")
        else:
            password = item.get("login", {}).get("password", "")
        return {"password": password}
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/ssh-key/{item_name}", dependencies=[Depends(verify_token)])
def get_ssh_key(item_name: str):
    try:
        item = _get_object(item_name)
        sk = item.get("sshKey", {})
        return {
            "name": item["name"],
            "public_key": sk.get("publicKey"),
            "fingerprint": sk.get("keyFingerprint"),
        }
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/item/{item_name}", dependencies=[Depends(verify_token)])
def get_item(item_name: str):
    try:
        item = _get_object(item_name)
        return {
            "name": item["name"],
            "username": item.get("login", {}).get("username"),
        }
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


class CreateItem(BaseModel):
    name: str
    username: str
    password: str
    notes: str | None = None
    url: str | None = None


@app.post("/items", dependencies=[Depends(verify_token)], status_code=201)
def create_item(body: CreateItem):
    try:
        payload = {
            "type": 1,
            "name": body.name,
            "login": {
                "username": body.username,
                "password": body.password,
                "uris": [{"match": None, "uri": body.url}] if body.url else [],
            },
            "notes": body.notes,
            "favorite": False,
        }
        _api("POST", "/object/item", payload)
        _api("POST", "/sync")
        return {"success": True, "name": body.name}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class CreateSshKey(BaseModel):
    name: str
    private_key: str
    public_key: str
    fingerprint: str
    notes: str | None = None


@app.post("/ssh-keys", dependencies=[Depends(verify_token)], status_code=201)
def create_ssh_key(body: CreateSshKey):
    try:
        payload = {
            "type": 5,
            "name": body.name,
            "sshKey": {
                "privateKey": body.private_key,
                "publicKey": body.public_key,
                "keyFingerprint": body.fingerprint,
            },
            "notes": body.notes,
            "favorite": False,
        }
        _api("POST", "/object/item", payload)
        _api("POST", "/sync")
        return {"success": True, "name": body.name}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class UpdateItem(BaseModel):
    password: str | None = None
    username: str | None = None
    notes: str | None = None


@app.put("/items/{item_name}", dependencies=[Depends(verify_token)])
def update_item(item_name: str, body: UpdateItem):
    try:
        item = _get_object(item_name)
        if body.password is not None:
            item.setdefault("login", {})["password"] = body.password
        if body.username is not None:
            item.setdefault("login", {})["username"] = body.username
        if body.notes is not None:
            item["notes"] = body.notes
        _api("PUT", f"/object/item/{item['id']}", item)
        _api("POST", "/sync")
        return {"success": True, "name": item_name}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/items/{item_name}", dependencies=[Depends(verify_token)])
def delete_item(item_name: str):
    try:
        item = _get_object(item_name)
        _api("DELETE", f"/object/item/{item['id']}")
        _api("POST", "/sync")
        return {"success": True, "name": item_name}
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))
