"""The writer chosen on the Models page is the writer the extraction rules
were measured on (1 Nov 2026).

`services/facts/extractor._measured_writer` turns on the rules measured on
gpt-oss-120b (the change of state written as the new state, the faithful
observation; on by default since 0.1.20 "for the gpt-oss writer alone") by
reading `client.forced_model`, "the model the Models page chose wins over the
one asked for". That property was a default body of `core.llm.Provider`, a
`typing.Protocol`, which the concrete providers never subclassed: a protocol's
members are not inherited by a class that only matches it structurally (PEP
544, "Explicitly declaring implementation"). So on a real provider the
attribute did not exist, `_measured_writer` read the file's model name
("mistral-small-latest" by default), and a gpt-oss chosen on the Models page,
the Cloud's European writer (Scaleway gpt-oss-120b) among them, never had the
rules it was measured with, from 0.1.20 to 1.0.2. The test that pinned the
rule used a fake client that set the attribute itself
(tests/test_observation_rules.py), so it passed.

Here the real classes, every provider the product can build, each built by
`core.llm.get_provider` itself (the Models page's vendors, Gemini on its own
client and every other one through the OpenAI-compatible door, and the file's
writers), and a structural guard: every member of the protocol exists on each
of them, and every method takes the protocol's parameters the way the protocol
lets a caller pass them, with its defaults, so a member only the protocol has
can never be read as None again. Until 3 Nov 2026 the guard built the two
classes by hand, so a third one returned by `get_provider` would not have been
checked. The Models page's vendors come from `PROVIDERS`; the file's writers
from the values `EXTRACTION_PROVIDER` declares (a `Literal` in
`AgentSettings`, which reads every line into one of them), so a branch
`get_provider` gains for a new value comes under the guard with the value.

Here too which writer gets which gpt-oss rule: the extraction rules go to the
weights they were measured on, gpt-oss-120b, under any host's spelling; the
least reasoning to every gpt-oss. No network."""
import inspect
import json
from typing import get_args

import pytest

from korely_agent.config import AgentSettings
from korely_agent.core import llm
from korely_agent.core.llm import GeminiProvider, MistralProvider, Provider
from korely_agent.services.facts import extractor
from korely_agent.services.model_choice import PROVIDERS

OVH = "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1"

# label: EXTRACTION_PROVIDER, one per value the setting declares.
FILE_WRITERS = {f"file: {v}": v
                for v in get_args(AgentSettings.model_fields["extraction_provider"].annotation)}
WRITERS = [f"models page: {v}" for v in PROVIDERS] + list(FILE_WRITERS)


def _built_by_get_provider(label):
    """(provider, the model the Models page forced or None) for the writer
    `label` names, as `core.llm.get_provider` builds it: a vendor of the
    Models page chosen on the screen (`screen_writers`), or one of the file's
    writers. Built when a test asks, so a writer that cannot be built fails
    its own case."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(llm, "_saved_elsewhere", lambda: None)   # no database read
        if label in FILE_WRITERS:
            # Every writer's key, so whichever value it is can be built; the
            # OpenAI-compatible door at OVHcloud's address.
            s = llm.get_settings()
            mp.setattr(llm, "screen_writers", lambda: {})
            mp.setattr(llm, "current_google_key", lambda: "k")
            mp.setattr(s, "extraction_provider", FILE_WRITERS[label])
            mp.setattr(s, "mistral_api_key", "k")
            mp.setattr(s, "mistral_api_base", OVH)
            region, model = None, None
        else:
            vendor = PROVIDERS[label.removeprefix("models page: ")]
            region, model = vendor.region, vendor.prefer[0]
            mp.setattr(llm, "screen_writers", lambda: {
                "default": vendor.region, vendor.region: (vendor.id, vendor.base, "k", model)})
        llm.reset_provider_cache()
        try:
            return llm.get_provider(region), model
        finally:
            llm.reset_provider_cache()


def _protocol_members():
    """What `Provider` declares: its annotations and the names its body defines."""
    names = set(getattr(Provider, "__annotations__", {}))
    names |= {n for n in vars(Provider) if not n.startswith("_")}
    return sorted(names)


_P = inspect.Parameter


def _takes(declared, theirs):
    """What of `declared` (a protocol method's signature) the signature
    `theirs` does not accept as the protocol lets a caller pass it, by the
    typing specification's rules for a callable standing in for another
    (Callables, "Assignability rules for callables": theirs accepts every
    call the protocol's accepts). A positional parameter is theirs at the
    same place; a keyword-only one is theirs by name, keyword-only or
    standard, or taken by their **kwargs ("Parameter kinds", "**kwargs
    parameters"); a default the protocol has is theirs too, and the same one,
    which is stricter than the specification (a call may leave it out, so it
    asks only for a default); and they require nothing the protocol does not
    pass."""
    mine = [p for p in declared.parameters.values() if p.name != "self"]
    their = [p for p in theirs.parameters.values() if p.name != "self"]
    by_name = {p.name: p for p in their}
    positional = [p.name for p in their if p.kind in (_P.POSITIONAL_ONLY, _P.POSITIONAL_OR_KEYWORD)]
    var_keyword = any(p.kind is _P.VAR_KEYWORD for p in their)
    found = []
    for i, p in enumerate(mine):
        t = by_name.get(p.name)
        if p.kind is _P.POSITIONAL_OR_KEYWORD:
            ok = t is not None and t.kind is _P.POSITIONAL_OR_KEYWORD and positional[i:i + 1] == [p.name]
        elif p.kind is _P.KEYWORD_ONLY:
            ok = (t.kind in (_P.POSITIONAL_OR_KEYWORD, _P.KEYWORD_ONLY) if t is not None else var_keyword)
        else:
            ok = t is not None and t.kind is p.kind
        if ok and t is not None and p.default is not _P.empty:
            ok = t.default == p.default
        if not ok:
            found.append(p.name)
    found += [p.name for p in their if p.name not in declared.parameters
              and p.kind not in (_P.VAR_POSITIONAL, _P.VAR_KEYWORD) and p.default is _P.empty]
    return found


def _unlike_the_protocol(provider):
    """What `provider` lacks of the protocol: a member it does not have, or a
    parameter of a protocol method its method does not take as the protocol
    lets a caller pass it (`_takes`). A class matches a protocol by its
    members and inherits nothing from it (PEP 544)."""
    found = []
    for member in _protocol_members():
        if not (member in dir(type(provider)) or member in vars(provider)) or not hasattr(provider, member):
            found.append(member)
            continue
        declared = inspect.getattr_static(Provider, member, None)
        if not inspect.isfunction(declared):
            continue
        theirs = inspect.signature(getattr(type(provider), member))
        found += [f"{member}({name})" for name in _takes(inspect.signature(declared), theirs)]
    return found


def test_the_protocol_declares_what_the_extraction_reads():
    assert {"name", "forced_model", "complete"} <= set(_protocol_members())


def test_the_protocol_takes_the_reasoning_its_callers_pass():
    """services/predicates.py asks `complete(..., reasoning=think)` of any
    provider; the protocol says so, with the default both classes have."""
    p = inspect.signature(Provider.complete).parameters["reasoning"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is True


def test_every_writer_get_provider_builds_is_checked():
    built = [_built_by_get_provider(label)[0] for label in WRITERS]
    assert all(b is not None for b in built)
    assert {type(b) for b in built} == {GeminiProvider, MistralProvider}


@pytest.mark.parametrize("label", WRITERS)
def test_every_member_of_the_protocol_is_on_every_real_provider(label):
    """The guard that would have caught it: a member is on the real class,
    never only on the protocol."""
    provider, _forced = _built_by_get_provider(label)
    assert _unlike_the_protocol(provider) == [], label


def test_a_new_provider_in_get_provider_comes_under_the_guard(monkeypatch):
    """A class `get_provider` starts returning is checked without being named
    here: one without `forced_model` and without `reasoning` is found."""
    class Third:
        name = "third"

        def __init__(self, *a, **k):
            pass

        def complete(self, messages, *, model=None, max_tokens=1024,
                     temperature=0.2, json_only=False):
            raise AssertionError("never called")

    monkeypatch.setattr(llm, "MistralProvider", Third)
    third, _ = _built_by_get_provider("models page: scaleway")
    assert type(third) is Third
    assert _unlike_the_protocol(third) == ["complete(reasoning)", "forced_model"]
    assert _unlike_the_protocol(_built_by_get_provider("file: gemini")[0]) == []


def _complete_of(source):
    namespace = {}
    exec(f"def complete(self, {source}): pass", namespace)
    return inspect.signature(namespace["complete"])


@pytest.mark.parametrize("source,missing", [
    # the protocol's own parameters, another way of writing them
    ("messages, *, model=None, max_tokens=1024, temperature=0.2, json_only=False, reasoning=True", []),
    ("messages, model=None, max_tokens=1024, temperature=0.2, json_only=False, reasoning=True", []),
    ("messages, *, model=None, max_tokens=1024, temperature=0.2, json_only=False, **kw", []),
    ("messages, *, model=None, max_tokens=1024, temperature=0.2, json_only=False, reasoning=True, x=1", []),
    # what a caller of the protocol could not call
    ("messages, *, model=None, max_tokens=1024, temperature=0.2, json_only=False", ["reasoning"]),
    ("messages, *, model=None, max_tokens=1024, temperature=0.2, json_only=False, reasoning=False",
     ["reasoning"]),
    ("*, messages, model=None, max_tokens=1024, temperature=0.2, json_only=False, reasoning=True",
     ["messages"]),
    ("model, messages, max_tokens=1024, temperature=0.2, json_only=False, reasoning=True",
     ["messages", "model"]),
    ("messages, region, *, model=None, max_tokens=1024, temperature=0.2, json_only=False, reasoning=True",
     ["region"]),
])
def test_the_guard_reads_a_signature_as_a_caller_calls_it(source, missing):
    assert _takes(inspect.signature(Provider.complete), _complete_of(source)) == missing


@pytest.mark.parametrize("label", WRITERS)
def test_every_real_provider_says_which_model_the_models_page_chose(label):
    provider, forced = _built_by_get_provider(label)
    assert provider.forced_model == forced, label


def test_the_european_writer_gets_the_rules_measured_on_it(monkeypatch):
    eu = MistralProvider("k", PROVIDERS["scaleway"].base, force_model="gpt-oss-120b",
                         vendor="scaleway", region="eu")
    # The file's model is another one, as on the Cloud: the Models page wins.
    monkeypatch.setattr(extractor, "_select_provider", lambda model_name: (eu, "mistral-small-latest"))
    assert extractor._measured_writer(None) == "gpt-oss"
    assert extractor._states(None) is True and extractor._faithful(None) is True
    assert extractor._attributes(None) is False


def test_a_gpt_oss_chosen_on_openrouter_is_a_gpt_oss_too(monkeypatch):
    router = MistralProvider("k", PROVIDERS["openrouter"].base, force_model="openai/gpt-oss-120b:nitro",
                             vendor="openrouter")
    monkeypatch.setattr(extractor, "_select_provider", lambda model_name: (router, "mistral-small-latest"))
    assert extractor._measured_writer(None) == "gpt-oss"


def test_other_writers_are_read_as_before(monkeypatch):
    for client, file_model, measured in (
            (MistralProvider("k", PROVIDERS["mistral"].base, force_model="mistral-small-latest"), "x", None),
            (MistralProvider("k", PROVIDERS["scaleway"].base, force_model="llama-3.3-70b-instruct"), "x", None),
            (MistralProvider("k", PROVIDERS["scaleway"].base), "gpt-oss-120b", "gpt-oss"),
            (MistralProvider("k", PROVIDERS["scaleway"].base), "mistral-small-latest", None),
            (GeminiProvider("k", force_model="gemini-3.1-flash-lite"), "x", "gemini"),
            (GeminiProvider("k"), "gemini-3.1-flash-lite", "gemini")):
        monkeypatch.setattr(extractor, "_select_provider", lambda model_name, c=client, m=file_model: (c, m))
        assert extractor._measured_writer(None) == measured, (client.name, client.forced_model, file_model)


def test_the_provider_still_sends_the_forced_model(monkeypatch):
    """The value the extraction now reads is the one every call already sent."""
    sent = {}

    class Done(Exception):
        pass

    def stop(vendor, model, region):
        sent["model"] = model
        raise Done()

    monkeypatch.setattr(llm, "_before_the_call", stop)
    eu = MistralProvider("k", PROVIDERS["scaleway"].base, force_model="gpt-oss-120b", vendor="scaleway", region="eu")
    with pytest.raises(Done):
        eu.complete([{"role": "user", "content": "x"}], model="mistral-small-latest")
    assert sent["model"] == eu.forced_model == "gpt-oss-120b"


# The extraction rules follow the weights they were measured on, gpt-oss-120b,
# under the name each host gives them; the least reasoning, the floor of the
# whole family, goes to every gpt-oss (3 Nov 2026: until then the rules went to
# any name containing "gpt-oss", and the least reasoning asked for it with the
# case of the name). model, the rules, the least reasoning.
@pytest.mark.parametrize("model,measured,least", [
    ("gpt-oss-120b", "gpt-oss", True),                             # Scaleway, OVHcloud
    ("openai/gpt-oss-120b:nitro", "gpt-oss", True),                # OpenRouter
    ("openai/gpt-oss-120b:free", "gpt-oss", True),
    ("GPT-OSS-120B", "gpt-oss", True),
    ("accounts/fireworks/models/gpt-oss-120b", "gpt-oss", True),   # Fireworks
    ("gpt-oss:120b", "gpt-oss", True),                             # Ollama's tag
    ("hf.co/unsloth/gpt-oss-120b-GGUF:Q4_K_M", "gpt-oss", True),   # a GGUF through Ollama
    ("openai.gpt-oss-120b-1:0", "gpt-oss", True),                  # Bedrock
    ("/models/gpt-oss-120b/", "gpt-oss", True),                    # a vLLM served path
    (" gpt-oss-120b ", "gpt-oss", True),
    ("gpt-oss-20b", None, True),
    ("openai/gpt-oss-20b:nitro", None, True),
    ("gpt-oss:20b", None, True),
    ("gpt-oss-1200b", None, True),
    ("gpt-oss-120", None, True),
    ("gpt-oss", None, True),                                       # a served name without the size
    ("gpt-oss-safeguard-120b", None, True),                        # another model of the family
    ("xgpt-oss-120b", None, False),                                # "gpt-oss" inside another word
    ("gpt-oss120b", None, True),                                   # the size glued to the name
    ("gpt-ossx", None, True),
    ("llama-3.3-70b-instruct", None, False),
    ("gemini-3.1-flash-lite", None, False),
])
def test_the_rules_follow_the_measured_weights_and_the_least_reasoning_the_family(
        monkeypatch, model, measured, least):
    for base in (PROVIDERS["scaleway"].base, PROVIDERS["openrouter"].base, OVH):
        writer = MistralProvider("k", base, force_model=model)
        monkeypatch.setattr(extractor, "_select_provider", lambda m, w=writer: (w, "mistral-small-latest"))
        assert extractor._measured_writer(None) == measured, (base, model)
        fields = llm._least_reasoning(base, model)
        assert fields == (llm._least_reasoning(base, "gpt-oss-120b") if least else {}), (base, model)


def test_the_measured_weights_are_named_as_the_name_is_read():
    """An entry of MEASURED_GPT_OSS is compared with what `_gpt_oss_weights`
    reads from a name: one it could never return would never match."""
    assert llm.MEASURED_GPT_OSS
    for entry in llm.MEASURED_GPT_OSS:
        assert llm._gpt_oss_weights(entry) == entry


def test_both_rules_for_every_writer_the_models_page_lists(monkeypatch):
    """For every preferred writer of every vendor, on its own door: the least
    reasoning goes to a gpt-oss, the extraction rules to the measured one,
    and the measured one is a gpt-oss."""
    for vendor in PROVIDERS.values():
        for model in vendor.prefer:
            writer = MistralProvider("k", vendor.base, force_model=model, vendor=vendor.id)
            measured = llm.measured_gpt_oss(model)
            assert bool(llm._least_reasoning(vendor.base, model)) is llm._gpt_oss(model), (vendor.id, model)
            assert not measured or llm._gpt_oss(model), (vendor.id, model)
            monkeypatch.setattr(extractor, "_select_provider", lambda m, w=writer: (w, "mistral-small-latest"))
            assert (extractor._measured_writer(None) == "gpt-oss") is measured, (vendor.id, model)


def test_gemini_is_gemini_even_with_a_gpt_oss_name(monkeypatch):
    client = GeminiProvider("k", force_model="gpt-oss-120b")
    monkeypatch.setattr(extractor, "_select_provider", lambda m: (client, "gpt-oss-120b"))
    assert extractor._measured_writer(None) == "gemini"


def test_the_forced_model_wins_over_the_files_in_both_directions(monkeypatch):
    for forced, file_model, expected in (("gpt-oss-20b", "gpt-oss-120b", None),
                                         ("gpt-oss-120b", "gpt-oss-20b", "gpt-oss"),
                                         (None, "gpt-oss-120b", "gpt-oss"),
                                         (None, None, None)):
        writer = MistralProvider("k", PROVIDERS["scaleway"].base, force_model=forced)
        monkeypatch.setattr(extractor, "_select_provider", lambda m, w=writer, f=file_model: (w, f))
        assert extractor._measured_writer(None) == expected, (forced, file_model)


class _Reply:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _sent_payload(monkeypatch, provider, **call):
    """The JSON body `provider.complete` sends, the host answering a reply
    that is read as every other one. The call's outcome goes to a copy of
    what /health reads (`llm._last_call`), not to the process's."""
    sent = {}

    def fake_urlopen(req, timeout=None):
        sent["payload"] = json.loads(req.data)
        return _Reply(b'{"choices":[{"message":{"content":"x"}}],"usage":{}}')

    monkeypatch.setattr(llm, "_last_call", dict(llm._last_call))
    monkeypatch.setattr(llm, "_before_the_call", lambda *a, **k: None)
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    provider.complete([{"role": "user", "content": "hi"}], **call)
    return sent["payload"]


@pytest.mark.parametrize("reasoning", [True, False])
def test_the_request_of_the_measured_writer_carries_the_least_reasoning(monkeypatch, reasoning):
    w = MistralProvider("k", PROVIDERS["scaleway"].base, force_model="gpt-oss-120b", vendor="scaleway")
    assert _sent_payload(monkeypatch, w, reasoning=reasoning)["reasoning_effort"] == "low"


@pytest.mark.parametrize("model", ["gpt-oss-20b", "openai/gpt-oss-20b"])
def test_the_request_of_another_gpt_oss_carries_the_least_reasoning_too(monkeypatch, model):
    w = MistralProvider("k", PROVIDERS["openrouter"].base, force_model=model, vendor="openrouter")
    assert _sent_payload(monkeypatch, w)["reasoning"] == {"effort": "low"}


def test_the_request_of_another_model_carries_none(monkeypatch):
    w = MistralProvider("k", PROVIDERS["openrouter"].base, force_model="meta-llama/llama-3.3-70b-instruct",
                        vendor="openrouter")
    payload = _sent_payload(monkeypatch, w)
    assert "reasoning" not in payload and "reasoning_effort" not in payload


def test_a_call_that_names_the_model_without_a_forced_one_is_judged_by_that_name(monkeypatch):
    w = MistralProvider("k", PROVIDERS["scaleway"].base, vendor="scaleway")
    assert _sent_payload(monkeypatch, w, model="GPT-OSS-120B")["reasoning_effort"] == "low"
    assert "reasoning_effort" not in _sent_payload(monkeypatch, w, model="llama-3.3-70b-instruct")
