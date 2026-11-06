"""iCloud auth — mocked pyicloud / keyring only."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from app.database import connect
from app.services import icloud_client
from tests.helpers import make_mock_icloud_api, make_mock_phone


def test_list_family_accounts(client):
    r = client.get("/api/icloud/accounts")
    assert r.status_code == 200
    data = r.json()
    labels = {a["label"] for a in data["accounts"]}
    assert {"Dan", "Jess", "Graham", "Lillian"} <= labels
    assert data["active_id"] in {a["id"] for a in data["accounts"]}


def test_select_account(client):
    r = client.post("/api/icloud/accounts/select", json={"account_id": 3})
    assert r.status_code == 200
    assert r.json()["account_id"] == 3
    assert r.json()["account_label"] == "Graham"


def test_login_does_not_start_second_2fa_challenge(client, monkeypatch):
    api = make_mock_icloud_api(requires_2fa=True)
    monkeypatch.setattr(icloud_client, "_store_password", lambda *a, **k: None)
    monkeypatch.setattr(icloud_client, "_clear_cookie_files", lambda *a, **k: None)

    with patch.object(icloud_client, "_create_service", return_value=api):
        r = client.post(
            "/api/icloud/auth",
            json={
                "apple_id": "graham.custodio@icloud.com",
                "password": "secret-password",
                "account_id": 3,
            },
        )
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "2fa_required"
    assert data["delivery_method"] == "trusted_device"
    api.request_2fa_code.assert_not_called()
    assert "Graham" in data["message"] or "2FA" in data["message"]


def test_login_connected_without_2fa(client, monkeypatch):
    api = make_mock_icloud_api(requires_2fa=False, trusted=True)
    monkeypatch.setattr(icloud_client, "_store_password", lambda *a, **k: None)
    monkeypatch.setattr(icloud_client, "_clear_cookie_files", lambda *a, **k: None)

    with patch.object(icloud_client, "_create_service", return_value=api):
        r = client.post(
            "/api/icloud/auth",
            json={
                "apple_id": "dan.custodio@icloud.com",
                "password": "secret-password",
                "account_id": 1,
            },
        )
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "connected"
    api.request_2fa_code.assert_not_called()


def test_submit_2fa_success(client, monkeypatch):
    api = make_mock_icloud_api(requires_2fa=True)
    monkeypatch.setattr(icloud_client, "_store_password", lambda *a, **k: None)
    monkeypatch.setattr(icloud_client, "_clear_cookie_files", lambda *a, **k: None)

    with patch.object(icloud_client, "_create_service", return_value=api):
        login = client.post(
            "/api/icloud/auth",
            json={
                "apple_id": "graham.custodio@icloud.com",
                "password": "secret-password",
                "account_id": 3,
            },
        )
    assert login.status_code == 200
    assert login.json()["status"] == "2fa_required"

    # After successful validate, session is trusted
    def _validate(code):
        api.requires_2fa = False
        api.is_trusted_session = True
        return True

    api.validate_2fa_code.side_effect = _validate
    r = client.post("/api/icloud/auth/2fa", json={"code": "123456"})
    assert r.status_code == 200
    assert r.json()["status"] == "connected"
    api.validate_2fa_code.assert_called()


def test_submit_2fa_rejects_bad_format(client):
    r = client.post("/api/icloud/auth/2fa", json={"code": "abc"})
    assert r.status_code == 400


def test_resend_2fa(client, monkeypatch):
    api = make_mock_icloud_api(requires_2fa=True)
    monkeypatch.setattr(icloud_client, "_store_password", lambda *a, **k: None)
    monkeypatch.setattr(icloud_client, "_clear_cookie_files", lambda *a, **k: None)

    with patch.object(icloud_client, "_create_service", return_value=api):
        client.post(
            "/api/icloud/auth",
            json={
                "apple_id": "graham.custodio@icloud.com",
                "password": "secret-password",
                "account_id": 3,
            },
        )
    api.request_2fa_code.reset_mock()
    r = client.post("/api/icloud/auth/2fa/resend", json={})
    assert r.status_code == 200
    assert r.json()["status"] == "2fa_required"
    api.request_2fa_code.assert_not_called()
    api._request_2fa_code.assert_called_once()


def test_login_prefers_sms_when_phone_available(client, monkeypatch):
    api = make_mock_icloud_api(requires_2fa=True)
    api._trusted_phone_number.return_value = make_mock_phone()
    monkeypatch.setattr(icloud_client, "_store_password", lambda *a, **k: None)
    monkeypatch.setattr(icloud_client, "_clear_cookie_files", lambda *a, **k: None)

    with patch.object(icloud_client, "_create_service", return_value=api):
        r = client.post(
            "/api/icloud/auth",
            json={
                "apple_id": "graham.custodio@icloud.com",
                "password": "secret-password",
                "account_id": 3,
            },
        )
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "2fa_required"
    assert data["delivery_method"] == "sms"
    assert data["sms_available"] is True
    assert "SMS" in data["message"]
    api.request_2fa_code.assert_not_called()
    api._request_sms_2fa_code.assert_called()


def test_submit_2fa_uses_sms_verifier(client, monkeypatch):
    api = make_mock_icloud_api(requires_2fa=True)
    api._trusted_phone_number.return_value = make_mock_phone()
    monkeypatch.setattr(icloud_client, "_store_password", lambda *a, **k: None)
    monkeypatch.setattr(icloud_client, "_clear_cookie_files", lambda *a, **k: None)

    with patch.object(icloud_client, "_create_service", return_value=api):
        client.post(
            "/api/icloud/auth",
            json={
                "apple_id": "graham.custodio@icloud.com",
                "password": "secret-password",
                "account_id": 3,
            },
        )

    def _sms(_code):
        api.requires_2fa = False
        api.requires_2sa = False
        api.is_trusted_session = True

    api._validate_sms_code.side_effect = _sms
    r = client.post("/api/icloud/auth/2fa", json={"code": "654321"})
    assert r.status_code == 200
    assert r.json()["status"] == "connected"
    api._validate_sms_code.assert_called()
    api.validate_2fa_code.assert_not_called()


def test_request_2fa_sms(client, monkeypatch):
    api = make_mock_icloud_api(requires_2fa=True)
    api._trusted_phone_number.return_value = make_mock_phone()
    api._request_sms_2fa_code.return_value = True
    monkeypatch.setattr(icloud_client, "_store_password", lambda *a, **k: None)
    monkeypatch.setattr(icloud_client, "_clear_cookie_files", lambda *a, **k: None)

    with patch.object(icloud_client, "_create_service", return_value=api):
        client.post(
            "/api/icloud/auth",
            json={
                "apple_id": "graham.custodio@icloud.com",
                "password": "secret-password",
                "account_id": 3,
            },
        )
    api._request_sms_2fa_code.reset_mock()
    r = client.post("/api/icloud/auth/2fa/sms", json={})
    assert r.status_code == 200
    assert r.json()["status"] == "2fa_required"
    api._request_sms_2fa_code.assert_called_once()


def test_request_2fa_sms_without_phone(client, monkeypatch):
    api = make_mock_icloud_api(requires_2fa=True)
    api._trusted_phone_number.return_value = None
    monkeypatch.setattr(icloud_client, "_store_password", lambda *a, **k: None)
    monkeypatch.setattr(icloud_client, "_clear_cookie_files", lambda *a, **k: None)

    with patch.object(icloud_client, "_create_service", return_value=api):
        client.post(
            "/api/icloud/auth",
            json={
                "apple_id": "graham.custodio@icloud.com",
                "password": "secret-password",
                "account_id": 3,
            },
        )
    r = client.post("/api/icloud/auth/2fa/sms", json={})
    assert r.status_code == 400
    api._request_sms_2fa_code.assert_not_called()


def test_stale_2fa_cookies_force_relogin(client, monkeypatch):
    """Cookie restore that still requires 2FA should clear and ask to sign in again."""
    with connect() as conn:
        conn.execute(
            "UPDATE icloud_accounts SET apple_id = ? WHERE id = 3",
            ("graham.custodio@icloud.com",),
        )
    from app.database import set_setting
    from app.database import SETTING_ACTIVE_ACCOUNT

    set_setting(SETTING_ACTIVE_ACCOUNT, "3")

    stale = make_mock_icloud_api(requires_2fa=True)
    monkeypatch.setattr(icloud_client, "_clear_cookie_files", lambda *a, **k: None)
    monkeypatch.setattr(icloud_client, "_migrate_legacy_cookies", lambda *a, **k: None)

    with patch.object(icloud_client, "_create_service", return_value=stale) as create:
        r = client.get("/api/icloud/status")
    assert r.status_code == 200
    data = r.json()
    assert data["account_id"] == 3
    assert data["requires_2fa"] is False
    assert data["connected"] is False
    assert data["error"] and "Sign in again" in data["error"]
    # Must not pass keyring password on background status (that re-triggers MFA)
    assert create.call_args.kwargs.get("password") in (None, "")
    stale.request_2fa_code.assert_not_called()


def test_status_does_not_request_2fa_for_in_memory_pending(client, monkeypatch):
    """Polling /status mid-2FA must not push another Apple prompt."""
    api = make_mock_icloud_api(requires_2fa=True)
    with icloud_client._lock:
        icloud_client._api = api
        icloud_client._pending_2fa = True
        icloud_client._session_account_id = 1
        icloud_client._2fa_code_requested = False

    r = client.get("/api/icloud/status")
    assert r.status_code == 200
    assert r.json()["requires_2fa"] is True
    api.request_2fa_code.assert_not_called()


_GRAHAM_TRUSTED_79 = {
    "id": 2,
    "lastTwoDigits": "79",
    "nonFTEU": True,
    "obfuscatedNumber": "(•••) •••-••79",
    "numberWithDialCode": "+1 (•••) •••-••79",
    "pushMode": "sms",
}
_DEFAULT_98 = {
    "id": 1,
    "lastTwoDigits": "98",
    "nonFTEU": False,
    "obfuscatedNumber": "(•••) •••-••98",
    "numberWithDialCode": "+1 (•••) •••-••98",
    "pushMode": "sms",
}


def test_choose_graham_phone_from_last_two_digits():
    api = make_mock_icloud_api(requires_2fa=True)
    api._trusted_phone_number.return_value = None
    api._auth_data = {
        "phoneNumberVerification": {
            "trustedPhoneNumber": _DEFAULT_98,
            "trustedPhoneNumbers": [_GRAHAM_TRUSTED_79],
        }
    }
    account = {"label": "Graham"}
    assert icloud_client._choose_phone_id(api, account=account) == 2
    state = icloud_client._phone_state(api, account)
    assert state["preferred_missing"] is False
    graham = next(p for p in state["phones"] if p["id"] == 2)
    assert graham["suffix"] == "79"
    assert graham["non_fteu"] is True
    assert icloud_client._sms_put_body(graham) == {
        "phoneNumber": {"id": 2, "nonFTEU": True},
        "mode": "sms",
    }


def test_send_sms_retries_preferred_phone_after_423():
    apple_423 = {
        "trustedPhoneNumbers": [_GRAHAM_TRUSTED_79],
        "phoneNumber": {
            "nonFTEU": False,
            "numberWithDialCode": "+1 (•••) •••-••98",
            "obfuscatedNumber": "(•••) •••-••98",
            "lastTwoDigits": "98",
            "pushMode": "sms",
        },
    }
    apple_423_text = json.dumps(apple_423)

    class FakeResp:
        def __init__(self, status, text=""):
            self.status_code = status
            self.text = text

    class FakeSession:
        def __init__(self):
            self.calls = []

        def request_raw(self, method, url, json=None, headers=None):
            self.calls.append(json)
            inner = (json or {}).get("phoneNumber") or {}
            if inner.get("id") == 1:
                return FakeResp(423, apple_423_text)
            if inner.get("id") == 2 and inner.get("nonFTEU") is True:
                return FakeResp(200, "{}")
            return FakeResp(400, "unexpected")

    api = SimpleNamespace(
        _auth_data={
            "phoneNumberVerification": {"trustedPhoneNumber": _DEFAULT_98}
        },
        session=FakeSession(),
        _auth_endpoint="https://idmsa.apple.com/appleauth/auth",
        _get_auth_headers=lambda extra: extra,
        two_factor_delivery_method="trusted_device",
        _trusted_phone_number=lambda: None,
    )
    with icloud_client._lock:
        icloud_client._session_account_id = 3
        icloud_client._sms_phone_id = None
    hint = icloud_client._send_sms_request(api)
    assert api.session.calls[0]["phoneNumber"]["id"] == 1
    assert api.session.calls[1] == {
        "phoneNumber": {"id": 2, "nonFTEU": True},
        "mode": "sms",
    }
    assert icloud_client._sms_phone_id == 2
    assert hint and "79" in hint
    err = icloud_client._sms_http_error(423, json.dumps(apple_423))
    assert "locked" in err.lower()
    assert "trustedPhoneNumbers" not in err


_APPLE_412_VALID = {
    "trustedPhoneNumbers": [_GRAHAM_TRUSTED_79],
    "phoneNumber": {
        **_GRAHAM_TRUSTED_79,
        "numberWithDialCode": "+1 (720) 456-1779",
    },
    "securityCode": {
        "code": "164131",
        "tooManyCodesSent": False,
        "tooManyCodesValidated": False,
        "securityCodeLocked": False,
        "securityCodeCooldown": False,
        "valid": True,
    },
    "mode": "sms",
    "hsa2Account": True,
}


def test_sms_verify_treats_412_valid_code_as_success():
    body = json.dumps(_APPLE_412_VALID)
    ok, err = icloud_client._sms_verify_outcome(412, body)
    assert ok is True
    assert err is None
    bad = json.dumps({**_APPLE_412_VALID, "securityCode": {**_APPLE_412_VALID["securityCode"], "valid": False}})
    ok, err = icloud_client._sms_verify_outcome(412, bad)
    assert ok is False
    assert err and "not valid" in err.lower()
    assert "trustedPhoneNumbers" not in err


def test_verify_sms_code_accepts_apple_412_valid():
    apple_text = json.dumps(_APPLE_412_VALID)

    class FakeResp:
        def __init__(self, status, text=""):
            self.status_code = status
            self.text = text

    class FakeSession:
        def request_raw(self, method, url, json=None, headers=None):
            return FakeResp(412, apple_text)

    api = SimpleNamespace(
        _auth_data={"phoneNumberVerification": {"trustedPhoneNumbers": [_GRAHAM_TRUSTED_79]}},
        session=FakeSession(),
        _auth_endpoint="https://idmsa.apple.com/appleauth/auth",
        _get_auth_headers=lambda extra: extra,
        _trusted_phone_number=lambda: None,
    )
    with icloud_client._lock:
        icloud_client._sms_phone_id = 2
    ok, err = icloud_client._verify_sms_code(api, "164131")
    assert ok is True
    assert err is None
