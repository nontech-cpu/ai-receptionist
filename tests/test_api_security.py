import pytest
import openpyxl
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from app.core.config import settings
from app.services.sheet_service import sheet_service

TEST_ADMIN_KEY = "test_secret_admin_key_12345"

def test_api_appointments_requires_authentication(client: TestClient, monkeypatch):
    """
    Sub-Phase D Requirement:
    /api/appointments exposes patient health data and must reject unauthenticated requests with HTTP 401.
    """
    monkeypatch.setattr(settings, "ADMIN_API_KEY", TEST_ADMIN_KEY)

    # 1. Unauthenticated request -> HTTP 401
    res_unauth = client.get("/api/appointments")
    assert res_unauth.status_code == 401

    # 2. Invalid key -> HTTP 401
    res_bad = client.get("/api/appointments", headers={"X-Admin-Key": "wrong_key"})
    assert res_bad.status_code == 401

    # 3. Valid key via X-Admin-Key -> HTTP 200
    res_auth = client.get("/api/appointments", headers={"X-Admin-Key": TEST_ADMIN_KEY})
    assert res_auth.status_code == 200

    # 4. Valid key via Bearer token -> HTTP 200
    res_bearer = client.get("/api/appointments", headers={"Authorization": f"Bearer {TEST_ADMIN_KEY}"})
    assert res_bearer.status_code == 200

def test_api_make_call_validation(client: TestClient, monkeypatch):
    """
    Validates /api/make-call outbound dialer endpoint:
    - Missing 'to_number' parameter returns HTTP 400 Bad Request.
    - Missing Twilio credentials fails safely with HTTP 500.
    - Does not introduce Admin API key requirement for outbound testing dialer.
    """
    # 1. Missing to_number -> HTTP 400 Bad Request
    res_no_num = client.post("/api/make-call")
    assert res_no_num.status_code == 400
    assert "required" in res_no_num.json().get("error", "").lower()

    # 2. Missing Twilio credentials -> HTTP 500
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "")
    res_no_cred = client.post("/api/make-call?to_number=+919829012345")
    assert res_no_cred.status_code == 500
    assert "not configured" in res_no_cred.json().get("error", "").lower()

def test_api_clinic_profile_modification_requires_auth(client: TestClient, monkeypatch):
    """
    Sub-Phase D Requirement:
    POST /api/clinic-profile modifies clinic metadata and must require ADMIN_API_KEY.
    """
    monkeypatch.setattr(settings, "ADMIN_API_KEY", TEST_ADMIN_KEY)

    res_unauth = client.post("/api/clinic-profile", json={"doctor_name": "Dr. Hacker"})
    assert res_unauth.status_code == 401

def test_api_set_telegram_requires_authentication(client: TestClient, monkeypatch):
    """
    Sub-Phase D Requirement:
    POST /api/set-telegram-chat-id must require ADMIN_API_KEY.
    """
    monkeypatch.setattr(settings, "ADMIN_API_KEY", TEST_ADMIN_KEY)

    res_unauth = client.post("/api/set-telegram-chat-id", json={"chat_id": "999999"})
    assert res_unauth.status_code == 401

def test_missing_admin_key_fails_safely(client: TestClient, monkeypatch):
    """
    Sub-Phase D Requirement:
    Missing production ADMIN_API_KEY must fail safely (never allow open access).
    """
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "")

    response = client.get("/api/appointments")
    assert response.status_code == 401

def test_twilio_webhook_signature_verification_rejects_invalid_signature(client: TestClient, monkeypatch):
    """
    Sub-Phase D Requirement:
    Twilio incoming webhooks with invalid signatures must receive HTTP 403 Forbidden.
    """
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "test_twilio_auth_token_xyz")
    monkeypatch.setattr(settings, "TWILIO_VALIDATE_SIGNATURE", True)

    payload = {"CallSid": "CA123456", "From": "+17372212163"}
    headers = {"X-Twilio-Signature": "invalid_signature_hash_123"}

    response = client.post("/voice/incoming", data=payload, headers=headers)
    assert response.status_code == 403

def test_twilio_webhook_signature_verification_accepts_valid_signature(client: TestClient, monkeypatch):
    """
    Sub-Phase D Requirement:
    Twilio incoming webhooks with valid cryptographic signatures must succeed (HTTP 200).
    """
    test_auth_token = "secret_twilio_token_123"
    webhook_url = "http://testserver/voice/incoming"

    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", test_auth_token)
    monkeypatch.setattr(settings, "TWILIO_VALIDATE_SIGNATURE", True)
    monkeypatch.setattr(settings, "WEBHOOK_BASE_URL", "http://testserver")

    payload = {"CallSid": "CA_VALID_SIGNATURE_01", "From": "+919829012345"}

    # Generate valid signature using official Twilio RequestValidator
    validator = RequestValidator(test_auth_token)
    valid_sig = validator.compute_signature(webhook_url, payload)

    response = client.post("/voice/incoming", data=payload, headers={"X-Twilio-Signature": valid_sig})
    assert response.status_code == 200
    assert "response" in response.text.lower() or "gather" in response.text.lower()

def test_audio_path_traversal_attack_blocked(client: TestClient):
    """
    Sub-Phase D Requirement:
    Protect audio file serving against directory traversal (e.g. /api/audio/../../.env).
    """
    # 1. Path traversal with ..
    res_traversal1 = client.get("/api/audio/../../.env")
    assert res_traversal1.status_code in (400, 403, 404)

    # 2. Path traversal with slashes
    res_traversal2 = client.get("/api/audio/..%2F..%2Frequirements.txt")
    assert res_traversal2.status_code in (400, 403, 404)

def test_dashboard_xss_protection(client: TestClient):
    """
    Sub-Phase D Requirement:
    Review dashboard rendering for XSS vulnerabilities; verify escapeHtml is present.
    """
    res = client.get("/")
    assert res.status_code == 200
    html = res.text
    assert "escapeHtml" in html
    assert "escapeHtml(a.patient_name)" in html

def test_formula_injection_prevention(protect_production_excel):
    """
    Scenario AR: Formula / CSV Injection in Excel.
    """
    temp_excel = protect_production_excel

    malicious_name = "=SUM(1+1)"
    sheet_service.save_appointment(
        patient_name=malicious_name,
        phone_number="9829012345",
        date_str="2026-11-25",
        time_slot="11:00 AM",
        symptoms="Checkup"
    )

    wb = openpyxl.load_workbook(temp_excel)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()

    saved_name = rows[1][2]  # Row 2, Column 3 (Patient Name)
    assert not saved_name.startswith("=")
