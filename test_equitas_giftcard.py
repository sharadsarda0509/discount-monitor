import base64
import hashlib
import hmac
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import check_discount
import check_equitas_giftcard as monitor


def product(denominations):
    return {
        "Success": True,
        "Data": {
            "Id": monitor.PRODUCT_ID,
            "PriceType": "Slab",
            "Denominations": denominations,
        },
    }


class EquitasApiTests(unittest.TestCase):
    def test_reads_public_constants_from_rotated_string_table_without_execution(self):
        source = (
            "function table(){const values=['encrypted','unused','equitas','program'];}"
            "const env={'programId':lookup(0x101),'ee':lookup(0x102),"
            "'baseUrl':lookup(0x103),'pgUrl':lookup(0x103),'shortName':lookup(0x100)};"
        )
        self.assertEqual(monitor.extract_public_config(source), {
            "program_id": "program", "encrypted_key": "encrypted",
        })
        with self.assertRaises(ValueError):
            monitor.extract_public_config("unexpected bundle format")

    def test_decrypts_and_authenticates_api_payload_with_32_byte_nonce(self):
        client = object.__new__(monitor.EquitasClient)
        client.response_key = b"k" * 32
        nonce = b"n" * 32
        payload = product(["500"])
        ciphertext = nonce + AESGCM(client.response_key).encrypt(
            nonce, json.dumps(payload).encode(), None,
        )
        response = Mock(text=base64.b64encode(ciphertext).decode())
        self.assertEqual(client.decode_response(response), payload)
        response.text = base64.b64encode(
            nonce + AESGCM(client.response_key).encrypt(
                nonce, b'{"Success":false}', None,
            ),
        ).decode()
        with self.assertRaises(ValueError):
            client.decode_response(response)

    def test_signs_api_request_and_uses_fresh_anonymous_token(self):
        client = object.__new__(monitor.EquitasClient)
        client.session = Mock()
        client.config = {
            "apiDetails": {"egv": {
                "baseUrl": "https://egvb9.loyltytech.com/V2", "moduleId": "gift-module",
            }},
            "applicationId": "app",
        }
        client.program_id, client.client_id = "program", "383"
        client.client_key, client.client_secret, client.token = "key", "secret", "anonymous-token"
        url = "https://egvb9.loyltytech.com/V2/GiftCard/Products/M11082265"
        with patch.object(monitor.os, "urandom", return_value=b"12345678"), \
                patch.object(monitor.time, "time_ns", return_value=123_000_000), \
                patch.object(client, "decode_response", return_value=product(["500"])):
            client.get("egv", f"GiftCard/Products/{monitor.PRODUCT_ID}")
        call = client.session.get.call_args
        self.assertEqual(call.args, (url,))
        headers = call.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer anonymous-token")
        raw = "383|GET|https%3a%2f%2fegvb9.loyltytech.com%2fv2%2fgiftcard%2fproducts%2fm11082265|MTIzNDU2Nzg=|123|"
        signature = base64.b64encode(hmac.new(
            b"secret", raw.encode(), hashlib.sha256,
        ).digest()).decode()
        self.assertEqual(headers["sign_auth"], f"key:{signature}:MTIzNDU2Nzg=:123")

    def test_only_explicit_available_denominations_count(self):
        self.assertEqual(monitor.available_denominations(product(["500"])), {500})
        self.assertEqual(
            monitor.available_denominations(product(["500", "5000", 10000])),
            {500, 5000, 10000},
        )
        self.assertEqual(monitor.available_denominations(product([])), set())
        for values in (None, {}, [True], [5000.5], ["5000.0"], ["invalid"]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                monitor.available_denominations(product(values))
        for payload in (
            {"Success": False, "Data": product(["5000"])["Data"]},
            {"Success": True, "Data": {}},
            {"Success": True, "Data": {**product(["5000"])["Data"], "Id": "other"}},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                monitor.available_denominations(payload)


class EquitasMonitorTests(unittest.TestCase):
    def test_alert_is_suppressed_for_full_24_hours_without_skipping_stock_checks(self):
        now = monitor.get_ist_now()
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "last_alert.json"
            state_file.write_text(json.dumps({
                "equitas_amazon_5000": (now - timedelta(hours=24) + timedelta(seconds=1)).isoformat(),
                "equitas_amazon_10000": (now - timedelta(hours=24)).isoformat(),
            }))
            with patch.object(check_discount, "STATE_DIR", Path(directory)), \
                    patch.object(check_discount, "STATE_FILE", state_file), \
                    patch.object(check_discount, "COOLDOWN_HOURS", 24), \
                    patch.object(check_discount, "get_ist_now", return_value=now), \
                    patch.object(monitor, "EquitasClient") as client, \
                    patch.object(monitor, "send_alert") as send, \
                    patch.object(monitor, "record_alert"):
                client.return_value.get.return_value = product(["5000", "10000"])
                self.assertTrue(monitor.check_equitas_giftcard())
            client.return_value.get.assert_called_once_with(
                "egv", f"GiftCard/Products/{monitor.PRODUCT_ID}",
            )
            send.assert_called_once_with([10000])

    def test_500_only_never_alerts(self):
        with patch.object(monitor, "EquitasClient") as client, \
                patch.object(monitor, "send_alert") as send, \
                patch.object(monitor, "record_alert") as record:
            client.return_value.get.return_value = product(["500"])
            self.assertFalse(monitor.check_equitas_giftcard())
        send.assert_not_called()
        record.assert_not_called()

    def test_cooldown_is_independent_for_each_target(self):
        with patch.object(monitor, "EquitasClient") as client, \
                patch.object(monitor, "should_send_alert", side_effect=[False, True]) as due, \
                patch.object(monitor, "send_alert") as send, \
                patch.object(monitor, "record_alert") as record:
            client.return_value.get.return_value = product(["500", "5000", "10000"])
            self.assertTrue(monitor.check_equitas_giftcard())
        self.assertEqual(
            [call.args[0] for call in due.call_args_list],
            ["equitas_amazon_5000", "equitas_amazon_10000"],
        )
        send.assert_called_once_with([10000])
        record.assert_called_once_with("equitas_amazon_10000")

    def test_failed_delivery_does_not_start_cooldown(self):
        with patch.object(monitor, "EquitasClient") as client, \
                patch.object(monitor, "should_send_alert", return_value=True), \
                patch.object(monitor, "send_alert", side_effect=RuntimeError("delivery failed")), \
                patch.object(monitor, "record_alert") as record:
            client.return_value.get.return_value = product(["5000"])
            with self.assertRaisesRegex(RuntimeError, "delivery failed"):
                monitor.check_equitas_giftcard()
        record.assert_not_called()


if __name__ == "__main__":
    unittest.main()
