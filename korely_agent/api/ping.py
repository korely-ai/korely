"""/v1/ping: does this key work against this install?

The smallest authenticated call there is, for checking a freshly minted key
before pointing an agent at it:

    curl -H "Authorization: Bearer kor_self_..." https://your-domain/v1/ping
    -> 200 {"ok": true, "tier": "hobby", "region": "eu",
            "processing_region": "eu", "scopes": [...]}

The same code serves both products (KORELY_MODE=hosted, 9 Oct 2026), and the
two differ on the key, so each is said here (audit of 17 Oct 2026).
On an install of one's own a key is `kor_self_...`, and every `kor_live_` key
gets a 401 that says it looks like the hosted service's (core/auth.refusal_for;
no `kor_live_` key is live here since 0.2.0). On the Cloud `kor_live_` is the prefix it
mints (services/keys.minted_prefix) and such a 401 is only an unknown key.

`tier` is the plan name a key carries. On the Cloud it is the account's plan,
rewritten at every change of plan (hosted/billing.py). On an install of one's
own every key carries `hobby` and it limits nothing (models.AgentApiKey). It
is still in the reply there because this reply is what clients parse to check
a key, and a field that vanishes is a break. The clients read 2026-09-28 (korely-memory 0.1.14 for
Python, 0.1.6 for JS, and its n8n node) take `tier` only from the hosted
signup reply, never from here; the n8n credential test looks at the status.

`processing_region` (2026-10-07) is where a memory is read by a model: "eu"
or "global", the region of the key's project (its own, else the install's
default writer's, services/regions.py, 8 Oct 2026), null when that region
has no writer. The hosted product answers the same field for the project of
a key, so the SDKs read one field on both.

`region` is not where the data is: it is the label the operator set with
REGION (`eu` when unset), which this process cannot check, since it runs
wherever it is put (audit of 19 Oct 2026: /docs published it as "the region
its data is in", so an install that never set it told its clients their data
was in the EU wherever it ran). CONTRIBUTING.md: never say a place the code
does not establish.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..config import settings
from ..core.auth import enforce_rate_limit, key_project, presented_secret, verify_api_key
from ..db import AgentApiKey, get_db
from ..services import keys as key_service
from ..services import regions
from ..services.usage import log_v1_request
from ._bodies import its_parameters_only
from .errors import v1_errors

# No scope check and no parameters. Counted against the account's ceiling
# like every /v1 route that writes to the trail (audit of 18 Oct 2026): it
# had no ceiling while it wrote an audit row per call, kept 400 days,
# so a key past its ceiling kept growing the shared table through it. Its
# errors: the key (401), the ceiling (429), ours (500) and a database that
# did not answer while the key was checked (503, core/auth.credential_lookup).
router = APIRouter(prefix="/v1", tags=["v1"], dependencies=[Depends(enforce_rate_limit)],
                   responses=v1_errors(401, 429, 500, 503))


@router.get("/ping")
def ping(key: AgentApiKey = Depends(verify_api_key), db: Session = Depends(get_db),
         _only_its_own: None = Depends(its_parameters_only),
         secret: Optional[str] = Depends(presented_secret)) -> dict:
    """Check a key before pointing an agent at it: 200 with the `scopes` the
    secret sent holds (the old secret of a rotated key: without `admin`), the
    `processing_region` where a model reads its memories (null when that
    region has no model to write with) and `region`, a label the operator set
    with REGION (`eu` when not set), which the service cannot check: not a
    statement of where the data is. Or 401. No scope is needed. It counts
    against the request ceiling like every other call: past it, 429 with
    `Retry-After`."""
    # In the trail like every /v1 call (CONTRIBUTING.md): which key was checked,
    # and when. It wrote nothing until the audit of 17 Oct 2026. A read with
    # no end user and nothing read.
    log_v1_request(db, key.tenant_id, "ping:get", api_key_id=key.id)
    return {
        "ok": True,
        "tier": key.tier,
        # What the operator declared with REGION, not a measurement: this
        # process cannot see where the machine it runs on is.
        "region": settings.region,
        "processing_region": regions.processing(db, key.tenant_id, key_project(db, key)),
        # What this secret holds: the old one of a rotation, no `admin`.
        "scopes": key_service.scopes_of(key, secret),
    }
