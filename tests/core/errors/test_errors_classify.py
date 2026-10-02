from __future__ import annotations

from svbg.core.errors import classify as classify_fn
from svbg.core.errors.classify import DEFAULT, ClassifierRegistry, Severity, register


class PanelError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


def test_default_for_unknown() -> None:
    assert classify_fn(ValueError("x")) == DEFAULT
    assert DEFAULT.title_ru
    assert DEFAULT.hint_ru


def test_builtin_timeout_and_connection() -> None:
    assert classify_fn(TimeoutError()).severity is Severity.WARN
    assert "время" in classify_fn(TimeoutError()).title_ru
    assert "связи" in classify_fn(ConnectionResetError()).title_ru


def test_registered_rule_wins_and_unregisters() -> None:
    unregister = register(
        lambda e: isinstance(e, PanelError) and e.status == 401, "Панель отклонила токен", "Проверьте токен"
    )
    try:
        c = classify_fn(PanelError(401))
        assert c.title_ru == "Панель отклонила токен"
        assert c.hint_ru == "Проверьте токен"
        assert classify_fn(PanelError(500)) == DEFAULT
    finally:
        unregister()
    assert classify_fn(PanelError(401)) == DEFAULT


def test_priority_and_order() -> None:
    reg = ClassifierRegistry()
    reg.register(lambda e: True, "first", "h1")
    reg.register(lambda e: True, "second", "h2")
    assert reg.classify(ValueError()).title_ru == "second"  # later registration wins at equal priority
    reg.register(lambda e: True, "low", "h", priority=-5)
    reg.register(lambda e: True, "high", "h", priority=5)
    assert reg.classify(ValueError()).title_ru == "high"


def test_broken_predicate_is_skipped() -> None:
    reg = ClassifierRegistry()
    reg.register_types(ValueError, "value", "h")

    def broken(e: BaseException) -> bool:
        raise RuntimeError("bad rule")

    reg.register(broken, "never", "h")
    assert reg.classify(ValueError()).title_ru == "value"
    assert reg.classify(KeyError()) == DEFAULT
