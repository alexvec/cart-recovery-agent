"""Phone numbers are PII: stored encrypted, looked up by keyed hash, never logged in clear."""

import hashlib
import hmac
from collections.abc import MutableMapping
from typing import Any

import phonenumbers
from cryptography.fernet import Fernet
from phonenumbers import timezone as pn_timezone

from recovery.config import get_settings


def normalize_phone(raw: str | None, default_region: str | None = None) -> str | None:
    """Return the number in E.164 form, or None if it isn't a valid number."""
    if not raw:
        return None
    try:
        num = phonenumbers.parse(raw, default_region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(num):
        return None
    return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)


def phone_country(e164: str) -> str:
    return phonenumbers.region_code_for_number(phonenumbers.parse(e164)) or ""


def phone_timezones(e164: str) -> tuple[str, ...]:
    zones = pn_timezone.time_zones_for_number(phonenumbers.parse(e164))
    return tuple(z for z in zones if z != "Etc/Unknown")


def phone_hash(e164: str) -> str:
    key = get_settings().phone_hash_key.get_secret_value().encode()
    return hmac.new(key, e164.encode(), hashlib.sha256).hexdigest()


def _fernet() -> Fernet:
    return Fernet(get_settings().pii_encryption_key.get_secret_value().encode())


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


REDACTED_KEYS = {"phone", "to_number", "email", "first_name", "last_name", "address1", "name"}


def redact_processor(_logger: Any, _method: str, event: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """structlog processor: redaction lives in the logger, not at every call site."""
    for key in list(event):
        if key in REDACTED_KEYS:
            event[key] = "[redacted]"
    return event
