# traderecord status

Last checked: 2026-11-22

- State: v0.1.0 published (public, Apache-2.0). Since then (unreleased on main): synthetic baseline in `examples/`, restored tests (916 pass, 54 skipped as private-only), README diagrams.
- Known gaps: 24 test files and 53 single tests need private records (listed in `private_only.json`); not installable with `pip install` (runs from a checkout).
- Next step: synthetic equivalents for the private-only tests; package for pip.
- Source of the engine: a private repository. Engine changes are exported one way with `scripts/public_export.py` (private side), which sanitizes, scans for leaks and reports drift. Do not edit exported files here; change them in the private repo.
