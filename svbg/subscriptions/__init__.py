"""Subscriptions: one bot subscription = one panel user (02 §3).

Modules (imported explicitly — they depend on the panel writer, which depends on these tables):

* :mod:`~svbg.subscriptions.tables` — ``subscriptions``, ``subscription_events``, ``trial_grants``,
  ``channel_members``, squad substitutions;
* :mod:`~svbg.subscriptions.service` — stage 1 primitives (create / change / disable / close);
* :mod:`~svbg.subscriptions.lifecycle` — purchase / renew / plan change / device addon / extend (billing's
  ``fulfill`` and admin grants call it in their transaction);
* :mod:`~svbg.subscriptions.hold` — freeze / unfreeze and ``can_spend`` (X5);
* :mod:`~svbg.subscriptions.trial` — trial activation; :mod:`~svbg.subscriptions.channel` — required channel;
* :mod:`~svbg.subscriptions.devices` — link reissue and HWID device actions with cooldowns;
* :mod:`~svbg.subscriptions.terms` — plan terms and the catalog port; :mod:`~svbg.subscriptions.journal` —
  ``subscription_events`` (X2); :mod:`~svbg.subscriptions.hooks` — durable domain events (X3).
"""
