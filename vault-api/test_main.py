"""Smoke-Tests fuer die im Code-Review-Backlog geaenderte Logik (#1, #2, #5).

Kein Framework — stdlib + assert. Laeuft wo fastapi installiert ist:
    python -m pytest test_main.py        # falls pytest da ist
    python test_main.py                  # direkt (nutzt assert-Bloecke unten)
Oder im Container:
    docker compose exec vault-api python /app/test_main.py

Netz/`bw serve` werden NICHT gebraucht: _api wird gestubbt, die getesteten
Funktionen sind rein env- bzw. argument-getrieben.
"""
import os
import importlib

import main


def _reload_clean_env():
    # _tls_reject/_maybe_sync lesen os.environ zur Laufzeit — Env vor jedem Fall
    # zuruecksetzen, damit Faelle sich nicht beeinflussen.
    for k in ("VAULT_INSECURE_TLS", "VAULT_SYNC_AFTER_WRITE"):
        os.environ.pop(k, None)


# ── #1 TLS: Verify default an, Opt-out nur via VAULT_INSECURE_TLS=1 ──────────
def test_tls_reject_default_verifies():
    _reload_clean_env()
    assert main._tls_reject() == "1"  # verify an


def test_tls_reject_opt_out():
    _reload_clean_env()
    os.environ["VAULT_INSECURE_TLS"] = "1"
    assert main._tls_reject() == "0"  # nur "1" schaltet ab


def test_tls_reject_other_value_stays_secure():
    _reload_clean_env()
    os.environ["VAULT_INSECURE_TLS"] = "0"  # alles ausser "1" bleibt sicher
    assert main._tls_reject() == "1"


# ── #2 Token: konstant-zeitlicher Vergleich, fail-closed ────────────────────
def _verify(token_env, presented):
    import fastapi
    from fastapi.security import HTTPAuthorizationCredentials

    orig = main.VAULT_API_TOKEN
    main.VAULT_API_TOKEN = token_env
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=presented)
    try:
        main.verify_token(creds)
        return None
    except fastapi.HTTPException as e:
        return e.status_code
    finally:
        main.VAULT_API_TOKEN = orig


def test_verify_token_correct_passes():
    assert _verify("s3cret", "s3cret") is None


def test_verify_token_wrong_rejected():
    assert _verify("s3cret", "wrong") == 401


def test_verify_token_empty_config_fail_closed():
    # Kein Token konfiguriert -> niemand kommt rein, auch nicht mit "".
    assert _verify("", "") == 401


# ── #5 Sync: Default an, abschaltbar via VAULT_SYNC_AFTER_WRITE=0 ────────────
def _capture_sync():
    calls = []
    orig = main._api
    main._api = lambda *a, **k: calls.append(a)
    try:
        main._maybe_sync()
    finally:
        main._api = orig
    return calls


def test_maybe_sync_default_on():
    _reload_clean_env()
    calls = _capture_sync()
    assert calls == [("POST", "/sync")]


def test_maybe_sync_disabled():
    _reload_clean_env()
    os.environ["VAULT_SYNC_AFTER_WRITE"] = "0"
    assert _capture_sync() == []


def test_maybe_sync_other_value_stays_on():
    _reload_clean_env()
    os.environ["VAULT_SYNC_AFTER_WRITE"] = "1"
    assert _capture_sync() == [("POST", "/sync")]


# ── Endpoint-Logik: Stubs statt Netz/`bw serve` ─────────────────────────────
# main._get_object / main._api / main._maybe_sync sind die einzigen I/O-Punkte
# der Endpoints. Pro Test patchen, in finally zuruecksetzen (Muster wie oben).

def _patch(**attrs):
    """Setzt main-Attribute, gibt die Originale zum Restore zurueck."""
    orig = {k: getattr(main, k) for k in attrs}
    for k, v in attrs.items():
        setattr(main, k, v)
    return orig


def _restore(orig):
    for k, v in orig.items():
        setattr(main, k, v)


# ── get_secret: SSH-Key (type 5) liest privateKey, sonst login.password ──────
def test_get_secret_login_password():
    orig = _patch(_get_object=lambda n: {"type": 1, "login": {"password": "pw123"}})
    try:
        assert main.get_secret("x") == {"password": "pw123"}
    finally:
        _restore(orig)


def test_get_secret_ssh_key_uses_private_key():
    orig = _patch(_get_object=lambda n: {"type": 5, "sshKey": {"privateKey": "PRIV"}})
    try:
        assert main.get_secret("x") == {"password": "PRIV"}
    finally:
        _restore(orig)


# ── get_ssh_key / get_item: Feld-Mapping ────────────────────────────────────
def test_get_ssh_key_maps_fields():
    item = {"name": "k", "sshKey": {"publicKey": "PUB", "keyFingerprint": "SHA256:abc"}}
    orig = _patch(_get_object=lambda n: item)
    try:
        assert main.get_ssh_key("k") == {
            "name": "k", "public_key": "PUB", "fingerprint": "SHA256:abc",
        }
    finally:
        _restore(orig)


def test_get_item_maps_name_and_username():
    orig = _patch(_get_object=lambda n: {"name": "srv", "login": {"username": "u"}})
    try:
        assert main.get_item("srv") == {"name": "srv", "username": "u"}
    finally:
        _restore(orig)


# ── list_items: list/object/items -> [{name, username}], username optional ──
def test_list_items_maps_and_handles_missing_username():
    data = {"data": [
        {"name": "a", "login": {"username": "ua"}},
        {"name": "b"},  # kein login -> username None
    ]}
    orig = _patch(_api=lambda m, p, *a: data if (m, p) == ("GET", "/list/object/items") else None)
    try:
        assert main.list_items() == [
            {"name": "a", "username": "ua"},
            {"name": "b", "username": None},
        ]
    finally:
        _restore(orig)


# ── list_items synct vor dem Lesen (sonst Drift zum lokalen bw-serve-Cache) ──
def test_list_items_syncs_before_read():
    _reload_clean_env()
    calls = []
    orig = _patch(_api=lambda m, p, *a: calls.append((m, p)) or {"data": []})
    try:
        main.list_items()
        # Sync-POST muss VOR dem List-GET kommen.
        assert calls == [("POST", "/sync"), ("GET", "/list/object/items")]
    finally:
        _restore(orig)


def test_list_items_no_sync_when_disabled():
    _reload_clean_env()
    os.environ["VAULT_SYNC_AFTER_WRITE"] = "0"
    calls = []
    orig = _patch(_api=lambda m, p, *a: calls.append((m, p)) or {"data": []})
    try:
        main.list_items()
        assert calls == [("GET", "/list/object/items")]  # kein Sync
    finally:
        _restore(orig)


# ── create_item: Payload-Shape + Sync ───────────────────────────────────────
def _capture_api():
    """Zeichnet alle _api-Aufrufe auf; _maybe_sync ueber _api mit erfasst."""
    calls = []
    return calls, (lambda m, p, body=None: calls.append((m, p, body)))


def test_create_item_payload_with_url():
    calls, stub = _capture_api()
    orig = _patch(_api=stub)
    try:
        main.create_item(main.CreateItem(name="n", username="u", password="pw", url="https://x"))
        method, path, payload = calls[0]
        assert (method, path) == ("POST", "/object/item")
        assert payload["type"] == 1
        assert payload["login"]["username"] == "u"
        assert payload["login"]["password"] == "pw"
        assert payload["login"]["uris"] == [{"match": None, "uri": "https://x"}]
        # _maybe_sync (default an) -> zweiter _api-Call POST /sync
        assert ("POST", "/sync", None) in calls
    finally:
        _restore(orig)


def test_create_item_no_url_empty_uris():
    _reload_clean_env()
    calls, stub = _capture_api()
    orig = _patch(_api=stub)
    try:
        main.create_item(main.CreateItem(name="n", username="u", password="pw"))
        payload = calls[0][2]
        assert payload["login"]["uris"] == []
    finally:
        _restore(orig)


# ── create_ssh_key: Typ 5 + sshKey-Struktur ─────────────────────────────────
def test_create_ssh_key_payload():
    calls, stub = _capture_api()
    orig = _patch(_api=stub)
    try:
        main.create_ssh_key(main.CreateSshKey(
            name="k", private_key="PRIV", public_key="PUB", fingerprint="FP",
        ))
        method, path, payload = calls[0]
        assert (method, path) == ("POST", "/object/item")
        assert payload["type"] == 5
        assert payload["sshKey"] == {
            "privateKey": "PRIV", "publicKey": "PUB", "keyFingerprint": "FP",
        }
    finally:
        _restore(orig)


# ── update_item: nur uebergebene Felder, PUT an /object/item/{id} ────────────
def test_update_item_partial_only_changes_given_fields():
    item = {"id": "ID9", "name": "n", "login": {"username": "old", "password": "oldpw"}}
    calls, stub = _capture_api()
    orig = _patch(_get_object=lambda n: item, _api=stub)
    try:
        main.update_item("n", main.UpdateItem(password="newpw"))  # nur password
        method, path, payload = calls[0]
        assert (method, path) == ("PUT", "/object/item/ID9")
        assert payload["login"]["password"] == "newpw"
        assert payload["login"]["username"] == "old"  # unangetastet
    finally:
        _restore(orig)


# ── delete_item: DELETE an /object/item/{id} ────────────────────────────────
def test_delete_item_calls_delete_by_id():
    calls, stub = _capture_api()
    orig = _patch(_get_object=lambda n: {"id": "ID7"}, _api=stub)
    try:
        main.delete_item("n")
        assert calls[0] == ("DELETE", "/object/item/ID7", None)
    finally:
        _restore(orig)


# ── _api: Auto-Unlock-Retry bei gesperrtem Vault ────────────────────────────
def test_api_retries_once_after_unlock_when_locked():
    seq = [
        (200, {"success": False, "message": "Vault is locked."}),  # 1. Versuch
        (200, {"success": True, "data": "OK"}),                    # nach unlock
    ]
    unlocked = []
    orig = _patch(
        _request=lambda *a, **k: seq.pop(0),
        _unlock=lambda: unlocked.append(True),
    )
    try:
        assert main._api("GET", "/x") == "OK"
        assert unlocked == [True]  # genau einmal entsperrt
    finally:
        _restore(orig)


def test_api_raises_when_still_failing():
    orig = _patch(
        _request=lambda *a, **k: (500, {"success": False, "message": "boom"}),
        _unlock=lambda: None,
    )
    try:
        raised = False
        try:
            main._api("GET", "/x")
        except RuntimeError:
            raised = True
        assert raised
    finally:
        _restore(orig)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
