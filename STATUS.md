# traderecord status

Last checked: 2026-11-22

- State: v0.1.0 published (public, Apache-2.0). Engine, workflows, synthetic strategy registry, CI green on Ubuntu and Windows (Python 3.10 and 3.13).
- Known gaps: no synthetic demo corpus yet; about two thirds of the original test files are not included because they depend on real record data; not installable with `pip install` (runs from a checkout).
- Next step: build a synthetic demo corpus, then restore the data-dependent tests on top of it.
- Source of the engine: a private repository. This repository is the public edition and is maintained separately; engine changes are exported one way from the private side.
