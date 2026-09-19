"""store.py — persistencia en SQLite (stdlib, sin ORM).

Un solo archivo `.db` en modo WAL es suficiente para un vendedor (o unos pocos): el volumen de
reclamos de una tienda no justifica un servidor de base de datos aparte, y WAL permite que el
proceso web (webhook + dashboard) y el proceso worker lean/escriban a la vez sin bloquearse
mutuamente en las lecturas.

Decisiones de diseño:
- Migraciones con `PRAGMA user_version`: cada versión es un bloque de DDL idempotente
  (`CREATE TABLE IF NOT EXISTS`); aplicar las migraciones dos veces no rompe nada (se prueba).
- La cola de jobs vive en esta misma base (tabla `jobs`): tomar un job es una transacción
  `BEGIN IMMEDIATE` que evita que dos workers (o el mismo worker en dos hilos) tomen el mismo
  trabajo — SQLite serializa escritores entre procesos, así que esto es seguro incluso con
  `webhook` y `worker` corriendo por separado.
- Los tokens OAuth se cifran con Fernet antes de tocar disco y se descifran solo en memoria al
  leerlos; nunca se registran en logs (ver `meli/oauth.py`).
- Todo lo "grande" o de forma variable (snapshot del reclamo, ranking de acciones, razones)
  se guarda como JSON en una columna TEXT: no necesitamos consultarlo por SQL, solo mostrarlo.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from copiloto.decision.priors import Outcome
from copiloto.domain import Category, EvidenceStrength, Recommendation

# ── Cifrado de tokens ───────────────────────────────────────────────────────────────────────


def _fernet(key: str | bytes):
    from cryptography.fernet import Fernet

    return Fernet(key.encode() if isinstance(key, str) else key)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _dumps(obj: Any) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False)


def _loads(raw: str | None) -> Any:
    if raw is None or raw == "":
        return None
    return json.loads(raw)


# ── Registros de dominio (vistas tipadas de las filas) ─────────────────────────────────────


@dataclass(frozen=True)
class Seller:
    id: str
    nickname: str | None
    access_token: str
    refresh_token: str
    token_expires_at: datetime
    needs_reauth: bool


@dataclass(frozen=True)
class Job:
    id: int
    kind: str
    payload: dict
    status: str
    attempts: int
    max_attempts: int
    last_error: str | None


@dataclass(frozen=True)
class Approval:
    id: int
    claim_id: str
    seller_id: str
    recommendation_id: int | None
    draft_id: int | None
    action: str
    params: dict
    decision: str
    edited_message: str | None
    approved_by: str | None
    created_at: str


_MIGRATIONS: tuple[str, ...] = (
    # v1: esquema inicial completo.
    """
    CREATE TABLE IF NOT EXISTS sellers (
        id TEXT PRIMARY KEY,
        nickname TEXT,
        access_token_enc TEXT NOT NULL,
        refresh_token_enc TEXT NOT NULL,
        token_expires_at TEXT NOT NULL,
        needs_reauth INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dedupe_key TEXT NOT NULL UNIQUE,
        topic TEXT NOT NULL,
        resource TEXT NOT NULL,
        claim_id TEXT,
        user_id TEXT,
        application_id TEXT,
        raw_payload TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_notifications_claim ON notifications(claim_id);

    CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        payload TEXT,
        status TEXT NOT NULL DEFAULT 'queued',
        attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 5,
        next_run_at TEXT,
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(status, next_run_at);

    CREATE TABLE IF NOT EXISTS claims (
        claim_id TEXT PRIMARY KEY,
        seller_id TEXT NOT NULL,
        status TEXT,
        stage TEXT,
        type TEXT,
        reason_id TEXT,
        category TEXT,
        confidence REAL,
        amount REAL,
        has_incentive INTEGER,
        affects_reputation TEXT,
        incentive_due_at TEXT,
        action_due_at TEXT,
        evidence_score REAL,
        snapshot TEXT,
        snapshot_hash TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_claims_seller_status ON claims(seller_id, status);

    CREATE TABLE IF NOT EXISTS recommendations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        claim_id TEXT NOT NULL,
        action TEXT NOT NULL,
        params TEXT,
        expected_cost REAL,
        prob_best REAL,
        ranking TEXT,
        rationale TEXT,
        reputation_lambda REAL,
        reputation_headroom INTEGER,
        extra_bad_days REAL,
        requires_approval INTEGER,
        approval_reasons TEXT,
        evidence_score REAL,
        evidence_reasons TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_recommendations_claim ON recommendations(claim_id);

    CREATE TABLE IF NOT EXISTS drafts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        claim_id TEXT NOT NULL,
        recommendation_id INTEGER,
        message TEXT NOT NULL,
        source TEXT NOT NULL,
        model TEXT,
        violations TEXT,
        seller_summary TEXT,
        risks TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_drafts_claim ON drafts(claim_id);

    CREATE TABLE IF NOT EXISTS approvals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        claim_id TEXT NOT NULL,
        seller_id TEXT NOT NULL,
        recommendation_id INTEGER,
        draft_id INTEGER,
        action TEXT NOT NULL,
        params TEXT,
        decision TEXT NOT NULL,
        edited_message TEXT,
        approved_by TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_approvals_claim ON approvals(claim_id);

    CREATE TABLE IF NOT EXISTS executions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        claim_id TEXT NOT NULL,
        action TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL,
        result TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS outcomes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        claim_id TEXT NOT NULL UNIQUE,
        seller_id TEXT NOT NULL,
        category TEXT NOT NULL,
        action TEXT NOT NULL,
        evidence_bucket TEXT NOT NULL,
        pct REAL,
        offer_accepted INTEGER,
        escalated INTEGER,
        mediation_won INTEGER,
        covered INTEGER,
        fulfillment_by_ml INTEGER NOT NULL DEFAULT 0,
        recovery_fraction REAL,
        resolved_without_cost INTEGER,
        affects_reputation_final TEXT,
        resolution TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_outcomes_seller ON outcomes(seller_id);

    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        claim_id TEXT,
        seller_id TEXT,
        kind TEXT NOT NULL,
        payload TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_events_claim ON events(claim_id);
    CREATE INDEX IF NOT EXISTS ix_events_kind ON events(kind);
    """,
)


def _bool_or_none(v: int | None) -> bool | None:
    return None if v is None else bool(v)


class Store:
    """Punto único de acceso a SQLite. Todas las escrituras pasan por `self._lock`: sqlite3
    ya serializa entre procesos (WAL), pero una sola conexión compartida entre hilos dentro
    del mismo proceso (FastAPI con endpoints sync corre en un threadpool) necesita su propio
    candado."""

    def __init__(self, db_path: str, fernet_key: str | bytes) -> None:
        self.db_path = db_path
        self._fernet = _fernet(fernet_key)
        self._lock = threading.RLock()
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self.migrate()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── Migraciones ─────────────────────────────────────────────────────────────────────

    def migrate(self) -> None:
        with self._lock:
            (version,) = self._conn.execute("PRAGMA user_version").fetchone()
            for i in range(version, len(_MIGRATIONS)):
                self._conn.executescript(_MIGRATIONS[i])
                self._conn.execute(f"PRAGMA user_version = {i + 1}")

    # ── Sellers ─────────────────────────────────────────────────────────────────────────

    def upsert_seller(
        self, seller_id: str, nickname: str | None, access_token: str, refresh_token: str, expires_at: datetime
    ) -> None:
        now = _now()
        enc_access = self._fernet.encrypt(access_token.encode()).decode()
        enc_refresh = self._fernet.encrypt(refresh_token.encode()).decode()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO sellers (id, nickname, access_token_enc, refresh_token_enc, token_expires_at,
                                      needs_reauth, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    nickname = excluded.nickname,
                    access_token_enc = excluded.access_token_enc,
                    refresh_token_enc = excluded.refresh_token_enc,
                    token_expires_at = excluded.token_expires_at,
                    needs_reauth = 0,
                    updated_at = excluded.updated_at
                """,
                (seller_id, nickname, enc_access, enc_refresh, expires_at.isoformat(), now, now),
            )

    def update_tokens(self, seller_id: str, access_token: str, refresh_token: str, expires_at: datetime) -> None:
        """Persiste la rotación de tokens. Se llama SIEMPRE antes de devolver el access token
        nuevo al llamador (ver `meli.oauth.TokenProvider.get`): el refresh token es de un solo
        uso y si el proceso muere después de usarlo pero antes de guardarlo, el vendedor queda
        bloqueado hasta re-autorizar."""
        enc_access = self._fernet.encrypt(access_token.encode()).decode()
        enc_refresh = self._fernet.encrypt(refresh_token.encode()).decode()
        with self._lock:
            self._conn.execute(
                """
                UPDATE sellers SET access_token_enc = ?, refresh_token_enc = ?, token_expires_at = ?,
                                    needs_reauth = 0, updated_at = ?
                WHERE id = ?
                """,
                (enc_access, enc_refresh, expires_at.isoformat(), _now(), seller_id),
            )

    def mark_needs_reauth(self, seller_id: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE sellers SET needs_reauth = 1, updated_at = ? WHERE id = ?", (_now(), seller_id))

    def get_seller(self, seller_id: str) -> Seller | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM sellers WHERE id = ?", (seller_id,)).fetchone()
        if row is None:
            return None
        return Seller(
            id=row["id"],
            nickname=row["nickname"],
            access_token=self._fernet.decrypt(row["access_token_enc"].encode()).decode(),
            refresh_token=self._fernet.decrypt(row["refresh_token_enc"].encode()).decode(),
            token_expires_at=datetime.fromisoformat(row["token_expires_at"]),
            needs_reauth=bool(row["needs_reauth"]),
        )

    def list_sellers(self) -> list[Seller]:
        with self._lock:
            rows = self._conn.execute("SELECT id FROM sellers").fetchall()
        out = []
        for row in rows:
            s = self.get_seller(row["id"])
            if s is not None:
                out.append(s)
        return out

    # ── Notificaciones ──────────────────────────────────────────────────────────────────

    def save_notification(
        self, dedupe_key: str, topic: str, resource: str, claim_id: str | None, user_id: str, application_id: str
    ) -> bool:
        """True si la notificación es nueva (hay que encolar trabajo); False si ya la vimos."""
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO notifications (dedupe_key, topic, resource, claim_id, user_id, application_id,
                                                created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (dedupe_key, topic, resource, claim_id, user_id, application_id, _now()),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def notification_to_draft_latencies_seconds(self) -> list[float]:
        """Segundos entre la primera notificación vista de cada reclamo y su primer borrador.
        Insumo de `copiloto stats` (p50/p90)."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT n.claim_id AS claim_id, MIN(n.created_at) AS notified_at, MIN(d.created_at) AS drafted_at
                FROM notifications n
                JOIN drafts d ON d.claim_id = n.claim_id
                WHERE n.claim_id IS NOT NULL
                GROUP BY n.claim_id
                """
            ).fetchall()
        out = []
        for row in rows:
            try:
                dt_n = datetime.fromisoformat(row["notified_at"])
                dt_d = datetime.fromisoformat(row["drafted_at"])
            except (TypeError, ValueError):
                continue
            out.append(max(0.0, (dt_d - dt_n).total_seconds()))
        return out

    # ── Jobs ────────────────────────────────────────────────────────────────────────────

    def enqueue_job(self, kind: str, payload: dict, next_run_at: datetime | None = None, max_attempts: int = 5) -> int:
        now = _now()
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO jobs (kind, payload, status, attempts, max_attempts, next_run_at, created_at, updated_at)
                VALUES (?, ?, 'queued', 0, ?, ?, ?, ?)
                """,
                (kind, _dumps(payload), max_attempts, next_run_at.isoformat() if next_run_at else None, now, now),
            )
            return int(cur.lastrowid)

    def claim_job(self) -> Job | None:
        """Toma atómicamente el job más antiguo listo para correr (BEGIN IMMEDIATE: adquiere
        el lock de escritura antes de decidir cuál tomar, así dos workers nunca chocan)."""
        now = _now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    """
                    SELECT * FROM jobs
                    WHERE status = 'queued' AND (next_run_at IS NULL OR next_run_at <= ?)
                    ORDER BY id LIMIT 1
                    """,
                    (now,),
                ).fetchone()
                if row is None:
                    self._conn.execute("COMMIT")
                    return None
                self._conn.execute(
                    "UPDATE jobs SET status = 'running', attempts = attempts + 1, updated_at = ? WHERE id = ?",
                    (now, row["id"]),
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return Job(
            id=row["id"],
            kind=row["kind"],
            payload=_loads(row["payload"]) or {},
            status="running",
            attempts=row["attempts"] + 1,
            max_attempts=row["max_attempts"],
            last_error=row["last_error"],
        )

    def complete_job(self, job_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE jobs SET status = 'done', updated_at = ? WHERE id = ?", (_now(), job_id))

    def fail_job(self, job_id: int, error: str) -> None:
        """Reintento con backoff exponencial (tope 5 min); agotados los intentos → `dead`."""
        with self._lock:
            row = self._conn.execute("SELECT attempts, max_attempts FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return
            if row["attempts"] >= row["max_attempts"]:
                self._conn.execute(
                    "UPDATE jobs SET status = 'dead', last_error = ?, updated_at = ? WHERE id = ?",
                    (error[:2000], _now(), job_id),
                )
                return
            backoff = min(300.0, 2.0 ** row["attempts"])
            next_run = datetime.now(UTC).timestamp() + backoff
            self._conn.execute(
                "UPDATE jobs SET status = 'queued', last_error = ?, next_run_at = ?, updated_at = ? WHERE id = ?",
                (error[:2000], datetime.fromtimestamp(next_run, tz=UTC).isoformat(), _now(), job_id),
            )

    def job_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
        return {row["status"]: row["n"] for row in rows}

    # ── Claims ──────────────────────────────────────────────────────────────────────────

    def save_claim(
        self,
        claim_id: str,
        seller_id: str,
        status: str | None,
        stage: str | None,
        type_: str | None,
        reason_id: str | None,
        category: str,
        confidence: float,
        amount: float,
        has_incentive: bool,
        affects_reputation: str,
        incentive_due_at: str | None,
        action_due_at: str | None,
        evidence_score: float,
        snapshot: dict,
        snapshot_hash: str,
    ) -> None:
        now = _now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO claims (claim_id, seller_id, status, stage, type, reason_id, category, confidence,
                                     amount, has_incentive, affects_reputation, incentive_due_at, action_due_at,
                                     evidence_score, snapshot, snapshot_hash, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(claim_id) DO UPDATE SET
                    status = excluded.status, stage = excluded.stage, type = excluded.type,
                    reason_id = excluded.reason_id, category = excluded.category, confidence = excluded.confidence,
                    amount = excluded.amount, has_incentive = excluded.has_incentive,
                    affects_reputation = excluded.affects_reputation, incentive_due_at = excluded.incentive_due_at,
                    action_due_at = excluded.action_due_at, evidence_score = excluded.evidence_score,
                    snapshot = excluded.snapshot, snapshot_hash = excluded.snapshot_hash,
                    updated_at = excluded.updated_at
                """,
                (
                    claim_id,
                    seller_id,
                    status,
                    stage,
                    type_,
                    reason_id,
                    category,
                    confidence,
                    amount,
                    int(has_incentive),
                    affects_reputation,
                    incentive_due_at,
                    action_due_at,
                    evidence_score,
                    _dumps(snapshot),
                    snapshot_hash,
                    now,
                    now,
                ),
            )

    def get_claim_row(self, claim_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM claims WHERE claim_id = ?", (claim_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["snapshot"] = _loads(d["snapshot"])
        d["has_incentive"] = bool(d["has_incentive"])
        return d

    def list_open_claims(self, seller_id: str | None = None) -> list[dict]:
        q = "SELECT * FROM claims WHERE status != 'closed' OR status IS NULL"
        params: list[Any] = []
        if seller_id is not None:
            q += " AND seller_id = ?"
            params.append(seller_id)
        q += " ORDER BY COALESCE(incentive_due_at, action_due_at, '9999') ASC"
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["snapshot"] = _loads(d["snapshot"])
            d["has_incentive"] = bool(d["has_incentive"])
            out.append(d)
        return out

    def list_claims(self, seller_id: str | None = None) -> list[dict]:
        q = "SELECT * FROM claims"
        params: list[Any] = []
        if seller_id is not None:
            q += " WHERE seller_id = ?"
            params.append(seller_id)
        q += " ORDER BY updated_at DESC"
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["snapshot"] = _loads(d["snapshot"])
            d["has_incentive"] = bool(d["has_incentive"])
            out.append(d)
        return out

    # ── Recomendaciones ─────────────────────────────────────────────────────────────────

    def save_recommendation(self, claim_id: str, rec: Recommendation) -> int:
        ranking = [
            {
                "action": e.action.value,
                "expected_cost": e.expected_cost,
                "cost_p05": e.cost_p05,
                "cost_p95": e.cost_p95,
                "prob_best": e.prob_best,
                "params": e.params,
                "escalation_prob": e.escalation_prob,
            }
            for e in rec.ranking
        ]
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO recommendations (claim_id, action, params, expected_cost, prob_best, ranking,
                                              rationale, reputation_lambda, reputation_headroom, extra_bad_days,
                                              requires_approval, approval_reasons, evidence_score, evidence_reasons,
                                              created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    claim_id,
                    rec.action.value,
                    _dumps(rec.params),
                    rec.expected_cost,
                    rec.prob_best,
                    _dumps(ranking),
                    _dumps(list(rec.rationale)),
                    rec.reputation_lambda,
                    rec.reputation_headroom,
                    rec.extra_bad_days,
                    int(rec.requires_approval),
                    _dumps(list(rec.approval_reasons)),
                    None,
                    None,
                    (rec.created_at or datetime.now(UTC)).isoformat(),
                ),
            )
            return int(cur.lastrowid)

    def get_latest_recommendation(self, claim_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM recommendations WHERE claim_id = ? ORDER BY id DESC LIMIT 1", (claim_id,)
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        for key in ("params", "ranking", "rationale", "approval_reasons"):
            d[key] = _loads(d[key])
        d["requires_approval"] = bool(d["requires_approval"])
        return d

    # ── Borradores ──────────────────────────────────────────────────────────────────────

    def save_draft(
        self,
        claim_id: str,
        recommendation_id: int | None,
        message: str,
        source: str,
        model: str | None,
        violations: Iterable[str],
        seller_summary: str,
        risks: Iterable[str],
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO drafts (claim_id, recommendation_id, message, source, model, violations,
                                     seller_summary, risks, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    claim_id,
                    recommendation_id,
                    message,
                    source,
                    model,
                    _dumps(list(violations)),
                    seller_summary,
                    _dumps(list(risks)),
                    _now(),
                ),
            )
            return int(cur.lastrowid)

    def get_latest_draft(self, claim_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM drafts WHERE claim_id = ? ORDER BY id DESC LIMIT 1", (claim_id,)
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["violations"] = _loads(d["violations"])
        d["risks"] = _loads(d["risks"])
        return d

    # ── Aprobaciones ────────────────────────────────────────────────────────────────────

    def create_approval(
        self,
        claim_id: str,
        seller_id: str,
        recommendation_id: int | None,
        draft_id: int | None,
        action: str,
        params: dict,
        decision: str,
        edited_message: str | None,
        approved_by: str | None,
    ) -> int:
        now = _now()
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO approvals (claim_id, seller_id, recommendation_id, draft_id, action, params,
                                        decision, edited_message, approved_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    claim_id,
                    seller_id,
                    recommendation_id,
                    draft_id,
                    action,
                    _dumps(params),
                    decision,
                    edited_message,
                    approved_by,
                    now,
                ),
            )
            return int(cur.lastrowid)

    def get_approval(self, approval_id: int) -> Approval | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            return None
        return Approval(
            id=row["id"],
            claim_id=row["claim_id"],
            seller_id=row["seller_id"],
            recommendation_id=row["recommendation_id"],
            draft_id=row["draft_id"],
            action=row["action"],
            params=_loads(row["params"]) or {},
            decision=row["decision"],
            edited_message=row["edited_message"],
            approved_by=row["approved_by"],
            created_at=row["created_at"],
        )

    # ── Ejecuciones (idempotencia) ──────────────────────────────────────────────────────

    def execution_exists(self, idempotency_key: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM executions WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        return row is not None

    def get_latest_execution(self, claim_id: str) -> dict | None:
        """Para `pipeline._maybe_record_outcome`: el `Outcome` debe reflejar lo que de verdad
        se EJECUTÓ (un humano pudo aprobar algo distinto a lo recomendado), no la recomendación."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM executions WHERE claim_id = ? ORDER BY id DESC LIMIT 1", (claim_id,)
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["result"] = _loads(d["result"])
        return d

    def begin_execution(self, idempotency_key: str, claim_id: str, action: str, result: dict) -> dict | None:
        """Reserva la llave en estado `pending` ANTES de tocar Mercado Libre. `None` = reservada
        por esta llamada (proceder); si ya existía devuelve la fila (status + result) para que el
        llamador decida: `done` → duplicado, `pending` → en duda, `failed` → reanudar."""
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO executions (claim_id, action, idempotency_key, status, result, created_at)
                    VALUES (?, ?, ?, 'pending', ?, ?)
                    """,
                    (claim_id, action, idempotency_key, _dumps(result), _now()),
                )
                return None
            except sqlite3.IntegrityError:
                row = self._conn.execute(
                    "SELECT * FROM executions WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
        d = dict(row)
        d["result"] = _loads(d["result"]) or {}
        return d

    def update_execution(self, idempotency_key: str, status: str, result: dict) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE executions SET status = ?, result = ? WHERE idempotency_key = ?",
                (status, _dumps(result), idempotency_key),
            )

    def record_execution(self, idempotency_key: str, claim_id: str, action: str, status: str, result: dict) -> bool:
        """True si quedó registrada; False si ya existía (otra ejecución ganó la carrera)."""
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO executions (claim_id, action, idempotency_key, status, result, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (claim_id, action, idempotency_key, status, _dumps(result), _now()),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    # ── Outcomes (aprendizaje) ──────────────────────────────────────────────────────────

    def save_outcome(
        self,
        claim_id: str,
        seller_id: str,
        outcome: Outcome,
        affects_reputation_final: str | None,
        resolution: str | None,
    ) -> None:
        """Upsert por `claim_id`: reprocesar el mismo reclamo cerrado no duplica el conteo."""
        now = _now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO outcomes (claim_id, seller_id, category, action, evidence_bucket, pct,
                                       offer_accepted, escalated, mediation_won, covered, fulfillment_by_ml,
                                       recovery_fraction, resolved_without_cost, affects_reputation_final,
                                       resolution, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(claim_id) DO UPDATE SET
                    category = excluded.category, action = excluded.action,
                    evidence_bucket = excluded.evidence_bucket, pct = excluded.pct,
                    offer_accepted = excluded.offer_accepted, escalated = excluded.escalated,
                    mediation_won = excluded.mediation_won, covered = excluded.covered,
                    fulfillment_by_ml = excluded.fulfillment_by_ml, recovery_fraction = excluded.recovery_fraction,
                    resolved_without_cost = excluded.resolved_without_cost,
                    affects_reputation_final = excluded.affects_reputation_final, resolution = excluded.resolution
                """,
                (
                    claim_id,
                    seller_id,
                    outcome.category.value,
                    outcome.action.value,
                    outcome.evidence_bucket.value,
                    outcome.pct,
                    None if outcome.offer_accepted is None else int(outcome.offer_accepted),
                    None if outcome.escalated is None else int(outcome.escalated),
                    None if outcome.mediation_won is None else int(outcome.mediation_won),
                    None if outcome.covered is None else int(outcome.covered),
                    int(outcome.fulfillment_by_ml),
                    outcome.recovery_fraction,
                    None if outcome.resolved_without_cost is None else int(outcome.resolved_without_cost),
                    affects_reputation_final,
                    resolution,
                    now,
                ),
            )

    def outcomes_for_seller(self, seller_id: str) -> list[Outcome]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM outcomes WHERE seller_id = ?", (seller_id,)).fetchall()
        out = []
        for row in rows:
            out.append(
                Outcome(
                    category=Category(row["category"]),
                    action=_action_from_value(row["action"]),
                    evidence_bucket=EvidenceStrength(row["evidence_bucket"]),
                    pct=row["pct"],
                    offer_accepted=_bool_or_none(row["offer_accepted"]),
                    escalated=_bool_or_none(row["escalated"]),
                    mediation_won=_bool_or_none(row["mediation_won"]),
                    covered=_bool_or_none(row["covered"]),
                    fulfillment_by_ml=bool(row["fulfillment_by_ml"]),
                    recovery_fraction=row["recovery_fraction"],
                    resolved_without_cost=_bool_or_none(row["resolved_without_cost"]),
                )
            )
        return out

    def outcome_exists(self, claim_id: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM outcomes WHERE claim_id = ?", (claim_id,)).fetchone()
        return row is not None

    # ── Eventos (auditoría) ─────────────────────────────────────────────────────────────

    def add_event(self, claim_id: str | None, seller_id: str | None, kind: str, payload: dict | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (claim_id, seller_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (claim_id, seller_id, kind, _dumps(payload or {}), _now()),
            )

    def list_events(self, claim_id: str | None = None, kind: str | None = None, limit: int = 200) -> list[dict]:
        q = "SELECT * FROM events WHERE 1=1"
        params: list[Any] = []
        if claim_id is not None:
            q += " AND claim_id = ?"
            params.append(claim_id)
        if kind is not None:
            q += " AND kind = ?"
            params.append(kind)
        q += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["payload"] = _loads(d["payload"])
            out.append(d)
        return out

    def event_kind_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT kind, COUNT(*) AS n FROM events GROUP BY kind").fetchall()
        return {row["kind"]: row["n"] for row in rows}


def _action_from_value(value: str):
    from copiloto.domain import Action

    return Action(value)


__all__ = ["Store", "Seller", "Job", "Approval"]
