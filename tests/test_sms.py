"""kenya-sms test suite — zero network calls."""
from __future__ import annotations

import json
import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch

from kenya_sms import (
    SMSClient, Template, Language, Templates, DeliveryStore,
    SendStatus, SendResult, DeliveryReceipt, normalise_phone,
)


class TestNormalisePhone:
    def test_07_prefix(self):
        assert normalise_phone("0712345678") == "254712345678"

    def test_254_prefix(self):
        assert normalise_phone("254712345678") == "254712345678"

    def test_plus_prefix(self):
        assert normalise_phone("+254712345678") == "254712345678"

    def test_9_digit(self):
        assert normalise_phone("712345678") == "254712345678"

    def test_01_safaricom_home(self):
        assert normalise_phone("0112345678") == "254112345678"

    def test_spaces_stripped(self):
        assert normalise_phone("0712 345 678") == "254712345678"

    def test_dashes_stripped(self):
        assert normalise_phone("0712-345-678") == "254712345678"

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            normalise_phone("12345")

    def test_non_kenyan_raises(self):
        with pytest.raises(ValueError):
            normalise_phone("255712345678")  # Tanzania

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            normalise_phone("")


class TestTemplate:
    def setup_method(self):
        self.tpl = Template(
            en="Dear {name}, your balance is KES {amount}.",
            sw="Habari {name}, salio lako ni KES {amount}.",
        )

    def test_render_english(self):
        result = self.tpl.render(Language.ENGLISH, name="Jane", amount="2,000")
        assert result == "Dear Jane, your balance is KES 2,000."

    def test_render_kiswahili(self):
        result = self.tpl.render(Language.KISWAHILI, name="Jane", amount="2,000")
        assert result == "Habari Jane, salio lako ni KES 2,000."

    def test_default_language_english(self):
        result = self.tpl.render(name="Jane", amount="100")
        assert result.startswith("Dear")

    def test_missing_variable_raises(self):
        with pytest.raises(ValueError, match="amount"):
            self.tpl.render(Language.ENGLISH, name="Jane")  # amount missing

    def test_character_count(self):
        n = self.tpl.character_count(Language.ENGLISH, name="Jane", amount="2,000")
        assert n == len("Dear Jane, your balance is KES 2,000.")

    def test_sms_parts_short_message(self):
        assert self.tpl.sms_parts(Language.ENGLISH, name="Jane", amount="2,000") == 1

    def test_sms_parts_long_message(self):
        long_tpl = Template(en="x" * 200, sw="y" * 200)
        assert long_tpl.sms_parts(Language.ENGLISH) == 2


class TestBuiltinTemplates:
    def test_contribution_received_renders(self):
        msg = Templates.CONTRIBUTION_RECEIVED.render(
            Language.ENGLISH, name="Jane", amount="2,000", cycle="January", receipt="NLJ7RT"
        )
        assert "Jane" in msg
        assert "2,000" in msg
        assert "NLJ7RT" in msg

    def test_contribution_reminder_kiswahili(self):
        msg = Templates.CONTRIBUTION_REMINDER.render(
            Language.KISWAHILI, name="John", amount="5,000", chama="Umoja", due_date="30 Jan"
        )
        assert "John" in msg
        assert "5,000" in msg
        assert msg.startswith("Habari")

    def test_drought_alert_renders(self):
        msg = Templates.DROUGHT_ALERT.render(
            Language.ENGLISH, county="Turkana", level="Severe",
            deficit="45", action="early harvest"
        )
        assert "Turkana" in msg
        assert "Severe" in msg

    def test_otp_renders(self):
        msg = Templates.OTP.render(Language.ENGLISH, code="483921", minutes="5")
        assert "483921" in msg
        assert "5" in msg

    def test_loan_approved_both_languages(self):
        kwargs = dict(name="Peter", amount="15,000", monthly="4,333", months="3")
        en = Templates.LOAN_APPROVED.render(Language.ENGLISH, **kwargs)
        sw = Templates.LOAN_APPROVED.render(Language.KISWAHILI, **kwargs)
        assert "Peter" in en and "Peter" in sw
        assert en != sw

    def test_all_templates_have_both_languages(self):
        templates = [
            Templates.CONTRIBUTION_RECEIVED,
            Templates.CONTRIBUTION_REMINDER,
            Templates.LOAN_APPROVED,
            Templates.DROUGHT_ALERT,
            Templates.PAYMENT_PROMPT,
            Templates.OTP,
        ]
        for tpl in templates:
            assert tpl.en
            assert tpl.sw
            assert tpl.en != tpl.sw


class TestDeliveryStore:
    def test_save_and_retrieve(self):
        store = DeliveryStore()
        receipt = DeliveryReceipt(
            message_id="MSG001", phone="254712345678",
            status="Success", failure_reason=None, network_code="63902"
        )
        store.save(receipt)
        assert store.get("MSG001") is receipt

    def test_get_missing_returns_none(self):
        store = DeliveryStore()
        assert store.get("NONEXISTENT") is None

    def test_all_returns_all(self):
        store = DeliveryStore()
        for i in range(5):
            store.save(DeliveryReceipt(f"MSG{i}", "254712345678", "Success", None, None))
        assert len(store.all()) == 5

    def test_failed_filters_correctly(self):
        store = DeliveryStore()
        store.save(DeliveryReceipt("MSG1", "254712345678", "Success", None, None))
        store.save(DeliveryReceipt("MSG2", "254723456789", "Failed", "InsufficientCredit", None))
        store.save(DeliveryReceipt("MSG3", "254734567890", "DeliveredToNetwork", None, None))
        failed = store.failed()
        assert len(failed) == 1
        assert failed[0].message_id == "MSG2"


def make_client(sandbox=True):
    return SMSClient(api_key="test_key", username="sandbox", sandbox=sandbox)


def at_response(phone="254712345678", status="Success", msg_id="MSG001", cost="KES 1.0000"):
    return {
        "SMSMessageData": {
            "Message": "Sent to 1/1 Total Cost: KES 1",
            "Recipients": [{
                "statusCode": 101,
                "number": f"+{phone}",
                "status": status,
                "cost": cost,
                "messageId": msg_id,
            }]
        }
    }


class TestSMSClientSend:
    def test_send_success(self):
        client = make_client()
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(at_response()).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = client.send("0712345678", "Test message")

        assert result.succeeded
        assert result.phone == "254712345678"
        assert result.message_id == "MSG001"
        assert result.cost == "KES 1.0000"

    def test_send_normalises_phone(self):
        client = make_client()
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(at_response(phone="254723456789")).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = client.send("0723456789", "Test")

        assert result.phone == "254723456789"

    def test_send_failure_status(self):
        client = make_client()
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            at_response(status="InvalidPhoneNumber")
        ).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = client.send("0712345678", "Test")

        assert not result.succeeded
        assert result.status == SendStatus.FAILED

    def test_send_network_error_returns_failed(self):
        client = make_client()
        with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
            result = client.send("0712345678", "Test")

        assert not result.succeeded
        assert "Connection refused" in result.error

    def test_send_template(self):
        client = make_client()
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(at_response()).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = client.send_template(
                "0712345678",
                Templates.CONTRIBUTION_RECEIVED,
                Language.KISWAHILI,
                name="Jane", amount="2,000", cycle="January", receipt="NLJ7RT",
            )
        assert result.succeeded

    def test_sender_id_included_when_set(self):
        client = SMSClient("key", "sandbox", sender_id="MYAPP", sandbox=True)
        captured = {}

        def mock_urlopen(req, timeout=None):
            captured["body"] = urllib.parse.parse_qs(req.data.decode())
            mock_response = MagicMock()
            mock_response.read.return_value = json.dumps(at_response()).encode()
            mock_response.__enter__ = lambda s: s
            mock_response.__exit__ = MagicMock(return_value=False)
            return mock_response

        import urllib.parse
        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            client.send("0712345678", "Test")

        assert captured["body"].get("from") == ["MYAPP"]


class TestDeliveryReceiptHandling:
    def test_parse_delivery_receipt(self):
        client = make_client()
        payload = {
            "id": "MSG001",
            "status": "Success",
            "phoneNumber": "+254712345678",
            "networkCode": "63902",
        }
        receipt = client.handle_delivery_receipt(payload)
        assert receipt.message_id == "MSG001"
        assert receipt.status == "Success"
        assert receipt.phone == "+254712345678"

    def test_receipt_stored(self):
        client = make_client()
        client.handle_delivery_receipt({"id": "MSG002", "status": "Success",
                                        "phoneNumber": "+254712345678", "networkCode": "63902"})
        assert client.delivery_store.get("MSG002") is not None

    def test_failure_reason_captured(self):
        client = make_client()
        payload = {"id": "MSG003", "status": "Failed",
                   "phoneNumber": "+254712345678", "networkCode": "63902",
                   "failureReason": "InsufficientCredit"}
        receipt = client.handle_delivery_receipt(payload)
        assert receipt.failure_reason == "InsufficientCredit"
