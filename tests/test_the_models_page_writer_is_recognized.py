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

Here the real classes, every provider the product can build (`core.llm`
`get_provider`: the Models page's vendors, Gemini on its own client and every
other one through the OpenAI-compatible door, and the file's writers), and a
structural guard: every member of the protocol exists on each of them, so a
member only the protocol has can never be read as None again. No network."""
import pytest

from korely_agent.core import llm
from korely_agent.core.llm import GeminiProvider, MistralProvider, Provider
from korely_agent.services.facts import extractor
from korely_agent.services.model_choice import PROVIDERS


def _built_as_the_product_builds_them():
    """(label, provider, the model the Models page forced or None), as
    `core.llm.get_provider` builds them."""
    out = []
    for vendor in PROVIDERS.values():
        model = vendor.prefer[0]
        built = (GeminiProvider("k", force_model=model) if vendor.id == "google"
                 else MistralProvider("k", vendor.base, force_model=model, vendor=vendor.id, region=vendor.region))
        out.append((f"models page: {vendor.id}", built, model))
    out.append(("file: gemini", GeminiProvider("k"), None))
    out.append(("file: an OpenAI-compatible base (OVHcloud)",
                MistralProvider("k", "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1", vendor="ovh"), None))
    return out


BUILT = _built_as_the_product_builds_them()


def _protocol_members():
    """What `Provider` declares: its annotations and the names its body defines."""
    names = set(getattr(Provider, "__annotations__", {}))
    names |= {n for n in vars(Provider) if not n.startswith("_")}
    return sorted(names)


def test_the_protocol_declares_what_the_extraction_reads():
    assert {"name", "forced_model", "complete"} <= set(_protocol_members())


@pytest.mark.parametrize("label,provider,_forced", BUILT, ids=[b[0] for b in BUILT])
def test_every_member_of_the_protocol_is_on_every_real_provider(label, provider, _forced):
    """The guard that would have caught it: a member is on the real class,
    never only on the protocol."""
    for member in _protocol_members():
        assert member in dir(type(provider)) or member in vars(provider), (label, member)
        assert hasattr(provider, member), (label, member)


@pytest.mark.parametrize("label,provider,forced", BUILT, ids=[b[0] for b in BUILT])
def test_every_real_provider_says_which_model_the_models_page_chose(label, provider, forced):
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
