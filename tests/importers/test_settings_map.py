"""Bedolaga settings → SvBG settings: ``.env`` parsing and the mapping of 06 §3 (pure, no database)."""

from __future__ import annotations

import pytest

from svbg.importers.bedolaga.settings_map import GIB, BedolagaSettingsSource, build_plan
from svbg.importers.envmap import (
    ValueParseError,
    effective_values,
    is_masked,
    parse_env,
    to_bool,
    to_int_list,
    to_str_list,
)

# A synthetic production-like Bedolaga .env (key names of 06 §3.2; values are made up).
OWNER_ENV = """\
# ===== Bedolaga =====
BOT_TOKEN=123456:AAbotTokenValue
ADMIN_IDS=111,222
SUPPORT_USERNAME=@myvpn_support
SUPPORT_MENU_ENABLED=true
SUPPORT_SYSTEM_MODE=contact
ADMIN_NOTIFICATIONS_ENABLED=true
ADMIN_NOTIFICATIONS_CHAT_ID=-1001234567890
ADMIN_NOTIFICATIONS_TOPIC_ID=5   # общий топик
ADMIN_NOTIFICATIONS_TICKET_TOPIC_ID=6
ADMIN_NOTIFICATIONS_NALOG_TOPIC_ID=7
ADMIN_REPORTS_ENABLED=true
ADMIN_REPORTS_CHAT_ID=-1001234567890
ADMIN_REPORTS_TOPIC_ID=8
ADMIN_REPORTS_SEND_TIME=10:00
TRAFFIC_FAST_CHECK_ENABLED=true
TRAFFIC_DAILY_THRESHOLD_GB=40
SUSPICIOUS_NOTIFICATIONS_TOPIC_ID=#9
CHANNEL_IS_REQUIRED_SUB=true
CHANNEL_REQUIRED_FOR_ALL=true
CHANNEL_DISABLE_TRIAL_ON_UNSUBSCRIBE=true
CHANNEL_SUB_ID=-100999
CHANNEL_LINK=https://t.me/old_channel
REMNAWAVE_API_URL=http://remnawave:3000
REMNAWAVE_API_KEY=eyJpanelSecretToken
REMNAWAVE_AUTH_TYPE=api_key
REMNAWAVE_USER_DESCRIPTION_TEMPLATE="Bot user: {full_name} {username}"
REMNAWAVE_USER_USERNAME_TEMPLATE="user_{telegram_id}"
REMNAWAVE_USER_DELETE_MODE=delete
REMNAWAVE_AUTO_SYNC_ENABLED=true
REMNAWAVE_AUTO_SYNC_TIMES=03:00
REMNAWAVE_WEBHOOK_ENABLED=true
REMNAWAVE_WEBHOOK_PATH=/remnawave-webhook
REMNAWAVE_WEBHOOK_SECRET=Whsec0abcdefghijklmnopqrstuvwxyz012345
REMNAWAVE_WEBHOOK_NOTIFY_NODE_CONNECTION_STATUS=true
WEBHOOK_NOTIFY_USER_ENABLED=true
WEBHOOK_NOTIFY_SUB_STATUS=true
WEBHOOK_NOTIFY_SUB_EXPIRED=true
WEBHOOK_NOTIFY_SUB_EXPIRING=true
WEBHOOK_NOTIFY_SUB_LIMITED=true
WEBHOOK_NOTIFY_TRAFFIC_RESET=true
WEBHOOK_NOTIFY_FIRST_CONNECTED=true
WEBHOOK_NOTIFY_NOT_CONNECTED=true
WEBHOOK_NOTIFY_BANDWIDTH_THRESHOLD=false
WEBHOOK_NOTIFY_DEVICES=false
WEBHOOK_NOTIFY_SUB_REVOKED=true
WEBHOOK_NOTIFY_SUB_DELETED=true
TRIAL_USER_TAG=TRIAL
PAID_SUBSCRIPTION_USER_TAG=PAID
SALES_MODE=classic
TARIFF_SWITCH_ENABLED=false
RESET_DEVICES_ON_RENEWAL=false
TRIAL_DURATION_DAYS=3
TRIAL_TRAFFIC_LIMIT_GB=0
TRIAL_DEVICE_LIMIT=5
DEFAULT_DEVICE_LIMIT=5
MAX_DEVICES_LIMIT=15
TRIAL_ADD_REMAINING_DAYS_TO_PAID=true
DEFAULT_TRAFFIC_RESET_STRATEGY=MONTH
RESET_TRAFFIC_ON_PAYMENT=false
FIXED_TRAFFIC_LIMIT_GB=0
TRAFFIC_SELECTION_MODE=fixed
TRAFFIC_TOPUP_ENABLED=false
AVAILABLE_SUBSCRIPTION_PERIODS=30,90,180,360
AVAILABLE_RENEWAL_PERIODS=30,90,180,360
PRICE_14_DAYS=9900
PRICE_30_DAYS=17900
PRICE_60_DAYS=34900
PRICE_90_DAYS=49900
PRICE_180_DAYS=89900
PRICE_360_DAYS=169900
PRICE_PER_DEVICE=1900
DEVICES_SELECTION_ENABLED=true
REFERRAL_PROGRAM_ENABLED=true
REFERRAL_COMMISSION_PERCENT=25
REFERRAL_WITHDRAWAL_ENABLED=false
REFERRAL_PARTNER_SECTION_VISIBLE=false
TELEGRAM_STARS_ENABLED=true
TELEGRAM_STARS_RATE_RUB=1
SUPPORT_TOPUP_ENABLED=true
CRYPTOBOT_ENABLED=true
CRYPTOBOT_API_TOKEN=12345:AAcryptoBotToken
CRYPTOBOT_WEBHOOK_SECRET=cbsecret
CRYPTOBOT_BASE_URL=https://pay.crypt.bot
CRYPTOBOT_TESTNET=false
CRYPTOBOT_WEBHOOK_PATH=/cryptobot-webhook
CRYPTOBOT_DEFAULT_ASSET=USDT
CRYPTOBOT_ASSETS=USDT,TON,ETH,TRX
CRYPTOBOT_INVOICE_EXPIRES_HOURS=24
YOOKASSA_ENABLED=false
YOOKASSA_SHOP_ID=1
YOOKASSA_SECRET_KEY=1
YOOKASSA_QUICK_AMOUNT_SELECTION_ENABLED=true
PLATEGA_ENABLED=false
PLATEGA_MERCHANT_ID=abc
ROLLYPAY_ENABLED=true
ROLLYPAY_API_KEY=rp_live_api_key
ROLLYPAY_SIGNING_SECRET=rp_signing_secret
ROLLYPAY_DISPLAY_NAME=СБП
ROLLYPAY_CURRENCY=RUB
ROLLYPAY_MIN_AMOUNT_KOPEKS=17900
ROLLYPAY_MAX_AMOUNT_KOPEKS=10000000
ROLLYPAY_RETURN_URL=https://t.me/bot
ROLLYPAY_PAYMENT_METHOD=sbp
CONNECT_BUTTON_MODE=miniapp_subscription
HIDE_SUBSCRIPTION_LINK=false
ENABLE_LOGO_MODE=true
LOGO_FILE=vpn_logo.png
MAIN_MENU_MODE=rich
MAIN_MENU_RICH_LOGO_URL=https://cabinet.example/logo.png
SKIP_RULES_ACCEPT=true
SKIP_REFERRAL_CODE=false
MONITORING_INTERVAL=60
ENABLE_NOTIFICATIONS=true
NOTIFICATION_RETRY_ATTEMPTS=3
MONITORING_LOGS_RETENTION_DAYS=30
INACTIVE_USER_DELETE_MONTHS=3
TRIAL_WARNING_HOURS=2
MAINTENANCE_AUTO_ENABLE=true
MAINTENANCE_CHECK_INTERVAL=30
MAINTENANCE_MESSAGE="Технические работы, скоро вернёмся"
DEFAULT_LANGUAGE=ru
AVAILABLE_LANGUAGES=ru,en
LANGUAGE_SELECTION_ENABLED=false
PRICE_ROUNDING_ENABLED=true
TZ=Europe/Moscow
BACKUP_AUTO_ENABLED=true
BACKUP_INTERVAL_HOURS=12
BACKUP_TIME=03:00
BACKUP_MAX_KEEP=7
BACKUP_SEND_ENABLED=true
BACKUP_SEND_CHAT_ID=-1001234567890
BACKUP_SEND_TOPIC_ID=11
BACKUP_ARCHIVE_PASSWORD='p@ss # not a comment'
BOT_RUN_MODE=webhook
WEBHOOK_URL=https://bot.example.com/
WEBHOOK_PATH=/webhook
WEBHOOK_SECRET_TOKEN=tgsecret
WEBHOOK_DROP_PENDING_UPDATES=true
AUTO_PURCHASE_AFTER_TOPUP_ENABLED=true
REFERRAL_REWARD_MODE=days
REFERRAL_DAYS_INVITER_DAYS=14
REFERRAL_DAYS_INVITEE_DAYS=7
REFERRAL_DAYS_TRIGGER=any
REFERRAL_DAYS_MIN_USER_ID=1500
REFERRAL_DAYS_SKIP_ALREADY_PAID=true
REFERRAL_DAYS_INVITER_MAX_PER_MONTH=20
REFERRAL_DAYS_BLOCK_RETRO_ATTACH=true
REFERRAL_DAYS_WAKE_DELAY_SECONDS=30
REFERRAL_DAYS_CHECK_INTERVAL_MINUTES=10
REFERRAL_DAYS_RETRY_SKIPPED_HOURS=168
IP_GUARD_ENABLED=true
IP_GUARD_EXCLUDED_NODE_UUIDS=node-cdn-1,node-cdn-2
CABINET_ENABLED=false
CABINET_URL=https://cabinet.example
SMTP_HOST=smtp.example
MODEM_ENABLED=false
VERSION_CHECK_INTERVAL_H4OURS=1
DATABASE_URL=postgresql://bedolaga
POSTGRES_PASSWORD=pg
REDIS_URL=redis://redis
LOG_LEVEL=INFO
DEBUG=false
PAYMENT_BALANCE_TEMPLATE=Пополнение {amount}
SOMETHING_NEW_UNKNOWN=1
"""

# system_settings rows (key = env name). The env wins; MAINTENANCE_MODE=true is the T−20 state (06 §4.4).
OWNER_SYSTEM = {
    "PRICE_30_DAYS": "19900",  # loses to .env
    "MAINTENANCE_MODE": "true",
    "IP_GUARD_WARN_IPS": "20",
    "IP_GUARD_BLOCK_IPS": "25",
    "IP_GUARD_WINDOW_MINUTES": "10",
    "IP_GUARD_BLOCK_MIN_SUBNETS": "10",
    "IP_GUARD_BLOCK_CONFIRM_LIVE_IPS": "10",
    "IP_GUARD_BLOCK_CONFIRM_CHECKS": "2",
    "IP_GUARD_SUSTAINED_BLOCK_CHECKS": "20",
    "IP_GUARD_IGNORE_CIDRS": "10.0.0.0/8, 192.168.0.0/16",
    "IP_GUARD_NOTIFY_USER": "true",
    "IP_GUARD_WHITELIST_PANEL_USER_IDS": "5,6",
    "IP_GUARD_CHECK_INTERVAL_SECONDS": "120",
    "IP_GUARD_USER_BLOCKED": "Ваша подписка заблокирована",
    "WL_QUOTA_ENABLED": "true",
    "WL_QUOTA_ENFORCE_SCOPE": "list",
    "WL_QUOTA_ENFORCE_LIST": "101,102",
    "WL_QUOTA_WARN_PERCENT": "80",
    "WL_QUOTA_USER_QUIET_HOURS": "00:00-09:00",
    "WL_QUOTA_TOPUP_ENABLED": "true",
    "WL_QUOTA_EMERGENCY_HOLD": "false",
    "WL_QUOTA_RELEASE_ON_DISABLE": "true",
    "WL_QUOTA_TOPUP_PACKAGES": '[{"gb": 10, "price": 9900}]',
    "WL_QUOTA_CYCLE_OFFSET_SECONDS": "5",
    "WL_QUOTA_GB_BYTES": "1000000000",
}

OWNER_WLQ = {
    "warn_percent": {"value": 85},
    "user_quiet_hours": {"value": "23:00-08:00"},
    "notify_user_off": {"value": False},
    "topup_kill_switch": {"value": True},
    "global_checks": {"jwt_lifetime": True},
}

OWNER_CHANNELS = [
    {
        "id": 1,
        "channel_id": "-1001111111111",
        "channel_link": "https://t.me/myvpn_news",
        "title": "MyVPN",
        "is_active": True,
        "sort_order": 0,
        "disable_trial_on_leave": True,
        "disable_paid_on_leave": False,
    },
    {
        "id": 2,
        "channel_id": "-1002222222222",
        "channel_link": None,
        "title": "old",
        "is_active": False,
        "sort_order": 1,
        "disable_trial_on_leave": True,
        "disable_paid_on_leave": True,
    },
]


def owner_source() -> BedolagaSettingsSource:
    return BedolagaSettingsSource.from_env_text(
        OWNER_ENV,
        system_settings=OWNER_SYSTEM,
        wlq_settings=OWNER_WLQ,
        required_channels=OWNER_CHANNELS,
    )


# ------------------------------------------------------------------------------------------ envmap


def test_parse_env_dotenv_rules() -> None:
    env = parse_env(
        "﻿# header\n"
        "A=1   # inline comment\n"
        "B=#starts with hash\n"
        'C="quoted # kept"\n'
        "D='single'  # cut\n"
        "export E=exported\n"
        'F="line1\\nline2 \\"q\\""\n'
        "G=\n"
        "not a line\n"
        "H=x#nospace\n"
        "A=2\n"
        'M="multi\nline"\n'
    )
    assert env.values["A"] == "2"  # last wins
    assert env.duplicates == ("A",)
    assert env.values["B"] == ""
    assert env.values["C"] == "quoted # kept"
    assert env.values["D"] == "single"
    assert env.values["E"] == "exported"
    assert env.values["F"] == 'line1\nline2 "q"'
    assert env.values["G"] == ""
    assert env.values["H"] == "x#nospace"
    assert env.values["M"] == "multi\nline"
    assert env.bad_lines == (9,)
    assert env.lines["A"] == 11
    assert env.lines["E"] == 6


def test_effective_values_env_wins_with_origin() -> None:
    merged = effective_values(("env", {"X": "1"}, {"X": 42}), ("system_settings", {"X": "2", "Y": "3"}, None))
    assert merged["X"].raw == "1"
    assert merged["X"].origin == "env:42"
    assert merged["Y"].origin == "system_settings"


@pytest.mark.parametrize(
    ("raw", "masked"),
    [("***", True), ("ab***", True), ("<masked>", True), ("••••", True), ("[REDACTED]", True),
     ("xxxxxxxx", True), ("rp_live_key", False), ("", False), (None, False), ("a*b", False)],
)  # fmt: skip
def test_is_masked(raw: str | None, masked: bool) -> None:
    assert is_masked(raw) is masked


def test_converters() -> None:
    assert to_bool("Yes") is True
    assert to_bool("") is False
    with pytest.raises(ValueParseError):
        to_bool("maybe")
    assert to_int_list("30, 90;180 360") == [30, 90, 180, 360]
    assert to_str_list('["USDT", "TON"]') == ["USDT", "TON"]
    with pytest.raises(ValueParseError):
        to_int_list("30,abc")


# ------------------------------------------------------------------------------------------ mapping


@pytest.fixture(scope="module")
def plan():  # type: ignore[no-untyped-def]
    return build_plan(owner_source())


def _changes(plan) -> dict[str, str]:  # type: ignore[no-untyped-def]
    return {c.key: c.raw for c in plan.changes}


def test_every_source_key_is_accounted(plan) -> None:  # type: ignore[no-untyped-def]
    assert plan.unaccounted() == set()
    assert len(plan.source_keys) == len(parse_env(OWNER_ENV).values) + len(
        set(OWNER_SYSTEM) - set(parse_env(OWNER_ENV).values)
    ) + len(OWNER_WLQ)


def test_core_settings(plan) -> None:  # type: ignore[no-untyped-def]
    ch = _changes(plan)
    assert ch["OWNER_IDS"] == "111,222"
    assert ch["SUPPORT_URL"] == "https://t.me/myvpn_support"
    assert ch["ADMIN_CHAT_ID"] == "-1001234567890"
    assert ch["REPORT_DAILY_ENABLED"] == "true"
    assert ch["REPORT_DAILY_AT"] == "10:00"
    assert ch["TIMEZONE"] == "Europe/Moscow"
    assert ch["REMNAWAVE_URL"] == "http://remnawave:3000"
    assert ch["REMNAWAVE_WEBHOOK_SECRET"] == "Whsec0abcdefghijklmnopqrstuvwxyz012345"
    assert plan.change("REMNAWAVE_WEBHOOK_SECRET").secret
    assert ch["PANEL_USERNAME_PREFIX"] == "user_"
    assert ch["PANEL_DESCRIPTION_TEMPLATE"] == "Bot user: {full_name} @{tg_username}"
    assert ch["NOTIFY_ADMIN_NODES"] == "true"
    assert ch["TRIAL_DAYS"] == "3"
    assert ch["TRIAL_CARRY_OVER"] == "true"
    assert ch["NOTIFY_TRIAL_ENDING_HOURS"] == "2"
    assert ch["MAINTENANCE_MODE"] == "auto"
    assert ch["MAINTENANCE_MESSAGE"] == "Технические работы, скоро вернёмся"
    assert ch["DEFAULT_LANGUAGE"] == "ru"
    assert ch["I18N_AVAILABLE"] == "ru,en"
    assert ch["I18N_ASK_ON_START"] == "false"
    assert ch["ONBOARDING_RULES"] == "off"
    assert ch["ONBOARDING_ASK_REFERRAL_CODE"] == "true"
    assert ch["PRICING_ROUNDING"] == "true"
    assert ch["BACKUP_ENABLED"] == "true"
    assert ch["BACKUP_AT"] == "03:00"
    assert ch["BACKUP_KEEP"] == "7"
    assert ch["BACKUP_TO_TELEGRAM"] == "true"
    assert ch["BACKUP_PASSWORD"] == "p@ss # not a comment"
    assert plan.change("BACKUP_PASSWORD").secret


def test_channel_from_required_channels(plan) -> None:  # type: ignore[no-untyped-def]
    ch = _changes(plan)
    assert ch["REQUIRED_CHANNEL_ID"] == "-1001111111111"
    assert ch["REQUIRED_CHANNEL_URL"] == "https://t.me/myvpn_news"
    assert ch["CHANNEL_REQUIRED_FOR"] == "all"
    assert ch["CHANNEL_LEAVE_ACTION"] == "trial"
    assert "TRIAL_AUDIENCE" not in ch  # redundant with CHANNEL_REQUIRED_FOR=all (06 §3.2)
    assert plan.skipped("CHANNEL_SUB_ID").category == "dead"
    assert plan.skipped("CHANNEL_LINK").category == "dead"


def test_notify_switches(plan) -> None:  # type: ignore[no-untyped-def]
    ch = _changes(plan)
    assert ch["NOTIFY_USER_EXPIRED"] == "true"
    assert ch["NOTIFY_USER_EXPIRING"] == "true"
    assert ch["NOTIFY_USER_FIRST_CONNECTED"] == "true"
    assert ch["NOTIFY_USER_DEVICES"] == "false"
    assert ch["NOTIFY_USER_REVOKED"] == "true"
    # LIMITED/TRAFFIC_RESET on, BANDWIDTH off: one of the merged switches is on → on.
    assert ch["NOTIFY_USER_TRAFFIC"] == "true"
    assert plan.skipped("WEBHOOK_NOTIFY_SUB_DELETED").category == "unknown"


def test_master_notify_switch_off() -> None:
    plan = build_plan(
        BedolagaSettingsSource(
            env={"WEBHOOK_NOTIFY_USER_ENABLED": "false", "WEBHOOK_NOTIFY_SUB_EXPIRED": "true"}
        )
    )
    assert _changes(plan)["NOTIFY_USER_EXPIRED"] == "false"


def test_secrets_and_tokens_never_copied(plan) -> None:  # type: ignore[no-untyped-def]
    ch = _changes(plan)
    deferred = {c.key for c in plan.deferred}
    assert "BOT_TOKEN" not in ch and "BOT_TOKEN" not in deferred
    assert plan.skipped("BOT_TOKEN").category == "secret"
    assert "REMNAWAVE_TOKEN" not in ch
    assert plan.skipped("REMNAWAVE_API_KEY").category == "secret"
    assert "WEBHOOK_SECRET" not in ch
    assert plan.skipped("WEBHOOK_SECRET_TOKEN") is not None
    assert plan.skipped("WEBHOOK_DROP_PENDING_UPDATES") is not None
    report = repr(plan.report())
    for secret in ("AAbotTokenValue", "eyJpanelSecretToken", "rp_live_api_key", "rp_signing_secret",
                   "AAcryptoBotToken", "Whsec0abc", "p@ss", "tgsecret"):  # fmt: skip
        assert secret not in report


def test_masked_secret_is_not_imported() -> None:
    plan = build_plan(
        BedolagaSettingsSource(
            system_settings={
                "ROLLYPAY_ENABLED": "true",
                "ROLLYPAY_API_KEY": "***",
                "CRYPTOBOT_API_TOKEN": "<masked>",
            }
        )
    )
    assert plan.change("PAY_ROLLYPAY_API_KEY") is None
    assert plan.skipped("ROLLYPAY_API_KEY").category == "secret"
    assert plan.skipped("CRYPTOBOT_API_TOKEN").category == "secret"


def test_payments_secrets_and_deferred_enable(plan) -> None:  # type: ignore[no-untyped-def]
    ch = _changes(plan)
    assert ch["PAY_ROLLYPAY_API_KEY"] == "rp_live_api_key"
    assert ch["PAY_ROLLYPAY_SIGNING_SECRET"] == "rp_signing_secret"
    assert ch["PAY_ROLLYPAY_PAYMENT_METHOD"] == "sbp"
    assert ch["PAY_CRYPTOBOT_API_TOKEN"] == "12345:AAcryptoBotToken"
    assert ch["PAY_CRYPTOBOT_ACCEPTED_ASSETS"] == "USDT,TON,ETH,TRX"
    assert ch["PAY_CRYPTOBOT_INVOICE_HOURS"] == "24"
    assert ch["PAY_CRYPTOBOT_TEST_MODE"] == "false"
    assert ch["PAY_STARS_RATE"] == "1"
    # Cash desks are switched on only at step 8 of the T0 runbook, never by the import itself.
    for key in ("PAY_ROLLYPAY_ENABLED", "PAY_CRYPTOBOT_ENABLED", "PAY_STARS_ENABLED"):
        assert key not in ch
        assert plan.deferred_change(key).raw == "true"
        assert "шаге 8" in plan.deferred_change(key).note
    assert plan.skipped("ROLLYPAY_BASE_URL") is None  # not in this .env
    # 179 ₽ = the RollyPay plugin's own minimum: nothing to carry, nothing to warn about.
    assert plan.skipped("ROLLYPAY_MIN_AMOUNT_KOPEKS") is None
    assert "ROLLYPAY_MIN_AMOUNT_KOPEKS" in plan.consumed
    assert "WALLET_TOPUP_MAX" in plan.skipped("ROLLYPAY_MAX_AMOUNT_KOPEKS").reason
    assert not any("RollyPay" in w for w in plan.warnings)
    assert plan.skipped("YOOKASSA_SHOP_ID").category == "dead"
    assert plan.skipped("PLATEGA_MERCHANT_ID").category == "dead"
    assert plan.skipped("AUTO_PURCHASE_AFTER_TOPUP_ENABLED") is not None


def test_enabled_foreign_cash_desk_is_a_warning() -> None:
    plan = build_plan(BedolagaSettingsSource(env={"PLATEGA_ENABLED": "true", "PLATEGA_SECRET": "s"}))
    assert plan.skipped("PLATEGA_SECRET").category == "conflict"
    assert any("PLATEGA" in w for w in plan.warnings)


def test_rollypay_minimum_different_from_plugin_is_a_warning() -> None:
    plan = build_plan(
        BedolagaSettingsSource(env={"ROLLYPAY_ENABLED": "true", "ROLLYPAY_MIN_AMOUNT_KOPEKS": "10000"})
    )
    skipped = plan.skipped("ROLLYPAY_MIN_AMOUNT_KOPEKS")
    assert skipped.category == "conflict" and "10000" in skipped.reason and "17900" in skipped.reason
    assert any("ROLLYPAY_MIN_AMOUNT_KOPEKS" in w for w in plan.warnings)
    assert plan.unaccounted() == set()


def test_disabled_known_cash_desk() -> None:
    plan = build_plan(BedolagaSettingsSource(env={"ROLLYPAY_ENABLED": "false", "ROLLYPAY_API_KEY": "k"}))
    assert _changes(plan) == {"PAY_ROLLYPAY_ENABLED": "false"}
    assert plan.skipped("ROLLYPAY_API_KEY").category == "dead"
    assert plan.deferred == []


def test_maintenance_and_telegram(plan) -> None:  # type: ignore[no-untyped-def]
    assert plan.skipped("MAINTENANCE_MODE").origin == "system_settings"  # T−20 state not carried
    assert plan.deferred_change("BOT_MODE").raw == "webhook"
    assert plan.deferred_change("PUBLIC_URL").raw == "https://bot.example.com"
    assert "BOT_MODE" not in _changes(plan)


def test_catalog(plan) -> None:  # type: ignore[no-untyped-def]
    cat = plan.catalog
    assert cat.prices == {30: 17900, 90: 49900, 180: 89900, 360: 169900}  # env wins over system_settings
    assert plan.skipped("PRICE_14_DAYS") is not None
    assert plan.skipped("PRICE_60_DAYS") is not None
    assert cat.device_limit == 5
    assert cat.device_addon is not None
    assert cat.device_addon.to_json() == {
        "price_minor": 1900,
        "per_days": 30,
        "currency": "RUB",
        "max_devices": 15,
    }
    assert cat.traffic_bytes == 0
    assert cat.reset_strategy == "MONTH"
    assert cat.traffic_on_renew == "keep"
    assert cat.devices_on_renew == "keep"
    assert cat.panel_tag == "PAID"
    assert (cat.trial_device_limit, cat.trial_traffic_bytes, cat.trial_panel_tag) == (5, 0, "TRIAL")
    # prices are data, never settings
    assert not any(c.key.startswith("PRICE") for c in plan.changes)


def test_traffic_in_gib() -> None:
    plan = build_plan(
        BedolagaSettingsSource(env={"FIXED_TRAFFIC_LIMIT_GB": "100", "TRIAL_TRAFFIC_LIMIT_GB": "5"})
    )
    assert plan.catalog.traffic_bytes == 100 * GIB
    assert plan.catalog.trial_traffic_bytes == 5 * GIB


def test_topics_and_cdn(plan) -> None:  # type: ignore[no-untyped-def]
    topics = {t.kind: (t.chat_id, t.thread_id) for t in plan.topics}
    assert topics == {
        "payments": (-1001234567890, 5),
        "reports": (-1001234567890, 8),
        "backups": (-1001234567890, 11),
    }
    assert plan.skipped("SUSPICIOUS_NOTIFICATIONS_TOPIC_ID") is not None  # value "#9" is empty
    assert plan.skipped("ADMIN_NOTIFICATIONS_TICKET_TOPIC_ID") is not None
    assert plan.cdn_nodes == ["node-cdn-1", "node-cdn-2"]


def test_referral_days(plan) -> None:  # type: ignore[no-untyped-def]
    ch = _changes(plan)
    assert ch["REFERRAL_ENABLED"] == "true"
    assert ch["REFERRAL_MODE"] == "days"
    assert ch["REFERRAL_INVITER_DAYS"] == "14"
    assert ch["REFERRAL_INVITEE_DAYS"] == "7"
    assert ch["REFERRAL_TRIGGER"] == "trial_or_paid"  # any ↔ trial_or_paid
    assert ch["REFERRAL_INVITER_CAP_30D"] == "20"
    for key in (
        "REFERRAL_DAYS_MIN_USER_ID",
        "REFERRAL_DAYS_BLOCK_RETRO_ATTACH",
        "REFERRAL_COMMISSION_PERCENT",
        "REFERRAL_WITHDRAWAL_ENABLED",
        "REFERRAL_DAYS_RETRY_SKIPPED_HOURS",
    ):
        assert plan.skipped(key) is not None, key


def test_referral_money_mode_is_not_switched_on() -> None:
    plan = build_plan(
        BedolagaSettingsSource(env={"REFERRAL_PROGRAM_ENABLED": "true", "REFERRAL_REWARD_MODE": "balance"})
    )
    assert "REFERRAL_ENABLED" not in _changes(plan)
    assert plan.skipped("REFERRAL_REWARD_MODE").category == "conflict"


def test_ip_guard(plan) -> None:  # type: ignore[no-untyped-def]
    ch = _changes(plan)
    assert ch["IP_GUARD_WARN_IPS"] == "20"
    assert ch["IP_GUARD_BLOCK_IPS"] == "25"
    assert ch["IP_GUARD_MIN_SUBNETS"] == "10"
    assert ch["IP_GUARD_CONFIRM_LIVE_IPS"] == "10"
    assert ch["IP_GUARD_CONFIRM_CHECKS"] == "2"
    assert ch["IP_GUARD_SUSTAINED_CHECKS"] == "20"
    assert ch["IP_GUARD_IGNORE_CIDRS"] == "10.0.0.0/8,192.168.0.0/16"
    assert "IP_GUARD_AUTO_BLOCK" not in ch  # stays off until calibration
    assert plan.deferred_change("IP_GUARD_ENABLED").raw == "true"
    assert "IP_GUARD_ENABLED" not in ch
    assert plan.skipped("IP_GUARD_WHITELIST_PANEL_USER_IDS").category == "data"
    assert plan.skipped("IP_GUARD_CHECK_INTERVAL_SECONDS") is not None
    assert plan.skipped("IP_GUARD_USER_BLOCKED") is not None


def test_lte(plan) -> None:  # type: ignore[no-untyped-def]
    ch = _changes(plan)
    assert ch["LTE_ENFORCE"] == "shadow"  # always starts in shadow; imported blocks stay
    assert plan.deferred_change("LTE_ENFORCE").raw == "on"
    assert ch["LTE_ENFORCE_LIST"] == "101,102"
    assert plan.deferred_change("LTE_ENABLED").raw == "true"
    assert ch["LTE_WARN_PERCENT"] == "85"  # wlq_settings beats system_settings
    assert ch["LTE_QUIET_HOURS"] == "23:00-08:00"
    assert ch["LTE_TOPUP_ENABLED"] == "false"  # topup_kill_switch
    assert ch["LTE_GB_BYTES"] == "1000000000"
    assert "LTE_NOTIFY_USER" not in ch  # notify_user_off=false and no WL_QUOTA_NOTIFY_USER
    assert plan.skipped("WL_QUOTA_EMERGENCY_HOLD") is not None
    assert plan.skipped("WL_QUOTA_RELEASE_ON_DISABLE") is not None
    assert plan.skipped("WL_QUOTA_TOPUP_PACKAGES").category == "data"
    assert plan.skipped("wlq_settings.global_checks") is not None
    # «При выключении» is pinned to keep: no switch ever releases the imported blocks (06 §0 п.6).
    assert ch["LTE_OFF_ACTION"] == "keep"
    assert not any(c.raw == "off" for c in [*plan.changes, *plan.deferred] if c.key == "LTE_ENFORCE")


@pytest.mark.parametrize(
    ("scope", "now", "later", "lst"),
    [("off", "shadow", None, None), ("all", "shadow", "on", ""), ("list", "shadow", "on", "101,102")],
)
def test_lte_scope_mapping(scope: str, now: str, later: str | None, lst: str | None) -> None:
    plan = build_plan(
        BedolagaSettingsSource(env={"WL_QUOTA_ENFORCE_SCOPE": scope, "WL_QUOTA_ENFORCE_LIST": "101,102"})
    )
    ch = _changes(plan)
    assert ch["LTE_ENFORCE"] == now
    deferred = plan.deferred_change("LTE_ENFORCE")
    assert (deferred.raw if deferred else None) == later
    assert ch.get("LTE_ENFORCE_LIST") == (lst if lst is not None else "")


def test_lte_keys_without_analog_are_listed() -> None:
    plan = build_plan(
        BedolagaSettingsSource(
            env={
                "WL_QUOTA_ADMIN_SUMMARY_TIME": "09:00",
                "WL_QUOTA_PANEL_DESCRIPTION_ENABLED": "true",
                "WL_QUOTA_NEW_NODE_TWIN_DELAY_MINUTES": "10",
            },
            wlq_settings={"admin_summary_time": {"value": "08:30"}, "new_node_twin_delay_minutes": 5},
        )
    )
    assert plan.changes == [] and plan.deferred == []
    for key in (
        "WL_QUOTA_ADMIN_SUMMARY_TIME",
        "WL_QUOTA_PANEL_DESCRIPTION_ENABLED",
        "WL_QUOTA_NEW_NODE_TWIN_DELAY_MINUTES",
        "wlq_settings.admin_summary_time",
        "wlq_settings.new_node_twin_delay_minutes",
    ):
        assert "в SvBG нет" in plan.skipped(key).reason, key
    assert plan.unaccounted() == set()


def test_lte_kill_switch_warns() -> None:
    plan = build_plan(BedolagaSettingsSource(wlq_settings={"kill_switch": {"on": True, "by": "admin"}}))
    assert plan.warnings and "аварийный" in plan.warnings[0]


def test_dead_subsystem_unknown_lists(plan) -> None:  # type: ignore[no-untyped-def]
    assert plan.skipped("MODEM_ENABLED").category == "dead"
    assert plan.skipped("VERSION_CHECK_INTERVAL_H4OURS").category == "dead"
    assert plan.skipped("CABINET_URL").category == "dead"
    assert plan.skipped("DATABASE_URL").category == "dead"
    assert plan.skipped("PAYMENT_BALANCE_TEMPLATE").category == "dead"
    assert plan.skipped("SOMETHING_NEW_UNKNOWN").category == "unknown"
    text = plan.render_not_transferred()
    assert text.startswith("Не перенесено:")
    assert "SOMETHING_NEW_UNKNOWN" in text


def test_invalid_values_are_listed_not_raised() -> None:
    plan = build_plan(
        BedolagaSettingsSource(
            env={
                "TRIAL_DURATION_DAYS": "three",
                "ADMIN_REPORTS_SEND_TIME": "25:99",
                "TZ": "Mars/Base",
                "PAID_SUBSCRIPTION_USER_TAG": "paid tag",
                "SUPPORT_USERNAME": "@x y",
            }
        )
    )
    for key in ("TRIAL_DURATION_DAYS", "ADMIN_REPORTS_SEND_TIME", "TZ", "PAID_SUBSCRIPTION_USER_TAG",
                "SUPPORT_USERNAME"):  # fmt: skip
        assert plan.skipped(key).category == "invalid", key
    assert plan.changes == []
    assert plan.unaccounted() == set()


def test_report_is_json_and_deterministic(plan) -> None:  # type: ignore[no-untyped-def]
    import json

    a = json.dumps(build_plan(owner_source()).report(), ensure_ascii=False, sort_keys=True)
    b = json.dumps(plan.report(), ensure_ascii=False, sort_keys=True)
    assert a == b
    assert plan.report()["counts"]["changes"] == len(plan.changes)
