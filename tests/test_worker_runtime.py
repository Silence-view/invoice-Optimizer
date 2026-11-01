"""Worker thread lifecycle, independent of the financial PostgreSQL tests."""
from threading import Event, Thread

from invoice_optimizer import scheduler, worker


def _capture_lifecycle(monkeypatch):
    stopped = Event()
    threads = []
    monkeypatch.setattr(worker, "Event", lambda: stopped)

    def startable_thread(**kwargs):
        thread = Thread(**kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr(worker, "Thread", startable_thread)
    monkeypatch.setattr(worker,"run_job",lambda db,kinds=None:False)
    monkeypatch.setattr(worker,"record_heartbeat",lambda *args,**kwargs:None)
    return stopped, threads


def test_monthly_preparation_does_not_block_continuous_tick(monkeypatch):
    started, release, finished = Event(), Event(), Event()
    stopped, threads = _capture_lifecycle(monkeypatch)
    dispatches = []

    def prepare(db):
        started.set()
        try:
            assert release.wait(5), "Main worker never progressed while monthly work was blocked"
        finally:
            finished.set()

    def tick(db,**kwargs):
        assert started.wait(5)
        assert not finished.is_set()
        release.set()
        raise KeyboardInterrupt

    monkeypatch.setattr(scheduler, "prepare_due_invoices", prepare)
    monkeypatch.setattr(scheduler, "dispatch_approved", lambda db: dispatches.append(db))
    monkeypatch.setattr(worker, "tick", tick)
    database = object()
    try:
        worker.run_forever(database)
    finally:
        release.set()
    assert dispatches == [database]
    assert stopped.is_set() and finished.is_set()
    assert len(threads) == 2 and all(not thread.is_alive() for thread in threads)


def test_monthly_exception_is_logged_without_stopping_main_loop(monkeypatch, caplog):
    attempted = Event()
    stopped, threads = _capture_lifecycle(monkeypatch)
    ticks = []

    def prepare(db):
        attempted.set()
        raise RuntimeError("Synthetic monthly failure")

    def tick(db,**kwargs):
        assert attempted.wait(5)
        ticks.append(db)
        raise KeyboardInterrupt

    monkeypatch.setattr(scheduler, "prepare_due_invoices", prepare)
    monkeypatch.setattr(scheduler, "dispatch_approved", lambda db: None)
    monkeypatch.setattr(worker, "tick", tick)
    worker.run_forever(object())
    assert len(ticks) == 1
    assert "Monthly preparation failed" in caplog.text
    assert stopped.is_set() and not threads[0].is_alive()


def test_main_loop_error_retries_and_interrupt_stops_waiting_scheduler(monkeypatch, caplog):
    prepared = Event()
    stopped, threads = _capture_lifecycle(monkeypatch)
    sleeps, ticks = [], []

    def tick(db,**kwargs):
        assert prepared.wait(5)
        ticks.append(db)
        if len(ticks) == 1:
            raise RuntimeError("Synthetic continuous-worker failure")
        raise KeyboardInterrupt

    monkeypatch.setattr(scheduler, "prepare_due_invoices", lambda db: prepared.set())
    monkeypatch.setattr(scheduler, "dispatch_approved", lambda db: None)
    monkeypatch.setattr(worker, "tick", tick)
    monkeypatch.setattr(worker.time, "sleep", sleeps.append)
    worker.run_forever(object())
    assert len(ticks) == 2 and sleeps == [3]
    assert "Worker loop failed" in caplog.text
    assert stopped.is_set() and not threads[0].is_alive()


def test_training_does_not_block_scoring_or_heartbeat(monkeypatch):
    started,release,finished=Event(),Event(),Event()
    stopped,threads=_capture_lifecycle(monkeypatch)
    heartbeats=[]
    def training(db,kinds=None):
        assert kinds==('TRAIN_MODEL',)
        started.set()
        try:assert release.wait(5),'Scoring did not run while training was occupied'
        finally:finished.set()
        return False
    def continuous(db,**kwargs):
        assert kwargs['job_kinds']==('ASSESS','SCORE','RENDER')
        assert started.wait(5) and not finished.is_set()
        release.set()
        raise KeyboardInterrupt
    monkeypatch.setattr(worker,'run_job',training)
    monkeypatch.setattr(worker,'tick',continuous)
    monkeypatch.setattr(worker,'record_heartbeat',lambda db,identifier,status='RUNNING':heartbeats.append((identifier,status)))
    monkeypatch.setattr(scheduler,'prepare_due_invoices',lambda db:None)
    monkeypatch.setattr(scheduler,'dispatch_approved',lambda db:None)
    try:worker.run_forever(object())
    finally:release.set()
    assert stopped.is_set() and finished.is_set()
    assert all(not thread.is_alive() for thread in threads)
    assert heartbeats[0][1]=='RUNNING' and heartbeats[-1][1]=='STOPPED'
    assert heartbeats[0][0]==heartbeats[-1][0]


def test_idle_worker_heartbeat_persists_without_audit_noise(db):
    from sqlalchemy import select,func
    from invoice_optimizer.models import RuntimeOption,Audit
    worker.record_heartbeat(db,'test-worker')
    worker.record_heartbeat(db,'test-worker')
    with db.transaction() as session:
        heartbeat=session.get(RuntimeOption,'worker.heartbeat').value
        assert heartbeat['worker_id']=='test-worker' and heartbeat['status']=='RUNNING'
        assert heartbeat['at'].endswith('+00:00')
        assert session.scalar(select(func.count()).select_from(Audit))==0
