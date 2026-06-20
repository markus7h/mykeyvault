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


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
