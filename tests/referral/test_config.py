"""Referral settings: everything that shapes a reward is owner-only (review: day farming by an admin)."""

from __future__ import annotations

from svbg.referral.config import SETTINGS
from svbg.tg.admin.settings import is_business

REWARD_KEYS = {
    "REFERRAL_MODE",
    "REFERRAL_INVITER_DAYS",
    "REFERRAL_INVITEE_DAYS",
    "REFERRAL_TRIGGER",
    "REFERRAL_INVITER_CAP_30D",
    "REFERRAL_INVITER_CAP_TOTAL",
    "REFERRAL_PERCENT",
}


def test_reward_keys_are_owner_only_and_hidden_from_business_admins() -> None:
    by_key = {d.key: d for d in SETTINGS}
    assert set(by_key) >= REWARD_KEYS
    for key in REWARD_KEYS:
        assert by_key[key].owner_only, key
        assert not is_business(by_key[key]), key


def test_only_the_switch_stays_a_business_setting() -> None:
    assert {d.key for d in SETTINGS if is_business(d)} == {"REFERRAL_ENABLED"}
