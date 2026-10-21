"""Default-off, fixture-only execution reservations; no order adapter or migration.

All claims, capacity checks and allocation changes share BEGIN IMMEDIATE.
Durable attempts are not broker idempotency keys. No worker/TTL cleanup releases
an attempted or external possible order. JSON projections grant no authority.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import sqlite3
import threading
import uuid

from trade_safety_kit import execution_binding as binding


class LedgerError(ValueError):
    pass


class LedgerDisabled(LedgerError):
    pass


_SCHEMA = """
CREATE TABLE config (singleton INTEGER PRIMARY KEY CHECK(singleton=1), payload TEXT NOT NULL);
CREATE TABLE snapshots (account TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE baseline (account TEXT, market TEXT, instrument TEXT, asset TEXT, quantity INTEGER, exposure INTEGER,
                       PRIMARY KEY(account,market,instrument));
CREATE TABLE possible_orders (account TEXT, order_id TEXT, payload TEXT NOT NULL, PRIMARY KEY(account,order_id));
CREATE TABLE opportunities (account TEXT, market TEXT, instrument TEXT, opportunity TEXT, fact_hash TEXT,
                           intent_id TEXT UNIQUE, PRIMARY KEY(account,market,instrument,opportunity),
                           UNIQUE(account,market,instrument,fact_hash));
CREATE TABLE active_instruments (account TEXT, market TEXT, instrument TEXT, intent_id TEXT UNIQUE,
                                PRIMARY KEY(account,market,instrument));
CREATE TABLE entries (intent_id TEXT PRIMARY KEY, request TEXT NOT NULL, request_hash TEXT NOT NULL,
                      state TEXT NOT NULL, attempt_id TEXT UNIQUE, broker_order_id TEXT,
                      filled INTEGER NOT NULL, spent INTEGER NOT NULL, fees INTEGER NOT NULL,
                      closed_unfilled INTEGER NOT NULL, terminal INTEGER NOT NULL, revision INTEGER NOT NULL,
                      receipt_hash TEXT, UNIQUE(broker_order_id,intent_id));
CREATE TABLE allocations (allocation_id TEXT PRIMARY KEY, intent_id TEXT UNIQUE, account TEXT, market TEXT,
                          instrument TEXT, asset TEXT, owner TEXT, book TEXT, quantity INTEGER NOT NULL,
                          exposure INTEGER NOT NULL, version INTEGER NOT NULL, plan_hash TEXT NOT NULL, plan_generation INTEGER NOT NULL);
CREATE TABLE exits (exit_id TEXT PRIMARY KEY, request TEXT NOT NULL, request_hash TEXT NOT NULL,
                    attempt_id TEXT UNIQUE, broker_order_id TEXT, state TEXT NOT NULL,
                    filled INTEGER NOT NULL, closed_unfilled INTEGER NOT NULL, terminal INTEGER NOT NULL,
                    revision INTEGER NOT NULL, receipt_hash TEXT);
CREATE TABLE shared_heads (namespace TEXT, head_key TEXT, head_id TEXT NOT NULL, revision INTEGER NOT NULL,
                           payload_hash TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(namespace,head_key));
CREATE TABLE gateway_preparations (intent_id TEXT PRIMARY KEY, payload_hash TEXT UNIQUE NOT NULL,
                                  attempt_id TEXT UNIQUE NOT NULL, permit_json TEXT NOT NULL);
"""


@dataclass(frozen=True)
class CASReceipt:
    """Fixture SQL CAS result, bound to its owned transaction; never live authority."""
    transaction_id: str
    connection_identity: int
    namespace: str
    head_key: str
    head_id: str
    revision: int
    payload_hash: str


class _BorrowedCursor:
    def __init__(self, cursor, transaction):
        self.__cursor = cursor; self.__transaction = transaction

    def fetchone(self):
        return self._read("fetchone")

    def fetchall(self):
        return self._read("fetchall")

    def fetchmany(self, size=1):
        return self._read("fetchmany", size)

    def _read(self, operation, *arguments):
        with self.__transaction._ledger.transaction(transaction=self.__transaction):
            return getattr(self.__cursor, operation)(*arguments)

    def __iter__(self):
        self.__transaction.check(); return self

    def __next__(self):
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row

    @property
    def rowcount(self):
        with self.__transaction._ledger.transaction(transaction=self.__transaction):
            return self.__cursor.rowcount


class LedgerTransaction:
    """Borrowed operations on one SQLite connection; no commit/rollback/close API.

    Native fixture CAS owners may use namespaced shared_heads SQL or cas_head.
    This is a cooperative source interface, not a sandbox against Python callers
    accessing private implementation objects. It grants no production authority.
    """
    __slots__=("_ledger","_thread","_transaction_id","_state","_receipts","_rollback_only")

    def __init__(self, ledger):
        self._ledger = ledger; self._thread = threading.get_ident()
        self._transaction_id = uuid.uuid4().hex; self._state = "ACTIVE"
        self._receipts = []
        self._rollback_only = False

    @property
    def transaction_id(self):return self._transaction_id

    @property
    def state(self):return self._state

    def check(self):
        if self._rollback_only:raise LedgerError("owned_transaction_rollback_only")
        if (self.state != "ACTIVE" or self._ledger._active_transaction is not self
                or threading.get_ident() != self._thread or not self._ledger.db.in_transaction):
            raise LedgerError("transaction_handle_expired_foreign_or_unowned")

    @property
    def connection_identity(self):
        self.check(); return id(self._ledger.db)

    def execute(self, sql, parameters=()):
        self.check()
        def authorize(action, first, _second, _db, _source):
            if action in {sqlite3.SQLITE_SELECT,sqlite3.SQLITE_READ,sqlite3.SQLITE_FUNCTION,sqlite3.SQLITE_RECURSIVE}:
                return sqlite3.SQLITE_OK
            if action in {sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE} and first=="shared_heads":
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        self._ledger.db.set_authorizer(authorize)
        try: cursor = self._ledger.db.execute(sql, parameters)
        except sqlite3.DatabaseError as exc: raise LedgerError("borrowed_sql_rejected") from exc
        finally: self._ledger.db.set_authorizer(None)
        return _BorrowedCursor(cursor, self)

    def read_head(self, namespace, head_key):
        self.check(); binding.text(namespace, "head_namespace"); binding.text(head_key, "head_key")
        row = self._ledger.db.execute("SELECT * FROM shared_heads WHERE namespace=? AND head_key=?", (namespace,head_key)).fetchone()
        return {**dict(row), "payload":json.loads(row["payload"])} if row else None

    def cas_head(self, *, namespace, head_key, expected_head_id, expected_revision,
                 head_id, revision, payload, expected_payload_hash):
        self.check()
        for value,name in ((namespace,"head_namespace"),(head_key,"head_key"),(head_id,"head_id")):
            binding.text(value,name)
        binding.integer(expected_revision,"expected_head_revision"); binding.integer(revision,"head_revision",1)
        binding.sha(expected_payload_hash,"head_payload_hash")
        if type(payload) is not dict or binding.digest(payload) != expected_payload_hash:
            raise LedgerError("head_payload_hash_conflict")
        old = self.read_head(namespace,head_key)
        encoded = binding.canonical(payload)
        if old is None:
            if expected_head_id is not None or expected_revision != 0 or revision != 1:
                raise LedgerError("head_predecessor_missing")
            self._ledger.db.execute("INSERT INTO shared_heads VALUES (?,?,?,?,?,?)",
                                    (namespace,head_key,head_id,revision,expected_payload_hash,encoded))
        else:
            binding.text(expected_head_id,"expected_head_id")
            if old["head_id"] != expected_head_id or old["revision"] != expected_revision or revision <= expected_revision:
                raise LedgerError("head_compare_and_swap_conflict")
            changed = self._ledger.db.execute("UPDATE shared_heads SET head_id=?,revision=?,payload_hash=?,payload=? WHERE namespace=? AND head_key=? AND head_id=? AND revision=?",
                (head_id,revision,expected_payload_hash,encoded,namespace,head_key,expected_head_id,expected_revision)).rowcount
            if changed != 1: raise LedgerError("head_compare_and_swap_conflict")
        receipt = CASReceipt(self.transaction_id,self.connection_identity,namespace,head_key,head_id,revision,expected_payload_hash)
        self._receipts.append(receipt); return receipt

    def require_cas_receipt(self, receipt, *, request_payload_hash):
        self.check()
        if not any(receipt is r for r in self._receipts): raise LedgerError("CAS_join_missing_or_foreign_transaction")
        row = self.read_head(receipt.namespace,receipt.head_key)
        if (row is None or row["head_id"] != receipt.head_id or row["revision"] != receipt.revision
                or row["payload_hash"] != receipt.payload_hash or binding.digest(row["payload"]) != receipt.payload_hash
                or row["payload"].get("request_payload_sha256") != request_payload_hash):
            raise LedgerError("CAS_join_payload_or_revision_conflict")


class NativeFixtureView:
    """Borrowed native SQL only; no connect, commit, rollback or close API."""
    def __init__(self, ledger, transaction):
        self._ledger, self._transaction = ledger, transaction

    @property
    def in_transaction(self):
        self._transaction.check(); return True

    def execute(self, sql, parameters=()):
        with self._ledger.transaction(transaction=self._transaction):
            self._ledger._verify_native_fixture()
            from trade_safety_kit import t1_sqlite_fixture as component
            def authorize(action, first, _second, _db, _source):
                if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}:
                    return sqlite3.SQLITE_OK
                if action == sqlite3.SQLITE_READ and first in component.TABLES | {"sqlite_master", "sqlite_schema"}:
                    return sqlite3.SQLITE_OK
                if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE} and first in component.TABLES:
                    return sqlite3.SQLITE_OK
                return sqlite3.SQLITE_DENY
            self._ledger.db.set_authorizer(authorize)
            try:
                cursor = self._ledger.db.execute(sql, parameters)
            except sqlite3.DatabaseError as exc:
                raise LedgerError("native_borrowed_sql_rejected") from exc
            finally:
                self._ledger.db.set_authorizer(None)
            return _BorrowedCursor(cursor, self._transaction)


class Ledger:
    def __init__(self, path, *, policy, accounts, enabled=False, native_component=None, fixture_root=None):
        if enabled is not True:
            raise LedgerDisabled("execution_ledger_disabled")
        self.policy = json.loads(binding.canonical(binding.validate_policy(policy)))
        if type(accounts) is not list: raise binding.BindingError("account_bindings_unknown")
        self.accounts = {}
        for account in accounts:
            binding.validate_account(account)
            if account["account_id"] in self.accounts: raise binding.BindingError("duplicate_account_binding")
            self.accounts[account["account_id"]] = json.loads(binding.canonical(account))
        if set(self.accounts) != set(self.policy["accounts"]):
            raise binding.BindingError("account_membership_unknown")
        self._active_transaction = None
        self.native_component = native_component
        if native_component is not None:
            from trade_safety_kit import t1_sqlite_fixture as component
            if native_component != component.SCHEMA:
                raise LedgerError("native_component_unsupported")
            component.fixture_path(path, fixture_root)
        self.db = sqlite3.connect(str(path), timeout=5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        try:
            tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            expected = {"config", "snapshots", "baseline", "possible_orders", "opportunities", "active_instruments", "entries", "allocations", "exits", "shared_heads", "gateway_preparations"}
            allowed = [expected] if native_component is None else [expected, expected | component.TABLES]
            if tables and tables not in allowed: raise LedgerError("existing_database_schema_unsupported")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            config_value = {"schema": "execution-ledger/fixture-v2", "policy": self.policy, "accounts": self.accounts}
            if native_component is not None:
                config_value["native_component"] = component.descriptor()
                config_value["native_fixture_initialized"] = False
            config = binding.canonical(config_value)
            with self.transaction():
                # Re-read after taking the writer lock; another process may initialize.
                tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if tables and tables not in allowed: raise LedgerError("existing_database_schema_unsupported")
                if not tables:
                    for statement in _SCHEMA.split(";"):
                        if statement.strip(): self.db.execute(statement)
                    self.db.execute("INSERT INTO config VALUES (1,?)", (config,))
                elif native_component is None and self.db.execute("SELECT payload FROM config WHERE singleton=1").fetchone()[0] != config:
                    raise LedgerError("ledger_policy_or_account_binding_changed")
                if native_component is not None:
                    initialized = self._native_fixture_initialized()
                    component.verify(self.db, [s.strip() for s in _SCHEMA.split(";") if s.strip()], initialized=initialized)
        except Exception:
            self.db.close(); raise

    def close(self):
        if self._active_transaction is not None: raise LedgerError("owned_transaction_still_active")
        self.db.close()

    def _verify_native_fixture(self):
        from trade_safety_kit import t1_sqlite_fixture as component
        if not self._native_fixture_initialized():
            raise LedgerError("native_fixture_initialization_required")
        component.verify(self.db, [s.strip() for s in _SCHEMA.split(";") if s.strip()])

    def _native_fixture_initialized(self):
        from trade_safety_kit import t1_sqlite_fixture as component
        if self.native_component != component.SCHEMA:
            raise LedgerError("native_component_required")
        config = self.db.execute("SELECT payload FROM config WHERE singleton=1").fetchone()[0]
        for initialized in (False, True):
            expected = {"schema":"execution-ledger/fixture-v2", "policy":self.policy, "accounts":self.accounts,
                "native_component":component.descriptor(), "native_fixture_initialized":initialized}
            if config == binding.canonical(expected):
                return initialized
        raise LedgerError("native_component_config_or_lifecycle_conflict")

    def initialize_native_fixture(self, *, transaction):
        with self.transaction(transaction=transaction):
            from trade_safety_kit import t1_sqlite_fixture as component
            if self.native_component != component.SCHEMA:
                raise LedgerError("native_component_required")
            initialized = self._native_fixture_initialized()
            component.verify(self.db, [s.strip() for s in _SCHEMA.split(";") if s.strip()], initialized=initialized)
            if not initialized:
                if self.db.execute("SELECT count(*) FROM entries").fetchone()[0]:
                    raise LedgerError("native_component_existing_attempt_adoption_forbidden")
                for statement in component.statements():
                    self.db.execute(statement)
                component.verify(self.db, [s.strip() for s in _SCHEMA.split(";") if s.strip()])
                previous = self.db.execute("SELECT payload FROM config WHERE singleton=1").fetchone()[0]
                value = json.loads(previous); value["native_fixture_initialized"] = True
                if self.db.execute("UPDATE config SET payload=? WHERE singleton=1 AND payload=?",
                        (binding.canonical(value), previous)).rowcount != 1:
                    raise LedgerError("native_component_initialization_CAS_conflict")
            self._verify_native_fixture()

    def native_view(self, *, transaction):
        with self.transaction(transaction=transaction):
            self._verify_native_fixture()
            return NativeFixtureView(self, transaction)

    @contextmanager
    def atomic(self):
        if self._active_transaction is not None or self.db.in_transaction:
            raise LedgerError("nested_or_unowned_transaction")
        self.db.execute("BEGIN IMMEDIATE")
        transaction = LedgerTransaction(self); self._active_transaction = transaction
        try:
            yield transaction
            transaction.check()
            self._commit_owned()
            transaction._state = "COMMITTED"
        except BaseException:
            self.db.rollback(); transaction._state = "ROLLED_BACK"; raise
        finally: self._active_transaction = None

    def _commit_owned(self):
        self.db.commit()

    @contextmanager
    def transaction(self, *, transaction=None):
        if transaction is not None:
            if type(transaction) is not LedgerTransaction or transaction._ledger is not self:
                raise LedgerError("transaction_handle_foreign")
            transaction.check()
            try:yield transaction
            except BaseException:
                transaction._rollback_only = True
                raise
        else:
            with self.atomic() as owned: yield owned

    def _account(self, account):
        try: return self.accounts[account]
        except KeyError: raise LedgerError("account_unknown") from None

    def _snapshot(self, account, now):
        row = self.db.execute("SELECT payload FROM snapshots WHERE account=?", (account,)).fetchone()
        if not row: raise LedgerError("account_snapshot_missing")
        value = json.loads(row[0])
        binding.validate_snapshot(value, self.policy, self._account(account), now)
        return value

    def observe(self, snapshot, *, now):
        binding.integer(now, "now", 1)
        binding.validate_snapshot(snapshot, self.policy, self._account(snapshot["account_id"]), now)
        account = snapshot["account_id"]
        reconciliation_conflict = False
        with self.transaction():
            old = self.db.execute("SELECT payload FROM snapshots WHERE account=?", (account,)).fetchone()
            if old:
                previous = json.loads(old[0])
                if snapshot["revision"] < previous["revision"] or (snapshot["revision"] == previous["revision"] and snapshot != previous):
                    raise LedgerError("snapshot_revision_conflict")
                if snapshot == previous: return
            owned = self.db.execute("SELECT * FROM allocations WHERE account=? AND quantity>0", (account,)).fetchall()
            provided = {}
            for row in snapshot["holdings"]:
                for allocation_id, quantity in row["allocation_quantities"].items():
                    if allocation_id in provided: raise LedgerError("duplicate_snapshot_allocation")
                    provided[allocation_id] = (row, quantity)
            if set(provided) != {r["allocation_id"] for r in owned}: raise LedgerError("snapshot_allocation_membership_conflict")
            for allocation in owned:
                row, quantity = provided[allocation["allocation_id"]]
                if (row["market"] != allocation["market"] or row["instrument_id"] != allocation["instrument"]
                        or row["asset_class"] != allocation["asset"] or quantity != allocation["quantity"]
                        or row["allocation_versions"][allocation["allocation_id"]] != allocation["version"]):
                    raise LedgerError("snapshot_owned_quantity_or_version_conflict")
            for attempt, watermark in snapshot["cash_watermarks"].items():
                entry = self.db.execute("SELECT * FROM entries WHERE attempt_id=?", (attempt,)).fetchone()
                if (not entry or json.loads(entry["request"])["account_id"] != account
                        or watermark != {"filled_qty": entry["filled"], "spent_krw": entry["spent"], "receipt_revision": entry["revision"]}):
                    raise LedgerError("cash_watermark_conflict")
            self.db.execute("DELETE FROM baseline WHERE account=?", (account,))
            for row in snapshot["holdings"]:
                external_qty = row["quantity"] - sum(row["allocation_quantities"].values())
                external_value = row["exposure_krw"] - sum(row["allocation_exposure_krw"].values())
                if (external_qty < 0 or external_value < 0 or (external_qty == 0 and external_value != 0)
                        or (external_qty > 0 and external_value <= 0)):
                    raise LedgerError("snapshot_physical_quantity_or_valuation_conflict")
                self.db.execute("INSERT INTO baseline VALUES (?,?,?,?,?,?)",
                                (account, row["market"], row["instrument_id"], row["asset_class"], external_qty, external_value))
                for allocation_id, exposure in row["allocation_exposure_krw"].items():
                    self.db.execute("UPDATE allocations SET exposure=? WHERE allocation_id=?", (exposure, allocation_id))
            for order in snapshot["orders"]:
                local = self._local_orders(account, order["order_id"])
                if local:
                    imported = self.db.execute("SELECT 1 FROM possible_orders WHERE account=? AND order_id=?", (account, order["order_id"])).fetchone()
                    if len(local) != 1 or imported or not self._matches_local_order(order, *local[0]):
                        identities = {(account, request["market"], request["instrument_id"]) for _, _, request in local}
                        self._retain_possible_order(account, order, identities=identities, force_unknown=True)
                        reconciliation_conflict = True
                    # Exact local buys/exits already carry their quantity claims.
                    # Never duplicate them as imported external possible orders.
                    continue
                # Imported possible orders never disappear on an empty later snapshot.
                self._retain_possible_order(account, order)
            self.db.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?)", (account, binding.canonical(snapshot)))
        if reconciliation_conflict:
            # Commit the uncertainty and current coherent positions before
            # rejecting order authority; rollback would revive older authority.
            raise LedgerError("broker_order_snapshot_conflict_uncertainty_retained")

    @staticmethod
    def _order_fences(account, order):
        identities = {(account, order["market"], order["instrument_id"])}
        identities.update((r["account_id"], r["market"], r["instrument_id"]) for r in order.get("identity_fences", []))
        return identities

    def _retain_possible_order(self, account, order, *, identities=(), force_unknown=False):
        previous = self.db.execute("SELECT payload FROM possible_orders WHERE account=? AND order_id=?", (account, order["order_id"])).fetchone()
        stored = json.loads(previous[0]) if previous else dict(order)
        fences = self._order_fences(account, stored) | self._order_fences(account, order) | set(identities)
        old_contract = {k:v for k,v in stored.items() if k != "identity_fences"}
        if force_unknown or old_contract != order:
            stored["known"] = False
        # Keep original coordinates and every conflicting coordinate. Neither
        # a later row nor an empty snapshot can overwrite an earlier sell fence.
        stored["identity_fences"] = [dict(account_id=a, market=m, instrument_id=i) for a,m,i in sorted(fences)]
        self.db.execute("INSERT OR REPLACE INTO possible_orders VALUES (?,?,?)", (account, order["order_id"], binding.canonical(stored)))

    def _local_orders(self, account, broker_order_id):
        result = []
        for kind, table in (("entry", "entries"), ("exit", "exits")):
            for row in self.db.execute(f"SELECT * FROM {table} WHERE broker_order_id=?", (broker_order_id,)):
                request = json.loads(row["request"])
                if request["account_id"] == account: result.append((kind, row, request))
        return result

    def _matches_local_order(self, order, kind, row, request):
        asset = request["asset_class"] if kind == "entry" else self.allocation(request["allocation_id"])["asset"]
        if (not order["known"] or row["terminal"] or order["side"] != ("buy" if kind == "entry" else "sell")
                or order["market"] != request["market"] or order["instrument_id"] != request["instrument_id"]
                or order["asset_class"] != asset
                or order["remaining_qty"] != request["quantity"]-row["filled"]-row["closed_unfilled"]):
            return False
        # No broker maximum is inferred from a quote or ignored in dedup.
        # Exit proceeds never supply cash; only buys have an approved cost bound.
        return kind == "exit" or order["remaining_max_krw"] == self._remaining(row, request)

    @staticmethod
    def _remaining(entry, request):
        if entry["terminal"]: return 0
        return ((request["quantity"]-entry["filled"]-entry["closed_unfilled"]) * request["max_unit_execution_krw"]
                + request["fee_ceiling_krw"]-entry["fees"])

    def _inventory(self):
        result = {}
        def add(account, market, instrument, asset, quantity, exposure):
            key = (account, market, instrument)
            row = result.setdefault(key, [asset, 0, 0])
            if row[0] != asset: raise LedgerError("instrument_classification_conflict")
            row[1] += quantity; row[2] += exposure
        for row in self.db.execute("SELECT * FROM baseline"):
            add(row["account"], row["market"], row["instrument"], row["asset"], row["quantity"], row["exposure"])
        for row in self.db.execute("SELECT * FROM allocations"):
            add(row["account"], row["market"], row["instrument"], row["asset"], row["quantity"], row["exposure"])
        for entry in self.db.execute("SELECT * FROM entries"):
            r = json.loads(entry["request"])
            add(r["account_id"], r["market"], r["instrument_id"], r["asset_class"],
                0 if entry["terminal"] else r["quantity"]-entry["filled"]-entry["closed_unfilled"], self._remaining(entry, r))
        for row in self.db.execute("SELECT * FROM possible_orders"):
            order = json.loads(row["payload"])
            if not order["known"]:
                raise LedgerError("possible_order_capacity_unknown")
            if order["side"] == "buy":
                add(row["account"], order["market"], order["instrument_id"], order["asset_class"], order["remaining_qty"], order["remaining_max_krw"])
        return result

    def reserve(self, intent, *, now, transaction=None):
        with self.transaction(transaction=transaction):
            binding.integer(now, "now", 1)
            binding.validate_intent(intent, self.policy, self._account(intent["account_id"]))
            request = binding.canonical(intent); request_hash = binding.digest(intent)
            existing = self.db.execute("SELECT request_hash FROM entries WHERE intent_id=?", (intent["intent_id"],)).fetchone()
            if existing:
                if existing[0] != request_hash: raise LedgerError("intent_identity_conflict")
                return request_hash
            if now > intent["expires_at"]: raise LedgerError("intent_expired")
            snapshots = {a: self._snapshot(a, now) for a in self.policy["accounts"]}
            inventory = self._inventory()
            key = (intent["account_id"], intent["market"], intent["instrument_id"])
            current = inventory.get(key, [intent["asset_class"], 0, 0])
            if current[0] != intent["asset_class"]: raise LedgerError("instrument_classification_conflict")
            if intent["asset_class"] == "stock":
                cap = self.policy["score_caps_krw"][intent["score_band"]]
                if current[2]+intent["max_execution_krw"] > cap: raise LedgerError("per_stock_score_cap_exceeded")
            else:
                used = sum(v[2] for k,v in inventory.items() if v[0] == "etf" and k[0] in self.policy["accounts"])
                if used+intent["max_execution_krw"] > self.policy["etf_aggregate_ceiling_krw"]:
                    raise LedgerError("separate_etf_aggregate_cap_exceeded")
            if current[1] > 0: raise LedgerError("initial_entry_requires_flat_instrument")
            self._check_initial_instrument(key)
            occupied = {k for k,v in inventory.items() if k[0] in self.policy["capacity_accounts"]
                        and v[0] in self.policy["capacity_asset_classes"] and (v[1] > 0 or v[2] > 0)}
            if key[0] in self.policy["capacity_accounts"] and intent["asset_class"] in self.policy["capacity_asset_classes"]:
                occupied.add(key)
            if len(occupied) > self.policy["max_occupied"]: raise LedgerError("supplied_capacity_limit_exceeded")
            account = intent["account_id"]; cash_used = 0
            for entry in self.db.execute("SELECT * FROM entries"):
                r = json.loads(entry["request"])
                if r["account_id"] == account:
                    watermark = snapshots[account]["cash_watermarks"].get(entry["attempt_id"], {"spent_krw": 0})
                    cash_used += self._remaining(entry, r)+entry["spent"]-watermark["spent_krw"]
            for row in self.db.execute("SELECT payload FROM possible_orders WHERE account=?", (account,)):
                order = json.loads(row[0])
                if order["side"] == "buy": cash_used += order["remaining_max_krw"]
            if cash_used+intent["max_execution_krw"] > snapshots[account]["settled_cash_krw"]:
                raise LedgerError("shared_cash_exceeded")
            try:
                self.db.execute("INSERT INTO opportunities VALUES (?,?,?,?,?,?)", (*key, intent["opportunity_id"], intent["source_fact_sha256"], intent["intent_id"]))
                self.db.execute("INSERT INTO active_instruments VALUES (?,?,?,?)", (*key, intent["intent_id"]))
                self.db.execute("INSERT INTO entries VALUES (?,?,?,'RESERVED',NULL,NULL,0,0,0,0,0,0,NULL)", (intent["intent_id"], request, request_hash))
                self.db.execute("INSERT INTO allocations VALUES (?,?,?,?,?,?,?,?,0,0,0,?,?)", (intent["allocation_id"], intent["intent_id"], *key, intent["asset_class"], intent["owner_id"], intent["book_id"], intent["plan_sha256"], intent["plan_generation"]))
            except sqlite3.IntegrityError as exc:
                raise LedgerError("opportunity_instrument_or_allocation_already_reserved") from exc
            return request_hash

    def begin_entry(self, intent_id, *, attempt_id, now, transaction=None):
        with self.transaction(transaction=transaction):
            binding.text(intent_id, "intent_id")
            binding.text(attempt_id, "attempt_id"); binding.integer(now, "now", 1)
            row = self.db.execute("SELECT * FROM entries WHERE intent_id=?", (intent_id,)).fetchone()
            if not row or row["state"] != "RESERVED": raise LedgerError("entry_send_right_unavailable")
            request = json.loads(row["request"])
            if now > request["expires_at"]: raise LedgerError("intent_expired")
            for account in self.policy["accounts"]: self._snapshot(account, now)
            inventory = self._inventory()
            current = inventory[(request["account_id"], request["market"], request["instrument_id"])]
            if request["asset_class"] == "stock" and current[2] > self.policy["score_caps_krw"][request["score_band"]]:
                raise LedgerError("per_stock_score_cap_exceeded")
            if request["asset_class"] == "etf" and sum(v[2] for v in inventory.values() if v[0] == "etf") > self.policy["etf_aggregate_ceiling_krw"]:
                raise LedgerError("separate_etf_aggregate_cap_exceeded")
            occupied = {k for k,v in inventory.items() if k[0] in self.policy["capacity_accounts"] and v[0] in self.policy["capacity_asset_classes"] and (v[1]>0 or v[2]>0)}
            if len(occupied) > self.policy["max_occupied"]: raise LedgerError("supplied_capacity_limit_exceeded")
            self._check_initial_instrument((request["account_id"],request["market"],request["instrument_id"]))
            self._check_cash(request["account_id"], now)
            self._attempt_available(attempt_id)
            self.db.execute("UPDATE entries SET state='SUBMITTING',attempt_id=? WHERE intent_id=?", (attempt_id, intent_id))
            return row["request_hash"]

    def _check_initial_instrument(self, key):
        baseline = self.db.execute("SELECT quantity FROM baseline WHERE account=? AND market=? AND instrument=?", key).fetchone()
        if baseline and baseline[0] > 0: raise LedgerError("initial_entry_requires_flat_instrument")
        for row in self.db.execute("SELECT payload FROM possible_orders WHERE account=?", (key[0],)):
            order = json.loads(row[0])
            if key in self._order_fences(key[0], order):
                raise LedgerError("possible_order_conflicts_with_initial_entry")

    def _attempt_available(self, attempt_id):
        if any(self.db.execute(f"SELECT 1 FROM {table} WHERE attempt_id=?", (attempt_id,)).fetchone() for table in ("entries", "exits")):
            raise LedgerError("attempt_identity_reused")

    def _check_cash(self, account, now):
        snapshot = self._snapshot(account, now); used = 0
        for entry in self.db.execute("SELECT * FROM entries"):
            request = json.loads(entry["request"])
            if request["account_id"] == account:
                covered = snapshot["cash_watermarks"].get(entry["attempt_id"], {"spent_krw":0})
                used += self._remaining(entry, request)+entry["spent"]-covered["spent_krw"]
        for row in self.db.execute("SELECT payload FROM possible_orders WHERE account=?", (account,)):
            order=json.loads(row[0])
            if not order["known"]: raise LedgerError("possible_order_capacity_unknown")
            if order["side"]=="buy": used+=order["remaining_max_krw"]
        if used>snapshot["settled_cash_krw"]: raise LedgerError("shared_cash_exceeded")

    def mark_unknown(self, attempt_id, *, transaction=None):
        with self.transaction(transaction=transaction):
            changed = self.db.execute("UPDATE entries SET state='SUBMISSION_UNKNOWN' WHERE attempt_id=? AND terminal=0 AND state!='RESERVED'", (attempt_id,)).rowcount
            changed += self.db.execute("UPDATE exits SET state='SUBMISSION_UNKNOWN' WHERE attempt_id=? AND terminal=0", (attempt_id,)).rowcount
            if changed != 1: raise LedgerError("attempt_identity_unknown")

    def cancel_unsent(self, intent_id):
        with self.transaction():
            row = self.db.execute("SELECT * FROM entries WHERE intent_id=?", (intent_id,)).fetchone()
            if not row or row["state"] != "RESERVED" or row["attempt_id"]: raise LedgerError("possible_order_cannot_cancel_locally")
            self.db.execute("UPDATE entries SET state='CLOSED',terminal=1 WHERE intent_id=?", (intent_id,))
            self._release_if_flat(intent_id)

    def _receipt_identity(self, proof, row, request, kind):
        binding.profiles(proof, self.policy)
        for key in ("account_id", "market", "instrument_id", "allocation_id", "owner_id", "book_id"):
            if proof[key] != request[key]: raise LedgerError("receipt_exact_identity_conflict")
        if proof["request_sha256"] != row["request_hash"]: raise LedgerError("receipt_request_hash_conflict")
        if row["broker_order_id"] and row["broker_order_id"] != proof["broker_order_id"]:
            raise LedgerError("receipt_order_identity_conflict")
        # Broker identities cannot mint quantity in two allocations or routes.
        for table in ("entries", "exits"):
            for other in self.db.execute(f"SELECT attempt_id,request FROM {table} WHERE broker_order_id=?", (proof["broker_order_id"],)):
                if other["attempt_id"] != proof["attempt_id"] and json.loads(other["request"])["account_id"] == proof["account_id"]:
                    raise LedgerError("broker_order_identity_reused")
        h = binding.digest(proof)
        if proof["revision"] < row["revision"] or (proof["revision"] == row["revision"] and h != row["receipt_hash"]):
            raise LedgerError("receipt_revision_conflict")
        if proof["filled_qty"] < row["filled"] or proof["closed_unfilled_qty"] < row["closed_unfilled"]:
            raise LedgerError("receipt_quantity_rollback")
        if proof["filled_qty"]+proof["closed_unfilled_qty"] > request["quantity"]:
            raise LedgerError("receipt_quantity_exceeds_request")
        if proof["terminal"] and proof["filled_qty"]+proof["closed_unfilled_qty"] != request["quantity"]:
            raise LedgerError("terminal_quantity_unaccounted")
        if row["terminal"] and (not proof["terminal"] or proof["filled_qty"] != row["filled"]):
            raise LedgerError("terminal_receipt_conflict")
        return h

    def entry_receipt(self, proof, *, transaction=None):
        with self.transaction(transaction=transaction):
            binding.validate_receipt(proof, "entry")
            row = self.db.execute("SELECT * FROM entries WHERE attempt_id=?", (proof["attempt_id"],)).fetchone()
            if not row: raise LedgerError("attempt_identity_unknown")
            request = json.loads(row["request"]); h = self._receipt_identity(proof, row, request, "entry")
            if h == row["receipt_hash"]: return
            if (proof["spent_krw"] < row["spent"] or proof["fee_paid_krw"] < row["fees"]
                    or proof["fee_paid_krw"] > request["fee_ceiling_krw"]
                    or not proof["fee_paid_krw"] <= proof["spent_krw"] <= proof["filled_qty"]*request["max_unit_execution_krw"]+proof["fee_paid_krw"]):
                raise LedgerError("receipt_execution_cost_conflict")
            allocation = self.db.execute("SELECT * FROM allocations WHERE allocation_id=?", (request["allocation_id"],)).fetchone()
            delta = proof["filled_qty"]-row["filled"]; quantity = allocation["quantity"]+delta
            if quantity == 0 and proof["holding_krw"] != 0: raise LedgerError("flat_allocation_has_exposure")
            if quantity > 0 and proof["holding_krw"] <= 0: raise LedgerError("owned_valuation_unknown")
            self.db.execute("UPDATE entries SET state=?,broker_order_id=?,filled=?,spent=?,fees=?,closed_unfilled=?,terminal=?,revision=?,receipt_hash=? WHERE intent_id=?",
                            ("TERMINAL" if proof["terminal"] else "OPEN", proof["broker_order_id"], proof["filled_qty"], proof["spent_krw"], proof["fee_paid_krw"], proof["closed_unfilled_qty"], int(proof["terminal"]), proof["revision"], h, row["intent_id"]))
            self.db.execute("UPDATE allocations SET quantity=?,exposure=?,version=version+1 WHERE allocation_id=?", (quantity, proof["holding_krw"], request["allocation_id"]))
            self._release_if_flat(row["intent_id"])

    def reserve_exit(self, request, *, attempt_id, now, transaction=None):
        with self.transaction(transaction=transaction):
            binding.validate_exit(request); binding.text(attempt_id, "attempt_id"); binding.integer(now, "now", 1)
            if now > request["expires_at"]: raise LedgerError("exit_intent_expired")
            snapshot = self._snapshot(request["account_id"], now)
            if snapshot["snapshot_id"] != request["position_snapshot_id"] or snapshot["revision"] != request["position_snapshot_revision"]:
                raise LedgerError("exit_position_snapshot_conflict")
            allocation = self.db.execute("SELECT * FROM allocations WHERE allocation_id=?", (request["allocation_id"],)).fetchone()
            if not allocation: raise LedgerError("allocation_missing")
            for key, column in (("account_id","account"),("market","market"),("instrument_id","instrument"),("owner_id","owner"),("book_id","book"),("expected_allocation_version","version"),("plan_sha256","plan_hash")):
                if request[key] != allocation[column]: raise LedgerError("exit_owned_identity_or_version_conflict")
            if request["plan_generation"] != allocation["plan_generation"]: raise LedgerError("exit_plan_generation_conflict")
            for row in self.db.execute("SELECT payload FROM possible_orders WHERE account=?", (request["account_id"],)):
                possible = json.loads(row[0])
                if (request["account_id"], request["market"], request["instrument_id"]) in self._order_fences(request["account_id"], possible):
                    raise LedgerError("exit_possible_order_ownership_unknown")
            held = next((r for r in snapshot["holdings"] if (r["market"],r["instrument_id"]) == (request["market"],request["instrument_id"])), None)
            if (not held or held["allocation_quantities"].get(request["allocation_id"]) != allocation["quantity"]
                    or held["allocation_versions"].get(request["allocation_id"]) != allocation["version"]):
                raise LedgerError("exit_fresh_owned_position_required")
            reserved = sum(json.loads(r["request"])["quantity"]-r["filled"]-r["closed_unfilled"] for r in self.db.execute("SELECT * FROM exits WHERE terminal=0")
                           if json.loads(r["request"])["allocation_id"] == request["allocation_id"])
            if request["quantity"] > allocation["quantity"]-reserved: raise LedgerError("exit_exceeds_owned_available_quantity")
            # This durable claim precedes any future adapter; no dispatch exists here.
            self._attempt_available(attempt_id)
            try:
                self.db.execute("INSERT INTO exits VALUES (?,?,?, ?,NULL,'SUBMITTING',0,0,0,0,NULL)",
                                (request["exit_id"], binding.canonical(request), binding.digest(request), attempt_id))
            except sqlite3.IntegrityError as exc: raise LedgerError("exit_attempt_already_claimed") from exc
            return binding.digest(request)

    def exit_receipt(self, proof, *, transaction=None):
        with self.transaction(transaction=transaction):
            binding.validate_receipt(proof, "exit")
            row = self.db.execute("SELECT * FROM exits WHERE attempt_id=?", (proof["attempt_id"],)).fetchone()
            if not row: raise LedgerError("attempt_identity_unknown")
            request = json.loads(row["request"]); h = self._receipt_identity(proof, row, request, "exit")
            if h == row["receipt_hash"]: return
            allocation = self.db.execute("SELECT * FROM allocations WHERE allocation_id=?", (request["allocation_id"],)).fetchone()
            if proof["expected_allocation_version"] != allocation["version"]: raise LedgerError("exit_receipt_allocation_version_conflict")
            quantity = allocation["quantity"]-(proof["filled_qty"]-row["filled"])
            if quantity < 0 or (quantity == 0 and proof["remaining_holding_krw"] != 0) or (quantity > 0 and proof["remaining_holding_krw"] <= 0):
                raise LedgerError("exit_receipt_owned_quantity_or_valuation_conflict")
            self.db.execute("UPDATE exits SET state=?,broker_order_id=?,filled=?,closed_unfilled=?,terminal=?,revision=?,receipt_hash=? WHERE exit_id=?",
                            ("TERMINAL" if proof["terminal"] else "OPEN", proof["broker_order_id"], proof["filled_qty"], proof["closed_unfilled_qty"], int(proof["terminal"]), proof["revision"], h, request["exit_id"]))
            self.db.execute("UPDATE allocations SET quantity=?,exposure=?,version=version+1 WHERE allocation_id=?", (quantity, proof["remaining_holding_krw"], request["allocation_id"]))
            self._release_if_flat(allocation["intent_id"])

    def _release_if_flat(self, intent_id):
        entry = self.db.execute("SELECT * FROM entries WHERE intent_id=?", (intent_id,)).fetchone()
        allocation = self.db.execute("SELECT * FROM allocations WHERE intent_id=?", (intent_id,)).fetchone()
        if not entry["terminal"] or allocation["quantity"] != 0: return
        for row in self.db.execute("SELECT request FROM exits WHERE terminal=0"):
            if json.loads(row[0])["allocation_id"] == allocation["allocation_id"]: return
        self.db.execute("DELETE FROM active_instruments WHERE intent_id=?", (intent_id,))

    def allocation(self, allocation_id):
        row = self.db.execute("SELECT * FROM allocations WHERE allocation_id=?", (allocation_id,)).fetchone()
        return dict(row) if row else None

    def inventory(self):
        with self.transaction(): return self._inventory()
