import unittest
from unittest.mock import patch

import check_croma_iphone as monitor


class CromaPriceGateTests(unittest.TestCase):
    def test_new_skus_require_price_strictly_below_90000(self):
        for sku in monitor.IPHONE17_NEW_PRICE_SKUS:
            for price, expected in [
                (89999, True),
                (89999.99, True),
                ("89999.99", True),
                (90000, False),
                (90000.01, False),
                (94999, False),
                (95000, False),
                (95990, False),
                (None, False),
                ("invalid", False),
                (0, False),
                (-1, False),
                (float("nan"), False),
                (float("inf"), False),
            ]:
                with self.subTest(sku=sku, price=price):
                    product = {
                        "sku": sku,
                        "name": monitor._PRODUCTS[sku]["name"],
                        "price": price,
                        "mrp": 99900,
                    }
                    self.assertEqual(monitor._passes_gate(product), expected)

    def test_price_ceiling_applies_without_discount_configuration(self):
        with patch.object(monitor, "DISCOUNT_SKUS", set()), \
                patch.object(monitor, "DISCOUNT_MODELS", set()):
            self.assertFalse(monitor._passes_gate({
                "sku": "324530", "name": "Apple iPhone 17 (256GB, Black)",
                "price": 95990, "mrp": 99900,
            }))
            for sku in ("317396", "309621"):
                with self.subTest(sku=sku):
                    self.assertTrue(monitor._passes_gate({
                        "sku": sku, "name": monitor._PRODUCTS[sku]["name"],
                        "price": 95990, "mrp": 95990,
                    }))

    def test_configured_discount_gate_is_preserved(self):
        with patch.object(monitor, "DISCOUNT_SKUS", {"324530", "317396"}):
            for sku in ("324530", "317396"):
                with self.subTest(sku=sku):
                    self.assertFalse(monitor._passes_gate({
                        "sku": sku, "name": monitor._PRODUCTS[sku]["name"],
                        "price": 89999, "mrp": 89999,
                    }))

    def test_monitor_only_sends_qualifying_products(self):
        for price, expected in [(95990, False), (95000, False), (94999, False),
                                (90000, False), (89999, True)]:
            with self.subTest(price=price):
                product = {
                    "sku": "324530", "name": monitor._PRODUCTS["324530"]["name"],
                    "price": price, "mrp": 99900,
                    "fulfillments": [{"method": "Home delivery"}],
                }
                with patch.object(monitor, "_skus", return_value=["324530"]), \
                        patch.object(monitor, "fetch_product", return_value=product), \
                        patch.object(monitor, "check_stock", return_value=product), \
                        patch.object(monitor, "should_send_alert", return_value=True), \
                        patch.object(monitor, "send_alert", return_value=True) as send, \
                        patch.object(monitor, "record_alert") as record, \
                        patch("builtins.print"):
                    self.assertEqual(monitor.check_croma_iphone(), expected)
                    if expected:
                        send.assert_called_once_with([product])
                        record.assert_called_once_with("croma_iphone")
                    else:
                        send.assert_not_called()
                        record.assert_not_called()


if __name__ == "__main__":
    unittest.main()
