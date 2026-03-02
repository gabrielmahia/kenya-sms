"""
kenya-sms — Africa's Talking SMS wrapper for Kenya.

Provides:
  - Typed client wrapping Africa's Talking REST API
  - Bilingual template engine (English / Kiswahili)
  - Delivery receipt store (in-memory by default, pluggable)
  - Bulk sending with rate-limit backoff
  - Phone number normalisation (same logic as mpesa-python)

Usage::

    from kenya_sms import SMSClient, Template, Language

    client = SMSClient(
        api_key="your_at_api_key",
        username="your_at_username",
        sender_id="MYAPP",          # Registered sender ID, or None for shortcode
    )

    # Single message
    result = client.send("0712345678", "Your order has been confirmed.")

    # Bilingual template
    tpl = Template(
        en="Dear {name}, your contribution of KES {amount} has been received.",
        sw="Habari {name}, mchango wako wa KES {amount} umepokelewa.",
    )
    result = client.send_template("254712345678", tpl, name="Jane", amount="2,000")

    # Bulk
    results = client.send_bulk([
        ("254712345678", "Message A"),
        ("254723456789", "Message B"),
    ])
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

logger = logging.getLogger("kenya_sms")


# ── Phone normalisation ────────────────────────────────────────────────────────

def normalise_phone(raw: str) -> str:
    """Normalise a Kenyan phone number to E.164 without '+'.

    Accepts:
        0712345678      → 254712345678
        +254712345678   → 254712345678
        254712345678    → 254712345678
        712345678       → 254712345678

    Raises:
        ValueError: if the result is not a 12-digit string starting with 254.
    """
    phone = raw.strip().replace(" ", "").replace("-", "")
    phone = phone.lstrip("+")
    if phone.startswith("07") or phone.startswith("01"):
        phone = "254" + phone[1:]
    elif len(phone) == 9 and phone[0] in "7":
        phone = "254" + phone
    if not (phone.startswith("254") and phone.isdigit() and len(phone) == 12):
        raise ValueError(
            f"Cannot normalise phone number {raw!r}. "
            "Expected formats: 0712345678, +254712345678, 254712345678"
        )
    return phone


# ── Bilingual template ─────────────────────────────────────────────────────────

class Language(str, Enum):
    ENGLISH = "en"
    KISWAHILI = "sw"


@dataclass
class Template:
    """A bilingual SMS template (English / Kiswahili).

    Variables use Python str.format_map syntax: {name}, {amount}, etc.

    Examples::

        tpl = Template(
            en="Dear {name}, your payment of KES {amount} is confirmed. Ref: {ref}",
            sw="Habari {name}, malipo yako ya KES {amount} yamethibitishwa. Kumb: {ref}",
        )
        msg = tpl.render(language=Language.KISWAHILI, name="Jane", amount="2,000", ref="INV001")
    """
    en: str
    sw: str

    def render(self, language: Language = Language.ENGLISH, **kwargs: str) -> str:
        template = self.en if language == Language.ENGLISH else self.sw
        try:
            return template.format_map(kwargs)
        except KeyError as exc:
            raise ValueError(f"Template variable {exc} not provided. Got: {list(kwargs.keys())}") from exc

    def character_count(self, language: Language = Language.ENGLISH, **kwargs: str) -> int:
        return len(self.render(language, **kwargs))

    def sms_parts(self, language: Language = Language.ENGLISH, **kwargs: str) -> int:
        """Number of SMS parts (160 chars each, or 153 for multi-part)."""
        n = self.character_count(language, **kwargs)
        if n <= 160:
            return 1
        return -(-n // 153)  # ceiling division


# ── Common templates ───────────────────────────────────────────────────────────

class Templates:
    """Pre-built templates for common Kenya use cases."""

    CONTRIBUTION_RECEIVED = Template(
        en="Dear {name}, your contribution of KES {amount} for {cycle} has been received. Receipt: {receipt}.",
        sw="Habari {name}, mchango wako wa KES {amount} kwa {cycle} umepokelewa. Risiti: {receipt}.",
    )

    CONTRIBUTION_REMINDER = Template(
        en="Dear {name}, a reminder: KES {amount} contribution for {chama} is due by {due_date}.",
        sw="Habari {name}, ukumbusho: mchango wa KES {amount} kwa {chama} unastahili kufikia {due_date}.",
    )

    LOAN_APPROVED = Template(
        en="Dear {name}, your loan of KES {amount} has been approved. Repayment: KES {monthly}/month for {months} months.",
        sw="Habari {name}, mkopo wako wa KES {amount} umeidhinishwa. Malipo: KES {monthly}/mwezi kwa miezi {months}.",
    )

    DROUGHT_ALERT = Template(
        en="ALERT — {county}: {level} drought stress. Rainfall deficit: {deficit}mm. Consider {action}.",
        sw="TAHADHARI — {county}: ukame wa kiwango {level}. Upungufu wa mvua: {deficit}mm. Fikiria {action}.",
    )

    PAYMENT_PROMPT = Template(
        en="Hi {name}, please complete your M-Pesa payment of KES {amount} to {paybill}, account {account}.",
        sw="Habari {name}, tafadhali maliza malipo yako ya M-Pesa ya KES {amount} kwa {paybill}, akaunti {account}.",
    )

    OTP = Template(
        en="Your verification code is {code}. Valid for {minutes} minutes. Do not share.",
        sw="Nambari yako ya uthibitisho ni {code}. Halali kwa dakika {minutes}. Usishiriki.",
    )


# ── Result types ───────────────────────────────────────────────────────────────

class SendStatus(str, Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    QUEUED   = "queued"     # AT accepted but not yet delivered


@dataclass
class SendResult:
    phone: str
    status: SendStatus
    message_id: Optional[str] = None
    cost: Optional[str] = None      # e.g. "KES 1.0000"
    status_code: Optional[int] = None
    error: Optional[str] = None
    sent_at: datetime = field(default_factory=datetime.utcnow)

    @property
    def succeeded(self) -> bool:
        return self.status == SendStatus.SUCCESS


@dataclass
class DeliveryReceipt:
    message_id: str
    phone: str
    status: str         # "Success", "Failed", "Rejected", etc.
    failure_reason: Optional[str]
    network_code: Optional[str]
    received_at: datetime = field(default_factory=datetime.utcnow)


# ── Delivery receipt store ─────────────────────────────────────────────────────

class DeliveryStore:
    """In-memory delivery receipt store.

    Implement the same interface backed by a database for production use.
    """
    def __init__(self):
        self._receipts: dict[str, DeliveryReceipt] = {}

    def save(self, receipt: DeliveryReceipt) -> None:
        self._receipts[receipt.message_id] = receipt

    def get(self, message_id: str) -> Optional[DeliveryReceipt]:
        return self._receipts.get(message_id)

    def all(self) -> list[DeliveryReceipt]:
        return list(self._receipts.values())

    def failed(self) -> list[DeliveryReceipt]:
        return [r for r in self._receipts.values() if r.status.lower() not in ("success", "deliveredtonetwork")]


# ── Client ─────────────────────────────────────────────────────────────────────

class SMSClient:
    """Africa's Talking SMS client for Kenya.

    Args:
        api_key:    Africa's Talking API key (from dashboard)
        username:   Africa's Talking username (use "sandbox" for testing)
        sender_id:  Registered sender ID, or None to use shared shortcode
        sandbox:    If True, sends to sandbox (no real SMS sent)
        delivery_store: DeliveryStore instance for tracking receipts
    """

    AT_BASE     = "https://api.africastalking.com/version1"
    AT_SANDBOX  = "https://api.sandbox.africastalking.com/version1"
    RATE_LIMIT  = 10           # Max requests per second to AT API
    MAX_BULK    = 1000         # AT's per-request limit for bulk sends
    MAX_RETRIES = 3

    def __init__(
        self,
        api_key: str,
        username: str,
        sender_id: Optional[str] = None,
        sandbox: bool = False,
        delivery_store: Optional[DeliveryStore] = None,
    ):
        self._api_key    = api_key
        self._username   = username
        self._sender_id  = sender_id
        self._base       = self.AT_SANDBOX if sandbox else self.AT_BASE
        self._store      = delivery_store or DeliveryStore()
        self._last_call  = 0.0

    def _throttle(self) -> None:
        """Ensure at most RATE_LIMIT calls per second."""
        now = time.monotonic()
        gap = 1.0 / self.RATE_LIMIT
        elapsed = now - self._last_call
        if elapsed < gap:
            time.sleep(gap - elapsed)
        self._last_call = time.monotonic()

    def _post(self, endpoint: str, params: dict, retries: int = MAX_RETRIES) -> dict:
        self._throttle()
        url = f"{self._base}{endpoint}"
        body = urllib.parse.urlencode(params).encode()
        headers = {
            "apiKey": self._api_key,
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        for attempt in range(retries):
            try:
                req = urllib.request.Request(url, data=body, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=15) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    wait = 2 ** attempt
                    logger.warning("AT rate limit — waiting %ss (attempt %d)", wait, attempt + 1)
                    time.sleep(wait)
                    continue
                raise
            except urllib.error.URLError as e:
                if attempt == retries - 1:
                    raise
                time.sleep(2 ** attempt)
        raise RuntimeError(f"AT API request failed after {retries} retries")

    def send(self, phone: str, message: str) -> SendResult:
        """Send a single SMS to a Kenyan phone number.

        Args:
            phone:   Any Kenyan phone format (normalised automatically)
            message: Message text (max 160 chars for single part)

        Returns:
            SendResult with status, message_id, and cost.
        """
        normalised = normalise_phone(phone)
        params = {
            "username": self._username,
            "to":       f"+{normalised}",
            "message":  message,
        }
        if self._sender_id:
            params["from"] = self._sender_id

        logger.info("sms_send phone=%s chars=%d", normalised, len(message))

        try:
            response = self._post("/messaging", params)
            recipients = response.get("SMSMessageData", {}).get("Recipients", [])
            if not recipients:
                return SendResult(phone=normalised, status=SendStatus.FAILED,
                                  error="No recipients in AT response")
            rec = recipients[0]
            at_status = rec.get("status", "").lower()
            status = SendStatus.SUCCESS if at_status == "success" else SendStatus.FAILED
            return SendResult(
                phone=normalised,
                status=status,
                message_id=rec.get("messageId"),
                cost=rec.get("cost"),
                status_code=rec.get("statusCode"),
                error=rec.get("status") if status == SendStatus.FAILED else None,
            )
        except Exception as exc:
            logger.error("sms_send_failed phone=%s error=%s", normalised, exc)
            return SendResult(phone=normalised, status=SendStatus.FAILED, error=str(exc))

    def send_template(
        self,
        phone: str,
        template: Template,
        language: Language = Language.ENGLISH,
        **kwargs: str,
    ) -> SendResult:
        """Render a bilingual template and send it."""
        message = template.render(language=language, **kwargs)
        return self.send(phone, message)

    def send_bulk(
        self,
        recipients: list[tuple[str, str]],
        batch_size: int = 100,
    ) -> list[SendResult]:
        """Send different messages to multiple recipients.

        Args:
            recipients: List of (phone, message) tuples
            batch_size: How many to send per AT API call (max 1000)

        Returns:
            List of SendResult, one per recipient, in the same order.
        """
        results: list[SendResult] = []
        for i in range(0, len(recipients), batch_size):
            batch = recipients[i : i + batch_size]
            # AT bulk API takes comma-separated numbers; for different messages
            # we fall back to individual sends (AT does not support per-recipient
            # messages in a single call)
            for phone, message in batch:
                results.append(self.send(phone, message))
        return results

    def send_broadcast(self, phones: list[str], message: str) -> list[SendResult]:
        """Send the same message to multiple recipients efficiently.

        Uses AT's comma-separated 'to' field for batch delivery.
        """
        normalised = [normalise_phone(p) for p in phones]
        params = {
            "username": self._username,
            "to":       ",".join(f"+{p}" for p in normalised),
            "message":  message,
        }
        if self._sender_id:
            params["from"] = self._sender_id

        logger.info("sms_broadcast count=%d chars=%d", len(normalised), len(message))

        try:
            response = self._post("/messaging", params)
            at_recipients = response.get("SMSMessageData", {}).get("Recipients", [])
            at_by_phone = {
                r.get("number", "").lstrip("+").lstrip("0"): r
                for r in at_recipients
            }
            results = []
            for phone in normalised:
                # Match by last 9 digits (AT sometimes strips country code)
                key = phone[-9:]
                rec = next((v for k, v in at_by_phone.items() if k.endswith(key)), None)
                if rec:
                    status = SendStatus.SUCCESS if rec.get("status", "").lower() == "success" else SendStatus.FAILED
                    results.append(SendResult(
                        phone=phone, status=status,
                        message_id=rec.get("messageId"), cost=rec.get("cost"),
                    ))
                else:
                    results.append(SendResult(phone=phone, status=SendStatus.QUEUED))
            return results
        except Exception as exc:
            logger.error("sms_broadcast_failed error=%s", exc)
            return [SendResult(phone=p, status=SendStatus.FAILED, error=str(exc))
                    for p in normalised]

    def handle_delivery_receipt(self, payload: dict) -> DeliveryReceipt:
        """Parse and store an AT delivery report callback.

        Mount this at your callback URL and pass the POST body dict.
        AT posts: id, status, phoneNumber, networkCode, failureReason (optional)
        """
        receipt = DeliveryReceipt(
            message_id=payload.get("id", ""),
            phone=payload.get("phoneNumber", ""),
            status=payload.get("status", ""),
            failure_reason=payload.get("failureReason"),
            network_code=payload.get("networkCode"),
        )
        self._store.save(receipt)
        logger.info(
            "delivery_receipt msg_id=%s phone=%s status=%s",
            receipt.message_id, receipt.phone, receipt.status,
        )
        return receipt

    @property
    def delivery_store(self) -> DeliveryStore:
        return self._store
