#!/usr/bin/env python3
"""Monitor Equitas Amazon Pay voucher denominations using its anonymous API."""

import ast
import base64
import hashlib
import hmac
import json
import os
import re
import smtplib
import sys
import time
from email.mime.text import MIMEText
from urllib.parse import quote, urljoin, urlparse

import requests
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from check_discount import STATE_DIR, get_ist_now, record_alert, should_send_alert

ORIGIN = "https://xchange.equitas.bank.in"
PRODUCT_ID = "M11082265"
PRODUCT_URL = f"{ORIGIN}/gift-voucher/product/{PRODUCT_ID}"
TARGET_DENOMINATIONS = (5000, 10000)
PUBLIC_CONFIG_FILE = STATE_DIR / "equitas_public_client.json"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/131.0.0.0 Safari/537.36"


def extract_public_config(source):
    """Read the public environment constants without executing downloaded JS."""
    table_match = re.search(r"const\s+\w+\s*=\s*(\['.*?\]);", source, re.DOTALL)
    environment = re.search(
        r"'programId':([^,]+),'ee':([^,]+),'baseUrl':[^,]+,"
        r"'pgUrl':[^,]+,'shortName':([^,}]+)", source,
    )
    if not table_match or not environment:
        raise ValueError("Equitas public client configuration format changed")
    strings = [
        ast.literal_eval(match.group())
        for match in re.finditer(r"'(?:\\.|[^'\\])*'", table_match[1])
    ]
    anchor = re.fullmatch(r"\w+\((0x[0-9a-f]+)\)", environment[3])
    if not anchor or strings.count("equitas") != 1:
        raise ValueError("Cannot resolve Equitas public client string table")
    anchor_index = int(anchor[1], 16)
    table_index = strings.index("equitas")

    def resolve(expression):
        calls = expression.split("+")
        values = []
        for call in calls:
            match = re.fullmatch(r"\w+\((0x[0-9a-f]+)\)", call)
            if not match:
                raise ValueError("Unsupported Equitas public constant expression")
            index = (int(match[1], 16) - anchor_index + table_index) % len(strings)
            values.append(strings[index])
        return "".join(values)

    return {"program_id": resolve(environment[1]), "encrypted_key": resolve(environment[2])}


def decode_config_value(value, program_id):
    key = program_id.replace("-", "").encode("ascii")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
    padded = decryptor.update(base64.b64decode(value, validate=True)) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")


class EquitasClient:
    def __init__(self, session):
        self.session = session
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Origin": ORIGIN,
            "Referer": PRODUCT_URL,
        })
        page = self.session.get(PRODUCT_URL, timeout=30)
        page.raise_for_status()
        script = re.search(r'<script[^>]+src=["\'](main\.[^"\']+\.js)["\']', page.text)
        if not script:
            raise ValueError("Equitas main application bundle not found")
        bundle_url = urljoin(ORIGIN + "/", script[1])
        cached = None
        if PUBLIC_CONFIG_FILE.exists():
            cached = json.loads(PUBLIC_CONFIG_FILE.read_text())
        if cached and cached.get("bundle_url") == bundle_url:
            public = cached
        else:
            bundle = self.session.get(bundle_url, timeout=60)
            bundle.raise_for_status()
            public = {**extract_public_config(bundle.text), "bundle_url": bundle_url}
            STATE_DIR.mkdir(exist_ok=True)
            # Cache only public, encrypted bootstrap constants, never API tokens/secrets.
            PUBLIC_CONFIG_FILE.write_text(json.dumps(public))
        self.program_id = public["program_id"]
        self.response_key = decode_config_value(public["encrypted_key"], self.program_id).encode()
        response = self.session.post(
            f"{ORIGIN}/portalapi/bankProgram/getBankProgram",
            headers={"programId": self.program_id, "shortName": "equitas",
                     "Content-Type": "application/json"},
            timeout=30,
        )
        self.config = self.decode_response(response)["Data"][0]["programConfig"]
        self.client_id = (
            decode_config_value(self.config["m"], self.program_id)
            if self.config.get("m") else str(self.config["clientId"])
        )
        self.client_key = self.config_part(("l", "f", "z"), "clientKey")
        self.client_secret = self.config_part(("t", "s", "u"), "clientSecret")
        self.token = None
        self.token = self.get("common", "GenerateToken")["Data"]["accessToken"]

    def config_part(self, keys, fallback):
        if all(self.config.get(key) for key in keys):
            value = "".join(
                self.config[key].replace("==", "") if i < len(keys) - 1 else self.config[key]
                for i, key in enumerate(keys)
            )
            return decode_config_value(value, self.program_id)
        return self.config[fallback]

    def decode_response(self, response):
        response.raise_for_status()
        encrypted = base64.b64decode(response.text, validate=True)
        plaintext = AESGCM(self.response_key).decrypt(encrypted[:32], encrypted[32:], None)
        payload = json.loads(plaintext)
        if not isinstance(payload, dict) or payload.get("Success") is not True:
            raise ValueError("Equitas API returned an unsuccessful response")
        return payload

    def get(self, module, path):
        details = self.config["apiDetails"][module]
        url = details["baseUrl"].rstrip("/") + "/" + path
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in {
            "comb9.loyltytech.com", "egvb9.loyltytech.com",
        }:
            raise ValueError("Unexpected Equitas API origin")
        nonce = base64.b64encode(os.urandom(8)).decode()
        timestamp = str(time.time_ns() // 1_000_000)
        encoded_url = quote(url, safe="~()*!.'-").lower()
        message = f"{self.client_id}|GET|{encoded_url}|{nonce}|{timestamp}|"
        signature = base64.b64encode(hmac.new(
            self.client_secret.encode(), message.encode(), hashlib.sha256,
        ).digest()).decode()
        headers = {
            "client_id": self.client_id,
            "channel": "WEB",
            "applicationid": self.config["applicationId"],
            "programid": self.program_id,
            "moduleid": details["moduleId"],
            "sign_auth": f"{self.client_key}:{signature}:{nonce}:{timestamp}",
            "userip": "127.0.0.1",
            "useragent": USER_AGENT,
            "Content-Type": "application/json",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return self.decode_response(self.session.get(url, headers=headers, timeout=30))


def available_denominations(payload):
    if not isinstance(payload, dict) or payload.get("Success") is not True:
        raise ValueError("Unsuccessful Equitas product response")
    product = payload.get("Data")
    if not isinstance(product, dict) or product.get("Id") != PRODUCT_ID:
        raise ValueError("Unexpected Equitas product")
    denominations = product.get("Denominations")
    if product.get("PriceType") != "Slab" or not isinstance(denominations, list):
        raise ValueError("Equitas denomination availability is missing or unsupported")
    available = set()
    for value in denominations:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError("Invalid Equitas denomination")
        if not re.fullmatch(r"[1-9][0-9]*", str(value)):
            raise ValueError("Invalid Equitas denomination")
        available.add(int(value))
    return available


def send_alert(denominations):
    amounts = ", ".join(f"Rs.{amount}" for amount in denominations)
    title = f"Equitas Amazon Pay voucher in stock: {amounts}"
    body = f"{title}\n\n{PRODUCT_URL}\n\nChecked at: {get_ist_now()}"
    sent = False
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        try:
            requests.post(
                f"https://ntfy.sh/{topic}", data=body.encode(),
                headers={"Title": title, "Priority": "high", "Tags": "gift,shopping",
                         "Click": PRODUCT_URL}, timeout=15,
            ).raise_for_status()
            print("ntfy notification sent")
            sent = True
        except requests.RequestException as error:
            print(f"ntfy notification failed: {error}", file=sys.stderr)
    sender = os.environ.get("SENDER_EMAIL")
    receiver = os.environ.get("RECEIVER_EMAIL")
    password = os.environ.get("EMAIL_PASSWORD")
    if all((sender, receiver, password)):
        message = MIMEText(body)
        message["Subject"], message["From"], message["To"] = title, sender, receiver
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
                server.login(sender, password)
                server.send_message(message)
            print("Email notification sent")
            sent = True
        except (smtplib.SMTPException, OSError) as error:
            print(f"Email notification failed: {error}", file=sys.stderr)
    if not sent:
        raise RuntimeError("No Equitas alert delivered; check notification configuration/errors")


def check_equitas_giftcard():
    with requests.Session() as session:
        client = EquitasClient(session)
        available = available_denominations(client.get("egv", f"GiftCard/Products/{PRODUCT_ID}"))
    print(f"[{get_ist_now()}] Equitas available denominations: {sorted(available)}")
    due = []
    for amount in TARGET_DENOMINATIONS:
        print(f"  Rs.{amount}: {'IN STOCK' if amount in available else 'OOS'}")
        if amount in available and should_send_alert(f"equitas_amazon_{amount}"):
            due.append(amount)
    if not due:
        return False
    send_alert(due)
    for amount in due:
        record_alert(f"equitas_amazon_{amount}")
    return True


if __name__ == "__main__":
    try:
        check_equitas_giftcard()
    except Exception as error:
        print(f"Equitas monitor failed ({type(error).__name__}): {error}", file=sys.stderr)
        sys.exit(1)
