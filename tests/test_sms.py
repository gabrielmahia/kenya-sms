"""Tests for KenyaSMS — all run in sandbox mode (no real SMS sent)."""
import pytest
from kenya_sms import KenyaSMS, Lang, normalize_phone, validate_phone, TEMPLATES

@pytest.fixture
def sms():
    return KenyaSMS(username="sandbox", api_key="test", sandbox=True)


# ── normalize_phone ──────────────────────────────────────────────────────────

def test_normalize_e164():
    assert normalize_phone("+254712345678") == "+254712345678"

def test_normalize_07xx():
    assert normalize_phone("0712345678") == "+254712345678"

def test_normalize_254():
    assert normalize_phone("254712345678") == "+254712345678"

def test_normalize_strips_spaces():
    assert normalize_phone(" 0712 345 678 ") == "+254712345678"

def test_validate_valid():
    assert validate_phone("+254712345678") is True

def test_validate_invalid():
    assert validate_phone("0712345678") is False
    assert validate_phone("+1") is False

# ── send ─────────────────────────────────────────────────────────────────────

def test_send_single(sms):
    result = sms.send("+254712345678", "Hello!")
    assert result.sent == 1
    assert result.failed == 0
    assert result.results[0].success

def test_send_bulk(sms):
    phones = ["+254712345678", "+254733345678", "+254700123456"]
    result = sms.send(phones, "Bulk message")
    assert result.sent == 3
    assert result.success_rate == 100.0

def test_send_normalizes_07xx(sms):
    result = sms.send("0712345678", "Normalized")
    assert result.sent == 1
    assert result.results[0].phone == "+254712345678"

def test_send_invalid_phone(sms):
    with pytest.raises(ValueError, match="Invalid phone"):
        sms.send("not-a-phone", "test")

def test_send_string_recipient(sms):
    result = sms.send("+254712345678", "Single string")
    assert result.sent == 1

# ── templates ─────────────────────────────────────────────────────────────────

def test_template_english(sms):
    result = sms.send_template(
        "+254712345678", "weather_alert", Lang.EN,
        {"county": "Nairobi", "alert": "Heavy rain"},
    )
    assert result.sent == 1

def test_template_swahili(sms):
    result = sms.send_template(
        "+254712345678", "drought_alert", Lang.SW,
        {"county": "Kitui", "severity": "75", "ndvi": "0.22"},
    )
    assert result.sent == 1

def test_template_payment_received(sms):
    result = sms.send_template(
        "+254712345678", "payment_received", Lang.EN,
        {"amount": "500", "reference": "QKL8TEST"},
    )
    assert result.sent == 1

def test_template_unknown_raises(sms):
    with pytest.raises(ValueError, match="Unknown template"):
        sms.send_template("+254712345678", "nonexistent")

def test_all_templates_have_both_langs():
    for name, langs in TEMPLATES.items():
        assert "en" in langs, f"Template {name} missing English"
        assert "sw" in langs, f"Template {name} missing Kiswahili"

# ── webhook parsing ──────────────────────────────────────────────────────────

def test_parse_delivery_report():
    payload = {
        "id": "msg123", "status": "Success",
        "phoneNumber": "+254712345678", "networkCode": "63902",
    }
    result = KenyaSMS.parse_delivery_report(payload)
    assert result["status"] == "Success"
    assert result["phone_number"] == "+254712345678"

def test_parse_inbound_sms():
    payload = {
        "from": "+254712345678", "to": "+254200000", "text": "STOP",
        "date": "2026-03-10", "id": "inbound_123",
    }
    result = KenyaSMS.parse_inbound_sms(payload)
    assert result["text"] == "STOP"
    assert result["from"] == "+254712345678"

def test_failures_property(sms):
    result = sms.send(["+254712345678", "+254733345678"], "test")
    assert result.failures == []  # sandbox: all succeed
