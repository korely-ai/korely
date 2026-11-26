"""Where your memories are written, chosen from the screen at first login.

The flow (2026-10-02): after the first login the operator answers
three questions. Where should the memories be processed, Europe or the
United States? Which provider? In Europe, Mistral or Scaleway. In the US,
OpenAI, Claude, Gemini, Grok or OpenRouter. Then their key. The writing
model comes preselected from our own measures, and they can pick another
from the provider's live list (OpenRouter's most used this week first).

Reading is not chosen here. In the product the customer's own model reads
what /v1/context returns, and we never pay for it.

The rules are the provider key's (provider_key.py). The key is encrypted
at rest (Fernet off SECRET_KEY) and never handed back: the screen gets its
last four characters. It is checked with the provider before it is stored.
A saved choice is in force without a restart. The database wins, then the
file: an install configured in `.env` keeps working and never learns this
module exists.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import InstallSetting
from .provider_key import (NoEncryptionKey, decrypt_secret, encrypt_secret,
                           hold_install_settings)
from .regions import EU, GLOBAL, HOSTED_DEFAULT, REGIONS

logger = logging.getLogger(__name__)

ROW = "model_choice"

# OpenRouter's "most used this week": how many the screen shows first.
TOP = 10


class Refused(ValueError):
    """The provider refused, or the choice is incomplete: nothing was saved."""


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    # "eu" | "global", the words the first login shows (Europe, Global). It was
    # "us" until 2026-10-06, from the EU/US columns of 2026-10-02: the hosted
    # product names its project regions the same (2026-10-06), and
    # nothing stores it, the region is read from the provider.
    region: str
    tagline: str            # the tile's one line
    base: str               # OpenAI-compatible base; Gemini's native API for google
    key_hint: str
    key_url: str            # where the operator creates a key
    prefer: Tuple[str, ...]  # writers in our order: the first the provider lists wins
    measured: Dict[str, str] = field(default_factory=dict)
    # The provider we choose for its region (2026-10-06: nobody has to choose
    # anything, in global or in Europe): the first login goes straight to
    # its key, and the others wait behind "Use another provider".
    recommended: bool = False


# The writers we measured carry their note: LongMemEval (2026-10-02) and
# LoCoMo conversation 26 with relative dates on (2026-10-06, 129 of 152 for
# both recommended writers). The other preferences are fast, cheap models nobody
# has measured yet, and the screen says so.
PROVIDERS: Dict[str, Provider] = {p.id: p for p in (
    Provider("mistral", "Mistral", EU, "Models made in Paris",
             "https://api.mistral.ai/v1", "Mistral API key",
             "https://console.mistral.ai/api-keys",
             ("mistral-small-latest", "mistral-medium-latest")),
    Provider("scaleway", "Scaleway", EU, "gpt-oss-120b, hosted in Paris",
             "https://api.scaleway.ai/v1", "Scaleway secret key",
             "https://console.scaleway.com/iam/api-keys",
             ("gpt-oss-120b", "llama-3.3-70b-instruct"),
             {"gpt-oss-120b": "Measured: 129 of 152 on LoCoMo, about €0.001 per memory."},
             recommended=True),
    Provider("openai", "OpenAI", GLOBAL, "GPT models",
             "https://api.openai.com/v1", "sk-...",
             "https://platform.openai.com/api-keys",
             ("gpt-4.1-mini", "gpt-6-luna", "gpt-5.6-luna", "gpt-5-mini", "gpt-4o-mini")),
    Provider("anthropic", "Claude", GLOBAL, "Models by Anthropic",
             "https://api.anthropic.com/v1", "sk-ant-...",
             "https://platform.claude.com/settings/keys",
             ("claude-haiku-4-5",)),
    Provider("google", "Gemini", GLOBAL, "Gemini 3.1 Flash-Lite, by Google",
             "https://generativelanguage.googleapis.com/v1beta", "AIza...",
             "https://aistudio.google.com/apikey",
             ("gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-3.8-flash"),
             {"gemini-3.1-flash-lite": "Measured: 129 of 152 on LoCoMo and 51 of 60 on "
                                       "LongMemEval, about $0.002 per memory."},
             recommended=True),
    Provider("xai", "Grok", GLOBAL, "Models by xAI",
             "https://api.x.ai/v1", "xai-...",
             "https://console.x.ai",
             ("grok-4-fast-non-reasoning", "grok-4-fast", "grok-3-mini")),
    Provider("openrouter", "OpenRouter", GLOBAL, "Hundreds of models, one key",
             "https://openrouter.ai/api/v1", "sk-or-...",
             "https://openrouter.ai/settings/keys",
             ("openai/gpt-oss-120b:nitro",),
             {"openai/gpt-oss-120b:nitro": "The fastest writer we measured, about 3 s per memory."}),
)}


# ── storage ────────────────────────────────────────────────────────────────

def _empty() -> Dict[str, Any]:
    return {"active": None, "providers": {}}


def _load(db: Session) -> Dict[str, Any]:
    """The stored choice, decrypted. A value that will not decrypt is loud
    and treated as absent, as with the provider key: SECRET_KEY changed."""
    row = db.get(InstallSetting, ROW, populate_existing=True)
    if row is None:
        return _empty()
    try:
        data = json.loads(decrypt_secret(row.value_encrypted))
        if isinstance(data, dict) and isinstance(data.get("providers"), dict):
            return data
        return _empty()
    except NoEncryptionKey:
        logger.error("[models] a choice is stored but SECRET_KEY is not set: it cannot be read")
    except Exception:
        logger.error("[models] the stored model choice will not decrypt. SECRET_KEY has "
                     "changed since it was saved and the old one is not in "
                     "SECRET_KEY_FALLBACKS; save it again from the screen.")
    return {**_empty(), "unreadable": True}


def _store(db: Session, data: Dict[str, Any], by: Optional[str]) -> None:
    token = encrypt_secret(json.dumps(data))
    row = db.get(InstallSetting, ROW)
    if row is None:
        row = InstallSetting(name=ROW, value_encrypted=token)
        db.add(row)
    else:
        row.value_encrypted = token
    row.updated_by = (by or "")[:64] or None
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    # The provider is memoized for the life of the process: without this the
    # choice is saved, the screen says so, and the old model keeps writing.
    from ..core.llm import reset_provider_cache

    reset_provider_cache()


def clear(db: Session) -> bool:
    """Forget the screen's choice: `.env` decides again."""
    hold_install_settings(db)
    row = db.get(InstallSetting, ROW)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    from ..core.llm import reset_provider_cache

    reset_provider_cache()
    return True


# What a clear forgets (`left_without_writer`): the writers chosen here
# (`clear`), or the Google key of the provider's own screen
# (provider_key.clear_google_key).
WRITERS, GOOGLE_KEY_SAVED = "writers", "google_key"


def left_without_writer(db: Session, forgetting: str = WRITERS) -> List[str]:
    """The regions that have a writer now and would have none once
    `forgetting` is forgotten, in the order of REGIONS. The rules are
    core/llm.regions_with_writer's (`regions_from`), asked of the rows this
    session reads now and of the same rows without what is forgotten: a
    region the file's writer still covers is not lost. Read under
    provider_key.hold_install_settings, the answer holds until the clear
    commits."""
    from ..core import llm
    from .provider_key import get_google_key

    data = _load(db)
    screen = _writers_of(data)
    saved = get_google_key(db)
    now = llm.regions_from(screen, saved)
    after = llm.regions_from({} if forgetting == WRITERS else screen,
                             None if forgetting == GOOGLE_KEY_SAVED else saved)
    return [r for r in REGIONS if r in now and r not in after]


def saved_key(db: Session, provider: str) -> Optional[str]:
    return ((_load(db).get("providers") or {}).get(provider) or {}).get("key") or None


# ── the provider ────────────────────────────────────────────────────────────

# How long the screen waits for a provider to list its models. Not
# LLM_TIMEOUT_SECONDS, which bounds a model call on a write: this is a person
# waiting on the first login for a list that costs nothing, and twenty
# seconds is already long for that (audit of 17 Oct 2026: it was a literal).
LIST_TIMEOUT_SECONDS = 20


def _get(url: str, headers: Dict[str, str]) -> Any:
    """A provider's answer to a GET with its key: `headers` are the key's
    (each provider's `_bearer` or its own), sent to `url`'s host alone,
    never on to the address of a redirect (core/llm.provider_request)."""
    from ..core.llm import provider_request

    req = provider_request(url, credentials=headers, method="GET")
    with urllib.request.urlopen(req, timeout=LIST_TIMEOUT_SECONDS) as r:
        return json.loads(r.read().decode() or "null")


def _bearer(key: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


# Not a model a writer can use: embeddings, speech, images, moderation.
_NOT_TEXT = ("embed", "bge", "whisper", "tts", "transcribe", "audio", "realtime",
             "image", "dall-e", "moderation", "search", "ocr", "live")


def _text_only(ids) -> List[str]:
    return [i for i in ids if i and not any(t in i.lower() for t in _NOT_TEXT)]


def _google_models(p: Provider, key: str) -> Tuple[List[str], List[str]]:
    listed = _get(f"{p.base}/models?pageSize=1000", {"x-goog-api-key": key}) or {}
    ids = [m.get("name", "").removeprefix("models/") for m in listed.get("models", [])
           if "generateContent" in (m.get("supportedGenerationMethods") or [])]
    return sorted(_text_only(i for i in ids if i.startswith("gemini"))), []


def _anthropic_models(p: Provider, key: str) -> Tuple[List[str], List[str]]:
    listed = _get(f"{p.base}/models?limit=1000",
                  {"x-api-key": key, "anthropic-version": "2023-06-01"}) or {}
    return [m["id"] for m in listed.get("data", []) if m.get("id")], []


def _openrouter_models(p: Provider, key: str) -> Tuple[List[str], List[str]]:
    # OpenRouter lists its models to anybody: the key is checked on /key.
    _get(f"{p.base}/key", _bearer(key))
    q = urllib.parse.urlencode({"output_modalities": "text",
                                "supported_parameters": "response_format",
                                "sort": "top-weekly"})
    rows = (_get(f"{p.base}/models?{q}", _bearer(key)) or {}).get("data", [])
    ids = [m["id"] for m in rows if m.get("id")]

    def paid(m):
        pr = m.get("pricing") or {}
        try:
            return float(pr.get("prompt") or 0) > 0
        except (TypeError, ValueError):
            return False

    # Most used this week, free and stealth endpoints left out of the top.
    top = [m["id"] for m in rows if m.get("id") and paid(m)][:TOP]
    return ids, top


def _compatible_models(p: Provider, key: str) -> Tuple[List[str], List[str]]:
    """An OpenAI-compatible /models: every text model it lists. OpenAI's
    used to be cut to the ids starting gpt-, o1, o3, o4 or chatgpt, a list
    written by hand that hid a family it did not know, and `save` refused
    what it hid (audit of 17 Oct 2026); its list carries no capability, so
    what is not text goes by `_text_only`, as on every other provider."""
    rows = (_get(f"{p.base}/models", _bearer(key)) or {}).get("data", [])
    if p.id == "mistral":
        rows = [m for m in rows if (m.get("capabilities") or {}).get("completion_chat", True)]
    return sorted(set(_text_only(m.get("id") for m in rows if isinstance(m, dict)))), []


_LISTERS = {"google": _google_models, "anthropic": _anthropic_models,
            "openrouter": _openrouter_models}


def _list_models(p: Provider, key: str) -> Tuple[List[str], List[str]]:
    """(models, most used first) as the provider lists them for this key.
    Raises urllib's errors: the caller turns them into the provider's words."""
    return _LISTERS.get(p.id, _compatible_models)(p, key)


def _listed(model: str, models: List[str]) -> bool:
    """OpenRouter's routing suffixes (":nitro", ":floor") are not separate
    entries in its list: the model is the part before them."""
    return model in models or model.split(":", 1)[0] in models


def suggested(p: Provider, models: List[str]) -> Optional[str]:
    """Our writer for this provider: the first preference it lists (a dated
    id counts for its alias, "claude-haiku-4-5-20251001" for
    "claude-haiku-4-5"), else the first model it lists."""
    for want in p.prefer:
        if _listed(want, models):
            return want
        dated = sorted(m for m in models if m.startswith(want + "-"))
        if dated:
            return dated[-1]
    return models[0] if models else None


def check(provider: str, key: str) -> Dict[str, Any]:
    """Ask the provider whether the key works, and which models it lists.
    Listing models costs nothing on every provider here."""
    p = PROVIDERS[provider]
    key = (key or "").strip()
    if not key:
        return {"ok": False, "detail": "no key given", "models": [], "top": [], "suggested": None}
    try:
        models, top = _list_models(p, key)
    except urllib.error.HTTPError as e:
        return {"ok": False, "detail": f"{p.label} refused the key (HTTP {e.code}).",
                "models": [], "top": [], "suggested": None}
    except Exception as e:
        return {"ok": False, "detail": f"Could not reach {p.label}: {type(e).__name__}: {e}"[:300],
                "models": [], "top": [], "suggested": None}
    if not models:
        return {"ok": False, "detail": f"{p.label} accepted the key but lists no text model for it.",
                "models": [], "top": [], "suggested": None}
    return {"ok": True, "detail": f"{p.label} answered", "models": models, "top": top,
            "suggested": suggested(p, models)}


# ── what the screen sees and saves ─────────────────────────────────────────

def _env_writer() -> Optional[Dict[str, Any]]:
    """The writer `.env` alone configures (the install of before), described
    as the screen's is, or None when the file configures none.

    Read as core/llm.get_provider builds it: EXTRACTION_PROVIDER=mistral is
    the OpenAI-compatible door, whose provider and region are its address's
    (vendor_of_base, region_of_base: None at an address none of ours), with
    MISTRAL_EXTRACTION_MODEL; otherwise Gemini with the key in force and
    EXTRACTION_MODEL. Settings showed dashes for this writer (`active` was
    the screen's choice only) while Usage named the model writing
    (functional test of 28 Oct 2026)."""
    from ..core.llm import current_google_key

    s = get_settings()
    if s.extraction_provider == "mistral":
        key = (s.mistral_api_key or "").strip()
        known = vendor_of_base(s.mistral_api_base)
        pid, region, model = known or "mistral", region_of_base(s.mistral_api_base), \
            s.mistral_extraction_model
        label = PROVIDERS[known].label if known else "OpenAI-compatible address in .env"
    else:
        key = current_google_key() or ""
        pid, region, model, label = "google", GLOBAL, s.extraction_model, PROVIDERS["google"].label
    if not key:
        return None
    return {"provider": pid, "label": label, "region": region, "model": model,
            "last_four": key[-4:]}


def describe(db: Session) -> Dict[str, Any]:
    """What the screen is allowed to know: never a key, its last four."""
    from ..core.llm import current_google_key, embedding_model_name

    s = get_settings()
    data = _load(db)
    row = db.get(InstallSetting, ROW)
    saved = data.get("providers") or {}
    providers = []
    for p in PROVIDERS.values():
        mine = saved.get(p.id) or {}
        key = mine.get("key") or ""
        providers.append({
            "id": p.id, "label": p.label, "region": p.region, "tagline": p.tagline,
            "key_hint": p.key_hint, "key_url": p.key_url,
            "key_set": bool(key), "last_four": key[-4:] if key else None,
            "model": mine.get("model"), "measured": p.measured,
            "recommended": p.recommended,
        })
    found = _in_force(db, data)
    active = None
    if found is not None:
        p, mine = found
        active = {"provider": p.id, "label": p.label, "region": p.region,
                  "model": mine.get("model"), "last_four": mine["key"][-4:]}
    # The writer in force whatever its source: the screen's, else the file's.
    from_file = _env_writer() if active is None else None
    env_ready = from_file is not None
    # The regions some writer can write, by the rules /health reads them
    # with (core/llm.regions_with_writer): a region written by the file's
    # writer read "No provider" here and `configured` there (5 Nov 2026).
    from ..core.llm import regions_from
    from .provider_key import get_google_key

    can_write = regions_from(_writers_of(data), get_google_key(db))
    file_writer = from_file or _env_writer()
    by_region = {}
    for region in REGIONS:
        w = _writer_of(data, region)
        if w is not None:
            by_region[region] = {"provider": w[0].id, "label": w[0].label,
                                 "model": w[1].get("model"), "source": "screen"}
        elif region in can_write and file_writer and file_writer["region"] == region:
            by_region[region] = {"provider": file_writer["provider"], "label": file_writer["label"],
                                 "model": file_writer["model"], "source": "env"}
        else:
            by_region[region] = None
    in_force = active or from_file
    return {
        "configured": active is not None or env_ready,
        "source": "screen" if active else ("env" if env_ready else None),
        # `region` is None for a file's writer at an address of no known region.
        "active": in_force,
        # The region a project that never chose one writes in
        # (core/llm.default_region): the writer in force's on an install of
        # one's own, the service's (Global) on the Cloud, where the page
        # says so (5 Nov 2026).
        "default_region": HOSTED_DEFAULT if s.hosted else (in_force or {}).get("region"),
        # The writer of each region, for the projects that chose one.
        "by_region": by_region,
        "providers": providers,
        "readable": not data.get("unreadable"),
        "can_store": bool((s.secret_key or "").strip()),
        "updated_at": row.updated_at if row is not None else None,
        "updated_by": row.updated_by if row is not None else None,
        "env_provider": s.extraction_provider,
        # `ready`: search by meaning works. Gemini vectors need a Google key
        # (the screen's, the file's, or the one chosen at first login).
        "embeddings": {"provider": s.embedding_provider, "model": embedding_model_name(),
                       "local": s.embedding_provider == "local",
                       "ready": s.embedding_provider == "local" or bool(current_google_key())},
    }


def save(db: Session, provider: str, *, key: Optional[str], model: str,
         by: Optional[str] = None) -> Dict[str, Any]:
    """Check, store, put in force. Raises Refused with nothing stored.

    An empty key keeps the one saved for this provider, and it is checked
    again: a key that stopped working is found here, not at the next write."""
    if provider not in PROVIDERS:
        raise Refused(f"unknown provider {provider!r}")
    p = PROVIDERS[provider]
    data = _load(db)
    if data.pop("unreadable", False):
        data = _empty()
    mine = (data.get("providers") or {}).get(provider) or {}
    key = (key or "").strip() or mine.get("key") or ""
    if not key:
        raise Refused(f"Paste your {p.label} key first.")
    model = (model or "").strip()
    if not model:
        raise Refused("Choose the model that writes your memories.")
    checked = check(provider, key)
    if not checked["ok"]:
        raise Refused(f"{checked['detail']} Nothing was saved.")
    if not _listed(model, checked["models"]):
        raise Refused(f"{p.label} does not list the model {model!r} for this key.")
    # Read again under the lock, after the provider answered (a call that
    # may take LIST_TIMEOUT_SECONDS is never made holding it): a save of
    # another provider meanwhile is kept, not overwritten.
    hold_install_settings(db)
    data = _load(db)
    if data.pop("unreadable", False):
        data = _empty()
    data.setdefault("providers", {})[provider] = {"key": key, "model": model}
    # Saved last, it is the install's default writer (`active`, what a
    # project that never chose a region follows) and the writer of its own
    # region (8 Oct 2026, services/regions.py): saving Scaleway after Gemini
    # leaves Gemini the writer of the projects that chose "global". On the
    # Cloud `active` is only the provider saved last: the default region is
    # the service's, Global (core/llm.default_region, 5 Nov 2026).
    data["active"] = provider
    data.setdefault("by_region", {})[p.region] = provider
    _store(db, data, by)
    return describe(db)


# ── in force ───────────────────────────────────────────────────────────────

def _in_force(db: Session, data: Optional[Dict[str, Any]] = None
              ) -> Optional[Tuple[Provider, Dict[str, Any]]]:
    data = data if data is not None else _load(db)
    pid = data.get("active")
    mine = (data.get("providers") or {}).get(pid or "") or {}
    if pid not in PROVIDERS or not mine.get("key") or not mine.get("model"):
        return None
    return PROVIDERS[pid], mine


def _writer_of(data: Dict[str, Any], region: str
               ) -> Optional[Tuple[Provider, Dict[str, Any]]]:
    """The writer saved for `region`: the provider saved last in it. A
    choice stored before regions (no `by_region`) has one writer, its
    active provider's, for that provider's region."""
    saved = data.get("providers") or {}
    pid = (data.get("by_region") or {}).get(region)
    if pid is None:
        active = data.get("active")
        if active in PROVIDERS and PROVIDERS[active].region == region:
            pid = active
    mine = saved.get(pid or "") or {}
    if pid not in PROVIDERS or PROVIDERS[pid].region != region \
            or not mine.get("key") or not mine.get("model"):
        return None
    return PROVIDERS[pid], mine


def region_of_base(base: Optional[str]) -> Optional[str]:
    """The region of an OpenAI-compatible address, from the providers we
    know; None for an address that is none of theirs."""
    base = (base or "").rstrip("/")
    return next((p.region for p in PROVIDERS.values() if p.base.rstrip("/") == base), None)


def vendor_of_base(base: Optional[str]) -> Optional[str]:
    base = (base or "").rstrip("/")
    return next((p.id for p in PROVIDERS.values() if p.base.rstrip("/") == base), None)


def read_writers() -> Dict[str, Any]:
    """The writers chosen on the screen, by region, and the default region
    (the active provider's): {"default": "eu" | "global" | None, "eu":
    (provider id, base URL, key, model) or None, "global": ...}. Own
    session; a database that cannot be read raises, as below."""
    from ..db import get_own_sessionmaker

    db = get_own_sessionmaker()()   # asked inside a model call's caller's work
    try:
        data = _load(db)
    finally:
        db.close()
    return _writers_of(data)


def _writers_of(data: Dict[str, Any]) -> Dict[str, Any]:
    """`read_writers` of a stored choice."""
    found = _in_force(None, data)
    out: Dict[str, Any] = {"default": found[0].region if found else None}
    for region in REGIONS:
        w = _writer_of(data, region)
        out[region] = (w[0].id, w[0].base, w[1]["key"], w[1]["model"]) if w else None
    return out
