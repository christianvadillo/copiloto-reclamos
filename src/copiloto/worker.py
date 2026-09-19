"""worker.py — bucle que toma jobs de la cola (tabla `jobs` en `store.py`) y los despacha.

Corre como proceso aparte del webhook (`copiloto worker` vs. `copiloto serve`): el webhook
nunca debe esperar a que un reclamo se procese para responder, y un worker lento o caído no
debe tumbar la recepción de notificaciones. Los dos se coordinan solo a través de SQLite.

`run_once`/`run_until_idle` existen para que los tests corran el worker de forma síncrona y
determinista (sin hilos, sin `sleep`); `run_forever` es lo que usa el proceso real, con apagado
limpio en SIGINT/SIGTERM para no dejar un job a medias.
"""

from __future__ import annotations

import logging
import signal
import time
from collections.abc import Callable
from typing import Any

from copiloto import actions, pipeline
from copiloto.config import Settings
from copiloto.meli.client import MeliClient
from copiloto.store import Job, Store

logger = logging.getLogger(__name__)


def _handle_process_claim(worker: Worker, job: Job) -> None:
    payload = job.payload
    pipeline.process_claim(
        store=worker.store,
        settings=worker.settings,
        meli=worker.meli,
        seller_id=str(payload["seller_id"]),
        claim_id=str(payload["claim_id"]),
        llm_client=worker.llm_client,
    )


def _handle_execute(worker: Worker, job: Job) -> None:
    payload = job.payload
    actions.execute_approval(
        store=worker.store, settings=worker.settings, meli=worker.meli, approval_id=int(payload["approval_id"])
    )


def _handle_reconcile(worker: Worker, job: Job) -> None:
    payload = job.payload or {}
    seller_ids = (
        [str(payload["seller_id"])] if payload.get("seller_id") else [s.id for s in worker.store.list_sellers()]
    )
    for seller_id in seller_ids:
        pipeline.reconcile_seller(store=worker.store, settings=worker.settings, meli=worker.meli, seller_id=seller_id)


def _handle_record_outcome(worker: Worker, job: Job) -> None:
    payload = job.payload
    pipeline.record_outcome(
        store=worker.store, meli=worker.meli, seller_id=str(payload["seller_id"]), claim_id=str(payload["claim_id"])
    )


JOB_HANDLERS: dict[str, Callable[[Worker, Job], None]] = {
    "process_claim": _handle_process_claim,
    "execute": _handle_execute,
    "reconcile": _handle_reconcile,
    "record_outcome": _handle_record_outcome,
}


class Worker:
    """Un solo hilo, una cola: el volumen de reclamos de un vendedor (o unos pocos) no
    justifica más. `meli`/`llm_client` son los mismos objetos inyectables de siempre, así que
    `copiloto demo` y los tests pueden apuntar el worker completo al simulador sin red."""

    def __init__(
        self,
        store: Store,
        settings: Settings,
        meli: MeliClient,
        llm_client: Any | None = None,
        poll_interval: float = 2.0,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.store = store
        self.settings = settings
        self.meli = meli
        self.llm_client = llm_client
        self.poll_interval = poll_interval
        self._sleep = sleep_fn
        self._stop = False

    def run_once(self) -> bool:
        """Procesa un job si hay alguno listo. Devuelve `True` si había uno (corrido o fallado),
        `False` si la cola estaba vacía — así `run_forever` sabe cuándo dormir."""
        job = self.store.claim_job()
        if job is None:
            return False
        handler = JOB_HANDLERS.get(job.kind)
        try:
            if handler is None:
                raise ValueError(f"tipo de job desconocido: {job.kind!r}")
            handler(self, job)
            self.store.complete_job(job.id)
        except Exception as exc:  # una falla del handler es un fallo de ESTE job, no del worker
            logger.exception("job %s (%s) falló", job.id, job.kind)
            self.store.fail_job(job.id, f"{type(exc).__name__}: {exc}")
        return True

    def run_until_idle(self, max_jobs: int = 10_000) -> int:
        """Para tests y `copiloto demo`: corre hasta vaciar la cola. Devuelve cuántos job tomó."""
        n = 0
        while n < max_jobs and self.run_once():
            n += 1
        return n

    def request_stop(self, *_args: object) -> None:
        self._stop = True

    def run_forever(self) -> None:
        self._stop = False
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)
        logger.info("worker arrancó (poll_interval=%.1fs)", self.poll_interval)
        while not self._stop:
            if not self.run_once():
                self._sleep(self.poll_interval)
        logger.info("worker se detuvo limpiamente")
