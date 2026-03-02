# kenya-sms

**Africa's Talking SMS wrapper for Kenya — bilingual templates, delivery tracking, bulk sending.**

[![CI](https://github.com/gabrielmahia/kenya-sms/actions/workflows/ci.yml/badge.svg)](https://github.com/gabrielmahia/kenya-sms/actions)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](#)
[![Tests](https://img.shields.io/badge/tests-43%20passing-brightgreen)](#)
[![Zero deps](https://img.shields.io/badge/dependencies-zero-brightgreen)](#)
[![License](https://img.shields.io/badge/License-CC%20BY--NC--ND%204.0-lightgrey)](LICENSE)

SMS is the primary notification channel for Kenya. This library wraps the
[Africa's Talking](https://africastalking.com) API with the patterns that
production Kenya systems actually need: bilingual English/Kiswahili templates,
phone number normalisation, delivery receipt tracking, rate-limit backoff, and
bulk broadcast sending.

---

## Install

```bash
pip install kenya-sms
```

---

## Quickstart

```python
from kenya_sms import SMSClient, Templates, Language

client = SMSClient(
    api_key="your_at_api_key",
    username="your_at_username",
    sender_id="MYAPP",      # Registered with Communications Authority of Kenya
)

# Single message — phone normalised automatically
result = client.send("0712345678", "Your order is confirmed.")
print(result.succeeded, result.cost)  # True, "KES 1.0000"

# Bilingual template — user's language preference
result = client.send_template(
    "254712345678",
    Templates.CONTRIBUTION_RECEIVED,
    language=Language.KISWAHILI,
    name="Jane", amount="2,000", cycle="January", receipt="NLJ7RT61SV",
)
# → "Habari Jane, mchango wako wa KES 2,000 kwa January umepokelewa. Risiti: NLJ7RT61SV."
```

---

## Bilingual templates

All templates render in English or Kiswahili with the same variables:

```python
from kenya_sms import Template, Language

# Built-in templates
Templates.CONTRIBUTION_RECEIVED   # Chama contribution confirmed
Templates.CONTRIBUTION_REMINDER   # Upcoming contribution due
Templates.LOAN_APPROVED           # Loan disbursement notice
Templates.DROUGHT_ALERT           # OpenResilience early warning
Templates.PAYMENT_PROMPT          # M-Pesa payment instructions
Templates.OTP                     # Verification code

# Custom template
tpl = Template(
    en="Dear {name}, your loan repayment of KES {amount} is due on {date}.",
    sw="Habari {name}, malipo yako ya mkopo ya KES {amount} yanastahili tarehe {date}.",
)
msg = tpl.render(Language.KISWAHILI, name="John", amount="4,333", date="31 Jan")
parts = tpl.sms_parts(Language.KISWAHILI, name="John", amount="4,333", date="31 Jan")
# → 1 part (within 160 chars)
```

---

## Phone number normalisation

Accepts any Kenyan format:

```python
from kenya_sms import normalise_phone

normalise_phone("0712345678")      # → "254712345678"
normalise_phone("+254712345678")   # → "254712345678"
normalise_phone("712345678")       # → "254712345678"
normalise_phone("0112345678")      # → "254112345678"  (Safaricom Home)
```

---

## Bulk sending

```python
# Different message per recipient
results = client.send_bulk([
    ("0712345678", "Your contribution for January is confirmed."),
    ("0723456789", "Mchango wako wa Januari umethibitishwa."),
    ("0734567890", "Your contribution is pending. Please pay by Friday."),
])

# Same message to many — uses AT's batch endpoint
results = client.send_broadcast(
    phones=["0712345678", "0723456789", "0734567890"],
    message="Chama meeting Saturday 10am at the usual venue.",
)
successful = [r for r in results if r.succeeded]
```

---

## Delivery tracking

```python
# Mount at your AT delivery report callback URL
@app.post("/sms/delivery")
async def delivery_report(request: Request):
    payload = await request.json()
    receipt = client.handle_delivery_receipt(payload)
    return {"status": "ok"}

# Query later
store = client.delivery_store
failed = store.failed()  # Messages that did not deliver
print(f"{len(failed)} undelivered messages")
```

---

## Sandbox mode

```python
client = SMSClient(
    api_key="sandbox",
    username="sandbox",
    sandbox=True,   # Routes to AT sandbox — no real SMS sent
)
```

---

## Use with OpenResilience

```python
from kenya_sms import SMSClient, Templates, Language
from kenya_counties import get

# Send drought alert to all Turkana subscribers
turkana = get("Turkana")
client = SMSClient(api_key=AT_KEY, username=AT_USER, sender_id="RESILIENCE")

for subscriber_phone in get_turkana_subscribers():
    client.send_template(
        subscriber_phone,
        Templates.DROUGHT_ALERT,
        Language.KISWAHILI,
        county=turkana.name,
        level="Kali",
        deficit="48",
        action="kuvuna mapema",
    )
```

---

## Design decisions

**Zero runtime dependencies.** The Africa's Talking API is a simple REST + form-encoded
endpoint. urllib.request handles it without httpx or requests.

**Template-first design.** In production Kenya systems, the same message goes out in
both languages constantly. Encoding this as a first-class type — not a function or a
string — means templates are testable, reusable, and auditable.

**Phone normalisation is non-negotiable.** Kenyan users enter phone numbers in at least
four different formats. Failing on anything other than E.164 means real users get dropped.
The normaliser accepts all common formats and rejects genuinely invalid input.

---

*Part of the [nairobi-stack](https://github.com/gabrielmahia/nairobi-stack) East Africa engineering ecosystem.*
*Maintained by [Gabriel Mahia](https://github.com/gabrielmahia). Kenya × USA.*
