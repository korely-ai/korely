"""What the log guards call a log line, in one place (audit of 20 Oct 2026).

The guards of the log rules of CONTRIBUTING.md
(tests/test_logs_carry_no_exception_text.py, tests/test_log_prefixes.py) each
wrote their own test of a call: the six level methods on `logger`, `log` or
`logging`. `logger.log(level, ...)`, which 0.2.0 put in core/llm.py, and the
loggers main.py names `access` and `errors`, were invisible to them.
"""
import ast

# Every method of logging.Logger that writes a line.
METHODS = {"debug", "info", "warning", "warn", "error", "critical", "fatal", "exception", "log"}


def logger_names(tree: ast.AST) -> set:
    """The names a module logs through: the module `logging` itself, the
    usual `logger` and `log`, and every name it binds to logging.getLogger."""
    names = {"logger", "log", "logging"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) \
                and ast.unparse(node.value.func) in ("logging.getLogger", "getLogger"):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    return names


def _receiver_logs(receiver: ast.AST, names: set) -> bool:
    """A name the module logs through, an attribute of that name (`self.log`),
    or a logger made on the spot: `logging.getLogger(__name__).warning(...)`
    (audit of 27 Oct 2026: hosted/__init__.py logs two lines that way)."""
    if isinstance(receiver, ast.Name):
        return receiver.id in names
    if isinstance(receiver, ast.Attribute):
        return receiver.attr in names
    return (isinstance(receiver, ast.Call)
            and ast.unparse(receiver.func) in ("logging.getLogger", "getLogger"))


def is_log_call(call: ast.AST, names: set) -> bool:
    return (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
            and call.func.attr in METHODS and _receiver_logs(call.func.value, names))


def message_args(call: ast.Call) -> list:
    """The arguments that make the line: all of them, but the level of
    `logger.log(level, ...)`."""
    return list(call.args[1:]) if call.func.attr == "log" else list(call.args)
