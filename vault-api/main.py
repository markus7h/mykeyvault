import os
import subprocess
import json
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Security, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

VAULT_URL = os.environ.get("VAULT_URL", "http://mykeyvault:80")
VAULT_API_TOKEN = os.environ.get("VAULT_API_TOKEN", "")
BW_CLIENTID = os.environ.get("BW_CLIENTID", "")
BW_CLIENTSECRET = os.environ.get("BW_CLIENTSECRET", "")
BW_PASSWORD = os.environ.get("BW_PASSWORD", "")

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


def _bw(args: list[str], input_text: str | None = None) -> str:
    global _session
    session = _ensure_ready()
    env = {**os.environ, "NODE_TLS_REJECT_UNAUTHORIZED": "0", "BW_SESSION": session}
    result = subprocess.run(
        ["bw"] + args,
        capture_output=True,
        text=True,
        input=input_text,
        env=env,
        timeout=15,
    )
    if result.returncode != 0:
        _session = None
        raise RuntimeError(f"bw {args[0]}: {result.stderr.strip()}")
    return result.stdout


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    # reset session on shutdown so next startup re-authenticates
    global _session, _configured
    _session = None
    _configured = False


app = FastAPI(title="vault-api", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/items", dependencies=[Depends(verify_token)])
def list_items():
    try:
        raw = _bw(["list", "items"])
        items = json.loads(raw)
        return [
            {"name": i["name"], "username": i.get("login", {}).get("username")}
            for i in items
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/secret/{item_name}", dependencies=[Depends(verify_token)])
def get_secret(item_name: str):
    try:
        # SSH-Keys (type 5) haben kein password-Feld; der private Key liegt in
        # sshKey.privateKey. Fuer alle anderen Typen byte-genau ueber
        # "bw get password" (kein .strip, damit ein zum Secret gehoerender
        # abschliessender Newline bei PEM/SSH erhalten bleibt).
        item = json.loads(_bw(["get", "item", item_name]))
        if item.get("type") == 5:
            password = item.get("sshKey", {}).get("privateKey", "")
        else:
            password = _bw(["get", "password", item_name])
        return {"password": password}
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/ssh-key/{item_name}", dependencies=[Depends(verify_token)])
def get_ssh_key(item_name: str):
    try:
        item = json.loads(_bw(["get", "item", item_name]))
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
        raw = _bw(["get", "item", item_name])
        item = json.loads(raw)
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
        encoded = _bw(["encode"], json.dumps(payload)).strip()
        _bw(["create", "item", encoded])
        _bw(["sync"])
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
        encoded = _bw(["encode"], json.dumps(payload)).strip()
        _bw(["create", "item", encoded])
        _bw(["sync"])
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
        raw = _bw(["get", "item", item_name])
        item = json.loads(raw)
        if body.password is not None:
            item.setdefault("login", {})["password"] = body.password
        if body.username is not None:
            item.setdefault("login", {})["username"] = body.username
        if body.notes is not None:
            item["notes"] = body.notes
        item_id = item["id"]
        encoded = _bw(["encode"], json.dumps(item)).strip()
        _bw(["edit", "item", item_id, encoded])
        _bw(["sync"])
        return {"success": True, "name": item_name}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/items/{item_name}", dependencies=[Depends(verify_token)])
def delete_item(item_name: str):
    try:
        raw = _bw(["get", "item", item_name])
        item_id = json.loads(raw)["id"]
        _bw(["delete", "item", item_id])
        _bw(["sync"])
        return {"success": True, "name": item_name}
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))
