import logging
from fastapi import Request, HTTPException
from twilio.request_validator import RequestValidator

from app.core.config import settings

logger = logging.getLogger(__name__)

def mask_sensitive_data(val: str, show_last: int = 4) -> str:
    """Masks sensitive strings (phone numbers, tokens) for logging."""
    if not val:
        return ""
    s = str(val).strip()
    if len(s) <= show_last:
        return "*" * len(s)
    return "*" * (len(s) - show_last) + s[-show_last:]

def verify_admin_key(request: Request) -> bool:
    """
    Enforces ADMIN_API_KEY authentication on sensitive endpoints.
    Checks 'X-Admin-Key' or 'Authorization: Bearer <key>'.
    Fails safely with HTTP 401 if the server has no configured key or the client key is invalid.
    """
    server_key = (getattr(settings, "ADMIN_API_KEY", "") or "").strip()
    if not server_key:
        logger.error("[SECURITY] Access denied: Server ADMIN_API_KEY is not configured.")
        raise HTTPException(status_code=401, detail="Unauthorized: Server Admin Key is not configured")

    client_key = request.headers.get("X-Admin-Key", "").strip()
    if not client_key:
        auth_header = request.headers.get("Authorization", "").strip()
        if auth_header.startswith("Bearer "):
            client_key = auth_header[7:].strip()

    if not client_key or client_key != server_key:
        logger.warning("[SECURITY] Unauthorized request rejected (invalid or missing admin key)")
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid or missing Admin API Key")

    return True

async def verify_twilio_signature(request: Request, data: dict = None) -> bool:
    """
    Enforces Twilio RequestValidator signature verification on incoming webhooks.
    Uses configured WEBHOOK_BASE_URL and actual request parameters.
    Fails safely with HTTP 403 if signature is missing or invalid.
    """
    should_verify = getattr(settings, "TWILIO_VALIDATE_SIGNATURE", False) or getattr(settings, "ENVIRONMENT", "") == "production"
    sig_header = request.headers.get("X-Twilio-Signature")

    # If verification is not explicitly required and no signature header is sent, allow in dev/test
    if not should_verify and not sig_header:
        return True

    auth_token = getattr(settings, "TWILIO_AUTH_TOKEN", "").strip()
    if not auth_token:
        logger.error("[SECURITY] Twilio validation required, but TWILIO_AUTH_TOKEN is missing on server.")
        raise HTTPException(status_code=403, detail="Forbidden: Twilio authentication not configured")

    if not sig_header:
        logger.warning("[SECURITY] Webhook missing X-Twilio-Signature header")
        raise HTTPException(status_code=403, detail="Forbidden: Missing Twilio signature")

    validator = RequestValidator(auth_token)

    # Use configured WEBHOOK_BASE_URL (never trust arbitrary client host/proto headers)
    if getattr(settings, "WEBHOOK_BASE_URL", ""):
        base_url = settings.WEBHOOK_BASE_URL.rstrip("/")
        url = f"{base_url}{request.url.path}"
    else:
        url = str(request.url).split("?")[0]

    # Convert request data to string params expected by RequestValidator
    params = {}
    if data:
        params = {k: str(v) for k, v in data.items()}

    valid = validator.validate(url, params, sig_header)
    if not valid:
        # Also validate against request.url directly in case of local ports
        valid = validator.validate(str(request.url), params, sig_header)

    if not valid:
        logger.warning(f"[SECURITY] Invalid Twilio signature on webhook {url}")
        raise HTTPException(status_code=403, detail="Forbidden: Invalid Twilio signature")

    return True
