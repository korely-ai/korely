"""Webhook signatures, checked by the Standard Webhooks reference verifier.

The dashboard, the module docstring and the headers all said Standard
Webhooks, and the signature was HMAC-SHA256 keyed by the whole `whsec_...`
string, over a secret made of URL-safe base64. The spec
(standard-webhooks.md, "Signature scheme" and the symmetric row of its table)
keys the HMAC with the base64-DECODED part after `whsec_`, a random secret of
24 to 64 bytes in standard base64. So `standardwebhooks` and `svix` rejected
every delivery, and the JS reference library threw on the secret itself.

No database: these run in `make test`.
"""
import base64
import hashlib
import hmac
import json
import re
import secrets
import typing as t
from datetime import datetime, timedelta, timezone
from math import floor
from pathlib import Path

import pytest

from korely_agent.services import webhooks

ROOT = Path(__file__).resolve().parents[1]


# ── the reference verifier, verbatim ───────────────────────────────────────
#
# standard-webhooks/standard-webhooks, libraries/python/standardwebhooks/
# webhooks.py @ adda72a6e707672d0b0a265a0f7ccb9a247874a3, from `hmac_data` to
# the end of the `Webhook` class. MIT License, Copyright (c) 2023 Standard
# Webhooks (libraries/python/LICENSE). Copied rather than installed so the
# suite needs no new dependency. This is what a customer's `standardwebhooks`
# library runs: a delivery it rejects is one nobody can verify with the
# official tooling.

def hmac_data(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


class WebhookVerificationError(Exception):
    pass


class EmptyWebhookSecretError(Exception):
    pass


class Webhook:
    _SECRET_PREFIX: str = "whsec_"
    _whsecret: bytes

    def __init__(self, whsecret: t.Union[str, bytes]):
        if isinstance(whsecret, str):
            if whsecret.startswith(self._SECRET_PREFIX):
                whsecret = whsecret[len(self._SECRET_PREFIX) :]
            # add padding in case whsecret is unpadded base64 (b64decode skips extra padding)
            whsecret = base64.b64decode(whsecret + "==")

        if not whsecret:
            raise EmptyWebhookSecretError("webhook secret may not be empty")

        if isinstance(whsecret, bytes):
            self._whsecret = whsecret
        else:
            raise RuntimeError("Invalid webhook secret")

    def verify(
        self,
        data: t.Union[bytes, str],
        headers: t.Dict[str, str],
        *,
        json_parse: bool = True,
    ) -> t.Any:
        """
        Verify the given webhook headers against the body bytes (data).

        Args:
            json_parse (bool): Whether to parse the data as JSON on success (default: True)

        Returns:
            After successful verification: if json_parse is True, returns the
            JSON-parsed input data; if it json_parse False, returns None.

        Raises:
            WebhookVerificationError if one of the required headers is missing,
            invalid, too old or too new; or no matching signature is found.
        """

        data = data if isinstance(data, str) else data.decode()
        headers = {k.lower(): v for (k, v) in headers.items()}
        msg_id = headers.get("webhook-id")
        msg_signature = headers.get("webhook-signature")
        msg_timestamp = headers.get("webhook-timestamp")
        if not (msg_id and msg_timestamp and msg_signature):
            raise WebhookVerificationError("Missing required headers")

        timestamp = self.__verify_timestamp(msg_timestamp)

        expected_sig = base64.b64decode(self.sign(msg_id=msg_id, timestamp=timestamp, data=data).split(",")[1])
        passed_sigs = msg_signature.split(" ")
        for versioned_sig in passed_sigs:
            (version, signature) = versioned_sig.split(",")
            if version != "v1":
                continue
            sig_bytes = base64.b64decode(signature)
            if hmac.compare_digest(expected_sig, sig_bytes):
                if json_parse:
                    return json.loads(data)
                else:
                    return

        raise WebhookVerificationError("No matching signature found")

    def sign(self, msg_id: str, timestamp: datetime, data: str) -> str:
        timestamp_str = str(floor(timestamp.replace(tzinfo=timezone.utc).timestamp()))
        to_sign = f"{msg_id}.{timestamp_str}.{data}".encode()
        signature = hmac_data(self._whsecret, to_sign)
        return f"v1,{base64.b64encode(signature).decode('utf-8')}"

    def __verify_timestamp(self, timestamp_header: str) -> datetime:
        webhook_tolerance = timedelta(minutes=5)
        now = datetime.now(tz=timezone.utc)
        try:
            timestamp = datetime.fromtimestamp(float(timestamp_header), tz=timezone.utc)
        except Exception:
            raise WebhookVerificationError("Invalid Signature Headers")

        if timestamp < (now - webhook_tolerance):
            raise WebhookVerificationError("Message timestamp too old")
        if timestamp > (now + webhook_tolerance):
            raise WebhookVerificationError("Message timestamp too new")
        return timestamp


# ── helpers ────────────────────────────────────────────────────────────────

def _delivery(monkeypatch, secret: str, payload: dict) -> tuple[str, dict]:
    """(body, headers) exactly as `_deliver` puts them on the wire: the POST
    to the checked address (`_post`, since 2026-10-06)."""
    import ipaddress
    import os

    sent = {}

    def post(url, body, headers, **how):
        sent.update(content=body, headers=headers)
        return 204

    monkeypatch.setattr(webhooks, "checked_destination",
                        lambda u: (u, [ipaddress.ip_address("203.0.113.7")]))
    monkeypatch.setattr(webhooks, "_post", post)
    for name in list(os.environ):
        if name.lower().endswith("_proxy"):
            monkeypatch.delenv(name)
    ok, code, err = webhooks._deliver("https://hooks.example/k", secret,
                                      "4b7c0e2a-6f1d-4c8e-9a3b-1d2e3f4a5b6c", payload)
    assert (ok, code, err) == (True, 204, None)
    return sent["content"], sent["headers"]


PAYLOAD = {"event": "memory.created", "data": {"id": "mem_1", "user_id": "maria"}}


# ── the tests ──────────────────────────────────────────────────────────────

class TestTheSpec:
    def test_the_reference_librarys_own_vector(self):
        """`test_sign_function` in the reference suite, same inputs, same
        expected string. Its secret is 24 bytes, 32 characters: see
        TestOldEndpoints for why that shape, as a stored secret, keeps the old
        key here, and so why the vector is checked on the key."""
        key = base64.b64decode("MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw")
        expected = "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="
        ours = webhooks.sign_with_key(key, "msg_p5jXN8AQM9LWM0D4loKWxJek",
                                      1614265330, '{"test": 2432232314}')
        assert ours == expected
        assert Webhook("whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw").sign(
            "msg_p5jXN8AQM9LWM0D4loKWxJek",
            datetime.fromtimestamp(1614265330, tz=timezone.utc),
            '{"test": 2432232314}') == expected, "the copy above is not the reference"

    def test_a_new_secret_is_what_the_spec_asks_for(self):
        """"Random. Between 24 bytes (192 bits) and 64 bytes (512 bits)",
        "base64 encoded, prefixed with whsec_". Standard alphabet and
        canonical, so the strict decoders take it (the JS reference uses
        @stablelib/base64), and short enough for the String(100) column."""
        for _ in range(200):
            s = webhooks.generate_secret()
            assert s.startswith("whsec_")
            body = s[len("whsec_"):]
            raw = base64.b64decode(body, validate=True)
            assert base64.b64encode(raw).decode() == body
            assert 24 <= len(raw) <= 64
            assert len(s) <= 100
            assert not webhooks.is_legacy_secret(s)
            assert webhooks.signing_key(s) == raw

    def test_every_new_secret_signs_what_the_reference_signs(self):
        at = datetime.fromtimestamp(1759000000, tz=timezone.utc)
        for _ in range(200):
            s = webhooks.generate_secret()
            assert webhooks.sign_payload(s, "msg_1", 1759000000, '{"a":1}') == \
                Webhook(s).sign("msg_1", at, '{"a":1}')

    def test_a_real_delivery_verifies_and_a_tampered_one_does_not(self, monkeypatch):
        secret = webhooks.generate_secret()
        body, headers = _delivery(monkeypatch, secret, PAYLOAD)
        assert {"webhook-id", "webhook-timestamp", "webhook-signature"} <= set(headers)
        assert Webhook(secret).verify(body, headers) == PAYLOAD
        with pytest.raises(WebhookVerificationError):
            Webhook(secret).verify(body.replace("maria", "mario"), headers)
        with pytest.raises(WebhookVerificationError):
            Webhook(webhooks.generate_secret()).verify(body, headers)


class TestOurVerifier:
    """`webhooks.verify`, the one verification of this codebase (the Cloud's
    Polar webhooks go through it too): it takes what the reference takes and
    refuses what the reference refuses."""

    def test_it_verifies_a_real_delivery_and_not_a_tampered_one(self, monkeypatch):
        secret = webhooks.generate_secret()
        body, headers = _delivery(monkeypatch, secret, PAYLOAD)
        ts = int(headers["webhook-timestamp"])
        kw = dict(webhook_id=headers["webhook-id"], timestamp=headers["webhook-timestamp"],
                  signature_header=headers["webhook-signature"], now=ts)
        webhooks.verify(webhooks.signing_key(secret), body=body, **kw)
        with pytest.raises(webhooks.SignatureError):
            webhooks.verify(webhooks.signing_key(secret), body=body.replace("maria", "mario"), **kw)
        with pytest.raises(webhooks.SignatureError):
            webhooks.verify(webhooks.signing_key(secret), body=body,
                            **{**kw, "now": ts + webhooks.TOLERANCE_SECONDS + 1})

    def test_a_bare_prefix_signs_nothing(self):
        """The reference library refuses an empty secret; a bare `whsec_` was
        signed with the six characters `whsec_` here and with an empty key in
        the Polar copy (audit of 17 Oct 2026). Neither: there is no key."""
        assert webhooks.secret_key("whsec_") == b""
        with pytest.raises(webhooks.SignatureError):
            webhooks.sign_payload("whsec_", "msg_1", 1759000000, '{"a":1}')
        with pytest.raises(EmptyWebhookSecretError):
            Webhook("whsec_")

    def test_a_delivery_with_no_key_is_refused_not_sent(self, monkeypatch):
        sent = []
        monkeypatch.setattr(webhooks, "_post", lambda *a, **k: sent.append(a) or 204)
        monkeypatch.setattr(webhooks, "checked_destination",
                            lambda url: (url, ["93.184.216.34"]))
        ok, code, err = webhooks._deliver("https://hooks.example/k", "whsec_", "d1", PAYLOAD)
        assert (ok, code, sent) == (False, None, []) and err.startswith("refused:")


class TestOldEndpoints:
    """Every endpoint created before 0.1.11 has a `token_urlsafe(24)` secret,
    and every receiver that verifies one today does it the way the
    instructions of that time said: the whole string as the key. Those keep
    working. Deciding by "is it valid standard base64" would not do it: about
    a third of those secrets have no `-` or `_` and are valid standard base64,
    and would have moved to a new key without a word."""

    def _literal(self, secret, body='{"event":"memory.created"}'):
        return "v1," + base64.b64encode(hmac.new(
            secret.encode(), f"wid_1.1750000000.{body}".encode(), hashlib.sha256
        ).digest()).decode()

    def test_every_old_secret_keeps_the_whole_string_as_its_key(self):
        valid_base64 = 0
        for _ in range(2000):
            old = "whsec_" + secrets.token_urlsafe(24)
            body = old[len("whsec_"):]
            if re.fullmatch(r"[A-Za-z0-9]+", body):
                valid_base64 += 1
            assert webhooks.is_legacy_secret(old)
            assert webhooks.sign_payload(old, "wid_1", 1750000000,
                                         '{"event":"memory.created"}') == self._literal(old)
        assert valid_base64 > 400, "the case this is about did not come up"

    def test_including_one_that_is_valid_standard_base64(self):
        # The reference vector's own secret has exactly the old shape.
        old = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
        assert base64.b64decode(old[6:], validate=True)
        assert webhooks.sign_payload(old, "wid_1", 1750000000,
                                     '{"event":"memory.created"}') == self._literal(old)


class TestTheDashboardSnippet:
    """What the Webhooks page tells people to paste. A snippet that does not
    verify a real delivery is the same failure as a wrong signature, one step
    later and on the customer's side."""

    def _snippet(self):
        page = (ROOT / "dashboard/src/pages/WebhooksPage.tsx").read_text()
        # Up to the first backtick that is not escaped: the template literal.
        m = re.search(r"const VERIFY_PY = `((?:\\.|[^`\\])*)`;", page, re.DOTALL)
        assert m, "no Python snippet on the page"
        code = m.group(1).replace("\\`", "`").replace("\\${", "${")
        scope: dict = {}
        exec(compile(code, "VERIFY_PY", "exec"), scope)
        return scope["verify"]

    def test_the_python_snippet_verifies_a_real_delivery(self, monkeypatch):
        verify = self._snippet()
        secret = webhooks.generate_secret()
        body, headers = _delivery(monkeypatch, secret, PAYLOAD)
        assert verify(secret, headers, body) is True
        assert verify(secret, headers, body.replace("maria", "mario")) is False
        # Several signatures, space-separated (the spec's rotation format).
        two = dict(headers)
        two["webhook-signature"] = "v1,AAAA " + headers["webhook-signature"]
        assert verify(secret, two, body) is True

    # The JavaScript snippet (audit of 17 Oct 2026: nothing ran it). The test
    # image has no node, so what it says is checked against the server's
    # signer everywhere, and the snippet itself verifies a real delivery
    # wherever node is installed (a developer's machine), skipped otherwise.

    def _js(self) -> str:
        page = (ROOT / "dashboard/src/pages/WebhooksPage.tsx").read_text()
        m = re.search(r"const VERIFY_JS = `((?:\\.|[^`\\])*)`;", page, re.DOTALL)
        assert m, "no JavaScript snippet on the page"
        return m.group(1).replace("\\`", "`").replace("\\${", "${")

    def test_the_js_snippet_says_what_the_server_does(self, monkeypatch):
        js = self._js()
        _body, headers = _delivery(monkeypatch, webhooks.generate_secret(), PAYLOAD)
        names = ("webhook-id", "webhook-timestamp", "webhook-signature")
        # The headers the server sends, by the names the snippet reads.
        assert set(names) <= set(headers), headers
        for name in names:
            assert f'headers["{name}"]' in js, name
        # The signed string, in the server's order: id.timestamp.body.
        assert '`${headers["webhook-id"]}.${headers["webhook-timestamp"]}.${rawBody}`' in js
        # The key: the base64-decoded part after the server's prefix.
        assert webhooks._SECRET_PREFIX == "whsec_"
        assert 'Buffer.from(secret.slice("whsec_".length), "base64")' in js
        # HMAC-SHA256 in base64 behind `v1,`, as `sign_with_key` writes it,
        # and every space-separated entry tried.
        assert webhooks.sign_with_key(b"k", "id", 1, "b").startswith("v1,")
        assert '"v1," + crypto.createHmac("sha256", key).update(signed).digest("base64")' in js
        assert '.split(" ")' in js

    def test_the_js_snippet_verifies_a_real_delivery_in_node(self, monkeypatch, tmp_path):
        import shutil
        import subprocess

        node = shutil.which("node")
        if node is None:
            pytest.skip("no node here (the test image has none); the check above ran")
        secret = webhooks.generate_secret()
        body, headers = _delivery(monkeypatch, secret, PAYLOAD)
        two = dict(headers, **{"webhook-signature": "v1,AAAA " + headers["webhook-signature"]})
        cases = [{"secret": secret, "headers": headers, "body": body},
                 {"secret": secret, "headers": headers, "body": body.replace("maria", "mario")},
                 {"secret": secret, "headers": two, "body": body},
                 {"secret": webhooks.generate_secret(), "headers": headers, "body": body}]
        script = tmp_path / "verify.mjs"
        script.write_text(self._js() + "\nconst cases = JSON.parse(process.argv[2]);\n"
                          "console.log(JSON.stringify(cases.map("
                          "(c) => verify(c.secret, c.headers, c.body))));\n")
        out = subprocess.run([node, str(script), json.dumps(cases)], capture_output=True,
                             text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        assert json.loads(out.stdout) == [True, False, True, False]
