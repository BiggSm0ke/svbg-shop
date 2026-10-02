"""Pure business rules (no SQL, no aiogram, no I/O): pricing and wallet rules.

Everything here is deterministic and unit-testable without a database; services call it with data they
already loaded.
"""
