"""The part of GitHub Actions' expression language the workflows use, so a
test can run a workflow's own conditions and matrices instead of matching
their text (17 Nov 2026).

Literals (strings in single quotes, true, false, null, numbers), context
paths (`github.event.repository.private`, `matrix.platform`), `!`, `==`,
`!=`, `&&`, `||`, parentheses and `fromJSON`, with GitHub's meaning: `&&`
and `||` give one of their operands, not a boolean; a missing property is
null; strings compare ignoring case; operands of different types compare as
numbers (docs.github.com, "Evaluate expressions in workflows and actions").
Anything else is refused, so a workflow that starts using more fails the
tests that read it instead of being read wrongly.
"""
import itertools
import json
import math
import re

_TOKEN = re.compile(r"""\s*(?:
    (?P<string>'(?:[^']|'')*')
  | (?P<number>-?\d+(?:\.\d+)?)
  | (?P<op>&&|\|\||==|!=|!|\(|\)|,)
  | (?P<name>[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*)
)""", re.X)

_WRAPPED = re.compile(r"^\s*\$\{\{(.*)\}\}\s*$", re.S)


def _tokens(text):
    out, pos = [], 0
    text = text.rstrip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise ValueError(f"not an expression these tests read: {text[pos:]!r}")
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
        pos = m.end()
    return out


def truthy(value):
    if isinstance(value, float) and math.isnan(value):
        return False
    return value not in (None, False, 0, "")


def _number(value):
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value) if value.strip() else 0.0
        except ValueError:
            return math.nan
    return math.nan


def _equal(a, b):
    if isinstance(a, str) and isinstance(b, str):
        return a.lower() == b.lower()
    if type(a) is type(b) and not isinstance(a, (int, float)):
        return a == b
    return _number(a) == _number(b)


class _Parser:
    def __init__(self, text, context):
        self.tokens, self.at, self.context = _tokens(text), 0, context

    def peek(self):
        return self.tokens[self.at] if self.at < len(self.tokens) else (None, None)

    def take(self, value=None):
        token = self.peek()
        if token[0] is None or (value is not None and token[1] != value):
            raise ValueError(f"expected {value!r}, found {token[1]!r}")
        self.at += 1
        return token

    def run(self):
        value = self.either()
        if self.peek()[0] is not None:
            raise ValueError(f"left over: {self.peek()[1]!r}")
        return value

    def either(self):
        value = self.both()
        while self.peek() == ("op", "||"):
            self.take()
            right = self.both()
            value = value if truthy(value) else right
        return value

    def both(self):
        value = self.compared()
        while self.peek() == ("op", "&&"):
            self.take()
            right = self.compared()
            value = right if truthy(value) else value
        return value

    def compared(self):
        value = self.negated()
        if self.peek() in (("op", "=="), ("op", "!=")):
            op = self.take()[1]
            same = _equal(value, self.negated())
            value = same if op == "==" else not same
        return value

    def negated(self):
        if self.peek() == ("op", "!"):
            self.take()
            return not truthy(self.negated())
        return self.primary()

    def primary(self):
        kind, text = self.take()
        if (kind, text) == ("op", "("):
            value = self.either()
            self.take(")")
            return value
        if kind == "string":
            return text[1:-1].replace("''", "'")
        if kind == "number":
            return float(text)
        if kind != "name":
            raise ValueError(f"unexpected {text!r}")
        if text in ("true", "false", "null"):
            return {"true": True, "false": False, "null": None}[text]
        if self.peek() == ("op", "("):
            if text.lower() != "fromjson":
                raise ValueError(f"a function these tests do not read: {text}")
            self.take("(")
            argument = self.either()
            self.take(")")
            return json.loads(argument)
        value = self.context
        for part in text.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        return value


def evaluate(expression, context):
    """An expression, bare (as an `if:` may be written) or in `${{ }}`."""
    m = _WRAPPED.match(expression)
    return _Parser(m.group(1) if m else expression, context).run()


def value(field, context):
    """A workflow's field as GitHub reads it: a field that is one `${{ }}`
    is that expression's value (a list from fromJSON stays a list), any
    other value is itself."""
    if isinstance(field, str) and "${{" in field:
        if not _WRAPPED.match(field) or field.count("${{") != 1:
            raise ValueError(f"an interpolated string these tests do not read: {field!r}")
        return evaluate(field, context)
    return field


def event(name, private):
    """The context of an event of a public or a private repository."""
    return {"github": {"event_name": name, "event": {"repository": {"private": private}}}}


def matrix(job, context):
    """The rows of a job's matrix, each a dict, in GitHub's order."""
    rows = job.get("strategy", {}).get("matrix")
    if rows is None:
        return [{}]
    if "include" in rows or "exclude" in rows:
        raise ValueError("include and exclude are not read here")
    keys = list(rows)
    lists = [value(rows[k], context) for k in keys]
    for k, v in zip(keys, lists):
        if not isinstance(v, list) or not v:
            raise ValueError(f"matrix.{k} is not a list of values: {v!r}")
    return [dict(zip(keys, combo)) for combo in itertools.product(*lists)]


def on(workflow):
    """A workflow's triggers: YAML 1.1 reads the key `on` as true."""
    return workflow.get("on", workflow.get(True))
