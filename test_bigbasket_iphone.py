import json
import unittest
from unittest.mock import patch

import check_bigbasket_iphone as monitor


def product(availability):
    return {
        "id": "40332363",
        "desc": "Apple iPhone 15 (128GB, Blue)",
        "absolute_url": "/pd/40332363/apple-iphone-15-128gb-blue-1-unit/",
        "availability": availability,
        "pricing": {"discount": {"prim_price": {"sp": "59900"}, "mrp": "69900"}},
    }


def listing(products):
    return {"tabs": [{"product_info": {"products": products}}]}


class BigBasketStockTests(unittest.TestCase):
    def scan(self, data):
        with patch.object(monitor.scrapfly_reader, "fetch", return_value={
            "status": 200, "text": json.dumps(data),
        }), patch.object(monitor, "SCRAPFLY_LISTING_PAGES", 1):
            return monitor._scrapfly_scan()

    def test_scrapfly_requests_the_website_storefront(self):
        with patch.object(monitor.scrapfly_reader, "fetch", return_value={
            "status": 200, "text": json.dumps(listing([])),
        }) as fetch, patch.object(monitor, "SCRAPFLY_LISTING_PAGES", 1):
            monitor._scrapfly_scan()
        headers = fetch.call_args.kwargs.get("headers", {})
        self.assertEqual(headers.get("x-channel"), "BB-WEB")
        self.assertEqual(headers.get("x-entry-context"), "bbnow")
        self.assertEqual(headers.get("x-entry-context-id"), "10")
        cookies = fetch.call_args.kwargs["cookies"]
        self.assertEqual(cookies.get("xentrycontext"), "bbnow")
        self.assertEqual(cookies.get("xentrycontextid"), "10")
        self.assertEqual(cookies.get("jentrycontextid"), "10")

    def test_all_stock_paths_require_explicit_orderability(self):
        cases = [
            ({"avail_status": "001", "button": "Add", "not_for_sale": False}, True),
            ({"avail_status": "000", "button": "Notify Me", "not_for_sale": False}, False),
            ({"avail_status": "001", "button": "Add", "not_for_sale": True}, False),
            ({"avail_status": "001", "button": "Notify Me", "not_for_sale": False}, False),
            ({"avail_status": "001", "not_for_sale": False}, False),
            ({"avail_status": "001", "button": "Add"}, False),
            ({"avail_status": "001", "button": "Add", "not_for_sale": None}, False),
            ({"avail_status": "001", "button": "Add", "not_for_sale": "false"}, False),
            ({"avail_status": "001", "button": "Coming Soon", "not_for_sale": False}, False),
            ({}, False),
        ]
        for availability, expected in cases:
            with self.subTest(availability=availability):
                p = product(availability)
                pd = monitor._parse_pd(p["id"], "iphone", {"product": p})
                self.assertEqual(pd["in_stock"], expected)
                infos = self.scan(listing([p]))
                self.assertEqual(infos[0]["in_stock"], expected)

    def test_catalog_variants_are_not_stock_evidence(self):
        data = listing([product({
            "avail_status": "000", "button": "Notify Me", "not_for_sale": False,
        })])
        available = product({
            "avail_status": "001", "button": "Add", "not_for_sale": False,
        })
        data["variant_product_info"] = [available]
        data["consolidated_variants_info"] = [available]
        self.assertFalse(self.scan(data)[0]["in_stock"])

    def test_invalid_success_response_is_a_failed_scan(self):
        for data in ({}, {"tabs": None}, {"tabs": {}}, [], None):
            with self.subTest(data=data), patch("builtins.print"):
                self.assertIsNone(self.scan(data))

    def test_empty_valid_listing_is_not_a_failed_scan(self):
        self.assertEqual(self.scan(listing([])), [])

    def test_direct_and_browser_requests_share_storefront(self):
        p = product({"avail_status": "001", "button": "Add", "not_for_sale": False})
        with patch.object(monitor, "_get") as get:
            get.return_value.status_code = 200
            get.return_value.json.return_value = {"product": p}
            self.assertTrue(monitor.check_product("build", p["id"], "iphone")["in_stock"])
        direct_headers = get.call_args.kwargs["headers"]
        with patch.object(monitor.brightdata_browser, "browser_fetch", return_value=[{
            "status": 200, "text": json.dumps({"product": p}),
        }]) as fetch:
            infos = monitor._browser_check("build", {p["id"]: "iphone"})
            self.assertTrue(infos[0]["in_stock"])
        browser_headers = fetch.call_args.args[1][0]["headers"]
        for headers in (direct_headers, browser_headers):
            self.assertEqual(headers["x-entry-context"], "bbnow")
            self.assertEqual(headers["x-entry-context-id"], "10")
        cookies = {c["name"]: c["value"] for c in fetch.call_args.kwargs["set_cookies"]}
        self.assertEqual(cookies["xentrycontextid"], "10")

    def test_failed_http_and_bad_json_are_not_stock_evidence(self):
        for response in (
            {"status": 403, "text": "Access Denied"},
            {"status": 200, "text": "not json"},
            {"status": 200, "text": json.dumps({"tabs": [{"product_info": {}}]})},
        ):
            with self.subTest(response=response), \
                    patch.object(monitor.scrapfly_reader, "fetch", return_value=response), \
                    patch.object(monitor, "SCRAPFLY_RETRIES", 0), \
                    patch.object(monitor, "SCRAPFLY_LISTING_PAGES", 1), \
                    patch("builtins.print"):
                self.assertIsNone(monitor._scrapfly_scan())

    def test_monitor_alerts_on_explicitly_orderable_handset(self):
        data = listing([product({
            "avail_status": "001", "button": "Add", "not_for_sale": False,
        })])
        with patch.object(monitor.scrapfly_reader, "is_enabled", return_value=True), \
                patch.object(monitor.scrapfly_reader, "fetch", return_value={
                    "status": 200, "text": json.dumps(data),
                }), patch.object(monitor, "_recently_scraped", return_value=False), \
                patch.object(monitor, "_mark_scraped"), \
                patch.object(monitor, "should_send_alert", return_value=True), \
                patch.object(monitor, "send_alert", return_value=True) as send, \
                patch.object(monitor, "record_alert") as record, \
                patch("builtins.print"):
            self.assertTrue(monitor.check_bigbasket_iphone())
        self.assertEqual(send.call_args.args[0][0]["id"], "40332363")
        record.assert_called_once_with("bigbasket_iphone")

    def test_monitor_never_alerts_on_ambiguous_availability(self):
        data = listing([product({"avail_status": "001"})])
        with patch.object(monitor.scrapfly_reader, "is_enabled", return_value=True), \
                patch.object(monitor.scrapfly_reader, "fetch", return_value={
                    "status": 200, "text": json.dumps(data),
                }), patch.object(monitor, "_recently_scraped", return_value=False), \
                patch.object(monitor, "_mark_scraped"), \
                patch.object(monitor, "send_alert") as send, \
                patch.object(monitor, "record_alert") as record, \
                patch("builtins.print"):
            self.assertFalse(monitor.check_bigbasket_iphone())
        send.assert_not_called()
        record.assert_not_called()


if __name__ == "__main__":
    unittest.main()
