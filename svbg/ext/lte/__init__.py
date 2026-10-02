"""LTE quotas (05 §2.1): owner module ``ext/lte``.

Pure engines (no I/O): :mod:`.model` (vocabulary), :mod:`.accounting` (Δ1–Δ8, intervals, buckets),
:mod:`.periods` (E0–E10), :mod:`.decide` (limits, per-subscription decisions, squad projection),
:mod:`.planner` (cycle plan with safety fuses), :mod:`.tables` (the 14 ``lte_*`` tables).
"""
