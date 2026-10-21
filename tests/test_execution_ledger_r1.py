"""PR12 reviewer regressions in temporary SQLite; evidence and budgets are fixtures."""
from pathlib import Path
import tempfile
import threading
import unittest

from trade_safety_kit.execution_binding import BindingError
from trade_safety_kit.execution_ledger import Ledger, LedgerError
from tests import execution_phase1_fixtures as f


class Phase1R1Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/"r1.sqlite"
        self.p=f.policy(("A","B"));self.accounts=[f.account("A"),f.account("B")]
        self.ledger=self.open();self.addCleanup(lambda:self.ledger.close())
        for name in self.p["accounts"]:self.ledger.observe(f.snapshot(self.p,name),now=f.NOW)

    def open(self):return Ledger(self.path,policy=self.p,accounts=self.accounts,enabled=True)

    def fill(self,identity="MSFT",*,name="A",market="us",instrument="MSFT",qty=4,filled=None,fee=0):
        request=f.intent(self.p,identity,name=name,instrument=instrument,qty=qty,fee=fee);request["market"]=market
        self.ledger.reserve(request,now=f.NOW)
        attempt="attempt:"+identity;self.ledger.begin_entry(identity,attempt_id=attempt,now=f.NOW)
        filled=qty if filled is None else filled
        proof=f.entry_receipt(self.p,request,attempt,qty=filled,terminal=filled==qty)
        self.ledger.entry_receipt(proof)
        return request,proof

    @staticmethod
    def order(identity="external-sell",*,market="us",instrument="MSFT",side="sell",qty=1,cost=100_000,known=True):
        return dict(order_id=identity,market=market,instrument_id=instrument,asset_class="stock" if known else None,
                    side=side if known else None,known=known,remaining_qty=qty if known else None,remaining_max_krw=cost if known else None)

    def positions(self,*,name="A",revision=2,orders=(),cash=5_000_000,watermarks=None):
        allocations=[dict(row) for row in self.ledger.db.execute("SELECT * FROM allocations WHERE account=? AND quantity>0",(name,))]
        holdings=[{**f.holding(a["instrument"],qty=a["quantity"],exposure=a["exposure"],asset=a["asset"],allocations=[a]),"market":a["market"]} for a in allocations]
        return f.snapshot(self.p,name,revision=revision,holdings=holdings,orders=list(orders),cash=cash,watermarks=watermarks)

    def exit(self,request,snapshot,identity="exit"):
        allocation=self.ledger.allocation(request["allocation_id"])
        return f.exit_request(request,allocation,snapshot,identity,qty=allocation["quantity"])

    def test_reviewer_external_sell_coordinate_conflict_retains_original_fence(self):
        msft,_=self.fill();self.fill("AAPL",instrument="AAPL")
        order=self.order()
        self.ledger.observe(self.positions(orders=[order]),now=f.NOW)
        conflicting=self.positions(revision=3,orders=[{**order,"instrument_id":"AAPL"}])
        self.ledger.observe(conflicting,now=f.NOW)
        with self.assertRaisesRegex(LedgerError,"ownership_unknown"):
            self.ledger.reserve_exit(self.exit(msft,conflicting),attempt_id="must-not-sell-MSFT",now=f.NOW)

    def test_reviewer_partial_buy_higher_same_quantity_cost_blocks_other_stock(self):
        request,proof=self.fill(qty=10,filled=2)
        order=self.order(proof["broker_order_id"],side="buy",qty=8,cost=4_800_000)
        snapshot=self.positions(orders=[order],cash=5_000_000)
        try:self.ledger.observe(snapshot,now=f.NOW)
        except LedgerError as exc:self.assertIn("broker_order_snapshot_conflict",str(exc))
        with self.assertRaises(LedgerError):
            self.ledger.reserve(f.intent(self.p,"AAPL",instrument="AAPL",qty=4,unit=1_000_000),now=f.NOW)

    def test_reviewer_local_partial_exit_snapshot_is_not_external_tombstone(self):
        request,_=self.fill();snapshot=self.positions()
        self.ledger.observe(snapshot,now=f.NOW)
        allocation=self.ledger.allocation(request["allocation_id"])
        first=f.exit_request(request,allocation,snapshot,"first",qty=2)
        self.ledger.reserve_exit(first,attempt_id="first-sell",now=f.NOW)
        proof=f.exit_receipt(self.p,first,"first-sell",qty=1,allocation_version=allocation["version"],remaining=300_000,terminal=False)
        self.ledger.exit_receipt(proof)
        partial=self.positions(revision=3,orders=[self.order(proof["broker_order_id"],qty=1)])
        self.ledger.observe(partial,now=f.NOW)
        current=self.ledger.allocation(request["allocation_id"])
        terminal=f.exit_receipt(self.p,first,"first-sell",qty=2,allocation_version=current["version"],remaining=200_000,revision=2)
        self.ledger.exit_receipt(terminal)
        refreshed=self.positions(revision=4);self.ledger.observe(refreshed,now=f.NOW)
        self.ledger.reserve_exit(self.exit(request,refreshed,"second"),attempt_id="second-sell",now=f.NOW)

    def test_reviewer_unknown_local_refresh_persists_admission_hold_after_error(self):
        _,proof=self.fill(qty=10,filled=2)
        unknown=self.positions(orders=[self.order(proof["broker_order_id"],known=False)])
        with self.assertRaisesRegex(LedgerError,"broker_order_snapshot_conflict"):
            self.ledger.observe(unknown,now=f.NOW)
        restarted=self.open();self.addCleanup(restarted.close)
        with self.assertRaisesRegex(LedgerError,"capacity_unknown"):
            restarted.reserve(f.intent(self.p,"AAPL",instrument="AAPL",qty=4,unit=1_000_000),now=f.NOW)

    def test_external_conflict_retains_original_and_every_new_instrument(self):
        requests=[self.fill(name,instrument=name)[0] for name in ("MSFT","AAPL","FAKEC")]
        order=self.order();self.ledger.observe(self.positions(orders=[order]),now=f.NOW)
        for revision,instrument in ((3,"AAPL"),(4,"FAKEC"),(5,"MSFT")):
            snapshot=self.positions(revision=revision,orders=[{**order,"instrument_id":instrument}])
            self.ledger.observe(snapshot,now=f.NOW)
        stored=self.ledger.db.execute("SELECT payload FROM possible_orders").fetchone()[0]
        import json
        self.assertEqual(json.loads(stored)["instrument_id"],"MSFT")
        for request in requests:
            with self.assertRaisesRegex(LedgerError,"ownership_unknown"):
                self.ledger.reserve_exit(self.exit(request,snapshot,request["intent_id"]+":exit"),attempt_id=request["intent_id"]+":sell",now=f.NOW)

    def test_external_market_conflict_retains_both_market_fences(self):
        us,_=self.fill();kr,_=self.fill("KR",market="kr",instrument="000660")
        order=self.order();self.ledger.observe(self.positions(orders=[order]),now=f.NOW)
        snapshot=self.positions(revision=3,orders=[{**order,"market":"kr","instrument_id":"000660"}])
        self.ledger.observe(snapshot,now=f.NOW)
        for request in (us,kr):
            with self.assertRaisesRegex(LedgerError,"ownership_unknown"):
                self.ledger.reserve_exit(self.exit(request,snapshot,request["intent_id"]+":exit"),attempt_id=request["intent_id"]+":sell",now=f.NOW)

    def test_conflicting_account_A_does_not_block_unrelated_owned_protection(self):
        msft,_=self.fill();aapl,_=self.fill("AAPL",instrument="AAPL")
        amd,_=self.fill("AMD",instrument="AMD");other,_=self.fill("B-MSFT",name="B")
        order=self.order();self.ledger.observe(self.positions(orders=[order]),now=f.NOW)
        a=self.positions(revision=3,orders=[{**order,"instrument_id":"AAPL"}]);self.ledger.observe(a,now=f.NOW)
        # Broker IDs are account-scoped: B's same string on AMD is independent.
        b=self.positions(name="B",orders=[{**order,"instrument_id":"AMD"}]);self.ledger.observe(b,now=f.NOW)
        for request,snapshot in ((amd,a),(other,b)):
            self.ledger.reserve_exit(self.exit(request,snapshot,request["intent_id"]+":exit"),attempt_id=request["intent_id"]+":sell",now=f.NOW)
        for request in (msft,aapl):
            with self.assertRaisesRegex(LedgerError,"ownership_unknown"):
                self.ledger.reserve_exit(self.exit(request,a,request["intent_id"]+":exit"),attempt_id=request["intent_id"]+":sell",now=f.NOW)

    def test_forged_account_refresh_cannot_move_an_external_fence(self):
        request,_=self.fill();order=self.order()
        snapshot=self.positions(orders=[order]);self.ledger.observe(snapshot,now=f.NOW)
        with self.assertRaises(BindingError):
            self.ledger.observe({**self.positions(name="B"),"account_binding_sha256":snapshot["account_binding_sha256"]},now=f.NOW)
        with self.assertRaisesRegex(LedgerError,"ownership_unknown"):
            self.ledger.reserve_exit(self.exit(request,snapshot),attempt_id="sell",now=f.NOW)

    def test_benign_external_identity_repeat_keeps_known_bound_once(self):
        order=self.order(side="buy",instrument="AAPL",qty=2,cost=200_000)
        for revision in (2,3):self.ledger.observe(self.positions(revision=revision,orders=[order]),now=f.NOW)
        self.ledger.observe(self.positions(revision=4),now=f.NOW)
        self.assertEqual(self.ledger.inventory()[("A","us","AAPL")][1:], [2,200_000])
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM possible_orders").fetchone()[0],1)

    def test_matched_buy_bound_dedup_and_partial_fill_transfer_with_fees(self):
        request,proof=self.fill(qty=10,filled=2,fee=100)
        for revision,filled,fee in ((2,2,0),(3,4,40)):
            if filled>2:
                proof=f.entry_receipt(self.p,request,proof["attempt_id"],qty=filled,fee=fee,revision=2)
                self.ledger.entry_receipt(proof)
            bound=(10-filled)*100_000+100-fee
            watermark={proof["attempt_id"]:{"filled_qty":filled,"spent_krw":proof["spent_krw"],"receipt_revision":proof["revision"]}}
            snapshot=self.positions(revision=revision,orders=[self.order(proof["broker_order_id"],side="buy",qty=10-filled,cost=bound)],watermarks=watermark)
            self.ledger.observe(snapshot,now=f.NOW)
            self.assertEqual(self.ledger.inventory()[("A","us","MSFT")][2],1_000_100)
            self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM possible_orders").fetchone()[0],0)
        self.ledger.reserve(f.intent(self.p,"AAPL",instrument="AAPL",qty=4,unit=1_000_000),now=f.NOW)

    def test_matched_buy_lower_or_unknown_bound_cannot_shrink_authority(self):
        _,proof=self.fill(qty=10,filled=2)
        for revision,order in ((2,self.order(proof["broker_order_id"],side="buy",qty=8,cost=700_000)),
                               (3,self.order(proof["broker_order_id"],known=False))):
            with self.assertRaisesRegex(LedgerError,"snapshot_conflict"):
                self.ledger.observe(self.positions(revision=revision,orders=[order]),now=f.NOW)
        self.ledger.observe(self.positions(revision=4),now=f.NOW)
        with self.assertRaisesRegex(LedgerError,"capacity_unknown"):
            self.ledger.reserve(f.intent(self.p,"AAPL",instrument="AAPL"),now=f.NOW)

    def test_matched_buy_coordinate_mismatch_keeps_original_and_reported_fences(self):
        msft,proof=self.fill(qty=10,filled=2);aapl,_=self.fill("AAPL",instrument="AAPL")
        snapshot=self.positions(orders=[self.order(proof["broker_order_id"],instrument="AAPL",side="buy",qty=8,cost=800_000)])
        with self.assertRaisesRegex(LedgerError,"snapshot_conflict"):self.ledger.observe(snapshot,now=f.NOW)
        for request in (msft,aapl):
            with self.assertRaisesRegex(LedgerError,"ownership_unknown"):
                self.ledger.reserve_exit(self.exit(request,snapshot,request["intent_id"]+":exit"),attempt_id=request["intent_id"]+":sell",now=f.NOW)

    def race(self,requests):
        barrier=threading.Barrier(len(requests));outcomes=[]
        def worker(request):
            ledger=self.open()
            try:
                barrier.wait();ledger.reserve(request,now=f.NOW);outcomes.append("won")
            except LedgerError:outcomes.append("blocked")
            except Exception as exc:outcomes.append("ERROR:"+repr(exc))
            finally:ledger.close()
        threads=[threading.Thread(target=worker,args=(r,)) for r in requests]
        for thread in threads:thread.start()
        for thread in threads:thread.join(5);self.assertFalse(thread.is_alive())
        return sorted(outcomes)

    def test_matched_partial_buy_last_cash_budget_claim_is_atomic(self):
        _,proof=self.fill(qty=10,filled=2)
        watermark={proof["attempt_id"]:{"filled_qty":2,"spent_krw":200_000,"receipt_revision":1}}
        snapshot=self.positions(orders=[self.order(proof["broker_order_id"],side="buy",qty=8,cost=800_000)],watermarks=watermark)
        self.ledger.observe(snapshot,now=f.NOW)
        requests=[f.intent(self.p,s,instrument=s,qty=4,unit=1_000_000) for s in ("AAPL","FAKEC")]
        self.assertEqual(self.race(requests),["blocked","won"])

    def test_failed_matched_refresh_blocks_all_later_competing_claims(self):
        _,proof=self.fill(qty=10,filled=2)
        with self.assertRaisesRegex(LedgerError,"snapshot_conflict"):
            self.ledger.observe(self.positions(orders=[self.order(proof["broker_order_id"],known=False)]),now=f.NOW)
        requests=[f.intent(self.p,s,instrument=s,qty=4,unit=1_000_000) for s in ("AAPL","FAKEC")]
        self.assertEqual(self.race(requests),["blocked","blocked"])

    def start_partial_exit(self):
        request,_=self.fill();snapshot=self.positions();self.ledger.observe(snapshot,now=f.NOW)
        allocation=self.ledger.allocation(request["allocation_id"])
        first=f.exit_request(request,allocation,snapshot,"first",qty=2)
        self.ledger.reserve_exit(first,attempt_id="first-sell",now=f.NOW)
        proof=f.exit_receipt(self.p,first,"first-sell",qty=1,allocation_version=allocation["version"],remaining=300_000,terminal=False)
        self.ledger.exit_receipt(proof)
        return request,first,proof

    def test_exact_nonterminal_local_exit_retains_only_its_unfilled_quantity(self):
        request,_,proof=self.start_partial_exit()
        snapshot=self.positions(revision=3,orders=[self.order(proof["broker_order_id"],qty=1)])
        self.ledger.observe(snapshot,now=f.NOW)
        allocation=self.ledger.allocation(request["allocation_id"])
        with self.assertRaisesRegex(LedgerError,"owned_available"):
            self.ledger.reserve_exit(f.exit_request(request,allocation,snapshot,"too-much",qty=3),attempt_id="too-much",now=f.NOW)
        self.ledger.reserve_exit(f.exit_request(request,allocation,snapshot,"remaining",qty=2),attempt_id="remaining",now=f.NOW)
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM possible_orders").fetchone()[0],0)

    def test_unknown_local_exit_refresh_holds_affected_instrument_only(self):
        other,_=self.fill("AAPL",instrument="AAPL")
        request,_,proof=self.start_partial_exit()
        snapshot=self.positions(revision=3,orders=[self.order(proof["broker_order_id"],known=False)])
        with self.assertRaisesRegex(LedgerError,"snapshot_conflict"):self.ledger.observe(snapshot,now=f.NOW)
        with self.assertRaisesRegex(LedgerError,"ownership_unknown"):
            self.ledger.reserve_exit(self.exit(request,snapshot),attempt_id="must-not-sell",now=f.NOW)
        self.ledger.reserve_exit(self.exit(other,snapshot,"unrelated"),attempt_id="unrelated",now=f.NOW)

    def test_mismatched_local_exit_quantity_side_market_or_symbol_stays_unknown(self):
        request,_,proof=self.start_partial_exit()
        order=self.order(proof["broker_order_id"],qty=1)
        changes=({"remaining_qty":2},{"side":"buy"},{"market":"kr"},{"instrument_id":"AAPL"})
        for revision,change in enumerate(changes,3):
            snapshot=self.positions(revision=revision,orders=[{**order,**change}])
            with self.assertRaisesRegex(LedgerError,"snapshot_conflict"):self.ledger.observe(snapshot,now=f.NOW)
            with self.assertRaisesRegex(LedgerError,"ownership_unknown"):
                self.ledger.reserve_exit(self.exit(request,snapshot,str(revision)),attempt_id=str(revision),now=f.NOW)

    def test_imported_uncertainty_survives_exact_local_exit_terminal_and_empty_snapshot(self):
        request,first,proof=self.start_partial_exit()
        snapshot=self.positions(revision=3,orders=[self.order(proof["broker_order_id"],qty=1),self.order("manual-order",known=False)])
        self.ledger.observe(snapshot,now=f.NOW)
        current=self.ledger.allocation(request["allocation_id"])
        self.ledger.exit_receipt(f.exit_receipt(self.p,first,"first-sell",qty=2,allocation_version=current["version"],remaining=200_000,revision=2))
        refreshed=self.positions(revision=4);self.ledger.observe(refreshed,now=f.NOW)
        with self.assertRaisesRegex(LedgerError,"ownership_unknown"):
            self.ledger.reserve_exit(self.exit(request,refreshed,"second"),attempt_id="second-sell",now=f.NOW)

    def test_pending_snapshot_cannot_reopen_an_exact_terminal_local_exit(self):
        request,first,proof=self.start_partial_exit()
        current=self.ledger.allocation(request["allocation_id"])
        self.ledger.exit_receipt(f.exit_receipt(self.p,first,"first-sell",qty=2,allocation_version=current["version"],remaining=200_000,revision=2))
        snapshot=self.positions(revision=3,orders=[self.order(proof["broker_order_id"],qty=1)])
        with self.assertRaisesRegex(LedgerError,"snapshot_conflict"):self.ledger.observe(snapshot,now=f.NOW)
        with self.assertRaisesRegex(LedgerError,"ownership_unknown"):
            self.ledger.reserve_exit(self.exit(request,snapshot),attempt_id="must-not-sell",now=f.NOW)


if __name__=="__main__":unittest.main()
