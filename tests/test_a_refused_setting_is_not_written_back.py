"""A setting refused at start never writes its value back (the review of 4
Nov 2026): the error goes to the container's log and often to a support
chat, and a key pasted on the wrong line of .env was printed whole, in the
validator's message and in pydantic's `input_value`. Every settings class
hides the input (`hide_input_in_errors`, pydantic's "Hide Input in Errors"),
and the validators say what a line accepts, and which entry of a list, never
what it holds."""
import pytest
from pydantic import ValidationError

# A value shaped like a provider key, made up: the test needs only to find it.
PASTED = "FAKE-sk-NOTASECRET-0123456789abcdef"
DB = "postgresql+psycopg://u:p@localhost:5432/x"


def _refusal(make) -> str:
    """The refusal as it reaches a log or a traceback: its text (`errors()`
    is the program's own reading of it, with the input, and is never
    written)."""
    with pytest.raises(ValidationError) as caught:
        make()
    return str(caught.value) + repr(caught.value)


@pytest.mark.parametrize("field", ["embedding_provider", "korely_mode"])
def test_the_install_s_settings_do_not_write_a_refused_line_back(field):
    from korely_agent.config import AgentSettings

    said = _refusal(lambda: AgentSettings(_env_file=None, database_url=DB, **{field: PASTED}))
    assert PASTED.lower() not in said.lower(), said
    assert field.upper() in said, "the refusal still names the line"


def test_a_gemini_vocabulary_on_the_other_door_is_not_written_back():
    from korely_agent.config import AgentSettings

    name = "gemini-" + PASTED
    said = _refusal(lambda: AgentSettings(_env_file=None, database_url=DB,
                                          extraction_provider="mistral", vocabulary_model=name))
    assert PASTED.lower() not in said.lower(), said


@pytest.mark.parametrize("module,cls", [("korely_agent.config", "AgentSettings")])
def test_every_settings_class_hides_its_input(module, cls):
    import importlib

    from pydantic_settings import BaseSettings

    mod = importlib.import_module(module)
    classes = [getattr(mod, cls)] if cls else [
        v for v in vars(mod).values()
        if isinstance(v, type) and issubclass(v, BaseSettings) and v is not BaseSettings
        and v.__module__ == mod.__name__]
    assert classes
    for c in classes:
        assert c.model_config.get("hide_input_in_errors") is True, c.__name__
