"""Extra lines under a settings section (the webhook address of a payment instance)."""

from __future__ import annotations

from types import SimpleNamespace

from svbg.app import App
from svbg.tg.admin.settings import SCREEN_SECTION
from tests.tg.admin.settings_harness import OWNER, EnvFactory, add_staff


def _notes(sid: str) -> list[str]:
    if sid == "logs":
        raise RuntimeError("boom")
    return (
        ["Адрес для вебхука:", "<code>https://bot.example.com/webhooks/pay/1/t</code>"]
        if sid == "sales"
        else []
    )


async def test_section_shows_notes_and_survives_a_failing_hook(make_senv: EnvFactory) -> None:
    env = await make_senv(screens_kw={"notes": _notes})
    await add_staff(env)
    await env.click(OWNER, f"v1:{SCREEN_SECTION}:o:sales")
    assert "<code>https://bot.example.com/webhooks/pay/1/t</code>" in env.text
    await env.click(OWNER, f"v1:{SCREEN_SECTION}:o:logs")
    assert "boom" not in env.text
    assert env.buttons  # the section still renders


class _Inst:
    def __init__(self, webhook: bool) -> None:
        self.caps = SimpleNamespace(webhook=webhook)


class _Instances:
    def __init__(self, inst: _Inst | None, url: str | None) -> None:
        self.inst, self.url = inst, url

    def by_slug(self, slug: str) -> _Inst | None:
        return self.inst if slug == "rollypay" else None

    def webhook_url(self, inst: _Inst) -> str | None:
        return self.url


def _app_notes(instances: _Instances | None, sid: str) -> list[str]:
    return App._settings_notes(SimpleNamespace(pay_instances=instances), sid)  # type: ignore[arg-type]


def test_app_notes_for_payment_sections() -> None:
    url = "https://bot.example.com/webhooks/pay/3/abc&x"
    lines = _app_notes(_Instances(_Inst(True), url), "payments.rollypay")
    assert lines[-1] == "<code>https://bot.example.com/webhooks/pay/3/abc&amp;x</code>"
    assert "включится" in _app_notes(_Instances(None, None), "payments.rollypay")[0]
    assert "PUBLIC_URL" in _app_notes(_Instances(_Inst(True), None), "payments.rollypay")[0]
    assert "сам проверяет" in _app_notes(_Instances(_Inst(False), None), "payments.rollypay")[0]
    assert _app_notes(_Instances(_Inst(True), url), "sales") == []
    assert _app_notes(None, "payments.rollypay") == []
