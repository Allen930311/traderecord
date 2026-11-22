# Contributing

Thanks for helping. Issues and pull requests are welcome.

## Before you open a PR

1. Open an issue first for anything larger than a small fix, so we agree on direction.
2. Run the tests: `pip install pyyaml pytest && python -m pytest`.
3. Keep a change focused: one topic per PR.
4. Add or update a test for behavior you change.

## Ground rules

- Never commit real trade data, account identifiers, API tokens or screenshots of a real account. Use synthetic data only.
- The record is append-only by design. A change that lets a record be silently rewritten will not be accepted.
- Workflows (`workflows/*.yaml`) are contracts. If you change one, update the tests that pin it.

## Reporting bugs

Include the command you ran, the output, your Python version and OS. Remove any personal data first.

By contributing you agree that your contribution is licensed under Apache-2.0.
