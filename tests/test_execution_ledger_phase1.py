"""Actual temporary SQLite transactions with synthetic evidence; no broker proof."""
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import unittest

from trade_safety_kit import execution_binding as b
from trade_safety_kit.execution_ledger import Ledger, LedgerError, LedgerDisabled
from tests import execution_phase1_fixtures as f


class Phase1LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/"fixture.sqlite"
        self.p = f.policy(("A","B")); self.accounts = [f.account("A"),f.account("B")]
        self.ledger = self.open(); self.addCleanup(lambda:self.ledger.close())
        for name in self.p["accounts"]: self.ledger.observe(f.snapshot(self.p,name), now=f.NOW)

    def open(self):
        return Ledger(self.path, policy=self.p, accounts=self.accounts, enabled=True)

    def fill(self, *, qty=4, total=4):
        request=f.intent(self.p,qty=total); self.ledger.reserve(request,now=f.NOW)
        self.ledger.begin_entry(request["intent_id"],attempt_id="attempt-one",now=f.NOW)
        proof=f.entry_receipt(self.p,request,"attempt-one",qty=qty,terminal=qty==total)
        self.ledger.entry_receipt(proof)
        return request,proof

    def position(self, request, *, external_qty=0, external_value=0, revision=2):
        allocation=self.ledger.allocation(request["allocation_id"])
        snap=f.snapshot(self.p,revision=revision,holdings=[f.holding(request["instrument_id"],qty=external_qty+allocation["quantity"],
               exposure=external_value+allocation["exposure"],allocations=[allocation])])
        self.ledger.observe(snap,now=f.NOW); return allocation,snap

    def test_default_off_does_not_create_database(self):
        path=Path(self.tmp.name)/"disabled.sqlite"
        with self.assertRaises(LedgerDisabled): Ledger(path,policy=self.p,accounts=self.accounts)
        self.assertFalse(path.exists())

    def test_missing_and_unknown_policy_inputs_block_without_creating_state(self):
        for key in ("accounts","capacity_accounts","max_occupied","valuation_profile","fx_profile","cost_profile","stock_ceiling_krw","score_caps_krw","etf_aggregate_ceiling_krw"):
            with self.subTest(key=key):
                p=deepcopy(self.p); p[key]=None
                path=Path(self.tmp.name)/f"unknown-{key}.sqlite"
                with self.assertRaises((b.BindingError,TypeError)): Ledger(path,policy=p,accounts=self.accounts,enabled=True)
                self.assertFalse(path.exists())

    def test_production_scope_and_guessed_cash_basis_are_unsupported(self):
        for key,value in (("scope","production"),("cash_basis","net_available")):
            p=deepcopy(self.p);p[key]=value
            with self.assertRaises(b.BindingError): Ledger(Path(self.tmp.name)/"bad.sqlite",policy=p,accounts=self.accounts,enabled=True)

    def test_account_and_content_git_hash_bindings_are_distinct(self):
        request=f.intent(self.p)
        for key,value in (("account_binding_sha256","0"*64),("policy_sha256","0"*64),("source_commit_sha1","0"*64),("content_manifest_sha256","0"*40)):
            with self.subTest(key=key):
                bad={**request,key:value}
                with self.assertRaises(b.BindingError): self.ledger.reserve(bad,now=f.NOW)
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM entries").fetchone()[0],0)

    def test_per_stock_cap_is_not_a_stock_portfolio_cap(self):
        # Each fixture stock reserves KRW4m; aggregate KRW8m is allowed.
        self.ledger.reserve(f.intent(self.p,"one",instrument="MSFT",qty=4,unit=1_000_000),now=f.NOW)
        self.ledger.reserve(f.intent(self.p,"two",instrument="AAPL",qty=4,unit=1_000_000),now=f.NOW)
        self.assertEqual(sum(v[2] for v in self.ledger.inventory().values()),8_000_000)

    def test_score_reduced_per_stock_limit_and_explicit_maximum_cost(self):
        with self.assertRaisesRegex(LedgerError,"per_stock_score_cap"):
            self.ledger.reserve(f.intent(self.p,qty=3,unit=1_000_000,band="fixture-reduced"),now=f.NOW)
        request=f.intent(self.p);request["max_unit_execution_krw"]=None
        with self.assertRaises(b.BindingError): self.ledger.reserve(request,now=f.NOW)
        request=f.intent(self.p);request["max_execution_krw"]-=1
        with self.assertRaisesRegex(b.BindingError,"bound_mismatch"): self.ledger.reserve(request,now=f.NOW)

    def test_etf_aggregate_is_separate_from_stock_cap(self):
        self.ledger.reserve(f.intent(self.p,"stock",instrument="MSFT",qty=4,unit=1_000_000),now=f.NOW)
        self.ledger.reserve(f.intent(self.p,"etf1",instrument="QQQ",asset="etf",qty=3,unit=1_000_000),now=f.NOW)
        with self.assertRaisesRegex(LedgerError,"etf_aggregate"):
            self.ledger.reserve(f.intent(self.p,"etf2",instrument="QLD",asset="etf",qty=3,unit=1_000_000),now=f.NOW)

    def test_stock_and_etf_compete_for_supplied_shared_cash(self):
        self.ledger.observe(f.snapshot(self.p,revision=2,cash=5_000_000),now=f.NOW)
        self.ledger.reserve(f.intent(self.p,"stock",qty=4,unit=1_000_000),now=f.NOW)
        with self.assertRaisesRegex(LedgerError,"shared_cash"):
            self.ledger.reserve(f.intent(self.p,"etf",instrument="QQQ",asset="etf",qty=2,unit=1_000_000),now=f.NOW)

    def test_etf_aggregate_uses_explicit_multi_account_membership(self):
        self.ledger.reserve(f.intent(self.p,"etf-A",asset="etf",instrument="QQQ",qty=3,unit=1_000_000),now=f.NOW)
        with self.assertRaisesRegex(LedgerError,"etf_aggregate"):
            self.ledger.reserve(f.intent(self.p,"etf-B",name="B",asset="etf",instrument="QLD",qty=3,unit=1_000_000),now=f.NOW)
        self.ledger.reserve(f.intent(self.p,"stock-B",name="B",instrument="AAPL",qty=4,unit=1_000_000),now=f.NOW)

    def test_supplied_capacity_membership_is_not_an_implicit_global_three(self):
        self.ledger.close();self.path=Path(self.tmp.name)/"scope.sqlite"
        self.p=f.policy(("A","B"),capacity=1);self.p["capacity_accounts"]=["A"]
        self.ledger=self.open()
        for name in self.p["accounts"]:self.ledger.observe(f.snapshot(self.p,name),now=f.NOW)
        self.ledger.reserve(f.intent(self.p,"A-first"),now=f.NOW)
        with self.assertRaisesRegex(LedgerError,"supplied_capacity"):
            self.ledger.reserve(f.intent(self.p,"A-second",instrument="AAPL"),now=f.NOW)
        for i,instrument in enumerate(("MSFT","AAPL","FAKEC","AMD")):
            self.ledger.reserve(f.intent(self.p,f"B-{i}",name="B",instrument=instrument),now=f.NOW)

    def test_active_instrument_and_original_opportunity_fences(self):
        one=f.intent(self.p);self.ledger.reserve(one,now=f.NOW)
        with self.assertRaises(LedgerError): self.ledger.reserve(f.intent(self.p,"C",opportunity="one",owner="scanner"),now=f.NOW)
        with self.assertRaises(LedgerError): self.ledger.reserve(f.intent(self.p,"different-source"),now=f.NOW)
        self.ledger.cancel_unsent("one")
        with self.assertRaises(LedgerError): self.ledger.reserve(f.intent(self.p,"new-local-id",opportunity="one"),now=f.NOW)
        self.ledger.reserve(f.intent(self.p,"new-opportunity"),now=f.NOW)

    def test_verified_account_separation(self):
        self.ledger.reserve(f.intent(self.p,"A-one",name="A"),now=f.NOW)
        self.ledger.reserve(f.intent(self.p,"B-one",name="B"),now=f.NOW)
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM active_instruments").fetchone()[0],2)

    def test_partial_fill_transfers_without_double_counting(self):
        request=f.intent(self.p,qty=10,fee=100)
        self.ledger.reserve(request,now=f.NOW);self.ledger.begin_entry("one",attempt_id="attempt",now=f.NOW)
        self.ledger.entry_receipt(f.entry_receipt(self.p,request,"attempt",qty=2,fee=20))
        amount=self.ledger.inventory()[("A","us","MSFT")][2]
        self.assertEqual(amount,1_000_100)
        allocation=self.ledger.allocation(request["allocation_id"])
        self.assertEqual(allocation["quantity"],2)
        self.assertEqual(allocation["exposure"],200_020)

    def test_identical_receipt_dedup_and_revision_identity_conflicts(self):
        request,proof=self.fill();before=self.ledger.allocation(request["allocation_id"])
        self.ledger.entry_receipt(proof);self.assertEqual(self.ledger.allocation(request["allocation_id"]),before)
        for key,value in (("book_id","manual"),("broker_order_id","wrong"),("filled_qty",3),("evidence_sha256","0"*64)):
            with self.subTest(key=key):
                with self.assertRaises(LedgerError): self.ledger.entry_receipt({**proof,key:value})
        self.assertEqual(self.ledger.allocation(request["allocation_id"]),before)

    def test_restart_timeout_and_ttl_never_release_possible_order(self):
        request=f.intent(self.p);self.ledger.reserve(request,now=f.NOW)
        self.ledger.begin_entry("one",attempt_id="attempt",now=f.NOW);self.ledger.mark_unknown("attempt")
        other=self.open();self.addCleanup(other.close)
        with self.assertRaises(LedgerError): other.begin_entry("one",attempt_id="retry",now=9000)
        with self.assertRaises(LedgerError): other.cancel_unsent("one")
        with self.assertRaises(LedgerError): other.reserve(f.intent(self.p,"replacement"),now=f.NOW)
        self.assertEqual(other.db.execute("SELECT state FROM entries").fetchone()[0],"SUBMISSION_UNKNOWN")

    def test_unknown_external_order_survives_empty_fresh_snapshot(self):
        order=dict(order_id="fixture-possible",market="us",instrument_id="MSFT",asset_class=None,side=None,known=False,remaining_qty=None,remaining_max_krw=None)
        self.ledger.observe(f.snapshot(self.p,revision=2,orders=[order]),now=f.NOW)
        self.ledger.observe(f.snapshot(self.p,revision=3),now=f.NOW)
        with self.assertRaisesRegex(LedgerError,"capacity_unknown"): self.ledger.reserve(f.intent(self.p),now=f.NOW)
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM possible_orders").fetchone()[0],1)

    def test_holding_and_known_pending_both_count_toward_that_stock_cap(self):
        order=dict(order_id="external",market="us",instrument_id="MSFT",asset_class="stock",side="buy",known=True,remaining_qty=3,remaining_max_krw=3_000_000)
        snap=f.snapshot(self.p,revision=2,holdings=[f.holding("MSFT",qty=2,exposure=2_000_000)],orders=[order])
        self.ledger.observe(snap,now=f.NOW)
        self.assertEqual(self.ledger.inventory()[("A","us","MSFT")][2],5_000_000)
        with self.assertRaisesRegex(LedgerError,"per_stock_score_cap"):
            self.ledger.reserve(f.intent(self.p,qty=1,unit=1_000_000),now=f.NOW)

    def test_external_order_conflict_latches_unknown_instead_of_small_old_bound(self):
        order=dict(order_id="external",market="us",instrument_id="MSFT",asset_class="stock",side="buy",known=True,remaining_qty=1,remaining_max_krw=100_000)
        self.ledger.observe(f.snapshot(self.p,revision=2,orders=[order]),now=f.NOW)
        self.ledger.observe(f.snapshot(self.p,revision=3,orders=[{**order,"remaining_max_krw":200_000}]),now=f.NOW)
        with self.assertRaisesRegex(LedgerError,"capacity_unknown"):
            self.ledger.reserve(f.intent(self.p,instrument="AAPL"),now=f.NOW)

    def test_send_boundary_rechecks_cash_and_possible_orders(self):
        request=f.intent(self.p);self.ledger.reserve(request,now=f.NOW)
        self.ledger.observe(f.snapshot(self.p,revision=2,cash=100),now=f.NOW)
        with self.assertRaisesRegex(LedgerError,"shared_cash"):
            self.ledger.begin_entry("one",attempt_id="attempt",now=f.NOW)
        self.assertEqual(self.ledger.db.execute("SELECT state FROM entries").fetchone()[0],"RESERVED")
        order=dict(order_id="unknown",market="us",instrument_id="MSFT",asset_class=None,side=None,known=False,remaining_qty=None,remaining_max_krw=None)
        self.ledger.observe(f.snapshot(self.p,revision=3,orders=[order]),now=f.NOW)
        with self.assertRaisesRegex(LedgerError,"capacity_unknown"):
            self.ledger.begin_entry("one",attempt_id="attempt",now=f.NOW)

    def test_snapshot_refresh_does_not_double_count_owned_fills(self):
        request,proof=self.fill(qty=2,total=10)
        allocation=self.ledger.allocation(request["allocation_id"])
        snap=f.snapshot(self.p,revision=2,holdings=[f.holding("MSFT",qty=2,exposure=200_000,allocations=[allocation])],
               watermarks={"attempt-one":{"filled_qty":2,"spent_krw":200_000,"receipt_revision":1}})
        self.ledger.observe(snap,now=f.NOW)
        self.assertEqual(self.ledger.inventory()[("A","us","MSFT")][2],1_000_000)
        with self.assertRaisesRegex(LedgerError,"snapshot_owned_quantity"):
            bad=deepcopy(snap);bad["revision"]=3;bad["snapshot_id"]="bad";bad["holdings"][0]["allocation_versions"][request["allocation_id"]]=99
            self.ledger.observe(bad,now=f.NOW)

    def test_zero_unknown_holding_valuation_cannot_hide_etf_usage(self):
        bad=f.snapshot(self.p,revision=2,holdings=[f.holding("QQQ",qty=1,exposure=0,asset="etf")])
        with self.assertRaisesRegex(b.BindingError,"valuation_unknown"):self.ledger.observe(bad,now=f.NOW)

    def test_mixed_holding_requires_external_quantity_valuation(self):
        request,_=self.fill();allocation=self.ledger.allocation(request["allocation_id"])
        # Positive manual/foreign quantity cannot borrow the owned valuation.
        snap=f.snapshot(self.p,revision=2,holdings=[f.holding("MSFT",qty=20,exposure=allocation["exposure"],allocations=[allocation])])
        with self.assertRaisesRegex(LedgerError,"physical_quantity_or_valuation_conflict"):
            self.ledger.observe(snap,now=f.NOW)
        self.assertEqual(self.ledger.inventory()[("A","us","MSFT")][1],4)

    def test_known_external_sell_blocks_initial_instrument_claim(self):
        order=dict(order_id="external-sell",market="us",instrument_id="MSFT",asset_class="stock",side="sell",known=True,remaining_qty=1,remaining_max_krw=100_000)
        self.ledger.observe(f.snapshot(self.p,revision=2,orders=[order]),now=f.NOW)
        with self.assertRaisesRegex(LedgerError,"possible_order_conflicts"):
            self.ledger.reserve(f.intent(self.p),now=f.NOW)

    def test_send_rechecks_known_same_instrument_orders_below_budget(self):
        self.ledger.reserve(f.intent(self.p),now=f.NOW)
        order=dict(order_id="external-buy",market="us",instrument_id="MSFT",asset_class="stock",side="buy",known=True,remaining_qty=1,remaining_max_krw=100_000)
        self.ledger.observe(f.snapshot(self.p,revision=2,orders=[order]),now=f.NOW)
        with self.assertRaisesRegex(LedgerError,"possible_order_conflicts"):
            self.ledger.begin_entry("one",attempt_id="must-not-send",now=f.NOW)
        self.assertEqual(self.ledger.db.execute("SELECT state FROM entries").fetchone()[0],"RESERVED")

    def test_missing_stale_or_incomplete_snapshots_block(self):
        with self.assertRaises(LedgerError): self.ledger.reserve(f.intent(self.p),now=6000)
        for key,value in (("complete",False),("fx_profile",None),("account_binding_sha256","0"*64)):
            with self.subTest(key=key):
                bad=f.snapshot(self.p,revision=2);bad[key]=value
                with self.assertRaises(b.BindingError): self.ledger.observe(bad,now=f.NOW)

    def test_policy_change_cannot_reopen_database_or_drop_limits(self):
        changed=deepcopy(self.p);changed["stock_ceiling_krw"]*=2
        with self.assertRaisesRegex(LedgerError,"policy_or_account"):
            Ledger(self.path,policy=changed,accounts=self.accounts,enabled=True)

    def test_owned_exit_cannot_borrow_manual_or_other_book_quantity(self):
        request,_=self.fill()
        # Sixteen external shares represent manual10 + foreign-book6; never T1.
        allocation,snap=self.position(request,external_qty=16,external_value=1_600_000)
        for changes in ({"quantity":5},{"owner_id":"scanner"},{"book_id":"manual"},{"account_id":"B"},
                        {"expected_allocation_version":99},{"plan_sha256":"0"*64},{"plan_generation":99}):
            with self.subTest(changes=changes):
                req={**f.exit_request(request,allocation,snap,qty=4),**changes}
                with self.assertRaises(LedgerError): self.ledger.reserve_exit(req,attempt_id="bad-exit",now=f.NOW)
        valid=f.exit_request(request,allocation,snap,qty=4)
        self.ledger.reserve_exit(valid,attempt_id="sell",now=f.NOW)
        self.ledger.exit_receipt(f.exit_receipt(self.p,valid,"sell",qty=4,allocation_version=1,remaining=0))
        self.assertEqual(self.ledger.allocation(request["allocation_id"])["quantity"],0)
        self.assertEqual(self.ledger.inventory()[("A","us","MSFT")][1],16)

    def test_exit_partial_fill_retains_unsold_claim_and_exact_version(self):
        request,_=self.fill();allocation,snap=self.position(request)
        exit=f.exit_request(request,allocation,snap,qty=4)
        self.ledger.reserve_exit(exit,attempt_id="sell",now=f.NOW)
        self.ledger.exit_receipt(f.exit_receipt(self.p,exit,"sell",qty=2,allocation_version=1,remaining=200_000,terminal=False))
        current,new_snapshot=self.position(request,revision=3)
        next_exit=f.exit_request(request,current,new_snapshot,"second",qty=1)
        with self.assertRaisesRegex(LedgerError,"owned_available"):
            self.ledger.reserve_exit(next_exit,attempt_id="second-sell",now=f.NOW)
        stale=f.exit_receipt(self.p,exit,"sell",qty=4,allocation_version=1,remaining=0,revision=2)
        with self.assertRaisesRegex(LedgerError,"version_conflict"):self.ledger.exit_receipt(stale)
        self.ledger.exit_receipt({**stale,"expected_allocation_version":2})
        self.assertEqual(self.ledger.allocation(request["allocation_id"])["quantity"],0)

    def test_plan_generation_is_separate_from_allocation_version(self):
        request,proof=self.fill()
        self.ledger.entry_receipt({**proof,"revision":2,"evidence_sha256":"0"*64})
        allocation,snap=self.position(request)
        self.assertEqual(allocation["version"],2);self.assertEqual(allocation["plan_generation"],1)
        self.ledger.reserve_exit(f.exit_request(request,allocation,snap,qty=4),attempt_id="sell",now=f.NOW)

    def test_broker_order_identity_cannot_mint_another_allocation(self):
        one,proof=self.fill()
        second=f.intent(self.p,"other",instrument="AAPL",qty=4)
        self.ledger.reserve(second,now=f.NOW);self.ledger.begin_entry("other",attempt_id="second-attempt",now=f.NOW)
        duplicate=f.entry_receipt(self.p,second,"second-attempt",qty=4,terminal=True)
        with self.assertRaisesRegex(LedgerError,"identity_reused"):
            self.ledger.entry_receipt({**duplicate,"broker_order_id":proof["broker_order_id"]})
        self.assertEqual(self.ledger.allocation(second["allocation_id"])["quantity"],0)

    def test_attempt_identity_is_shared_across_entry_and_exit(self):
        request,_=self.fill();allocation,snap=self.position(request)
        with self.assertRaisesRegex(LedgerError,"attempt_identity_reused"):
            self.ledger.reserve_exit(f.exit_request(request,allocation,snap,qty=4),attempt_id="attempt-one",now=f.NOW)

    def test_terminal_partial_entry_keeps_its_owned_position_reserved(self):
        request,proof=self.fill(qty=2,total=10)
        self.ledger.entry_receipt({**proof,"revision":2,"terminal":True,"closed_unfilled_qty":8,"evidence_sha256":"1"*64})
        self.assertEqual(self.ledger.inventory()[("A","us","MSFT")][2],200_000)
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM active_instruments").fetchone()[0],1)

    def test_unrelated_account_and_instrument_exits_continue(self):
        requests=[]
        for name,instrument in (("A","MSFT"),("B","AAPL")):
            r=f.intent(self.p,name+instrument,name=name,instrument=instrument,qty=4)
            self.ledger.reserve(r,now=f.NOW);self.ledger.begin_entry(r["intent_id"],attempt_id=r["intent_id"],now=f.NOW)
            self.ledger.entry_receipt(f.entry_receipt(self.p,r,r["intent_id"],qty=4,terminal=True));requests.append(r)
        order=dict(order_id="possible-A",market="us",instrument_id="MSFT",asset_class=None,side=None,known=False,remaining_qty=None,remaining_max_krw=None)
        for r in requests:
            a=self.ledger.allocation(r["allocation_id"])
            snap=f.snapshot(self.p,r["account_id"],revision=2,holdings=[f.holding(r["instrument_id"],qty=4,exposure=a["exposure"],allocations=[a])],orders=[order] if r["account_id"]=="A" else [])
            self.ledger.observe(snap,now=f.NOW)
            req=f.exit_request(r,a,snap,r["intent_id"]+"-exit",qty=4)
            if r["account_id"]=="A":
                with self.assertRaises(LedgerError):self.ledger.reserve_exit(req,attempt_id="A-sell",now=f.NOW)
            else:self.ledger.reserve_exit(req,attempt_id="B-sell",now=f.NOW)
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM exits").fetchone()[0],1)

    def race(self, action, args):
        gate=threading.Barrier(len(args));result=[]
        def worker(arg):
            ledger=self.open()
            try:
                gate.wait();getattr(ledger,action)(arg,now=f.NOW);result.append("won")
            except LedgerError:result.append("blocked")
            finally:ledger.close()
        threads=[threading.Thread(target=worker,args=(arg,)) for arg in args]
        for t in threads:t.start()
        for t in threads:t.join(5);self.assertFalse(t.is_alive())
        return result

    def test_competing_routes_thread_race_has_one_winner(self):
        requests=[f.intent(self.p,str(i),owner=owner,opportunity="same-fact") for i,owner in enumerate(("t1-B","t1-C","scanner","user-buy","tripod"))]
        result=self.race("reserve",requests)
        self.assertEqual(result.count("won"),1);self.assertEqual(result.count("blocked"),4)

    def test_stock_etf_last_cash_budget_thread_race(self):
        self.ledger.observe(f.snapshot(self.p,revision=2,cash=5_000_000),now=f.NOW)
        requests=[f.intent(self.p,"stock",qty=4,unit=1_000_000),f.intent(self.p,"etf",asset="etf",instrument="QQQ",qty=4,unit=1_000_000)]
        self.assertEqual(sorted(self.race("reserve",requests)),["blocked","won"])

    def test_supplied_capacity_last_slot_thread_race(self):
        # Fixture max2 is a test parameter, not a newly approved global max3.
        self.ledger.close();self.p=f.policy(("A","B"),capacity=2);self.path=Path(self.tmp.name)/"capacity.sqlite"
        self.ledger=self.open()
        for name in self.p["accounts"]:self.ledger.observe(f.snapshot(self.p,name),now=f.NOW)
        self.ledger.reserve(f.intent(self.p,"existing",instrument="FAKEC"),now=f.NOW)
        result=self.race("reserve",[f.intent(self.p,"one",instrument="MSFT"),f.intent(self.p,"two",instrument="AAPL")])
        self.assertEqual(result.count("won"),1)

    def test_two_exit_threads_cannot_borrow_the_same_owned_quantity(self):
        request,_=self.fill();allocation,snap=self.position(request)
        gate=threading.Barrier(2);result=[]
        def worker(index):
            ledger=self.open()
            try:
                gate.wait();ledger.reserve_exit(f.exit_request(request,allocation,snap,str(index),qty=3),attempt_id=f"sell-{index}",now=f.NOW);result.append("won")
            except LedgerError:result.append("blocked")
            finally:ledger.close()
        threads=[threading.Thread(target=worker,args=(i,)) for i in range(2)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(5);self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(result),["blocked","won"])


if __name__=="__main__":unittest.main()
