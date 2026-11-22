# examples

`make_synthetic_baseline.py` writes a small, fully invented baseline (12 fake historical
trades, fake pins, zero commit id) under `exports/` and `canonical/adjudications/`.
The committed output is what the tests read; `tests/test_synthetic_baseline.py` fails
if it drifts from the generator. None of it comes from real trades.
