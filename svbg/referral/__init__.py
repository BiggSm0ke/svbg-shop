"""Referral program (core, 05 §2.3): ``r_<code>`` binding, days rewards (deferred sides, caps, no
retro-binding), the basic percent mode, reports to «🤝 Партнёры», data of the «Пригласить» screen.

* :mod:`svbg.referral.rules` — pure decisions (:func:`~svbg.referral.rules.decide`);
* :mod:`svbg.referral.service` — :class:`~svbg.referral.service.ReferralService`;
* :mod:`svbg.referral.tables` — ``referral_codes``, ``referrals``, ``referral_rewards``;
* :mod:`svbg.referral.config` — the ``REFERRAL_*`` settings; :mod:`svbg.referral.texts` — texts;
* :mod:`svbg.referral.qr` — the QR code of the invitation link; :mod:`svbg.referral.wiring` — app glue.
"""

from __future__ import annotations
