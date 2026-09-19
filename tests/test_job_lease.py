"""Un job que quedó en `running` porque el proceso murió se vuelve a tomar tras el lease."""

from __future__ import annotations


def test_orphaned_running_job_is_reclaimed_after_lease(store):
    store.enqueue_job("process_claim", {"seller_id": "1", "claim_id": "9"})
    first = store.claim_job()
    assert first is not None
    # Mismo instante: el lease no ha vencido → nadie más lo toma.
    assert store.claim_job(lease_seconds=600) is None
    # Lease vencido (0 s) → se retoma, con un intento más.
    again = store.claim_job(lease_seconds=0)
    assert again is not None and again.id == first.id and again.attempts == first.attempts + 1


def test_orphaned_job_without_attempts_left_is_not_reclaimed(store):
    store.enqueue_job("process_claim", {"seller_id": "1", "claim_id": "9"})
    job = store.claim_job()
    store._conn.execute("UPDATE jobs SET attempts = max_attempts WHERE id = ?", (job.id,))
    assert store.claim_job(lease_seconds=0) is None
