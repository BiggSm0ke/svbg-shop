"""Stage 3 end to end — admin rights on money and the backup → restore round trip.

* an admin without ``wallet.adjust`` / ``subs.grant`` sees no money buttons on a user card and a forged
  callback gets «Нет прав» (nothing written); the owner adjusts the balance with a reason — the ledger row
  and ``admin_audit`` land together;
* «💾 Бэкап сейчас» in the bot → an encrypted archive in ``data/backups`` (+ the «💾 Бэкапы» topic) →
  ``restore`` into an empty database of another host → the bot starts there, the secrets decrypt and the
  edited content (with its picture) is present.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from svbg.app import App
from svbg.ops.restore import EnvMode, restore
from svbg.tg.ui import codec
from tests.e2e.conftest import OWNER_ID, AppEnv, StartApp
from tests.e2e.test_stage2_kit import FAST, GROUP, open_shop, until, until_async
from tests.e2e.test_stage3_kit import chat, create_database, drop_database, jpeg_bytes, tg  # noqa: F401
from tests.pgcluster import PgCluster

pytestmark = pytest.mark.pg

PASSWORD = "backup-password-42"


async def test_admin_without_rights_cannot_move_money(start_app: StartApp, app_env: AppEnv) -> None:
    async with open_shop(start_app, app_env) as shop:
        client = shop.person(5_401)
        await client.start()
        uid = await shop.user_id(5_401)
        clerk = chat(shop, 5_402)
        await clerk.start()
        await shop.db.raw(
            "update users set role = 'admin', perms = '[\"stats\"]'::jsonb where telegram_id = 5402"
        )
        assert shop.app.users is not None
        shop.app.users.invalidate(5_402)  # the cached context of the clerk

        await clerk.say("/user 5401", expect="5401")
        labels = [str(b.get("text")) for b in clerk.buttons()]
        assert not any("Баланс" in x or "Дни" in x for x in labels), labels
        for action in ("wal", "days"):
            toast = await clerk.click(codec.encode("aua", action, str(uid)))
            assert toast == "Нет прав", (action, toast)
        assert await shop.rows("select id from wallet_ledger where user_id = $1", uid) == []
        assert await shop.balance(5_401) == 0

        owner = chat(shop, OWNER_ID)
        await owner.start()
        await owner.say("/user 5401", expect="💰 Баланс")
        await owner.tap("💰 Баланс", expect="Сумма в")
        await owner.say("150", expect="Причина")
        await owner.say("Компенсация за сбой")
        await until_async(lambda: shop.balance(5_401), what="the balance adjusted")
        assert await shop.balance(5_401) == 15_000
        audit = await shop.rows(
            "select action, amount_minor, reason, actor_id from admin_audit where target like $1", f"%{uid}%"
        )
        money = [a for a in audit if a["amount_minor"] == 15_000]
        assert money and money[0]["reason"] == "Компенсация за сбой"
        denied = await shop.rows("select count(*) as n from admin_audit where action like '%denied%'")
        assert denied[0]["n"] >= 2  # both forged attempts are on record
        await shop.assert_wallet_invariants()


async def test_backup_then_restore_on_another_host(
    start_app: StartApp, app_env: AppEnv, pg_cluster: PgCluster, tmp_path: Path
) -> None:
    async with open_shop(start_app, app_env, extra_env={"BACKUP_PASSWORD": PASSWORD}) as shop:
        owner = chat(shop, OWNER_ID)
        await owner.start()
        await owner.say("/edit", expect="✏️ Экран")
        await owner.tap("✏️ Экран", expect="✏️ Экран «")
        await owner.tap("🖼 Медиа", expect="Пришлите фото")
        await owner.send_photo(jpeg_bytes(color=(0, 160, 60)), expect="⚡ Применено")
        await owner.tap("📝 Текст RU", expect="Пришлите новый текст")
        await owner.say("Главная до бэкапа", expect="⚡ Применено")
        await shop.fund(OWNER_ID, 12_300)
        await owner.say("/edit", expect="Режим правки выключен")

        await owner.start()
        await owner.tap("Админка", expect="Чтобы найти человека")
        await owner.tap("Система", expect="Состояние бота")
        await owner.tap("Бэкапы", expect="Бэкап сейчас")
        await owner.tap("💾 Бэкап сейчас")
        backups = app_env.data_dir / "backups"
        await until(lambda: backups.is_dir() and any(backups.iterdir()), timeout=60, what="a backup file")
        await until(
            lambda: any(
                c.ok and c.method == "sendDocument" and c.params.get("chat_id") == GROUP
                for c in shop.tg.calls
            ),
            timeout=60,
            what="the backup in «💾 Бэкапы»",
        )
        archive = sorted(p for p in backups.iterdir() if p.is_file())[-1]
        await shop.app.stop()  # the old host is gone

    # another host: an empty database, a .env with the same SECRET_KEY (the owner keeps it), no data
    name = f"restored_{uuid.uuid4().hex[:10]}"
    dsn = await create_database(pg_cluster, name)
    try:
        host = tmp_path / "host2"
        host.mkdir()
        env_path = host / ".env"
        env_path.write_text(
            f"DATABASE_URL={dsn}\nSECRET_KEY={app_env.values['SECRET_KEY']}\n", encoding="utf-8"
        )
        report = await restore(
            [archive], dsn, data_dir=host, env_path=env_path, password=PASSWORD, env_mode=EnvMode.WRITE
        )
        assert report is not None
        restored_env = env_path.read_text(encoding="utf-8")
        assert f"DATABASE_URL={dsn}" in restored_env and "BOT_TOKEN=" in restored_env

        env2 = AppEnv(data_dir=host, env_path=env_path, dsn=dsn, token=app_env.token, tg=app_env.tg)
        app = App(env2.options(environ={"DATA_DIR": str(host)}, **FAST))
        await app.start()
        try:
            assert app.pay_instances is not None
            rolly = app.pay_instances.by_slug("rollypay")
            assert rolly is not None and rolly.enabled  # its encrypted keys decrypt here
            assert app.content is not None
            home = app.content.get_screen("home")
            assert home is not None and home.text("ru").text == "Главная до бэкапа"
            db: Any = app.db
            media = await db.raw("select path from media")
            assert media and all((host / "media" / m["path"]).is_file() for m in media)
            wallet = await db.raw("select wallet_minor from users where telegram_id = $1", OWNER_ID)
            assert wallet[0]["wallet_minor"] == 12_300
            begin = len(app_env.tg.calls)
            app_env.tg.push_message(5_499, "/start")
            await until(
                lambda: any(
                    c.ok and c.method == "sendPhoto" and c.params.get("chat_id") == 5_499
                    for c in app_env.tg.calls[begin:]
                ),
                timeout=20,
                what="the restored bot answers with the restored home screen",
            )
        finally:
            await app.stop()
    finally:
        await drop_database(pg_cluster, name)
