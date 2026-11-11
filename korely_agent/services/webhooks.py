"""Outbound webhooks for the agent store.

Developers register endpoints; Korely POSTs four events:
  - memory.created      (a memory was added)
  - fact.invalidated    (a fact was superseded — keep external caches honest)
  - key.expiring        (an API key stops working within two weeks)
  - key.expired         (it has stopped)

and a fifth on the Cloud only (KORELY_MODE=hosted, the same code):
  - quota.warning       (the account passed 80% of its monthly writes)

It is added by hosted/quotas.register (`add_event`, about the account, so
every endpoint of the account receives it) and sent by `before_write`. On an
install of one's own there is no quota, the event is not registered, and an
endpoint that still lists it keeps the others it lists; the name matches
nothing there (audit of 17 Oct 2026: this said there
was no quota anywhere, beside the code that sends it on the Cloud).

Delivery is via an outbox (agent_webhook_deliveries) drained by a scheduler
worker, signed per the Standard Webhooks spec (symmetric `v1`: HMAC-SHA256
keyed by the base64-decoded part of the `whsec_` secret, over
`{id}.{timestamp}.{body}`, headers `webhook-id` / `webhook-timestamp` /
`webhook-signature`), retried with exponential backoff. Spec:
github.com/standard-webhooks/standard-webhooks, spec/standard-webhooks.md.
emit_event enqueues in the caller's transaction, so an event commits with
the write it is about or not at all, and never raises into the request.

Endpoints created before 0.1.11 keep the key they were created with, the whole
secret string: see `signing_key`.
"""
from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import json
import logging
import random
import re
import secrets
from datetime import datetime, timedelta, timezone
import ipaddress
import socket
import time
from typing import Any, Callable, Optional
from urllib.parse import urlparse
from uuid import UUID

import httpcore
from sqlalchemy import and_, or_, text
from sqlalchemy.orm import Session

from ..core import threads
from ..db.models import AgentWebhookDelivery, AgentWebhookEndpoint
from ..core.times import aware
from .memory_core import log_safe

logger = logging.getLogger(__name__)

# The events this product sends and nothing else: a name here is one an
# endpoint can wait for, so it has to be one something emits
# (tests/test_no_hosted_leftovers.py), and the screen offers exactly these.
VALID_EVENTS = {"memory.created", "fact.invalidated", "key.expiring", "key.expired"}
_OWN_EVENTS = frozenset(VALID_EVENTS)
# Events of the whole account, about no project: every endpoint of the
# account receives them, bound to a project or not.
_ACCOUNT_EVENTS: set = set()
# The event of the Webhooks page's "Send a test" (`send_test`): never
# emitted, so no endpoint subscribes to it, and a receiver acknowledges it
# like any event it does not act on.
TEST_EVENT = "ping"


def add_event(name: str, *, account_wide: bool = False) -> None:
    """An event a part of this codebase emits besides the product's own (the
    hosted service registers its quota warning from `hosted.install`, 8 Oct
    2026): from then on an endpoint can subscribe to it and `emit_event`
    sends it. `account_wide`: about no project."""
    VALID_EVENTS.add(name)
    if account_wide:
        _ACCOUNT_EVENTS.add(name)


def reset_events() -> None:
    """Back to the product's own events (main.create_app, with the hooks)."""
    VALID_EVENTS.clear()
    VALID_EVENTS.update(_OWN_EVENTS)
    _ACCOUNT_EVENTS.clear()

# The attempts of one delivery and the wait before each (2026-10-06): the
# example schedule of the Standard Webhooks specification ("Deliverability and
# reliability", the version cited for the signatures), the same as the hosted
# product's: at once, then 5 s, 5 min,
# 30 min, 2 h, 5 h, 10 h, 14 h, 20 h and 24 h, ten attempts over 75 h 35 min,
# each wait drawn within 10% so a receiver that comes back is not hit by every
# account at once. It was five attempts within about a minute, so an endpoint
# down for a lunch break lost every event of it.
SCHEDULE = (0, 5, 300, 1800, 7200, 18000, 36000, 50400, 72000, 86400)
ATTEMPTS = len(SCHEDULE)
_JITTER = 0.10

# An endpoint is switched off only when a delivery has spent all its attempts
# and the endpoint accepted nothing since that delivery was born: about three
# days of nothing, as Stripe does. It was 50 failures in a row, which a busy
# install reached in an afternoon of a receiver's deploy. Enable turns it back
# on (`enable_endpoint`); `failure_count` says how many attempts failed since
# the last success.

# One account cannot hold the queue, and an account alone is not held back
# (audit of 18 Oct 2026): a round takes every account's oldest due
# delivery, then every account's second, and so on until it is full, each
# account in its own order (`_claim`). Fair queueing per account (Demers,
# Keshav and Shenker, SIGCOMM 1989), as the facts drain and the batch drain
# share their claims (`_each_account_its_share`). It was two per account a
# round, oldest first across the install: an account alone got two
# deliveries every 15 s whatever the limit, a backlog that only grew, and ten
# accounts with an older backlog took every place of every round.
#
# Within one turn the accounts take their places in rotation, from the one
# after the last account the previous round served (`_served_last`), so with
# more accounts waiting than places each is served within
# ceil(accounts / limit) rounds, however old the others' backlogs. Kept in
# the process: after a restart the rotation starts again from the lowest id.
_served_last: Optional[UUID] = None
# Deliveries in flight at once, each in its own thread, which makes the HTTP
# call and then writes its own outcome (`_send_one`); one account holds at
# most half of them, and less when more accounts are in play
# (core/threads.Lanes.share). Shared in time as well as in count since the
# audit of 20 Oct 2026: a round
# started its attempts on 8 threads of its own and waited for the last, so one
# account whose receiver held each attempt to its deadline set the pace of
# every account's next round (milliseconds to 15 s, 46.5 s with its turns
# filled), and its stalled attempts took all 8 threads while the others'
# queued behind them.
_IN_FLIGHT = 8
_sending = threads.Lanes("webhook")
# How long a round collects its attempts: a receiver answers in milliseconds
# as a rule. An attempt still running then goes on in its thread, holding its
# account's share, and writes its own outcome; one never started is given
# back to the queue (`_released`). The next round does not wait for either.
_ROUND_PATIENCE = 1.0
# An attempt has this long, all included: the specification asks for 15 to
# 30 s and Svix uses 15. A receiver sending a byte every few seconds kept a
# read open for ever, and the whole queue behind it. The deadline is the
# attempt's own (`_Deadline`, counted from when it starts): every connect,
# handshake, write and read gets only what is left of it, and the socket is
# closed when it runs out. Until the audit of 17 Oct 2026 it was only how
# long the round waited for the answer: the thread went on reading, its
# socket and pool open, past the round and into the next ones.
_ATTEMPT_SECONDS = 15.0

# Endpoints per account, the hosted product's default ceiling. Each event fans
# out to every endpoint subscribed to it.
MAX_ENDPOINTS = 16


class TooManyEndpoints(ValueError):
    """The account has MAX_ENDPOINTS endpoints already."""


class EndpointDisabled(ValueError):
    """The endpoint is switched off; Enable first."""

# How long a finished delivery is kept is `webhook_delivery_retention_days` in
# config.py, next to the audit and usage retentions. It was a constant here, on
# the argument that a delivery record is not evidence; it is still a table on
# the customer's disk, and the other two retentions were already theirs to set.


class UnsafeWebhookUrl(ValueError):
    """The URL points somewhere this server must not be made to connect to."""


class InvalidWebhookEvents(ValueError):
    """The events asked for are not a subscription this server can honour."""


def checked_events(events: Optional[list[str]]) -> list[str]:
    """The events an endpoint subscribes to, or InvalidWebhookEvents.

    Left out (None) means every event, which is what `*` says and what the API
    has always done with no `events` field. Anything given is taken at its
    word. Unknown names used to be dropped in silence and, if none was left,
    replaced by `*`: a typo in the one event you wanted subscribed you to all
    of them. An empty list asks for nothing; it was read as everything.
    """
    if events is None:
        return ["*"]
    known = ", ".join(sorted(VALID_EVENTS))
    if not events:
        raise InvalidWebhookEvents(
            f"events is empty. Name at least one ({known}), or \"*\" for all of "
            f"them, or leave the field out.")
    unknown = sorted({e for e in events if e != "*" and e not in VALID_EVENTS})
    if unknown:
        raise InvalidWebhookEvents(
            f"unknown event(s): {', '.join(unknown)}. Known: {known}, or \"*\" for all.")
    return list(dict.fromkeys(events))


# IPv6 addresses that lead to an IPv4 one (a review of the same check in the
# hosted product, 2026-10-06). A NAT64 address reaches whatever IPv4
# address it carries through the network's gateway: refused whatever it
# carries, as the hosted product does, and said here rather than left to
# Python counting ::/8 as reserved. fec0::/10, site-local and deprecated,
# which Python calls global. An IPv4-mapped, 6to4 or Teredo address is judged
# by the IPv4 address inside, which is the one reached.
_NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))
_SITE_LOCAL = ipaddress.ip_network("fec0::/10")


def address_problem(addr) -> Optional[str]:
    """Why an address may not be reached by a webhook, or None. The one rule
    of a public address, which the hosted service applies elsewhere too."""
    if addr.version == 6:
        if any(addr in n for n in _NAT64):
            return "a NAT64 address, an IPv4 address behind the network's gateway"
        if addr in _SITE_LOCAL:
            return "a site-local address"
        inner = addr.ipv4_mapped or addr.sixtofour or (addr.teredo[1] if addr.teredo else None)
        if inner is not None and address_problem(inner):
            return f"an IPv4 address inside ({inner}) that is not public"
    if (not addr.is_global or addr.is_loopback or addr.is_link_local
            or addr.is_multicast or addr.is_reserved or addr.is_unspecified):
        return "not a public address"
    return None


def _resolve(host: str) -> list:
    """Every address the name has now, once. Raises UnsafeWebhookUrl, or
    TooManyTests while the account has its lookups under way.

    Looked up within core/threads.LOOKUP_SECONDS (`threads.lookup`), never
    on the caller's thread for as long as the resolver takes: a name whose
    server answers slowly or not at all is the caller's to choose, and the
    lookup of a create or an enable held a request thread every account
    shares for all of it (audit of 20 Oct 2026). A lookup left running holds
    its account's places until it ends."""
    try:
        infos = threads.lookup(socket.getaddrinfo, host, None)
    except socket.gaierror as e:
        raise UnsafeWebhookUrl(f"host does not resolve: {e}") from e
    except TimeoutError:
        raise UnsafeWebhookUrl(f"host did not resolve within {threads.LOOKUP_SECONDS:.0f} s") \
            from None
    except threads.Refused as e:
        raise TooManyTests(str(e)) from None
    out = []
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if addr not in out:
            out.append(addr)
    return out


def checked_destination(url: str) -> tuple[str, list]:
    """(url, the addresses it may be reached at), or UnsafeWebhookUrl. The
    delivery connects to one of these addresses and to nothing else
    (`_deliver`): the name is resolved once, here."""
    from ..config import settings

    url = (url or "").strip()
    if not url:
        raise UnsafeWebhookUrl("empty URL")

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeWebhookUrl(f"scheme {parsed.scheme!r} is not http(s)")
    if parsed.scheme == "http" and not settings.webhook_allow_http:
        raise UnsafeWebhookUrl(
            "http would send the payload in clear text; use https "
            "(or set webhook_allow_http for a local test)"
        )
    host = parsed.hostname
    if not host:
        raise UnsafeWebhookUrl("no host in URL")

    addresses = _resolve(host)
    if not settings.webhook_allow_private_urls:
        for addr in addresses:
            problem = address_problem(addr)
            if problem:
                raise UnsafeWebhookUrl(f"{host} resolves to {addr}, {problem}")
    # What the mode refuses besides (core/extensions; nothing on an install
    # of one's own).
    from ..core import extensions

    problem = extensions.hooks.webhook_destination_problem(host, addresses)
    if problem:
        raise UnsafeWebhookUrl(problem)
    return url, addresses


def check_url(url: str) -> str:
    """Return the URL, or refuse it.

    A webhook URL is an address this server will connect to, chosen by whoever
    holds an API key. On a machine inside a company network that is a request
    forgery primitive handed out with the key: the cloud metadata service at
    169.254.169.254, an admin panel on localhost, a database port on 10.x. The
    request goes out from behind the firewall, with the firewall's trust.

    So the host has to resolve to a public address, and every address it
    resolves to has to be public, because a name with two A records only needs
    one of them to be interesting. Both switches exist for local testing and
    both default to refusing.

    This is checked again at delivery time, because a name that resolved to a
    public address at registration can resolve to a private one later, and
    the delivery connects to the very addresses it checked
    (`checked_destination`, `_deliver`): until 2026-10-06 the check and httpx
    each looked the name up, and a name that answered differently between the
    two, on purpose, got through (DNS rebinding, stated in this docstring as
    the gap it left).
    """
    return checked_destination(url)[0]

STATUS_PENDING = "pending"
STATUS_DELIVERED = "delivered"
STATUS_FAILED = "failed"


_SECRET_PREFIX = "whsec_"

# Random bytes behind a new secret. The spec asks for 24 to 64 ("Random.
# Between 24 bytes (192 bits) and 64 bytes (512 bits)"); 32 is the SHA-256
# output length, below which RFC 2104 §3 calls an HMAC key "strongly
# discouraged". Standard base64 of 32 bytes is 44 characters ending in "=".
_SECRET_BYTES = 32

# The shape of every secret this install minted before 0.1.11:
# `secrets.token_urlsafe(24)`, which is always 32 characters of URL-safe
# base64 and never padded. No secret minted since has that shape (44
# characters, one "="), so the shape alone says which key an endpoint has.
#
# The shape, and not "is it valid standard base64". 35.5% of those old bodies
# (measured on 20,000) contain neither `-` nor `_` and so ARE canonical
# standard base64 of 24 bytes; deciding by validity would have moved a third
# of the existing endpoints to a new key, and broken the receivers that
# verify them today, without a word.
_LEGACY_BODY = re.compile(r"[A-Za-z0-9_-]{32}")


def generate_secret() -> str:
    """A Standard Webhooks symmetric secret: `whsec_` + standard base64 of
    random bytes (spec, symmetric row: "base64 encoded, prefixed with
    `whsec_`").

    Until 0.1.11 this was `token_urlsafe(24)`: URL-safe base64, whose `-` and
    `_` are not in the alphabet the spec's libraries decode with, so the JS
    reference library threw on the secret and the Python one decoded it into
    a different key.
    """
    raw = secrets.token_bytes(_SECRET_BYTES)
    return _SECRET_PREFIX + base64.b64encode(raw).decode("ascii")


# What a delivery to an endpoint of before 0.1.11 records as its error, at
# every attempt: the page's last error, and the way out.
LEGACY_REFUSAL = ("refused: the endpoint's secret is of before 0.1.11, which 0.2.0 does not "
                  "send; run manage rotate-webhook-secret")


def is_legacy_secret(secret: str) -> bool:
    """Whether this endpoint signs with the whole secret string as its key,
    the way every endpoint did before 0.1.11. Since 0.2.0 such an endpoint
    is not sent: each attempt is refused with LEGACY_REFUSAL (`_deliver`),
    counted on the endpoint as a failure, and `rotate-webhook-secret` moves
    it off."""
    body = secret[len(_SECRET_PREFIX):] if secret.startswith(_SECRET_PREFIX) else None
    return body is None or _LEGACY_BODY.fullmatch(body) is not None or _decoded(body) is None


def _decoded(body: str) -> Optional[bytes]:
    """The bytes of canonical standard base64, or None."""
    try:
        raw = base64.b64decode(body, validate=True)
    except (ValueError, TypeError):
        return None
    return raw if raw and base64.b64encode(raw).decode("ascii") == body else None


class SignatureError(ValueError):
    """A Standard Webhooks signature that cannot be made or does not verify:
    no key, missing headers, a timestamp out of tolerance, no matching entry."""


# The Standard Webhooks verification below is the one of this codebase: our
# own deliveries are signed by `sign_with_key`, and the Cloud's Polar webhooks
# are verified by `verify` with the key `secret_key` reads
# (hosted/polar_signature.py). They were two copies until the audit of 17 Oct
# 2026, and they already disagreed: on a secret of a bare `whsec_` the
# Polar copy signed with an empty HMAC key, which anybody can sign with.

# How far a message's timestamp may be from now, either way: the reference
# library's tolerance (standardwebhooks, `TOLERANCE`), against replays.
TOLERANCE_SECONDS = 5 * 60


def secret_key(secret: str) -> bytes:
    """The HMAC key a Standard Webhooks secret stands for: the decoded body of
    `whsec_<canonical standard base64>` (spec, symmetric signatures), else the
    whole string's UTF-8 bytes (a Polar dashboard secret, `polar_whs_...`,
    which Polar base64-encodes and the library decodes straight back; or a
    `whsec_` lookalike that is not base64). Empty for an empty secret or a
    bare `whsec_`: no key, which `sign_with_key` and `verify` refuse, as the
    reference library refuses an empty secret (`EmptyWebhookSecretError`)."""
    if secret.startswith(_SECRET_PREFIX):
        body = secret[len(_SECRET_PREFIX):]
        if not body:
            return b""
        raw = _decoded(body)
        if raw is not None:
            return raw
    return secret.encode("utf-8")


def signing_key(secret: str) -> bytes:
    """The HMAC key for `secret`.

    Spec (standard-webhooks.md, symmetric signatures): the secret is serialized
    as `whsec_` + base64 of the key, so the key is the base64-DECODED part
    after the prefix. The official libraries (`standardwebhooks`, `svix`) do
    exactly that, and a signature made any other way fails in all of them.

    An endpoint created before 0.1.11 keeps the key it was created with, the
    whole string encoded as UTF-8, which is what the verification snippets of
    that time did: its shape says so (`_LEGACY_BODY`), even where it happens
    to be valid base64. Its receiver keeps working; `manage
    rotate-webhook-secret` (or recreating the endpoint) gives it a secret the
    libraries accept. Every other secret is read as `secret_key` reads it.
    """
    if secret.startswith(_SECRET_PREFIX) and _LEGACY_BODY.fullmatch(secret[len(_SECRET_PREFIX):]):
        return secret.encode("utf-8")
    return secret_key(secret)


def sign_with_key(key: bytes, msg_id: str, timestamp: int | str, body: str) -> str:
    """Standard Webhooks symmetric signature (`v1`): HMAC-SHA256 over
    `{msg_id}.{timestamp}.{body}`, base64-encoded, prefixed `v1,`. `body` must
    be the exact text sent on the wire. SignatureError for an empty key."""
    if not key:
        raise SignatureError("no signing key: the secret is empty, or a bare whsec_")
    signed_content = f"{msg_id}.{timestamp}.{body}".encode("utf-8")
    digest = hmac.new(key, signed_content, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode("ascii")


def verify(key: bytes, *, webhook_id: Optional[str], timestamp: Optional[str],
           signature_header: Optional[str], body: str, now: Optional[float] = None) -> None:
    """Raise SignatureError unless `signature_header` holds a valid, fresh `v1`
    signature of `body` under `key`, as the reference library's
    `Webhook.verify` decides: the three headers present, the timestamp within
    `TOLERANCE_SECONDS` of now, and one of the space-separated `v1,<base64>`
    entries (several during a secret rotation) equal to ours, compared in
    constant time. An entry of another version is skipped."""
    if not (webhook_id and timestamp and signature_header):
        raise SignatureError("missing Standard Webhooks headers")
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        raise SignatureError("invalid timestamp") from None
    if abs(int(now if now is not None else time.time()) - ts) > TOLERANCE_SECONDS:
        raise SignatureError("timestamp outside tolerance")
    expected = sign_with_key(key, webhook_id, ts, body)
    for entry in signature_header.split():
        # Bytes: a header may carry any character, and compare_digest takes
        # only ASCII strings.
        if entry.startswith("v1,") and hmac.compare_digest(entry.encode("utf-8"),
                                                           expected.encode("ascii")):
            return
    raise SignatureError("no matching signature")


def sign_payload(secret: str, msg_id: str, timestamp: int, body: str) -> str:
    """The `webhook-signature` value for one delivery of an endpoint."""
    return sign_with_key(signing_key(secret), msg_id, timestamp, body)


# ── endpoint CRUD (dashboard) ─────────────────────────────────────────────
def create_endpoint(db: Session, tenant_id: UUID, *, url: str,
                    events: Optional[list[str]],
                    project_id: Optional[UUID] = None,
                    recheck: Optional[Callable[[Session], None]] = None) -> AgentWebhookEndpoint:
    """`project_id`: the project whose events it receives (and the
    account's); None for every project. The caller checks the project is
    this account's. `recheck`: what the caller reads again in the
    transaction that writes, after the lookup (core/auth.still_admin: the
    credential's `admin`, which may have gone while the name was looked
    up); it raises to refuse."""
    clean = checked_events(events)  # InvalidWebhookEvents; the router maps it to 422
    # The name is looked up with no transaction open, as enable_endpoint
    # does: the key's and the project's reads had one, and the connection
    # stayed out of the pool for the whole lookup, so a few slow creates of
    # one account made every other account's requests answer 503 (audit of
    # 19 Oct 2026; the fix after the audit of 18 Oct reached only the test and
    # the enable).
    db.rollback()
    # The lookup is an outbound wait of the account (`_an_outbound_place`):
    # past its places, 429 at once (audit of 20 Oct 2026).
    with _an_outbound_place(tenant_id):
        url = check_url(url)        # raises UnsafeWebhookUrl; the router maps it to 400
    if recheck is not None:
        recheck(db)
    # One create of the account at a time from the count to the commit: two
    # creates whose lookups answered together both counted 15 and both went
    # in, past MAX_ENDPOINTS (audit of 20 Oct 2026: the cap's count was not
    # locked). A transaction's lock, released by the commit or the
    # rollback, and waited on within a request's lock deadline
    # (core/threads.with_deadlines).
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
               {"k": f"webhook-endpoints:{tenant_id}"})
    held = db.query(AgentWebhookEndpoint).filter(AgentWebhookEndpoint.tenant_id == tenant_id).count()
    if held >= MAX_ENDPOINTS:
        db.rollback()
        raise TooManyEndpoints(f"this account has {held} webhook endpoints, the most it may have "
                               f"is {MAX_ENDPOINTS}: delete one first")
    ep = AgentWebhookEndpoint(
        tenant_id=tenant_id,
        url=url,
        secret=generate_secret(),
        events=clean,
        active=True,
        project_id=project_id,
    )
    db.add(ep)
    db.commit()
    db.refresh(ep)
    return ep


def list_endpoints(db: Session, tenant_id: UUID) -> list[AgentWebhookEndpoint]:
    return (
        db.query(AgentWebhookEndpoint)
        .filter(AgentWebhookEndpoint.tenant_id == tenant_id)
        .order_by(AgentWebhookEndpoint.created_at.desc(), AgentWebhookEndpoint.id.desc())
        .all()
    )


def rotate_secret(db: Session, tenant_id: UUID, endpoint_id: UUID) -> Optional[AgentWebhookEndpoint]:
    """Give an endpoint a new secret, keeping its id, URL, events and pending
    deliveries; None if the account has no such endpoint.

    Deliveries are signed when they are sent, not when they are queued, so
    anything still waiting goes out under the new secret. There is one secret
    at a time: from this moment until the receiver has the new one, its
    verification fails and the deliveries are retried with backoff, then
    given up after their attempts. The spec's zero-downtime rotation (two
    signatures in one header for a while) is not implemented."""
    ep = (
        db.query(AgentWebhookEndpoint)
        .filter(AgentWebhookEndpoint.tenant_id == tenant_id, AgentWebhookEndpoint.id == endpoint_id)
        .first()
    )
    if ep is None:
        return None
    ep.secret = generate_secret()
    db.commit()
    db.refresh(ep)
    return ep


def delete_endpoint(db: Session, tenant_id: UUID, endpoint_id: UUID) -> bool:
    ep = (
        db.query(AgentWebhookEndpoint)
        .filter(AgentWebhookEndpoint.tenant_id == tenant_id, AgentWebhookEndpoint.id == endpoint_id)
        .first()
    )
    if ep is None:
        return False
    db.delete(ep)
    db.commit()
    return True


# ── emit (fire-and-forget) ────────────────────────────────────────────────
_FROM_THE_DATA = object()


def _project_of(db: Session, tenant_id: UUID, event_type: str, data: dict) -> Optional[UUID]:
    """The project an event is about, read from what it names: the memory
    written (memory.created) or the fact closed (fact.invalidated); None when
    the thing named is not found, or has no project (the hosted product's
    rule)."""
    from ..db.models import AgentFact, AgentMemory
    from .ids import parse_fact_id, parse_mem_id

    if event_type == "memory.created":
        mid = parse_mem_id(data.get("id"))
        return db.query(AgentMemory.project_id).filter(
            AgentMemory.id == mid, AgentMemory.tenant_id == tenant_id).scalar() if mid else None
    if event_type == "fact.invalidated":
        fid = parse_fact_id(data.get("fact_id"))
        return db.query(AgentFact.project_id).filter(
            AgentFact.id == fid, AgentFact.tenant_id == tenant_id).scalar() if fid else None
    return None


def emit_event(db: Session, tenant_id: UUID, event_type: str, data: dict,
               project_id: Any = _FROM_THE_DATA, *, commit: bool = False) -> Optional[int]:
    """Enqueue a delivery for every active endpoint of this account subscribed
    to the event and to its project (8 Oct 2026, the hosted product's r64: an
    endpoint with no project takes every project's events; one bound to a
    project takes that project's and the account's). `project_id` is the
    project the event is about, read from the data when not given (the memory
    or the fact it names); an account-wide event (`add_event`) has none, and
    a None project reaches only the endpoints of every project.

    In the caller's transaction, in a savepoint: the deliveries commit with
    the write the event is about, in the caller's own commit, or go with its
    rollback. Every caller enqueues before that commit. Until the audit of 17
    Oct 2026 this ran after it and committed on its own, so a failure between
    the two commits kept the write and lost its event for good, with one
    warning in the log; and it committed or rolled back whatever else the
    caller had open (the quota warning ended the write's transaction before
    its count was registered).

    Never raises. Returns the number of deliveries enqueued (0: nobody
    listens), or None when the enqueue failed: its savepoint is undone and the
    caller's work is not, so a write whose whole point is the event (the
    expiry notice of `keys.warn_about_expiring`) can leave itself undone and
    be tried again. `commit=True` commits after, for a caller with nothing of
    its own in the transaction (a script, a test)."""
    if event_type not in VALID_EVENTS:
        return 0
    try:
        with db.begin_nested():
            n = _enqueue(db, tenant_id, event_type, data, project_id)
    except Exception as e:  # noqa: BLE001
        logger.error("[agent_webhooks] %s not enqueued, the write goes on without it: %s",
                     event_type, log_safe.stack(e))
        return None
    if commit and n:
        try:
            db.commit()
        except Exception as e:  # noqa: BLE001
            logger.error("[agent_webhooks] %s not committed: %s", event_type, log_safe.stack(e))
            db.rollback()
            return None
    return n


def listening(event_type: str, project_id: Any) -> Any:
    """The endpoints that take `event_type` about `project_id`, as a filter on
    AgentWebhookEndpoint (the account is the caller's): active, subscribed to
    the event or to `*`, and, unless the event is of the whole account, bound
    to no project or to that one. `project_id` is a value, or a column:
    `keys.warn_about_expiring` asks it of each key's project, to leave out of
    its round the keys whose notice nobody would hear.

    One rule for both. The subscription was read in Python here, after the
    query, so it could not be asked in SQL by anybody else. Every endpoint
    holds a list that is not empty (`checked_events`, and before it an empty
    list was stored as `*`), which `?|` reads as the Python check did."""
    from .end_user_places import naming_any

    E = AgentWebhookEndpoint
    rule = [E.active.is_(True), naming_any(E.events, ("*", event_type))]
    if event_type not in _ACCOUNT_EVENTS:
        rule.append(or_(E.project_id.is_(None), E.project_id == project_id))
    return and_(*rule)


def _enqueue(db: Session, tenant_id: UUID, event_type: str, data: dict, project_id: Any) -> int:
    """Add the deliveries of one event to the session; the caller commits."""
    payload = {"event": event_type, "data": data}
    if event_type not in _ACCOUNT_EVENTS:
        if project_id is _FROM_THE_DATA:
            project_id = _project_of(db, tenant_id, event_type, data)
        # The project the event is about: a receiver serving several
        # projects tells them apart. Null when it has none; absent on an
        # event of the whole account.
        payload["project_id"] = str(project_id) if project_id else None
    endpoints = db.query(AgentWebhookEndpoint).filter(
        AgentWebhookEndpoint.tenant_id == tenant_id, listening(event_type, project_id)).all()
    n = 0
    for ep in endpoints:
        # An endpoint that signs the way of before 0.1.11 is queued as any
        # other, and `_deliver` refuses each attempt with the reason: the
        # page then shows the failures and the last error, and a rotation
        # (`manage rotate-webhook-secret`) within the attempts sends what is
        # queued under the new secret. Until the audit of 19 Oct 2026 it was
        # skipped here with no row, so the page showed nothing wrong.
        db.add(AgentWebhookDelivery(
            tenant_id=tenant_id,
            endpoint_id=ep.id,
            event_type=event_type,
            payload=payload,
            status=STATUS_PENDING,
            max_attempts=ATTEMPTS,
        ))
        n += 1
    return n


# ── delivery worker ───────────────────────────────────────────────────────
def _renamed_if_erased_meanwhile(db: Session, d, *, finishing: bool) -> None:
    """A delivery that finishes now names the end user it was enqueued with;
    if they were erased since, or the project it is about was, the row keeps
    their pseudonym, as the finished ones did at the erasure
    (services/pseudonyms.py; audit of 17 Oct 2026). It was sent with the id
    its receiver knows. Finished: delivered, spent, or dropped (its endpoint
    off or gone)."""
    if not finishing:
        return
    from . import pseudonyms

    payload = d.payload or {}
    uid = (payload.get("data") or {}).get("user_id")
    if not uid:
        return
    p = pseudonyms.erased_since(db, d.tenant_id, uid, d.created_at,
                                project=payload.get("project_id"))
    if p or getattr(d, "erased_user", False):
        # Marked by the erasure itself (end_user_places.rename_in_deliveries),
        # which saw it still to be sent: the pseudonym, or nothing without
        # SECRET_KEY, whatever the clocks say (audit of 27 Oct 2026).
        p = p or pseudonyms.pseudonym(d.tenant_id, uid)
        d.payload = {**payload, "data": {**payload["data"], "user_id": p}}


class _Deadline:
    """What is left of one attempt's `_ATTEMPT_SECONDS`, from when it began."""

    def __init__(self, seconds: float):
        self._seconds = seconds
        self._end = time.monotonic() + seconds

    def run(self, operation, asked: Optional[float], late: type):
        """One network operation, `operation(timeout)`, with the timeout it
        asked for cut to what is left; `late` once nothing is, or when it ran
        out on the way."""
        left = self._end - time.monotonic()
        if left <= 0:
            raise late(f"no complete answer within {self._seconds:.0f} s")
        try:
            return operation(left if asked is None else min(asked, left))
        except late:
            if self._end - time.monotonic() <= 0:
                raise late(f"no complete answer within {self._seconds:.0f} s") from None
            raise


# A write goes out in pieces of this size, each with what is left of the
# deadline: a receiver taking a few bytes at a time cannot stretch one write.
_WRITE_PIECE = 16 * 1024


class _DeadlineStream(httpcore.NetworkStream):
    """A connection whose every operation ends by the attempt's deadline."""

    def __init__(self, inner: httpcore.NetworkStream, deadline: _Deadline):
        self._inner = inner
        self._deadline = deadline

    def read(self, max_bytes, timeout=None):
        return self._deadline.run(lambda t: self._inner.read(max_bytes, t),
                                  timeout, httpcore.ReadTimeout)

    def write(self, buffer, timeout=None):
        for i in range(0, len(buffer), _WRITE_PIECE):
            piece = buffer[i:i + _WRITE_PIECE]
            self._deadline.run(lambda t: self._inner.write(piece, t),
                               timeout, httpcore.WriteTimeout)

    def close(self):
        self._inner.close()

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        tls = self._deadline.run(
            lambda t: self._inner.start_tls(ssl_context, server_hostname, t),
            timeout, httpcore.ConnectTimeout)
        return _DeadlineStream(tls, self._deadline)

    def get_extra_info(self, info):
        return self._inner.get_extra_info(info)


class _AttemptBackend(httpcore.SyncBackend):
    """The network of one attempt: every connection ends by its deadline,
    and, with `address`, goes to the address the check approved whatever the
    name answers by then. TLS still speaks to the name: httpcore passes the
    URL's host as the server name, so the certificate is verified for it and
    SNI carries it. Without `address` (through the operator's proxy) it
    connects to the proxy by its name."""

    def __init__(self, deadline: _Deadline, address: Optional[str] = None):
        super().__init__()
        self._deadline = deadline
        self._address = address

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        stream = self._deadline.run(
            lambda t: super(_AttemptBackend, self).connect_tcp(
                self._address or host, port, timeout=t, local_address=local_address,
                socket_options=socket_options),
            timeout, httpcore.ConnectTimeout)
        return _DeadlineStream(stream, self._deadline)


_TIMEOUTS = {"connect": 5.0, "read": 10.0, "write": 5.0, "pool": 5.0}


def _post(url: str, body: str, headers: dict, *, deadline: _Deadline,
          address=None, proxy: Optional[httpcore.Proxy] = None) -> int:
    """POST to `url`, through `address` only or through `proxy`, within
    `deadline`; the response status. No redirect is followed, as with httpx's
    default before this.

    Only the status is read. The body never is: a receiver cannot keep the
    attempt reading (audit of 17 Oct 2026), and the headers are capped by
    httpcore (h11's largest incomplete event, 100 KiB). Leaving the body
    unread closes the connection, with the pool, on the way out."""
    import ssl

    with httpcore.ConnectionPool(ssl_context=ssl.create_default_context(), proxy=proxy,
                                 network_backend=_AttemptBackend(
                                     deadline, None if address is None else str(address))) as pool:
        with pool.stream("POST", url, headers=list(headers.items()),
                         content=body.encode("utf-8"), extensions={"timeout": _TIMEOUTS}) as resp:
            return resp.status


def _proxy_for(url: str) -> Optional[httpcore.Proxy]:
    """The operator's proxy for this URL, or None: the one of its scheme
    (HTTPS_PROXY for https), else ALL_PROXY, as httpx chose, unless NO_PROXY
    names the host. The proxy resolves the name, so there is no address to
    pin, and what it may reach is its policy.

    Until the audit of 17 Oct 2026 any proxy variable at all sent the
    delivery through httpx, which looked the name up again: NO_PROXY alone,
    or FTP_PROXY, which carry nothing here, reopened the DNS rebinding the
    pinned address closes."""
    import urllib.request
    from urllib.parse import unquote

    parsed = urlparse(url)
    proxies = urllib.request.getproxies()
    chosen = proxies.get(parsed.scheme) or proxies.get("all")
    if not chosen or urllib.request.proxy_bypass(parsed.hostname or ""):
        return None
    p = urlparse(chosen if "://" in chosen else "http://" + chosen)
    auth = (unquote(p.username or ""), unquote(p.password or "")) if p.username else None
    return httpcore.Proxy(p._replace(netloc=p.netloc.rpartition("@")[2]).geturl(), auth=auth)


def _deliver(url: str, secret: str, delivery_id: str, payload: dict) -> tuple[bool, Optional[int], Optional[str]]:
    """POST one signed delivery, within `_ATTEMPT_SECONDS` from now, the
    lookup of the name included. Returns (ok, response_status, error)."""
    if is_legacy_secret(secret):
        # An endpoint that signs the way of before 0.1.11: not sent since
        # 0.2.0, and said why, at every attempt; the delivery is retried on
        # the schedule as any failed one, so a rotation made meanwhile sends it.
        return False, None, LEGACY_REFUSAL
    deadline = _Deadline(_ATTEMPT_SECONDS)
    try:
        url, addresses = checked_destination(url)
    except UnsafeWebhookUrl as e:
        # Checked again here, not only at registration: DNS can be changed
        # after the fact, and a check that runs once is a check against honest
        # mistakes only.
        return False, None, f"refused: {e}"

    body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    ts = int(datetime.now(timezone.utc).timestamp())
    try:
        signature = sign_payload(secret, delivery_id, ts, body)
    except SignatureError as e:
        # A secret with no key in it (a bare `whsec_` written by hand): never
        # signed with an empty key, which anybody could sign with.
        return False, None, f"refused: {e}"
    headers = {
        "Content-Type": "application/json",
        "webhook-id": delivery_id,
        "webhook-timestamp": str(ts),
        "webhook-signature": signature,
        "User-Agent": "Korely-Webhooks/1.0",
    }
    proxy = _proxy_for(url)
    code, last_error = None, None
    for address in ([None] if proxy is not None else addresses):
        try:
            code = _post(url, body, headers, deadline=deadline, address=address, proxy=proxy)
            break
        except Exception as e:  # noqa: BLE001 - the next address, then a failure
            last_error = str(e)[:500]
    if code is None:
        return False, None, last_error
    ok = 200 <= code < 300
    return ok, code, None if ok else f"HTTP {code}"


# How long a claimed delivery is left alone before another worker may retry
# it, past the longest the round can keep it (`_lease_seconds`): room for a
# process that dies holding the claim. It was 120 s flat, and a round of 20
# that waited 15 s for each in turn took 300 s, so a second drain sent the
# same deliveries again while the first was still on them (audit of 17 Oct
# 2026).
_LEASE_SECONDS = 120


def _give_up_seconds() -> float:
    """When an attempt is no longer waited for: its deadline and a tenth more
    for the thread to hand its answer back."""
    return _ATTEMPT_SECONDS * 1.1


def _lease_seconds() -> float:
    """A claim's lease: the round's wait for its turn to start, the attempt,
    and the margin."""
    return _ROUND_PATIENCE + _give_up_seconds() + _LEASE_SECONDS


def process_deliveries(db: Session, *, limit: int = 20) -> dict:
    """One round of the delivery queue: claim, deliver, write down.

    Claim first, in one short transaction: the due deliveries, the accounts
    taking turns and each account's oldest first (`_claim`), locked with SKIP
    LOCKED and given a lease (`next_retry_at` pushed ahead, which is what the
    selection reads) longer than the round can take, then committed, so no
    lock and no transaction is held across the network. Two drains never take the same
    delivery; a worker that dies mid-flight loses its claim when the lease
    ends. Attempts are counted at the claim, so a crash costs one attempt,
    never a loop.

    Deliver in parallel, `_IN_FLIGHT` at once, each with `_ATTEMPT_SECONDS`
    all included; each attempt then writes its outcome (`_write_outcome`):
    delivered, or the next attempt on the schedule (`SCHEDULE`), or spent;
    and an endpoint that accepted nothing while one of its deliveries spent
    every attempt is switched off. Only over its own claim: a delivery
    claimed again since (its lease ran out, its attempts moved on) or gone is
    left as it is, so a late attempt changes nothing (audit of 17 Oct 2026).

    A full round says `more`, and the worker starts the next one at once
    instead of 15 s later (worker._loop, as for the facts drain): a backlog
    is drained at the pace of the receivers, not of the clock. Every round
    claims what it takes out of the due ones (a lease, then delivered or a
    wait on the schedule), so the rounds stop when the backlog is gone.

    The attempts run in core/threads.Lanes (`_sending`): each account at most
    its share of `_IN_FLIGHT` at once, each attempt writing its own outcome,
    and the round collecting them for `_ROUND_PATIENCE`. An attempt still
    running then is left to end on its own, and its account is not claimed
    for again while it holds its share; one never started goes back to the
    queue. A receiver that holds every attempt to its deadline slows its own
    account's deliveries, never the round of the others (audit of 20 Oct 2026).
    """
    slots = _IN_FLIGHT
    most = max(1, slots // 2)
    rows, claimed = _claim(db, limit, slots=slots, most=most)
    if not rows:
        return {"delivered": 0, "failed": 0}
    work, dropped = _to_send(db, rows)
    share = _sending.share(slots, {d.tenant_id for d, _ in work}, most=most)
    jobs = [(d.tenant_id, functools.partial(_send_one, d.id, claimed[d.id], ep.id, ep.url,
                                            ep.secret, d.payload))
            for d, ep in work]
    ended, never_started = _sending.run(jobs, slots=slots, share=share,
                                        patience=_ROUND_PATIENCE, give_up=_give_up_seconds())
    delivered = sum(1 for r in ended.values() if r is True)
    failed = sum(1 for r in ended.values() if r is False)
    _released(db, [work[i][0] for i in never_started], claimed)
    out = {"delivered": delivered, "failed": failed + dropped}
    if len(rows) >= limit or never_started:
        out["more"] = True
    return out


def _claim(db: Session, limit: int, *, slots: int = 0, most: int = 0) -> tuple[list, dict]:
    """The due deliveries this round takes, their attempts counted and their
    lease given, committed; and {id: attempts} as claimed. None while every
    slot of `_sending` is taken, and none of an account that holds `most`
    of them already (its attempts of earlier rounds still running).

    By turn (each account's oldest due delivery is its turn 1, the next its
    turn 2), then, within a turn, the accounts in rotation from the one after
    `_served_last`: never by age across accounts (audit of 18 Oct
    2026: oldest first, the accounts with an older backlog took every place)."""
    from sqlalchemy import case, func, select

    global _served_last
    D = AgentWebhookDelivery
    now = datetime.now(timezone.utc)
    due = (D.status.in_([STATUS_PENDING, STATUS_FAILED]), D.attempts < D.max_attempts,
           or_(D.next_retry_at.is_(None), D.next_retry_at <= now))
    turn = func.row_number().over(partition_by=D.tenant_id,
                                  order_by=(D.created_at.asc(), D.id.asc())).label("turn")
    ranked = select(D.id.label("id"), turn).where(*due).subquery()
    order = [ranked.c.turn.asc()]
    if _served_last is not None:
        order.append(case((D.tenant_id > _served_last, 0), else_=1))
    order += [D.tenant_id.asc(), D.created_at.asc(), D.id.asc()]
    if slots and not _sending.free(slots):
        db.commit()
        return [], {}
    full = [a for a, n in _sending.running().items() if most and n >= most]
    if full:
        due = due + (D.tenant_id.notin_(full),)
    rows = (
        db.query(D)
        .join(ranked, ranked.c.id == D.id)
        # No account takes more than a round: the turns past it are not read.
        .filter(ranked.c.turn <= limit, *due)
        .order_by(*order)
        .limit(limit)
        # The deliveries only: the ranking is a subquery with a window,
        # which cannot be locked.
        .with_for_update(of=D, skip_locked=True)
        .all()
    )
    if not rows:
        db.commit()
        return [], {}
    _served_last = rows[-1].tenant_id

    lease_until = now + timedelta(seconds=_lease_seconds())
    for d in rows:
        d.attempts = (d.attempts or 0) + 1
        d.next_retry_at = lease_until
    db.commit()
    return rows, {d.id: d.attempts for d in rows}


def _to_send(db: Session, rows: list) -> tuple[list, int]:
    """[(delivery, endpoint)] to send, and how many were dropped: due while
    their endpoint was off or gone, never sent late."""
    endpoints = {e.id: e for e in db.query(AgentWebhookEndpoint)
                 .filter(AgentWebhookEndpoint.id.in_({d.endpoint_id for d in rows})).all()}
    D = AgentWebhookDelivery
    off = [d for d in rows if endpoints.get(d.endpoint_id) is None
           or not endpoints[d.endpoint_id].active]
    # The deliveries to drop decided on the rows as they stand, locked, as
    # `_write_outcome` finishes one: an end user's erasure rewrites the
    # deliveries naming them under the same row locks (end_user_places.
    # rename_in_deliveries), and a drop decided on the rows the claim had
    # read wrote 'spent' over the erasure's mark once it committed, with the
    # id in clear for the retention's 30 days (the review of the audit
    # before 1.0.0). All of them in one statement, in id order, the
    # order the erasure locks them in: one at a time, in the claim's order,
    # two of one person could deadlock with it. Over this round's claim only.
    claimed = {d.id: d.attempts for d in off}
    still = ({d.id: d for d in db.query(D).filter(D.id.in_(list(claimed))).order_by(D.id)
              .with_for_update(key_share=True).populate_existing()} if claimed else {})
    work, dropped = [], 0
    for d in rows:
        if d.id not in claimed:
            work.append((d, endpoints[d.endpoint_id]))
            continue
        d = still.get(d.id)
        if d is None or d.attempts != claimed[d.id]:
            continue
        d.status = STATUS_FAILED
        d.last_error = "endpoint missing or disabled"
        d.next_retry_at = None
        d.attempts = d.max_attempts
        # Finished here, never sent: it takes the pseudonym of an end user
        # erased since, as one finished by its outcome does (audit of 18 Oct
        # 2026: it kept the id for the 30 days of retention).
        _renamed_if_erased_meanwhile(db, d, finishing=True)
        dropped += 1
    db.commit()
    return work, dropped


def _send_one(delivery_id: UUID, attempts: int, endpoint_id: UUID, url: str, secret: str,
              payload: dict) -> Optional[bool]:
    """One attempt, in a thread of `_sending`, and its outcome written in a
    session of its own: True delivered, False failed, None when the outcome
    was not this attempt's to write (claimed again meanwhile, or gone)."""
    from ..db import get_sessionmaker

    started = time.monotonic()
    try:
        outcome = _deliver(url, secret, str(delivery_id), payload)
    except Exception as e:  # noqa: BLE001 - one attempt that failed
        outcome = (False, None, str(e)[:500])
    if time.monotonic() - started > _give_up_seconds():
        # Past its deadline (something no timeout reached): its answer is
        # not read, as the round's own wait did not read it.
        outcome = (False, None, f"no complete answer within {_ATTEMPT_SECONDS:.0f} s")
    db = get_sessionmaker()()
    try:
        return _write_outcome(db, delivery_id, attempts, endpoint_id, outcome)
    finally:
        db.close()


def _released(db: Session, rows: list, claimed: dict) -> None:
    """Claims a round never started, given back to the queue: the attempt
    counted at the claim taken back, due now. Over this round's claims only."""
    D = AgentWebhookDelivery
    for d in rows:
        db.query(D).filter(D.id == d.id, D.attempts == claimed[d.id]).update(
            {D.attempts: D.attempts - 1, D.next_retry_at: None}, synchronize_session=False)
    db.commit()


def _write_outcome(db: Session, delivery_id: UUID, attempts: int, endpoint_id: UUID,
                   outcome: tuple) -> Optional[bool]:
    """Delivered, or the next attempt on the schedule, or spent; over this
    attempt's claim only. True delivered, False failed, None not written."""
    D = AgentWebhookDelivery
    # Written over its own claim only: locked, read again, and kept where the
    # attempts are still the ones its claim counted. A drain that claimed it
    # since (the lease ran out) has counted one more and owns it; the
    # endpoint is read again too, so a count of failures or an Enable made
    # meanwhile is not overwritten. FOR NO KEY UPDATE (`key_share`): an
    # event enqueued for the endpoint meanwhile (its foreign key's KEY SHARE)
    # does not wait on it.
    d = (db.query(D).filter(D.id == delivery_id)
         .with_for_update(key_share=True).populate_existing().first())
    ep = None
    if d is not None and d.attempts == attempts:
        ep = (db.query(AgentWebhookEndpoint).filter(AgentWebhookEndpoint.id == endpoint_id)
              .with_for_update(key_share=True).populate_existing().first())
    if ep is None:
        logger.info("[agent_webhooks] delivery %s: claimed again or gone during the attempt, "
                    "its outcome here is not written", delivery_id)
        db.rollback()
        return None
    ok, code, err = outcome
    _renamed_if_erased_meanwhile(db, d, finishing=ok or d.attempts >= d.max_attempts)
    d.response_status = code
    if ok:
        d.status = STATUS_DELIVERED
        d.delivered_at = datetime.now(timezone.utc)
        d.last_error = None
        d.next_retry_at = None
        ep.last_delivery_at = d.delivered_at
        ep.failure_count = 0
        db.commit()
        return True
    d.status = STATUS_FAILED
    d.last_error = err
    ep.failure_count = (ep.failure_count or 0) + 1
    if d.attempts >= d.max_attempts:
        _spent(d, ep, err)
    else:
        wait = SCHEDULE[min(d.attempts, ATTEMPTS - 1)]
        wait *= random.uniform(1 - _JITTER, 1 + _JITTER)
        d.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=wait)
    db.commit()
    return False


def _spent(d, ep, err: Optional[str]) -> None:
    """A delivery that spent its attempts: never selected again, and its
    endpoint switched off when it accepted nothing since the delivery was
    made."""
    d.next_retry_at = None
    accepted = aware(ep.last_delivery_at)
    born = aware(d.created_at)
    if ep.active and (accepted is None or born is None or accepted < born):
        ep.active = False
        logger.warning("[agent_webhooks] endpoint %s disabled: delivery %s spent its %d "
                       "attempts and the endpoint accepted nothing since: %s",
                       ep.id, d.id, d.attempts, err)


def enable_endpoint(db: Session, tenant_id: UUID, endpoint_id: UUID, *,
                    recheck: Optional[Callable[[Session], None]] = None,
                    ) -> Optional[AgentWebhookEndpoint]:
    """Switch an endpoint back on: its URL checked again (a name can point
    elsewhere since), its failures forgotten, URL and secret kept. The
    deliveries that fell due while it was off were dropped; the ones still
    ahead go out. None when there is no such endpoint. UnsafeWebhookUrl when
    its URL no longer passes. `recheck`, as for `create_endpoint`: read
    again after the lookup, in the transaction that switches it on."""
    ep = (db.query(AgentWebhookEndpoint)
            .filter(AgentWebhookEndpoint.tenant_id == tenant_id,
                    AgentWebhookEndpoint.id == endpoint_id)
            .first())
    if ep is None:
        return None
    url = ep.url
    # The name is looked up with no transaction open: the connection goes
    # back to the pool for it (audit of 18 Oct 2026), and the row is
    # read again after, as it is then (deleted meanwhile: None). A URL never
    # changes once the endpoint is made.
    db.rollback()
    with _an_outbound_place(tenant_id):   # as a create's lookup
        check_url(url)
    if recheck is not None:
        recheck(db)
    ep = (db.query(AgentWebhookEndpoint)
            .filter(AgentWebhookEndpoint.tenant_id == tenant_id,
                    AgentWebhookEndpoint.id == endpoint_id)
            .with_for_update()
            .first())
    if ep is None:
        db.rollback()
        return None
    ep.active = True
    ep.failure_count = 0
    db.commit()
    db.refresh(ep)
    return ep


def send_test(db: Session, tenant_id: UUID, endpoint_id: UUID) -> Optional[dict]:
    """Deliver one test event now, synchronously, and report what happened.

    Synchronous on purpose: the question this answers is "is my endpoint
    reachable and is my signature check right", and an answer that arrives in
    the delivery log fifteen seconds later does not answer it. It is the one
    place a webhook is sent outside the worker.

    The payload is a delivery's envelope (`_enqueue`: `event`, `data` and
    the project), with the event `ping`, which no subscription names, and
    `test: true`, so nobody's handler treats it as a real invalidation and
    acts on a fact that did not change. Until the audit of 17 Oct 2026 it was
    {id, type, test, created_at, data}: a receiver that decides on `event`,
    the key every real delivery and the Webhooks page's example carry, saw
    nothing it knew, and the test failed where the deliveries went through.

    The wait holds no database connection: the endpoint is read, the
    transaction ended, and only then the receiver called (audit of 18 Oct
    2026: the connection stayed out of the pool for the whole attempt,
    and fifteen tests of one account at once held the whole pool, every other
    account's requests answering 503). An account has
    core/threads.OUTBOUND_PER_ACCOUNT outbound waits at most, tests and the
    address checks of creates and enables together, each a thread of the
    process for up to `_ATTEMPT_SECONDS`; one more is TooManyTests.
    """
    import uuid as _uuid

    ep = (
        db.query(AgentWebhookEndpoint)
        .filter(AgentWebhookEndpoint.tenant_id == tenant_id,
                AgentWebhookEndpoint.id == endpoint_id)
        .first()
    )
    if ep is None:
        return None
    if not ep.active:
        raise EndpointDisabled("this endpoint is disabled: enable it, then test it")
    url, secret = ep.url, ep.secret
    # The project its deliveries are about, as on every project event: the
    # endpoint's, null for one that takes every project's.
    project_id = str(ep.project_id) if ep.project_id else None
    db.rollback()

    delivery_id = str(_uuid.uuid4())
    payload = {
        "event": TEST_EVENT,
        "test": True,
        "data": {"message": "This is a test delivery from your Korely install."},
        "project_id": project_id,
    }
    with _an_outbound_place(tenant_id):
        ok, response_status, error = _deliver(url, secret, delivery_id, payload)
    return {"delivered": bool(ok), "status": response_status, "error": error,
            "url": url}


class TooManyTests(threads.Refused):
    """The account has `core/threads.OUTBOUND_PER_ACCOUNT` outbound waits
    under way already (tests, and the lookups of creates and enables), or
    its lookups; `retry_after`: the seconds one attempt can take, at most."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retry_after=int(_ATTEMPT_SECONDS))


# An account's outbound waits on request threads at once (core/threads.Places):
# the Webhooks page's test, the lookup of a create and of an enable. A test
# holds one for its whole attempt, up to `_ATTEMPT_SECONDS`; a lookup until it
# ends, its caller gone or not.
_outbound = threads.Places(threads.OUTBOUND_PER_ACCOUNT, refusal=lambda n: TooManyTests(
    f"{n} webhook tests or address checks of this account are under way already: try "
    f"again once one has answered"))


def _an_outbound_place(tenant_id: UUID):
    """A place among the account's outbound waits, or TooManyTests."""
    return _outbound.held(str(tenant_id))


def purge_expired(db: Session, keep_days: int) -> int:
    """Delete finished deliveries past the retention window.

    Found by following a tester's upgrade criterion into the schema: audit,
    usage, sessions and rate limits were all being trimmed by the purge job and
    this table was not, so the outbox grew forever. On a busy install that is a
    table nobody looks at until a page that reads it gets slow, which is the
    kind of growth nobody notices at the time.

    Only finished rows: delivered, or failed with the attempts exhausted. A
    delivery still waiting for its next retry is live state and is never removed
    on age, however old the queue has become.
    """
    from ..core.times import at_most_a_century

    if keep_days <= 0:
        return 0
    keep = at_most_a_century("webhook_delivery_retention_days", keep_days, 1)
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep)
    n = (
        db.query(AgentWebhookDelivery)
        .filter(
            AgentWebhookDelivery.created_at < cutoff,
            or_(
                AgentWebhookDelivery.status == STATUS_DELIVERED,
                AgentWebhookDelivery.attempts >= AgentWebhookDelivery.max_attempts,
            ),
        )
        .delete(synchronize_session=False)
    )
    if n:
        db.commit()
        logger.info("[agent_webhooks] purged %d finished deliveries older than %d days",
                    n, keep_days)
    return n
