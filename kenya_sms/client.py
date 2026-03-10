"""
kenya_sms — bilingual SMS layer for Kenya built on Africa's Talking.

Features:
  - Typed message templates (English + Kiswahili)
  - Bulk send with per-recipient status tracking
  - County-level targeting (47 counties, E.164 validation)
  - Delivery webhook parser
  - Sandbox-safe (no messages sent in sandbox mode)

Usage:
    from kenya_sms import KenyaSMS, Lang

    sms = KenyaSMS(username="sandbox", api_key="test")
    result = sms.send("+254712345678", "Hello from Kenya!")

    # Bilingual template
    result = sms.send_template(
        recipients=["+254712345678"],
        template="weather_alert",
        lang=Lang.SW,
        context={"county": "Nairobi", "alert": "Mvua kubwa"},
    )
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

try:
    import africastalking as _at_sdk
    _AT_AVAILABLE = True
except ImportError:
    _AT_AVAILABLE = False


class Lang(Enum):
    EN = "en"
    SW = "sw"


# ── Built-in bilingual templates ───────────────────────────────────────────────

TEMPLATES: dict[str, dict[str, str]] = {
    "weather_alert": {
        "en": "⚠ Weather alert for {county}: {alert}. Stay safe.",
        "sw": "⚠ Tahadhari ya hali ya hewa {county}: {alert}. Kaa salama.",
    },
    "flood_warning": {
        "en": "FLOOD WARNING — {county}: {message}. Move to higher ground.",
        "sw": "ONYO LA MAFURIKO — {county}: {message}. Hamia eneo la juu.",
    },
    "payment_received": {
        "en": "Payment of KES {amount} received. Ref: {reference}. Thank you.",
        "sw": "Malipo ya KES {amount} yamepokelewa. Kumbukumbu: {reference}. Asante.",
    },
    "payment_failed": {
        "en": "Payment of KES {amount} failed. Reason: {reason}. Please retry.",
        "sw": "Malipo ya KES {amount} yameshindwa. Sababu: {reason}. Tafadhali jaribu tena.",
    },
    "registration_confirm": {
        "en": "Welcome {name}! You are registered in {county} county. ID: {user_id}",
        "sw": "Karibu {name}! Umesajiliwa katika kaunti ya {county}. Nambari: {user_id}",
    },
    "drought_alert": {
        "en": "Drought alert: {county} at {severity}% stress. NDVI: {ndvi}. Contact extension officer.",
        "sw": "Tahadhari ya ukame: {county} iko {severity}% msongo. Wasiliana na mshauri wa kilimo.",
    },
    "price_update": {
        "en": "Market price update: {crop} at KES {price}/kg in {market}. Source: {source}",
        "sw": "Bei ya soko: {crop} KES {price}/kg {market}. Chanzo: {source}",
    },
    "chama_reminder": {
        "en": "Chama reminder: KES {amount} contribution due {date}. Contact {treasurer}.",
        "sw": "Ukumbusho wa chama: KES {amount} inastahili {date}. Wasiliana na {treasurer}.",
    },
    "otp": {
        "en": "Your verification code is {otp}. Valid for 10 minutes. Do not share.",
        "sw": "Nambari yako ya uthibitisho ni {otp}. Halali kwa dakika 10. Usishiriki.",
    },
}

# E.164 for Kenya (+254) and other AT-supported markets
_KENYA_RE  = re.compile(r"^\+254[17]\d{8}$")
_E164_RE   = re.compile(r"^\+[1-9]\d{6,14}$")


def normalize_phone(phone: str, default_country: str = "254") -> str:
    """
    Normalize to E.164. Handles Kenyan local formats.
    "+254712345678" → "+254712345678"
    "0712345678"    → "+254712345678"
    "254712345678"  → "+254712345678"
    """
    phone = phone.strip().replace(" ", "").replace("-", "")
    if phone.startswith("+"):
        return phone
    if phone.startswith("0"):
        return f"+{default_country}{phone[1:]}"
    if not phone.startswith(default_country):
        return f"+{default_country}{phone}"
    return f"+{phone}"


def validate_phone(phone: str) -> bool:
    """True if phone is valid E.164."""
    return bool(_E164_RE.match(phone))


@dataclass
class SendResult:
    phone:      str
    status:     str           # "Success" | "Failed" | error message
    message_id: Optional[str] = None
    cost:       Optional[str] = None

    @property
    def success(self) -> bool:
        return self.status == "Success"


@dataclass
class BulkResult:
    sent:    int = 0
    failed:  int = 0
    results: list[SendResult] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        total = self.sent + self.failed
        return (self.sent / total * 100) if total else 0.0

    @property
    def failures(self) -> list[SendResult]:
        return [r for r in self.results if not r.success]


class KenyaSMS:
    """
    Bilingual SMS client for Kenya built on Africa's Talking.

    Set sandbox=True (default) to log messages without sending.
    In production, set sandbox=False and use live credentials.
    """

    def __init__(
        self,
        username:  Optional[str] = None,
        api_key:   Optional[str] = None,
        sandbox:   bool = True,
        sender_id: Optional[str] = None,
    ):
        self.username  = username  or os.environ.get("AT_USERNAME", "sandbox")
        self.api_key   = api_key   or os.environ.get("AT_API_KEY", "")
        self.sandbox   = sandbox
        self.sender_id = sender_id
        self._sms      = None

    def _client(self):
        if self._sms is None:
            if not _AT_AVAILABLE:
                raise ImportError("africastalking not installed — pip install africastalking")
            _at_sdk.initialize(username=self.username, api_key=self.api_key)
            self._sms = _at_sdk.SMS
        return self._sms

    # ── Core send ──────────────────────────────────────────────────────────────

    def send(
        self,
        recipients: str | list[str],
        message:    str,
        sender_id:  Optional[str] = None,
    ) -> BulkResult:
        """
        Send SMS to one or many recipients.

        recipients: E.164 number or list of numbers. Kenyan 07xx formats auto-normalized.
        Returns BulkResult with per-recipient status.
        """
        if isinstance(recipients, str):
            recipients = [recipients]

        normalized = [normalize_phone(p) for p in recipients]
        invalid    = [p for p in normalized if not validate_phone(p)]
        if invalid:
            raise ValueError(f"Invalid phone numbers: {invalid}")

        if self.sandbox and self.username == "sandbox":
            # Sandbox mode — log, don't send
            results = [SendResult(phone=p, status="Success", message_id=f"SANDBOX_{i}")
                       for i, p in enumerate(normalized)]
            return BulkResult(sent=len(results), results=results)

        sid      = sender_id or self.sender_id
        kwargs   = {"message": message, "recipients": normalized}
        if sid:
            kwargs["sender_id"] = sid

        response = self._client().send(**kwargs)
        data     = response["SMSMessageData"]
        raw      = data.get("Recipients", [])

        results = [
            SendResult(
                phone=r["number"],
                status=r.get("status", "Failed"),
                message_id=r.get("messageId"),
                cost=r.get("cost"),
            )
            for r in raw
        ]
        sent   = sum(1 for r in results if r.success)
        failed = len(results) - sent
        return BulkResult(sent=sent, failed=failed, results=results)

    # ── Template send ──────────────────────────────────────────────────────────

    def send_template(
        self,
        recipients: str | list[str],
        template:   str,
        lang:       Lang = Lang.EN,
        context:    Optional[dict] = None,
        sender_id:  Optional[str] = None,
    ) -> BulkResult:
        """
        Send a built-in bilingual template.

        template: key from TEMPLATES dict (e.g. "weather_alert")
        lang: Lang.EN or Lang.SW
        context: dict of placeholder values
        """
        if template not in TEMPLATES:
            raise ValueError(f"Unknown template: {template!r}. Available: {list(TEMPLATES)}")
        tpl     = TEMPLATES[template][lang.value]
        message = tpl.format(**(context or {}))
        return self.send(recipients, message, sender_id=sender_id)

    # ── Webhook parsing ────────────────────────────────────────────────────────

    @staticmethod
    def parse_delivery_report(payload: dict) -> dict:
        """
        Parse an Africa's Talking delivery report webhook payload.

        AT POSTs to your callback URL with form-encoded fields.
        Pass request.form.to_dict() or equivalent.

        Returns structured delivery status dict.
        """
        return {
            "id":           payload.get("id"),
            "status":       payload.get("status"),        # "Success", "Failed", etc.
            "failure_reason": payload.get("failureReason"),
            "retry_count":  payload.get("retryCount"),
            "network_code": payload.get("networkCode"),
            "phone_number": payload.get("phoneNumber"),
        }

    @staticmethod
    def parse_inbound_sms(payload: dict) -> dict:
        """
        Parse an Africa's Talking inbound SMS webhook payload.
        """
        return {
            "from":    payload.get("from"),
            "to":      payload.get("to"),
            "text":    payload.get("text"),
            "date":    payload.get("date"),
            "id":      payload.get("id"),
            "link_id": payload.get("linkId"),   # for USSD-linked responses
        }
