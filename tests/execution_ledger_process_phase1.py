"""Spawned SQLite fixture workers; no production imports, credentials or broker.

Run explicitly in a credential-cleared CI step. Its filename excludes it from
ordinary discovery under the unchanged main offline launcher, which forbids
process creation. Worker guards prohibit networking, credential reads,
additional subprocesses and database/filesystem writes outside its fixture root.
"""
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import unittest

from trade_safety_kit.execution_ledger import Ledger, LedgerError
from trade_safety_kit import execution_binding as binding
from trade_safety_kit.execution_gateway import Gateway, GatewayError, FixtureEvidenceCAS, CLAIM_NAMESPACE
from tests import execution_phase1_fixtures as f
from tests import execution_gateway_fixtures as g


def _guard_worker(root):
    root=Path(root).resolve()
    read_roots=(root,Path(__file__).resolve().parents[1],Path(sys.prefix).resolve(),Path(sys.base_prefix).resolve())
    def audit(event,values):
        if event in {"socket.connect","socket.bind","socket.getaddrinfo","socket.sendto","socket.sendmsg","subprocess.Popen","os.system","os.fork","os.exec","os.posix_spawn"}:
            raise RuntimeError("fixture worker prohibited external execution/network")
        if event=="sqlite3.connect":
            if not Path(values[0]).resolve().is_relative_to(root):raise RuntimeError("fixture database root required")
        if event=="open" and isinstance(values[0],(str,bytes,os.PathLike)):
            path=Path(os.fsdecode(values[0])).resolve();mode,flags=values[1:3]
            if path.name==".env":raise RuntimeError("fixture credential read prohibited")
            write=(isinstance(mode,str) and any(x in mode for x in "wax+")) or bool((flags or 0)&(os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC))
            if write and not path.is_relative_to(root):raise RuntimeError("fixture external write prohibited")
            if not write and not any(path.is_relative_to(r) for r in read_roots):raise RuntimeError("fixture external read prohibited")
        if event in {"os.remove","os.rmdir","os.mkdir","os.rename","os.link","os.symlink"}:
            for value in values[:2] if event in {"os.rename","os.link","os.symlink"} else values[:1]:
                if isinstance(value,(str,bytes,os.PathLike)) and not Path(os.fsdecode(value)).resolve().is_relative_to(root):
                    raise RuntimeError("fixture external mutation prohibited")
    sys.addaudithook(audit)


def _worker(path,policy,accounts,action,arg,ready,start,result):
    _guard_worker(Path(path).parent)
    ledger=Ledger(path,policy=policy,accounts=accounts,enabled=True)
    try:
        ready.put(True)
        if not start.wait(10):raise RuntimeError("fixture rendezvous timeout")
        if action=="reserve":ledger.reserve(arg,now=f.NOW)
        elif action=="exit":ledger.reserve_exit(arg,attempt_id=arg["exit_id"]+":attempt",now=f.NOW)
        elif action=="crash":
            ledger.begin_entry(arg,attempt_id="crashed-attempt",now=f.NOW)
            os._exit(23)  # Crash after durable claim, before any adapter could run.
        elif action in {"gateway", "gateway-crash-before", "gateway-crash-after"}:
            gateway=Gateway(ledger,enabled=True)
            kwargs=dict(expected_payload_hash=binding.digest(arg),cas_join=FixtureEvidenceCAS(),now=f.NOW)
            if action=="gateway-crash-before":
                with ledger.atomic() as transaction:
                    gateway.prepare_in_transaction(arg,transaction=transaction,**kwargs)
                    os._exit(23)  # Actual process death before the single commit.
            gateway.prepare(arg,**kwargs)
            if action=="gateway-crash-after":os._exit(23)
        else:raise RuntimeError("fixture action unknown")
        result.put("won")
    except (LedgerError,GatewayError,binding.BindingError):result.put("blocked")
    except Exception as exc:result.put("ERROR:"+type(exc).__name__+":"+str(exc))
    finally:ledger.close()


class Phase1ProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/"fixture.sqlite";self.p=f.policy();self.accounts=[f.account()]
        self.ledger=self.open();self.addCleanup(lambda:self.ledger.close())
        self.ledger.observe(f.snapshot(self.p),now=f.NOW)

    def open(self):return Ledger(self.path,policy=self.p,accounts=self.accounts,enabled=True)

    def run_workers(self,action,args,*,crash=False,before_release=None):
        ctx=multiprocessing.get_context("spawn");ready=ctx.Queue();result=ctx.Queue();start=ctx.Event()
        workers=[ctx.Process(target=_worker,args=(str(self.path),self.p,self.accounts,action,arg,ready,start,result)) for arg in args]
        try:
            for worker in workers:worker.start()
            for _ in workers:self.assertTrue(ready.get(timeout=10))
            if before_release is not None:before_release()
            start.set()
            outcomes=[] if crash else [result.get(timeout=10) for _ in workers]
            for worker in workers:
                worker.join(10);self.assertFalse(worker.is_alive());self.assertEqual(worker.exitcode,23 if crash else 0)
            self.assertFalse(any(x.startswith("ERROR:") for x in outcomes),outcomes)
            return outcomes
        finally:
            for worker in workers:
                if worker.is_alive():worker.terminate();worker.join(5)
            ready.close();ready.join_thread();result.close();result.join_thread()

    def test_competing_processes_claim_one_opportunity(self):
        requests=[f.intent(self.p,str(i),owner=owner,opportunity="same-original-fact") for i,owner in enumerate(("t1-B","t1-C","scanner","user-buy","tripod"))]
        outcomes=self.run_workers("reserve",requests)
        self.assertEqual(outcomes.count("won"),1);self.assertEqual(outcomes.count("blocked"),4)
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM entries").fetchone()[0],1)

    def test_last_supplied_capacity_slot_is_atomic_across_processes(self):
        self.ledger.close();self.path=Path(self.tmp.name)/"capacity.sqlite";self.p=f.policy(capacity=1)
        self.ledger=self.open();self.ledger.observe(f.snapshot(self.p),now=f.NOW)
        outcomes=self.run_workers("reserve",[f.intent(self.p,"one",instrument="MSFT"),f.intent(self.p,"two",instrument="AAPL")])
        self.assertEqual(sorted(outcomes),["blocked","won"])

    def test_crashed_owner_cannot_release_or_resubmit(self):
        request=f.intent(self.p);self.ledger.reserve(request,now=f.NOW)
        self.run_workers("crash",["one"],crash=True)
        restarted=self.open();self.addCleanup(restarted.close)
        with self.assertRaises(LedgerError):restarted.begin_entry("one",attempt_id="retry",now=f.NOW)
        with self.assertRaises(LedgerError):restarted.cancel_unsent("one")
        with self.assertRaises(LedgerError):restarted.reserve(f.intent(self.p,"replacement"),now=f.NOW)
        self.assertEqual(restarted.db.execute("SELECT state FROM entries").fetchone()[0],"SUBMITTING")

    def test_competing_exit_processes_reserve_only_owned_quantity(self):
        request=f.intent(self.p,qty=4);self.ledger.reserve(request,now=f.NOW)
        self.ledger.begin_entry("one",attempt_id="entry",now=f.NOW)
        self.ledger.entry_receipt(f.entry_receipt(self.p,request,"entry",qty=4,terminal=True))
        allocation=self.ledger.allocation(request["allocation_id"])
        snap=f.snapshot(self.p,revision=2,holdings=[f.holding("MSFT",qty=20,exposure=2_000_000,allocations=[allocation])])
        self.ledger.observe(snap,now=f.NOW)
        requests=[f.exit_request(request,allocation,snap,f"exit-{i}",qty=3) for i in range(2)]
        self.assertEqual(sorted(self.run_workers("exit",requests)),["blocked","won"])

    def partial_buy(self):
        request=f.intent(self.p,qty=10);self.ledger.reserve(request,now=f.NOW)
        self.ledger.begin_entry("one",attempt_id="partial-buy",now=f.NOW)
        proof=f.entry_receipt(self.p,request,"partial-buy",qty=2)
        self.ledger.entry_receipt(proof)
        allocation=self.ledger.allocation(request["allocation_id"])
        snapshot=f.snapshot(self.p,revision=2,cash=5_000_000,
            holdings=[f.holding("MSFT",qty=2,exposure=200_000,allocations=[allocation])],
            watermarks={"partial-buy":{"filled_qty":2,"spent_krw":200_000,"receipt_revision":1}})
        order=dict(order_id=proof["broker_order_id"],market="us",instrument_id="MSFT",asset_class="stock",side="buy",known=True,remaining_qty=8,remaining_max_krw=800_000)
        return snapshot,order

    def test_matched_partial_buy_last_cash_budget_is_atomic_across_processes(self):
        snapshot,order=self.partial_buy();self.ledger.observe({**snapshot,"orders":[order]},now=f.NOW)
        requests=[f.intent(self.p,s,instrument=s,qty=4,unit=1_000_000) for s in ("AAPL","FAKEC")]
        self.assertEqual(sorted(self.run_workers("reserve",requests)),["blocked","won"])

    def test_preopened_workers_see_uncertainty_committed_despite_refresh_error(self):
        snapshot,order=self.partial_buy()
        self.ledger.observe({**snapshot,"orders":[order]},now=f.NOW)
        def rejected_refresh():
            unknown={**order,"known":False,"asset_class":None,"side":None,"remaining_qty":None,"remaining_max_krw":None}
            with self.assertRaisesRegex(LedgerError,"snapshot_conflict"):
                self.ledger.observe({**snapshot,"snapshot_id":"unknown-newer","revision":3,"orders":[unknown]},now=f.NOW)
        requests=[f.intent(self.p,s,instrument=s,qty=4,unit=1_000_000) for s in ("AAPL","FAKEC")]
        self.assertEqual(self.run_workers("reserve",requests,before_release=rejected_refresh),["blocked","blocked"])
        restarted=self.open();self.addCleanup(restarted.close)
        with self.assertRaisesRegex(LedgerError,"capacity_unknown"):
            restarted.reserve(f.intent(self.p,"restart",instrument="AMD"),now=f.NOW)

    def gateway_request(self, request, route):
        value=g.payload(self.ledger,request,route=route,variant=route)
        Gateway(self.ledger,enabled=True).record_fixture_evidence(value["evidence"])
        return value

    def gateway_counts(self):
        return tuple(self.ledger.db.execute("SELECT count(*) FROM "+table).fetchone()[0] for table in ("entries","gateway_preparations"))+(self.ledger.db.execute("SELECT count(*) FROM shared_heads WHERE namespace=?",(CLAIM_NAMESPACE,)).fetchone()[0],)

    def test_gateway_distinct_routes_and_B_C_plans_cannot_double_claim(self):
        routes=("fixture-t1-B","fixture-t1-C","fixture-scanner","fixture-user-buy","fixture-tripod")
        requests=[self.gateway_request(f.intent(self.p,str(i),owner=route,opportunity="same-original-fact"),route) for i,route in enumerate(routes)]
        outcomes=self.run_workers("gateway",requests)
        self.assertEqual(outcomes.count("won"),1);self.assertEqual(outcomes.count("blocked"),4)
        self.assertEqual(self.gateway_counts(),(1,1,1))

    def test_gateway_last_supplied_cash_budget_has_one_process_winner(self):
        self.ledger.observe(f.snapshot(self.p,revision=2,cash=5_000_000),now=f.NOW)
        requests=[self.gateway_request(f.intent(self.p,s,instrument=s,qty=4,unit=1_000_000),"fixture-"+s) for s in ("AAPL","FAKEC")]
        self.assertEqual(sorted(self.run_workers("gateway",requests)),["blocked","won"])
        self.assertEqual(self.gateway_counts(),(1,1,1))

    def test_gateway_last_supplied_capacity_slot_has_one_process_winner(self):
        self.ledger.close();self.path=Path(self.tmp.name)/"gateway-capacity.sqlite";self.p=f.policy(capacity=1)
        self.ledger=self.open();self.ledger.observe(f.snapshot(self.p),now=f.NOW)
        requests=[self.gateway_request(f.intent(self.p,s,instrument=s),"fixture-"+s) for s in ("AAPL","FAKEC")]
        self.assertEqual(sorted(self.run_workers("gateway",requests)),["blocked","won"])
        self.assertEqual(self.gateway_counts(),(1,1,1))

    def test_gateway_same_payload_recovery_does_not_create_second_attempt(self):
        value=self.gateway_request(f.intent(self.p),"fixture-user-buy")
        self.assertEqual(self.run_workers("gateway",[value,value]),["won","won"])
        self.assertEqual(self.gateway_counts(),(1,1,1))
        self.assertEqual(self.ledger.db.execute("SELECT attempt_id,state FROM entries").fetchone()[:],(value["attempt_id"],"SUBMITTING"))

    def test_gateway_crash_before_commit_rolls_back_all_positive_claims(self):
        value=self.gateway_request(f.intent(self.p),"fixture-t1-B")
        self.run_workers("gateway-crash-before",[value],crash=True)
        self.assertEqual(self.gateway_counts(),(0,0,0))
        # The previously committed evidence observation survives the lost claim.
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM shared_heads").fetchone()[0],1)
        self.assertEqual(self.run_workers("gateway",[value]),["won"])

    def test_gateway_crash_after_commit_keeps_SUBMITTING_and_never_retries(self):
        value=self.gateway_request(f.intent(self.p),"fixture-t1-B")
        self.run_workers("gateway-crash-after",[value],crash=True)
        restarted=self.open();self.addCleanup(restarted.close)
        with self.assertRaises(LedgerError):restarted.begin_entry("one",attempt_id="retry",now=f.NOW)
        with self.assertRaises(LedgerError):restarted.cancel_unsent("one")
        self.assertEqual(self.run_workers("gateway",[value]),["won"])
        self.assertEqual(self.gateway_counts(),(1,1,1))


if __name__=="__main__":unittest.main()
