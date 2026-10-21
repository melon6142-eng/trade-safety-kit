# trade-safety-kit

Fail-closed guard rails for automated / algorithmic trading code in Python.
Extracted from a real trading system and generalised. No strategy, no signals -
just the parts that stop a bot from doing something it should not.

| Module | What it does |
|---|---|
| `entry_levels` | Validate long entry/stop/target (ordering, finite, risk-% bounds). |
| `allowlist` | Single-use, expiring allowlist naming the exact symbol/qty/limit price allowed to open risk. |
| `transport_gate` | Deny-by-default HTTP gate: closed inventory of read/exit/entry paths, entries need an allowlist claim; blocks redirects, query aliases, header/host smuggling. |
| `execution_binding` / `execution_ledger` / `execution_gateway` | SQLite ledger with compare-and-swap claims for idempotent, single-owner order intents (fixture profile; never grants live authority by itself). |
| `reconcile` | Broker-vs-local position reconciliation; partial/paginated/failed reads are "unknown", never "empty". |
| `brokers` | `Broker` protocol + Alpaca adapter (dry-run by default). |
| `backtest` | R-multiple backtester with an enforced look-ahead guard (signal sees only bars up to `i`, fills at next open, gap-aware stop-first exits). |
| `stats` | Paired A/B deltas: bootstrap CI, exact sign test, n<30 = provisional. |

## Install

```bash
pip install -e ".[dev]"     # or ".[backtest]" for pandas only
pytest -q
```

## Quickstart

```bash
python examples/quickstart.py
```

```python
from trade_safety_kit.transport_gate import TransportPolicy
policy = TransportPolicy(origin="https://broker.example.test",
                         read_paths=frozenset({"/v1/positions"}),
                         entry_paths=frozenset({"/v1/buy"}),
                         allowlist_path="allowlist.json")
policy.classify("POST", "https://broker.example.test/v1/buy")  # -> TransportDenied
```

Configuration is via environment variables only (see `.env.example`). Nothing
sends a live order unless you explicitly enable it (e.g. `ALPACA_EXECUTE=1`).

## Disclaimer

Not financial advice. Use at your own risk; test against a paper account first.

## License

MIT
