"""iCloud Photos auth + asset access via pyicloud (multi-account)."""

from __future__ import annotations

import json
import logging
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote

from app.config import ICLOUD_COOKIE_DIR, ICLOUD_DIR, ICLOUD_THUMBS_DIR, ensure_data_dirs
from app.database import (
    SETTING_ACTIVE_ACCOUNT,
    SETTING_APPLE_ID_LEGACY,
    connect,
    get_setting,
    set_setting,
)

logger = logging.getLogger(__name__)

KEYRING_SERVICE = "mycloudphotos-icloud"

_lock = threading.RLock()
_api: Any | None = None
_pending_2fa: bool = False
_session_account_id: int | None = None
_2fa_code_requested: bool = False
_last_2fa_error: str | None = None
_sms_phone_id: Any = None

# Last-4 of each person's own phone, used only to pick among Apple's trusted numbers.
_PREFERRED_SMS_LAST4 = {
    "Dan": "8918",
    "Graham": "1779",
}


def cookie_dir(account_id: int | None = None) -> Path:
    ensure_data_dirs()
    aid = account_id if account_id is not None else get_active_account_id()
    path = ICLOUD_COOKIE_DIR / f"a{aid}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _migrate_legacy_cookies(account_id: int) -> None:
    """Move flat cookie files into the account subfolder once."""
    ensure_data_dirs()
    dest = cookie_dir(account_id)
    if any(dest.iterdir()):
        return
    if not ICLOUD_COOKIE_DIR.exists():
        return
    for child in ICLOUD_COOKIE_DIR.iterdir():
        if child.name.startswith("a") and child.name[1:].isdigit():
            continue
        if child.name == ".gitignore":
            continue
        try:
            target = dest / child.name
            if not target.exists():
                shutil.move(str(child), str(target))
        except OSError as e:
            logger.warning("Could not migrate cookie %s: %s", child, e)


def get_active_account_id() -> int:
    raw = get_setting(SETTING_ACTIVE_ACCOUNT)
    if raw and raw.isdigit():
        with connect() as conn:
            row = conn.execute(
                "SELECT id FROM icloud_accounts WHERE id = ?", (int(raw),)
            ).fetchone()
            if row:
                return int(row["id"])
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM icloud_accounts ORDER BY sort_order, id LIMIT 1"
        ).fetchone()
    if not row:
        raise ValueError("No iCloud accounts configured — restart the app to seed family profiles")
    set_setting(SETTING_ACTIVE_ACCOUNT, str(row["id"]))
    return int(row["id"])


def get_account(account_id: int | None = None) -> dict[str, Any]:
    aid = account_id if account_id is not None else get_active_account_id()
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM icloud_accounts WHERE id = ?", (aid,)
        ).fetchone()
    if not row:
        raise ValueError(f"Unknown iCloud account id: {aid}")
    return dict(row)


def list_accounts() -> list[dict[str, Any]]:
    active = get_active_account_id()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT a.*,
                   (SELECT COUNT(*) FROM icloud_assets ia WHERE ia.account_id = a.id) AS asset_count,
                   (SELECT COALESCE(SUM(size), 0) FROM icloud_assets ia WHERE ia.account_id = a.id) AS asset_bytes
            FROM icloud_accounts a
            ORDER BY a.sort_order, a.id
            """
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        d["active"] = int(d["id"]) == active
        d["has_apple_id"] = bool((d.get("apple_id") or "").strip())
        out.append(d)
    return out


def set_active_account(account_id: int) -> dict[str, Any]:
    """Switch the active family profile; drops in-memory session for the previous one."""
    from app.services import jobs

    jobs.assert_can_switch_icloud_account()
    global _api, _pending_2fa, _session_account_id, _2fa_code_requested, _last_2fa_error, _sms_phone_id
    account = get_account(account_id)
    with _lock:
        if _session_account_id != int(account["id"]):
            _api = None
            _pending_2fa = False
            _session_account_id = None
            _2fa_code_requested = False
            _last_2fa_error = None
        set_setting(SETTING_ACTIVE_ACCOUNT, str(account["id"]))
    return auth_status()


def create_account(label: str, *, apple_id: str | None = None) -> dict[str, Any]:
    label = (label or "").strip()
    if not label:
        raise ValueError("Account name is required")
    apple = _normalize_apple_id(apple_id) if apple_id else None
    with connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) AS m FROM icloud_accounts"
        ).fetchone()
        sort_order = int(row["m"] or -1) + 1
        try:
            cur = conn.execute(
                """
                INSERT INTO icloud_accounts(label, apple_id, sort_order)
                VALUES (?, ?, ?)
                """,
                (label, apple or None, sort_order),
            )
        except Exception as e:
            if "UNIQUE" in str(e).upper():
                raise ValueError(f"An account named “{label}” already exists") from e
            raise
        new_id = int(cur.lastrowid)
    return {"account": get_account(new_id), "accounts": list_accounts()}


def update_account(
    account_id: int,
    *,
    label: str | None = None,
    apple_id: str | None = None,
) -> dict[str, Any]:
    account = get_account(account_id)
    new_label = (label if label is not None else account.get("label") or "").strip()
    if not new_label:
        raise ValueError("Account name is required")
    if apple_id is None:
        new_apple = account.get("apple_id")
    else:
        new_apple = _normalize_apple_id(apple_id) or None
    with connect() as conn:
        try:
            conn.execute(
                """
                UPDATE icloud_accounts
                SET label = ?, apple_id = ?, updated_at = datetime('now')
                WHERE id = ?
                """,
                (new_label, new_apple, account_id),
            )
        except Exception as e:
            if "UNIQUE" in str(e).upper():
                raise ValueError(f"An account named “{new_label}” already exists") from e
            raise
    return {"account": get_account(account_id), "accounts": list_accounts()}


def delete_account(account_id: int) -> dict[str, Any]:
    from app.services import jobs

    jobs.assert_can_switch_icloud_account()
    accounts = list_accounts()
    if len(accounts) <= 1:
        raise ValueError("Cannot delete the last family account")
    account = get_account(account_id)
    active = get_active_account_id()
    with connect() as conn:
        conn.execute("DELETE FROM icloud_assets WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM icloud_accounts WHERE id = ?", (account_id,))
    _clear_cookie_files(account_id)
    apple = (account.get("apple_id") or "").strip()
    if apple:
        _clear_password(apple)
    if active == account_id:
        remaining = list_accounts()
        set_active_account(int(remaining[0]["id"]))
    return {
        "deleted_id": account_id,
        "accounts": list_accounts(),
        "active_id": get_active_account_id(),
    }


def update_account_apple_id(account_id: int, apple_id: str | None) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE icloud_accounts
            SET apple_id = ?, updated_at = datetime('now')
            WHERE id = ?
            """,
            ((apple_id or "").strip() or None, account_id),
        )


def saved_apple_id(account_id: int | None = None) -> str | None:
    acc = get_account(account_id)
    return (acc.get("apple_id") or "").strip() or None


def _store_password(apple_id: str, password: str) -> None:
    try:
        import keyring

        keyring.set_password(KEYRING_SERVICE, apple_id, password)
    except Exception as e:
        logger.warning("Could not store password in keyring: %s", e)


def _load_password(apple_id: str) -> str | None:
    try:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, apple_id)
    except Exception:
        return None


def _clear_password(apple_id: str) -> None:
    try:
        import keyring

        keyring.delete_password(KEYRING_SERVICE, apple_id)
    except Exception:
        pass


def auth_status() -> dict[str, Any]:
    """
    Report connection state. May resume a *trusted* cookie session quietly.

    Never calls Apple's request_2fa_code() — that only happens on Resend 2FA
    (legacy push) or Send SMS. Background status polls must not push MFA prompts.
    """
    global _api, _pending_2fa, _session_account_id, _2fa_code_requested, _last_2fa_error, _sms_phone_id
    with _lock:
        account = get_account()
        account_id = int(account["id"])
        apple_id = (account.get("apple_id") or "").strip() or None
        connected = False
        requires_2fa = _pending_2fa and _session_account_id == account_id
        trusted = False
        error = None
        delivery = None
        sms_available = False
        phone_hint = None
        phones: list[dict[str, Any]] = []
        selected_phone_id = None
        preferred_missing = False
        preferred_last4 = None

        if _api is not None and _session_account_id == account_id:
            try:
                requires_2fa = bool(_api.requires_2fa)
                trusted = bool(_api.is_trusted_session)
                connected = not requires_2fa
                _pending_2fa = requires_2fa
                if requires_2fa:
                    delivery = _two_factor_delivery(_api)
                    sms_available = _sms_available(_api)
                    ps = _phone_state(_api, account)
                    phone_hint = ps.get("phone_hint")
                    phones = ps.get("phones") or []
                    selected_phone_id = ps.get("selected_phone_id")
                    preferred_missing = bool(ps.get("preferred_missing"))
                    preferred_last4 = ps.get("preferred_last4")
            except Exception as e:
                error = str(e)
                connected = False
        elif apple_id and not (_pending_2fa and _session_account_id == account_id):
            # Cookie-only resume. Do NOT pass the keyring password here — that restarts
            # SRP/login and can push a 2FA prompt just from opening the Archive tab.
            try:
                _migrate_legacy_cookies(account_id)
                api = _create_service(account_id, apple_id, password=None)
                if api.requires_2fa:
                    # Incomplete MFA cookie session — clear quietly; user must Sign in.
                    _api = None
                    _pending_2fa = False
                    _session_account_id = None
                    _2fa_code_requested = False
                    _clear_cookie_files(account_id)
                    requires_2fa = False
                    connected = False
                    error = (
                        f"Previous 2FA for {account.get('label')} expired. "
                        "Sign in again with Apple ID and password to get a fresh code."
                    )
                else:
                    _api = api
                    _pending_2fa = False
                    _session_account_id = account_id
                    _2fa_code_requested = False
                    connected = True
                    trusted = bool(api.is_trusted_session)
            except Exception as e:
                error = str(e)

        return {
            "account_id": account_id,
            "account_label": account.get("label"),
            "apple_id": apple_id,
            "connected": connected,
            "requires_2fa": requires_2fa,
            "delivery_method": delivery,
            "sms_available": sms_available,
            "phone_hint": phone_hint,
            "phones": phones,
            "selected_phone_id": selected_phone_id,
            "preferred_missing": preferred_missing,
            "preferred_last4": preferred_last4,
            "trusted_session": trusted,
            "cookie_dir": str(cookie_dir(account_id)),
            "error": error or _last_2fa_error,
            "accounts": list_accounts(),
        }


def _clear_cookie_files(account_id: int) -> None:
    """Remove pyicloud session/cookie files for one account profile."""
    ensure_data_dirs()
    cookie = cookie_dir(account_id)
    if cookie.exists():
        for child in list(cookie.iterdir()):
            try:
                if child.is_file():
                    child.unlink()
                elif child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
            except OSError as e:
                logger.warning("Could not remove %s: %s", child, e)


def _normalize_apple_id(apple_id: str) -> str:
    # SRP auth hashes the username — Apple expects the canonical lowercase email.
    return apple_id.strip().lower()


def _format_login_error(exc: BaseException, *, label: str, apple_id: str) -> str:
    """Turn pyicloud/Apple auth failures into actionable UI text (never includes password)."""
    parts: list[str] = []
    cur: BaseException | None = exc
    text_blobs: list[str] = []
    while cur is not None:
        text_blobs.append(str(cur))
        cur = cur.__cause__ or cur.__context__
    joined = "\n".join(text_blobs)
    code = None
    for token in ("-20101", "-20209", "-20755", "503", "401"):
        if token in joined:
            code = token
            break

    if "Invalid email/password" in joined or code == "-20101":
        parts.append(
            f"Apple rejected sign-in for {label} ({apple_id}) with “invalid email/password” "
            f"(code {code or '-20101'})."
        )
        parts.append(
            "This often happens even when the same password works on icloud.com, because "
            "third-party API login is stricter than the website."
        )
        parts.append(
            "Try these in order:\n"
            "1) Use the exact Apple ID email (lowercase), not a nickname.\n"
            "2) Use your normal Apple ID password — not an app-specific password.\n"
            "3) Click Reset sign-in (clears saved session cookies), then try once more.\n"
            "4) Sign in once at https://www.icloud.com in a private browser window, then retry here.\n"
            "5) If you just tried several times, wait 15–30 minutes — Apple rate-limits API logins.\n"
            "6) Check appleid.apple.com → Sign-In and Security for a temporary lock / Advanced Data Protection notes."
        )
    elif "503" in joined or "Temporarily Unavailable" in joined:
        parts.append(
            f"Apple auth is temporarily unavailable for {label} ({apple_id}). "
            "Wait ~15 minutes, sign in once at icloud.com, then retry here."
        )
    elif "2FA" in joined or "HSA2" in joined:
        parts.append(f"Two-factor authentication is required for {label} ({apple_id}).")
    else:
        parts.append(f"Could not sign in as {label} ({apple_id}): {exc}")

    return "\n\n".join(parts)


def _create_service(
    account_id: int,
    apple_id: str,
    password: str | None = None,
    *,
    fresh: bool = False,
) -> Any:
    from pyicloud import PyiCloudService

    ensure_data_dirs()
    if fresh:
        _clear_cookie_files(account_id)
    else:
        _migrate_legacy_cookies(account_id)
    _install_pyicloud_sms_prefer()
    return PyiCloudService(
        apple_id,
        password=password,
        cookie_directory=str(cookie_dir(account_id)),
        accept_terms=True,
    )


_pyicloud_sms_patched = False


def _install_pyicloud_sms_prefer() -> None:
    """Ask Apple for SMS 2FA instead of a trusted-device prompt when a phone exists."""
    global _pyicloud_sms_patched
    if _pyicloud_sms_patched:
        return
    from pyicloud import PyiCloudService

    orig = PyiCloudService._request_2fa_code

    def _prefer_sms(self) -> None:
        # Do not PUT here — login() sends one SMS with the id-only payload
        # Apple expects. A constructor PUT can succeed without delivering a text.
        try:
            phone = self._trusted_phone_number()
        except Exception:
            phone = None
        if phone is not None:
            try:
                self._clear_trusted_device_bridge_state()
            except Exception:
                pass
            try:
                self._set_two_factor_delivery_state("sms")
            except Exception:
                pass
            return
        orig(self)

    PyiCloudService._request_2fa_code = _prefer_sms  # type: ignore[method-assign]
    _pyicloud_sms_patched = True


def _phone_payload(api: Any) -> dict[str, Any] | None:
    """Return Apple's trusted-phone payload, or None if SMS is not available."""
    try:
        phone_fn = getattr(api, "_trusted_phone_number", None)
        if not callable(phone_fn):
            return None
        phone = phone_fn()
        if phone is None:
            return None
        payload_fn = getattr(phone, "as_phone_number_payload", None)
        if not callable(payload_fn):
            return None
        payload = payload_fn()
        if isinstance(payload, dict) and payload.get("id") is not None:
            return payload
    except Exception:
        logger.debug("Could not inspect trusted phone number", exc_info=True)
    return None


def _sms_available(api: Any) -> bool:
    return bool(_list_trusted_phones(api))


def _phone_state(api: Any, account: dict[str, Any] | None = None) -> dict[str, Any]:
    phones = _list_trusted_phones(api)
    pref = _preferred_last4(account)
    selected = _sms_phone_id
    if selected is None:
        selected = _choose_phone_id(api, account=account)
    hint = None
    for p in phones:
        if selected is not None and str(p["id"]) == str(selected):
            hint = p.get("hint") or (
                f"***-***-{p['last4']}"
                if p.get("last4")
                else (f"Number ending in {p['suffix']}" if p.get("suffix") else None)
            )
            break
    if hint is None:
        hint = _phone_hint(api)
    missing = bool(pref) and not any(_phone_matches_preferred(p, pref) for p in phones)
    return {
        "phones": phones,
        "selected_phone_id": selected,
        "phone_hint": hint,
        "preferred_last4": pref,
        "preferred_missing": missing,
    }


def _phone_hint(api: Any) -> str | None:
    """Masked number Apple associated with this 2FA challenge, if present."""
    phones = _list_trusted_phones(api)
    if not phones:
        return None
    selected = _sms_phone_id
    for p in phones:
        if selected is not None and str(p["id"]) == str(selected):
            return p.get("hint")
    return phones[0].get("hint")


def _list_trusted_phones(api: Any) -> list[dict[str, Any]]:
    """Trusted numbers Apple exposed for the current 2FA challenge."""
    data = getattr(api, "_auth_data", None)
    blobs: list[Any] = []

    def _add_one(value: Any) -> None:
        if isinstance(value, dict):
            blobs.append(value)

    def _add_many(value: Any) -> None:
        if isinstance(value, list):
            blobs.extend(value)

    if isinstance(data, dict):
        pnv = data.get("phoneNumberVerification")
        if not isinstance(pnv, dict):
            pnv = {}
        bridge = data.get("bridgeInitiateData")
        if not isinstance(bridge, dict):
            bridge = {}
        bpnv = bridge.get("phoneNumberVerification")
        if not isinstance(bpnv, dict):
            bpnv = {}
        # Lists first — Apple's default phoneNumber can be a different (wrong) line.
        _add_many(pnv.get("trustedPhoneNumbers"))
        _add_many(bpnv.get("trustedPhoneNumbers"))
        _add_one(pnv.get("trustedPhoneNumber"))
        _add_one(bpnv.get("trustedPhoneNumber"))
        _add_one(data.get("trustedPhoneNumber"))
        _add_one(pnv.get("phoneNumber"))
        _add_one(bpnv.get("phoneNumber"))
        _add_one(data.get("phoneNumber"))
    seen: set[Any] = set()
    out: list[dict[str, Any]] = []
    for blob in blobs:
        if not isinstance(blob, dict):
            continue
        pid = blob.get("id")
        if pid is None or pid in seen:
            continue
        seen.add(pid)
        last_two = blob.get("lastTwoDigits")
        last_two = str(last_two) if last_two is not None else ""
        raw = str(blob.get("numberWithDialCode") or blob.get("obfuscatedNumber") or "")
        digits = "".join(ch for ch in raw if ch.isdigit()) or last_two
        last4 = digits[-4:] if len(digits) >= 4 else None
        suffix = last_two[-2:] if len(last_two) >= 2 else (digits[-2:] if len(digits) >= 2 else None)
        hint = blob.get("obfuscatedNumber") or blob.get("numberWithDialCode")
        if hint:
            hint = str(hint).replace("•", "*")
        elif suffix:
            hint = f"Number ending in {suffix}"
        non_fteu = blob.get("nonFTEU")
        if not isinstance(non_fteu, bool):
            non_fteu = None
        push_mode = blob.get("pushMode")
        out.append(
            {
                "id": pid,
                "last4": last4,
                "suffix": suffix,
                "hint": hint,
                "non_fteu": non_fteu,
                "push_mode": str(push_mode) if push_mode else "sms",
            }
        )
    if not out:
        payload = _phone_payload(api)
        if payload and payload.get("id") is not None:
            out.append(
                {
                    "id": payload["id"],
                    "last4": None,
                    "suffix": None,
                    "hint": None,
                    "non_fteu": payload.get("nonFTEU"),
                    "push_mode": "sms",
                }
            )
    return out


def _preferred_last4(account: dict[str, Any] | None = None) -> str | None:
    label = (account or {}).get("label") or ""
    return _PREFERRED_SMS_LAST4.get(label)


def _phone_matches_preferred(phone: dict[str, Any], pref: str | None) -> bool:
    if not pref:
        return False
    if phone.get("last4") == pref:
        return True
    suffix = str(phone.get("suffix") or "")
    return bool(suffix) and pref.endswith(suffix)


def _choose_phone_id(
    api: Any,
    *,
    phone_id: Any = None,
    account: dict[str, Any] | None = None,
) -> Any:
    phones = _list_trusted_phones(api)
    if phone_id is not None:
        for p in phones:
            if str(p["id"]) == str(phone_id):
                return p["id"]
        payload = _phone_payload(api)
        if payload and str(payload.get("id")) == str(phone_id):
            return payload["id"]
        raise ValueError("That phone number is not a trusted number on this Apple ID.")
    pref = _preferred_last4(account)
    if pref:
        for p in phones:
            if _phone_matches_preferred(p, pref):
                return p["id"]
    if _sms_phone_id is not None:
        for p in phones:
            if str(p["id"]) == str(_sms_phone_id):
                return p["id"]
    if phones:
        return phones[0]["id"]
    payload = _phone_payload(api)
    if payload:
        return payload.get("id")
    return None


def _sms_put_body(phone: dict[str, Any]) -> dict[str, Any]:
    inner: dict[str, Any] = {"id": phone["id"]}
    if phone.get("non_fteu") is not None:
        inner["nonFTEU"] = bool(phone["non_fteu"])
    return {"phoneNumber": inner, "mode": phone.get("push_mode") or "sms"}


def _parse_apple_json(text: str) -> dict[str, Any] | None:
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _ingest_apple_phone_error(api: Any, text: str) -> None:
    """Pull trustedPhoneNumbers out of an Apple 423/4xx body into auth_data."""
    data = _parse_apple_json(text)
    if not data:
        return
    auth = getattr(api, "_auth_data", None)
    if not isinstance(auth, dict):
        api._auth_data = {}
        auth = api._auth_data
    pnv = auth.get("phoneNumberVerification")
    if not isinstance(pnv, dict):
        pnv = {}
        auth["phoneNumberVerification"] = pnv
    nums = data.get("trustedPhoneNumbers")
    if isinstance(nums, list) and nums:
        pnv["trustedPhoneNumbers"] = nums
    one = data.get("trustedPhoneNumber") or data.get("phoneNumber")
    if isinstance(one, dict):
        pnv["trustedPhoneNumber"] = one


def _sms_http_error(status: int, text: str) -> str:
    if status == 423:
        return (
            "Apple locked SMS for a few minutes (too many tries, or the wrong number was used). "
            "Wait 2–3 minutes, click Reset sign-in, Sign in once, then Send SMS."
        )
    if status == 412:
        return "Apple did not accept that code. Enter the newest 6-digit SMS."
    if _parse_apple_json(text):
        return f"Apple HTTP {status}"
    snippet = (text or "").replace("\n", " ").strip()
    if len(snippet) > 160:
        snippet = snippet[:160] + "…"
    return f"Apple HTTP {status}" + (f": {snippet}" if snippet else "")


def _sms_verify_outcome(status: int, text: str) -> tuple[bool, str | None]:
    """Interpret Apple's SMS-verify HTTP body. 412 + valid:true is success."""
    data = _parse_apple_json(text) or {}
    sc = data.get("securityCode")
    if not isinstance(sc, dict):
        sc = {}
    if sc.get("valid") is True:
        return True, None
    if sc.get("securityCodeLocked") or sc.get("tooManyCodesValidated"):
        return False, (
            "Apple locked this sign-in after too many code attempts. "
            "Wait a few minutes, click Reset sign-in, Sign in once, then Send SMS."
        )
    if sc.get("securityCodeCooldown") or sc.get("tooManyCodesSent"):
        return False, "Apple paused SMS for a few minutes. Wait, then click Send SMS once."
    if sc.get("valid") is False:
        return False, "That code is not valid. Enter the newest 6-digit SMS."
    if status in (200, 204, 409):
        return True, None
    if status == 412 and (sc or data.get("phoneNumber") or data.get("hsa2Account")):
        return True, None
    return False, _sms_http_error(status, text)


def _send_sms_request(api: Any, phone_id: Any = None) -> str | None:
    """Ask Apple to text a 2FA code using the trusted-number payload Apple expects."""
    global _sms_phone_id
    if not _list_trusted_phones(api):
        _refresh_mfa_phone(api)
    account = get_account(_session_account_id) if _session_account_id else None
    chosen = _choose_phone_id(api, phone_id=phone_id, account=account)
    if chosen is None:
        raise ValueError("No trusted phone number is available for SMS.")
    session = getattr(api, "session", None)
    endpoint = getattr(api, "_auth_endpoint", None)
    get_headers = getattr(api, "_get_auth_headers", None)
    if isinstance(endpoint, str) and session is not None and callable(get_headers):
        headers = get_headers(
            {
                "Accept": "application/json",
                "Content-Type": "application/json; charset=utf-8",
            }
        )
        url = f"{endpoint}/verify/phone"
        raw = getattr(session, "request_raw", None)
        pref = _preferred_last4(account)
        last_err = None
        tried: set[str] = set()
        remaining = [str(chosen)]
        while remaining:
            pid = remaining.pop(0)
            if pid in tried:
                continue
            tried.add(pid)
            phones_by_id = {str(p["id"]): p for p in _list_trusted_phones(api)}
            phone = phones_by_id.get(pid) or {"id": chosen, "push_mode": "sms"}
            body = _sms_put_body(phone)
            try:
                if callable(raw):
                    resp = raw("PUT", url, json=body, headers=headers)
                    status = int(getattr(resp, "status_code", 0) or 0)
                    text = getattr(resp, "text", None) or ""
                    if status == 423:
                        _ingest_apple_phone_error(api, text)
                        last_err = _sms_http_error(status, text)
                        logger.info(
                            "Apple SMS 423 for phone id %s; looking for preferred trusted number",
                            pid,
                        )
                        for p in _list_trusted_phones(api):
                            sp = str(p["id"])
                            if sp in tried or sp in remaining:
                                continue
                            if pref and _phone_matches_preferred(p, pref):
                                remaining.insert(0, sp)
                            elif not pref:
                                remaining.append(sp)
                        continue
                    if status >= 400:
                        last_err = _sms_http_error(status, text)
                        continue
                else:
                    session.put(url, json=body, headers=headers)
            except Exception as e:
                last_err = _apple_error_text(e)
                continue
            _sms_phone_id = phone["id"]
            clearer = getattr(api, "_clear_trusted_device_bridge_state", None)
            if callable(clearer):
                clearer()
            setter = getattr(api, "_set_two_factor_delivery_state", None)
            if callable(setter):
                setter("sms")
            try:
                api._two_factor_delivery_method = "sms"
                api.two_factor_delivery_method = "sms"
            except Exception:
                pass
            logger.info(
                "Requested iCloud SMS 2FA for phone id %s nonFTEU=%s",
                phone["id"],
                phone.get("non_fteu"),
            )
            return _phone_hint(api)
        raise ValueError(last_err or "Apple did not send an SMS.")
    send = getattr(api, "_request_sms_2fa_code", None)
    if callable(send):
        send(notice="Using SMS verification.")
        _prefer_sms_delivery(api, request_new=False)
        _sms_phone_id = chosen
        return _phone_hint(api)
    raise ValueError("This iCloud library cannot request an SMS 2FA code")


def _prefer_sms_delivery(api: Any, *, request_new: bool = False) -> None:
    """Mark the live challenge as SMS so validation hits Apple's phone endpoint."""
    if request_new:
        send = getattr(api, "_request_sms_2fa_code", None)
        if callable(send):
            send(notice="Using SMS verification.")
            return
    setter = getattr(api, "_set_two_factor_delivery_state", None)
    if callable(setter):
        try:
            setter("sms")
        except Exception:
            pass
    for attr in ("_two_factor_delivery_method", "two_factor_delivery_method"):
        try:
            setattr(api, attr, "sms")
        except Exception:
            pass


def _apple_error_text(exc: BaseException) -> str:
    reason = getattr(exc, "reason", None)
    code = getattr(exc, "code", None)
    text = str(reason or exc).strip() or type(exc).__name__
    parsed = _parse_apple_json(text)
    if parsed:
        _ok, err = _sms_verify_outcome(int(code or 0), text)
        if err:
            return err
        return f"Apple HTTP {code}" if code is not None else "Apple rejected that request."
    if len(text) > 200:
        text = text[:200] + "…"
    if code is not None and str(code) not in text:
        text = f"{text} (Apple {code})"
    return text


def _verify_sms_code(api: Any, code: str) -> tuple[bool, str | None]:
    """POST the SMS code to Apple. Does not call trust_session."""
    payload = _phone_payload(api)
    phones = {str(p["id"]): p for p in _list_trusted_phones(api)}
    phone_id = _sms_phone_id or (payload or {}).get("id")
    if phone_id is None and phones:
        phone_id = next(iter(phones.values()))["id"]
    if phone_id is None:
        return False, "No trusted phone number on this Apple ID"
    phone = phones.get(str(phone_id)) or {"id": phone_id, "push_mode": "sms"}
    session = getattr(api, "session", None)
    endpoint = getattr(api, "_auth_endpoint", None)
    get_headers = getattr(api, "_get_auth_headers", None)
    if isinstance(endpoint, str) and session is not None and callable(get_headers):
        headers = get_headers({"Accept": "application/json, text/javascript"})
        inner: dict[str, Any] = {"id": phone["id"]}
        if phone.get("non_fteu") is not None:
            inner["nonFTEU"] = bool(phone["non_fteu"])
        body = {
            "phoneNumber": inner,
            "securityCode": {"code": code},
            "mode": phone.get("push_mode") or "sms",
        }
        url = f"{endpoint}/verify/phone/securitycode"
        raw = getattr(session, "request_raw", None)
        status = 0
        text = ""
        try:
            if callable(raw):
                resp = raw("POST", url, json=body, headers=headers)
                status = int(getattr(resp, "status_code", 0) or 0)
                text = getattr(resp, "text", None) or ""
            else:
                resp = session.post(url, json=body, headers=headers)
                status = int(getattr(resp, "status_code", 200) or 200)
                text = getattr(resp, "text", None) or ""
        except Exception as e:
            status = int(getattr(e, "code", 0) or 0)
            text = str(getattr(e, "reason", None) or e)
            ok, err = _sms_verify_outcome(status, text)
            if ok:
                _ingest_apple_phone_error(api, text)
                logger.info(
                    "Apple accepted SMS 2FA for phone id %s via HTTP %s body",
                    phone_id,
                    status,
                )
                return True, None
            logger.info("SMS 2FA code rejected: %s", err)
            return False, err or _apple_error_text(e)
        _ingest_apple_phone_error(api, text)
        ok, err = _sms_verify_outcome(status, text)
        if ok:
            logger.info(
                "Apple accepted SMS 2FA for phone id %s (HTTP %s)",
                phone_id,
                status,
            )
            return True, None
        logger.info("SMS 2FA code rejected: HTTP %s %s", status, err)
        return False, err
    validate = getattr(api, "_validate_sms_code", None)
    if callable(validate):
        try:
            validate(code)
            return True, None
        except Exception as e:
            return False, _apple_error_text(e)
    return False, "SMS verification is not available"


def _two_factor_delivery(api: Any) -> str:
    return getattr(api, "two_factor_delivery_method", None) or "unknown"


def _requires_security_key(api: Any) -> bool:
    if _two_factor_delivery(api) == "security_key":
        return True
    names = getattr(api, "security_key_names", None)
    return isinstance(names, (list, tuple)) and len(names) > 0


def _refresh_mfa_phone(api: Any) -> None:
    """Re-read Apple's MFA options so SMS can find a trusted phone number."""
    getter = getattr(api, "_get_mfa_auth_options", None)
    if not callable(getter):
        return
    try:
        api._auth_data = getter()
    except Exception as e:
        logger.warning("Could not refresh MFA auth options: %s", e)


def _request_legacy_2fa(api: Any) -> None:
    """Re-send the constructor-style device/SMS prompt without starting HSA2 bridge."""
    fn = getattr(api, "_request_2fa_code", None)
    if not callable(fn):
        raise ValueError("This iCloud library cannot resend a 2FA code")
    fn()


def _2fa_required_payload(
    *,
    aid: int,
    apple_id: str | None,
    label: str,
    delivery: str,
    notice: str | None,
    sms_available: bool,
    kind: str = "login",
    phone_hint: str | None = None,
    phone_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ps = phone_state or {}
    phone_hint = ps.get("phone_hint") or phone_hint
    dest = f" ({phone_hint})" if phone_hint else ""
    if delivery == "sms" or kind == "sms" or sms_available:
        if kind == "sms":
            message = (
                f"Sent a new SMS{dest} for {label} ({apple_id}). "
                "Enter that text — wait up to a minute before requesting another."
            )
        elif kind == "resend":
            message = (
                f"Requested another SMS{dest} for {label}. "
                "Enter only the newest text-message code."
            )
        else:
            message = (
                f"Asked Apple to text a code{dest} for {label} ({apple_id}). "
                "Enter that SMS, then Submit 2FA. If nothing arrives, click Send SMS."
            )
    elif kind == "resend":
        message = (
            f"Requested another 2FA code for {label}. "
            "Enter only the newest 6-digit code."
        )
    else:
        message = (
            f"2FA required for {label} ({apple_id}). "
            "Enter the 6-digit code Apple sent for that Apple ID."
        )
    if ps.get("preferred_missing") and ps.get("preferred_last4"):
        message += (
            f" Apple does not have a trusted number ending in {ps['preferred_last4']} "
            f"on {label}’s Apple ID, so the text goes to {phone_hint or 'the family organizer’s number'}. "
            f"To text {label}’s own phone, add it at account.apple.com → Sign-In and Security."
        )
    if notice:
        message = f"{message} {notice}"
    return {
        "status": "2fa_required",
        "account_id": aid,
        "apple_id": apple_id,
        "delivery_method": delivery,
        "sms_available": sms_available,
        "phone_hint": phone_hint,
        "phones": ps.get("phones") or [],
        "selected_phone_id": ps.get("selected_phone_id"),
        "preferred_last4": ps.get("preferred_last4"),
        "preferred_missing": bool(ps.get("preferred_missing")),
        "message": message,
    }


def login(apple_id: str, password: str, *, account_id: int | None = None) -> dict[str, Any]:
    """Start Apple ID login for the active (or specified) family profile."""
    global _api, _pending_2fa, _session_account_id, _2fa_code_requested, _last_2fa_error, _sms_phone_id
    apple_id = _normalize_apple_id(apple_id)
    # Keep interior spaces (rare); only trim ends — password managers sometimes add a trailing newline
    password = password.replace("\r", "").replace("\n", "").strip()
    if not apple_id or not password:
        raise ValueError("Apple ID and password are required")

    aid = account_id if account_id is not None else get_active_account_id()
    account = get_account(aid)
    label = account.get("label") or "account"

    with _lock:
        # Drop in-memory session and on-disk cookies so a prior failed attempt
        # (or cookies from a different Apple ID in this profile folder) cannot poison SRP.
        _api = None
        _pending_2fa = False
        _session_account_id = None
        _2fa_code_requested = False
        _last_2fa_error = None
        _sms_phone_id = None
        _clear_cookie_files(aid)

        try:
            api = _create_service(aid, apple_id, password=password, fresh=True)
        except Exception as e:
            _api = None
            _pending_2fa = False
            _session_account_id = None
            _2fa_code_requested = False
            _clear_cookie_files(aid)
            raise ValueError(_format_login_error(e, label=label, apple_id=apple_id)) from e

        update_account_apple_id(aid, apple_id)
        set_setting(SETTING_ACTIVE_ACCOUNT, str(aid))
        set_setting(SETTING_APPLE_ID_LEGACY, apple_id)
        _store_password(apple_id, password)
        _api = api
        _session_account_id = aid

        if api.requires_2fa:
            _pending_2fa = True
            _2fa_code_requested = True
            notice = getattr(api, "two_factor_delivery_notice", None)
            if _sms_available(api):
                try:
                    hint = _send_sms_request(api)
                    if hint:
                        notice = f"Text should arrive at {hint}."
                except Exception as e:
                    logger.warning("SMS send on login failed for %s: %s", label, e)
                    notice = (
                        f"Could not send SMS automatically ({e}). "
                        "Click Send SMS."
                    )
            delivery = _two_factor_delivery(api)
            if _requires_security_key(api):
                _api = None
                _pending_2fa = False
                _session_account_id = None
                _2fa_code_requested = False
                raise ValueError(
                    f"2FA for {label} requires a hardware security key. "
                    "This app can't complete security-key prompts — temporarily "
                    "use a trusted device code / SMS on account.apple.com, or "
                    "approve from a device that doesn't require the key, then retry."
                )
            return _2fa_required_payload(
                aid=aid,
                apple_id=apple_id,
                label=label,
                delivery=delivery,
                notice=notice,
                sms_available=_sms_available(api),
                kind="login",
                phone_state=_phone_state(api, account),
            )

        if not api.is_trusted_session:
            try:
                api.trust_session()
            except Exception as e:
                logger.warning("trust_session failed: %s", e)

        _pending_2fa = False
        return {
            "status": "connected",
            "account_id": aid,
            "apple_id": apple_id,
            "trusted_session": bool(api.is_trusted_session),
            "message": f"Signed in as {label} ({apple_id}).",
        }


def resend_2fa() -> dict[str, Any]:
    """Re-trigger Apple's 2FA push/SMS for the in-progress login."""
    global _2fa_code_requested
    with _lock:
        if _api is None:
            raise ValueError("No login in progress — sign in with Apple ID first")
        if not (getattr(_api, "requires_2fa", False) or _pending_2fa):
            raise ValueError("No 2FA challenge is waiting — sign in again")
        account = get_account(_session_account_id)
        label = account.get("label") or "account"
        try:
            if _sms_available(_api):
                _send_sms_request(_api)
            else:
                _request_legacy_2fa(_api)
            _2fa_code_requested = True
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"Could not resend 2FA code: {e}") from e
        delivery = _two_factor_delivery(_api)
        notice = getattr(_api, "two_factor_delivery_notice", None)
        return _2fa_required_payload(
            aid=int(account["id"]),
            apple_id=account.get("apple_id"),
            label=label,
            delivery=delivery,
            notice=notice,
            sms_available=_sms_available(_api),
            kind="resend",
            phone_state=_phone_state(_api, account),
        )


def request_2fa_sms(phone_id: Any = None) -> dict[str, Any]:
    """Switch the in-progress 2FA challenge to SMS (child IDs / failed device prompts)."""
    global _2fa_code_requested
    with _lock:
        if _api is None:
            raise ValueError("No login in progress — sign in with Apple ID first")
        if not (getattr(_api, "requires_2fa", False) or _pending_2fa):
            raise ValueError("No 2FA challenge is waiting — sign in again")
        account = get_account(_session_account_id)
        label = account.get("label") or "account"
        try:
            hint = _send_sms_request(_api, phone_id=phone_id)
            _2fa_code_requested = True
        except Exception as e:
            raise ValueError(f"Could not send SMS 2FA for {label}: {e}") from e
        delivery = _two_factor_delivery(_api)
        notice = getattr(_api, "two_factor_delivery_notice", None)
        if hint:
            notice = f"Text should arrive at {hint}."
        return _2fa_required_payload(
            aid=int(account["id"]),
            apple_id=account.get("apple_id"),
            label=label,
            delivery=delivery or "sms",
            notice=notice,
            sms_available=True,
            kind="sms",
            phone_state=_phone_state(_api, account),
        )


def submit_2fa(code: str) -> dict[str, Any]:
    global _pending_2fa, _2fa_code_requested, _last_2fa_error
    code = (code or "").strip().replace(" ", "")
    if not code:
        raise ValueError("2FA code is required")
    if not code.isdigit() or len(code) not in (6, 4):
        raise ValueError("Enter the 6-digit SMS code")

    with _lock:
        if _api is None:
            raise ValueError("No login in progress — sign in with Apple ID first")
        delivery = _two_factor_delivery(_api)
        sms_ok = _sms_available(_api) or delivery == "sms"
        ok = False
        apple_err: str | None = None
        if sms_ok:
            _prefer_sms_delivery(_api, request_new=False)
            ok, apple_err = _verify_sms_code(_api, code)
            delivery = "sms"
        if not ok and not sms_ok:
            try:
                ok = bool(_api.validate_2fa_code(code))
            except Exception as e:
                apple_err = _apple_error_text(e)
                logger.info("2FA device verify failed: %s", apple_err)
                ok = False
        if not ok:
            _last_2fa_error = apple_err
            detail = f" {apple_err}" if apple_err else ""
            raise ValueError(
                "That code was rejected."
                + detail
                + " Enter the newest SMS for this sign-in. "
                "Do not click Sign in again after the text arrives — that starts a new code."
            )
        try:
            _api.trust_session()
        except Exception as e:
            logger.warning("trust_session failed after accepted 2FA: %s", e)
        _pending_2fa = False
        _2fa_code_requested = False
        _last_2fa_error = None
        account = get_account(_session_account_id)
        return {
            "status": "connected",
            "account_id": account["id"],
            "apple_id": account.get("apple_id"),
            "trusted_session": bool(_api.is_trusted_session),
            "delivery_method": delivery,
            "message": f"Two-factor authentication succeeded for {account['label']}.",
        }


def logout(*, clear_apple_id: bool = False) -> dict[str, Any]:
    """Sign out the active profile only (other family sessions stay intact)."""
    global _api, _pending_2fa, _session_account_id, _2fa_code_requested, _last_2fa_error, _sms_phone_id
    with _lock:
        account = get_account()
        account_id = int(account["id"])
        apple_id = (account.get("apple_id") or "").strip() or None
        _api = None
        _pending_2fa = False
        _session_account_id = None
        _2fa_code_requested = False
        _last_2fa_error = None
        _sms_phone_id = None
        if apple_id:
            _clear_password(apple_id)
        if clear_apple_id:
            update_account_apple_id(account_id, None)
        _clear_cookie_files(account_id)
        return {
            "status": "logged_out",
            "account_id": account_id,
            "account_label": account.get("label"),
            "apple_id": apple_id,
            "message": f"Signed out {account.get('label')}. Sign in again with their Apple ID.",
        }


def reset_auth() -> dict[str, Any]:
    """Clear stuck 2FA / expired session for the active profile."""
    return logout(clear_apple_id=False)


def get_api(*, require: bool = True) -> Any:
    """Return authenticated PyiCloudService for the active account."""
    with _lock:
        account_id = get_active_account_id()
        if _api is not None and _session_account_id == account_id:
            try:
                if not _api.requires_2fa and not _pending_2fa:
                    return _api
            except Exception:
                pass
        status = auth_status()
        if _api is not None and status.get("connected") and _session_account_id == account_id:
            return _api
        if require:
            if status.get("requires_2fa"):
                raise ValueError("Two-factor authentication required")
            label = status.get("account_label") or "this profile"
            raise ValueError(f"Not connected — sign in as {label} on the Archive tab")
        return _api


def list_photo_libraries(*, include_shared: bool = True) -> list[tuple[str, Any]]:
    """
    Return (library_key, library) pairs for inventory/archive.

    - ``root`` — private iCloud Photos library
    - ``shared:<zone>`` — iCloud Shared Photo Library zones (``SharedSync-*``)
    Skips the legacy Shared Albums / streams adapter keyed ``shared``.
    """
    api = get_api()
    photos = api.photos
    if hasattr(photos, "_libraries"):
        photos._libraries = None
    libraries = getattr(photos, "libraries", None) or {"root": photos}
    ordered: list[tuple[str, Any]] = []
    seen: set[str] = set()

    def _add(key: str, lib: Any) -> None:
        if key in seen or lib is None:
            return
        seen.add(key)
        ordered.append((key, lib))

    if "root" in libraries:
        _add("root", libraries["root"])
    else:
        _add("root", photos)

    for key, lib in libraries.items():
        if key in ("root", "shared"):
            continue
        if key.startswith("shared:"):
            if not include_shared:
                continue
            zone = getattr(lib, "zone_id", None) or {}
            zone_name = zone.get("zoneName") if isinstance(zone, dict) else None
            if zone_name and not str(zone_name).startswith("SharedSync-"):
                logger.info("Skipping non-SharedSync zone %s", key)
                continue
            _add(key, lib)
            continue
        _add(key, lib)

    return ordered


def describe_photo_libraries() -> list[dict[str, Any]]:
    try:
        libs = list_photo_libraries(include_shared=True)
    except Exception as e:
        return [{"key": "root", "scope": "private", "error": str(e)}]
    out: list[dict[str, Any]] = []
    for key, lib in libs:
        scope = getattr(lib, "scope", None) or (
            "shared-library" if key.startswith("shared:") else "private"
        )
        out.append({"key": key, "scope": scope})
    return out


def iter_library_assets(
    *, include_shared: bool = True
) -> Iterator[tuple[str, Any]]:
    for key, lib in list_photo_libraries(include_shared=include_shared):
        album = getattr(lib, "all", None)
        if album is None:
            continue
        logger.info("Scanning iCloud library %s", key)
        yield from ((key, photo) for photo in album)


def estimate_library_asset_count(*, include_shared: bool = True) -> int:
    """Best-effort total from iCloud album lengths (for progress %)."""
    total = 0
    for key, lib in list_photo_libraries(include_shared=include_shared):
        album = getattr(lib, "all", None)
        if album is None:
            continue
        try:
            n = len(album)
        except Exception as e:
            logger.debug("len(%s) failed: %s", key, e)
            continue
        if isinstance(n, int) and n > 0:
            total += n
    return total


def iter_all_assets(*, include_shared: bool = True) -> Iterator[Any]:
    for _key, photo in iter_library_assets(include_shared=include_shared):
        yield photo


def asset_to_row(photo: Any, *, library_key: str = "root") -> dict[str, Any]:
    created = photo.created
    if isinstance(created, datetime):
        if created.tzinfo is None:
            created_iso = created.replace(tzinfo=timezone.utc).isoformat()
        else:
            created_iso = created.astimezone(timezone.utc).isoformat()
    else:
        created_iso = None
    item_type = getattr(photo, "item_type", None) or "image"
    if item_type == "movie":
        item_type = "video"
    size = int(photo.size or 0)
    return {
        "asset_id": str(photo.id),
        "filename": str(photo.filename or photo.id),
        "created_at": created_iso,
        "size": size,
        "item_type": item_type,
        "is_live_photo": 1 if getattr(photo, "is_live_photo", False) else 0,
        "library_key": library_key or "root",
    }


def find_asset(asset_id: str, *, library_key: str | None = None) -> Any | None:
    libs = list_photo_libraries(include_shared=True)
    if library_key:
        libs = [(k, lib) for k, lib in libs if k == library_key] or libs

    for key, lib in libs:
        album = getattr(lib, "all", None)
        if album is None:
            continue
        try:
            photo = album.get(asset_id)
            if photo is not None:
                return photo
        except Exception as e:
            logger.debug("library %s album.get(%s) failed: %s", key, asset_id, e)

    for key, photo in iter_library_assets(include_shared=True):
        if library_key and key != library_key:
            continue
        if str(photo.id) == asset_id:
            return photo
    return None


def ensure_thumb(asset_id: str, *, item_type: str | None = None) -> Path:
    """
    Download and cache a small JPEG/preview for the asset under data/icloud_thumbs/.
    Returns the local path (may be a generated video placeholder).
    """
    ensure_data_dirs()
    account_id = get_active_account_id()
    dest = ICLOUD_THUMBS_DIR / f"a{account_id}" / f"{asset_id}.jpg"
    # Legacy flat thumb path
    legacy = ICLOUD_THUMBS_DIR / f"{asset_id}.jpg"
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    if legacy.exists() and legacy.stat().st_size > 0:
        return legacy

    photo = find_asset(asset_id)
    if photo is None:
        raise FileNotFoundError(f"iCloud asset not found: {asset_id}")

    versions = ("thumb", "medium", "thumb_image", "original")
    data: bytes | None = None
    last_err: Exception | None = None
    for ver in versions:
        try:
            url = photo.download_url(ver)
            if not url:
                continue
            api = get_api()
            with api.session.get(url, stream=True, timeout=60) as resp:
                resp.raise_for_status()
                chunks = []
                for chunk in resp.iter_content(chunk_size=256 * 1024):
                    if chunk:
                        chunks.append(chunk)
                data = b"".join(chunks)
            if data:
                break
        except Exception as e:
            last_err = e
            continue

    if not data:
        try:
            from app.services.hashing import make_video_thumbnail

            dest.parent.mkdir(parents=True, exist_ok=True)
            make_video_thumbnail(dest)
            return dest
        except Exception as e:
            raise RuntimeError(
                f"Could not download thumb for {asset_id}: {last_err or e}"
            ) from e

    try:
        from io import BytesIO

        from PIL import Image

        try:
            from pillow_heif import register_heif_opener

            register_heif_opener()
        except ImportError:
            pass

        img = Image.open(BytesIO(data))
        img = img.convert("RGB")
        img.thumbnail((320, 320))
        dest.parent.mkdir(parents=True, exist_ok=True)
        img.save(dest, format="JPEG", quality=80)
    except Exception:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

    return dest


def _guess_media_type(filename: str | None, item_type: str | None = None) -> str:
    name = (filename or "").lower()
    if name.endswith(".mov"):
        return "video/quicktime"
    if name.endswith(".mp4") or name.endswith(".m4v"):
        return "video/mp4"
    if name.endswith(".heic") or name.endswith(".heif"):
        return "image/heic"
    if name.endswith(".png"):
        return "image/png"
    if name.endswith(".webp"):
        return "image/webp"
    if name.endswith(".jpg") or name.endswith(".jpeg"):
        return "image/jpeg"
    if (item_type or "").lower() in ("video", "movie"):
        return "video/mp4"
    return "application/octet-stream"


def open_asset_media_stream(
    asset_id: str,
    *,
    range_header: str | None = None,
    prefer_medium: bool = True,
) -> tuple[Any, str, Any]:
    """
    Open a streaming HTTP response for an iCloud asset (via the authenticated session).

    Returns (photo, version_key, requests.Response). Caller must close the response.
    Prefers ``medium`` for faster preview when available, else ``original``.
    """
    photo = find_asset(asset_id)
    if photo is None:
        raise FileNotFoundError(f"iCloud asset not found: {asset_id}")

    order = ("medium", "original") if prefer_medium else ("original", "medium")
    url = None
    version = "original"
    for key in order:
        try:
            candidate = photo.download_url(key)
        except Exception:
            candidate = None
        if candidate:
            url = candidate
            version = key
            break
    if not url:
        raise RuntimeError(f"No download URL for {getattr(photo, 'filename', asset_id)}")

    api = get_api()
    headers: dict[str, str] = {}
    if range_header:
        headers["Range"] = range_header
    resp = api.session.get(url, stream=True, timeout=120, headers=headers or None)
    if resp.status_code not in (200, 206):
        resp.close()
        raise RuntimeError(
            f"iCloud media HTTP {resp.status_code} for {getattr(photo, 'filename', asset_id)}"
        )
    return photo, version, resp


def download_asset_to_path(photo: Any, dest: Path) -> int:
    """Stream original bytes to dest. Returns bytes written."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = photo.download_url("original")
    if not url:
        data = photo.download("original")
        if data is None:
            raise RuntimeError(f"No download URL for {getattr(photo, 'filename', photo.id)}")
        dest.write_bytes(data)
        return len(data)

    api = get_api()
    with api.session.get(url, stream=True) as resp:
        resp.raise_for_status()
        written = 0
        with dest.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
                    written += len(chunk)
    return written


def safe_filename(name: str, asset_id: str) -> str:
    raw = unquote(name or asset_id)
    cleaned = "".join(c if c.isalnum() or c in "._- ()[]" else "_" for c in raw).strip()
    if not cleaned or cleaned in (".", ".."):
        cleaned = asset_id
    if len(cleaned) > 120:
        stem = Path(cleaned).stem[:80]
        suffix = Path(cleaned).suffix[:20]
        cleaned = f"{stem}{suffix}"
    return cleaned


def delete_asset(photo: Any) -> None:
    """Move asset to iCloud Recently Deleted."""
    photo.delete()


def ensure_icloud_gitignore() -> None:
    ensure_data_dirs()
    gi = ICLOUD_DIR / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n!.gitignore\n", encoding="utf-8")
