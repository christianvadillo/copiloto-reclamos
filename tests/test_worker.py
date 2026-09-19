"""test_worker.py — el bucle de jobs: drena la cola, un job desconocido no tumba al worker,
y agotados los reintentos el job queda `dead` en vez de reintentarse para siempre."""

from __future__ import annotations

from copiloto.worker import Worker


def test_run_until_idle_drains_all_queued_jobs(store, settings, meli, fake):
    _fake_client, state = fake
    for claim_id in list(state.claims)[:3]:
        store.enqueue_job("process_claim", {"seller_id": state.seller_id, "claim_id": claim_id})

    worker = Worker(store, settings, meli, llm_client=None)
    n = worker.run_until_idle()

    assert n == 3
    assert store.job_counts() == {"done": 3}
    assert worker.run_once() is False  # la cola ya está vacía


def test_unknown_job_kind_does_not_crash_the_worker(store, settings, meli):
    job_id = store.enqueue_job("kind_que_no_existe", {}, max_attempts=1)
    worker = Worker(store, settings, meli, llm_client=None)

    handled = worker.run_once()

    assert handled is True
    assert store.job_counts() == {"dead": 1}
    row = store.claim_job()  # no debe volver a aparecer como 'queued'
    assert row is None
    del job_id


def test_failed_job_retries_then_goes_dead(store, settings, meli, monkeypatch):
    import copiloto.worker as worker_module

    def _always_fails(_worker, _job):
        raise RuntimeError("falla a propósito")

    monkeypatch.setitem(worker_module.JOB_HANDLERS, "process_claim", _always_fails)
    store.enqueue_job("process_claim", {"seller_id": "x", "claim_id": "y"}, max_attempts=2)
    worker = Worker(store, settings, meli, llm_client=None, sleep_fn=lambda _s: None)

    worker.run_once()  # intento 1: falla, vuelve a 'queued' con backoff
    assert store.job_counts() == {"queued": 1}

    # Forzamos que el backoff ya haya pasado para no dormir en el test.
    with store._lock:
        store._conn.execute("UPDATE jobs SET next_run_at = NULL")
    worker.run_once()  # intento 2: falla de nuevo, agotó max_attempts → 'dead'

    assert store.job_counts() == {"dead": 1}
