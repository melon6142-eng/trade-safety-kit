# Contributing

1. Fork and create a branch.
2. `pip install -e ".[dev]"` and run `pytest -q`. All tests must pass.
3. Keep the **fail-closed** rule: unknown input, missing config or a partial
   broker read must deny or report "unknown", never silently allow.
4. Never commit real credentials, account numbers, order IDs or holdings.
   Use obvious fakes (`FAKEA`, `fake-key`, `*.example.test`) in tests.
5. New broker adapters must be dry-run by default and implement
   `trade_safety_kit.brokers.Broker`. Only official, documented broker APIs.
6. Open a PR describing the behaviour change and the test that pins it.
