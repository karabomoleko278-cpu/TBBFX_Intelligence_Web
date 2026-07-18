"""
Local state database layer for the TBBFX Centralized Feature Factory.

Uses Python's stdlib ``sqlite3`` (zero extra dependencies, completely free) so
historical training data and previously-calibrated SVI volatility parameters are
durably preserved across restarts of the engine.

Tables
------
svi_parameters   : every SVI smile calibration (a, b, rho, m, sigma + arb flags)
gex_snapshots    : net GEX / gamma-flip readings produced by OptionsExposureEngine
training_samples : (GEX, Vanna, CVD, OBI, Skew) feature vectors + forward returns
                   consumed by the ExecutionOptimizer ML core
optimization_runs: persisted output of each optimizer run (weights, win rate, ...)

The class is safe to share between the FastAPI event loop and the background
stream/scan tasks: ``check_same_thread=False`` plus a single write lock.
"""

import hashlib
import os
import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np


class StateDatabase:
    """Durable SQLite-backed store for training data and volatility parameters."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        # Make sure the parent directory exists before sqlite tries to open it.
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)

        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL improves concurrency between the writer (streams) and readers (API).
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._init_schema()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------
    def _init_schema(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS svi_parameters (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker    TEXT    NOT NULL,
                    ts        REAL    NOT NULL,
                    a         REAL, b REAL, rho REAL, m REAL, sigma REAL,
                    dte       REAL,
                    forward   REAL,
                    arb_free  INTEGER,
                    success   INTEGER
                );
                CREATE INDEX IF NOT EXISTS ix_svi_ticker_ts
                    ON svi_parameters (ticker, ts DESC);

                CREATE TABLE IF NOT EXISTS gex_snapshots (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker     TEXT  NOT NULL,
                    ts         REAL  NOT NULL,
                    spot       REAL,
                    net_gex    REAL,
                    gamma_flip REAL,
                    regime     TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_gex_ticker_ts
                    ON gex_snapshots (ticker, ts DESC);

                CREATE TABLE IF NOT EXISTS training_samples (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker     TEXT  NOT NULL,
                    ts         REAL  NOT NULL,
                    gex        REAL, vanna REAL, cvd REAL, obi REAL, skew REAL,
                    fwd_return REAL
                );
                CREATE INDEX IF NOT EXISTS ix_train_ticker_ts
                    ON training_samples (ticker, ts);

                CREATE TABLE IF NOT EXISTS optimization_runs (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts          REAL NOT NULL,
                    weights     TEXT,
                    threshold   REAL,
                    win_rate    REAL,
                    profit      REAL,
                    success     INTEGER,
                    message     TEXT
                );

                CREATE TABLE IF NOT EXISTS governance_audit_trail (
                    transaction_id            TEXT PRIMARY KEY,
                    timestamp                 TEXT NOT NULL,
                    symbol                    TEXT NOT NULL,
                    action_taken              TEXT NOT NULL,
                    data_lineage_source       TEXT NOT NULL,
                    active_trade_parameters   TEXT,
                    execution_telemetry       TEXT,
                    cryptographic_state_hash  TEXT NOT NULL UNIQUE
                );
                CREATE INDEX IF NOT EXISTS ix_governance_audit_symbol_ts
                    ON governance_audit_trail (symbol, timestamp DESC);

                CREATE TABLE IF NOT EXISTS macro_fallback_states (
                    cache_key    TEXT PRIMARY KEY,
                    provider     TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at   TEXT NOT NULL
                );
                """
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # SVI volatility parameters
    # ------------------------------------------------------------------
    def save_svi_parameters(
        self,
        ticker: str,
        params: Dict[str, float],
        dte: float,
        forward: float,
        arb_free: bool,
        success: bool,
        ts: Optional[float] = None,
    ) -> int:
        ts = ts if ts is not None else time.time()
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO svi_parameters
                   (ticker, ts, a, b, rho, m, sigma, dte, forward, arb_free, success)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ticker.upper(), ts,
                    params.get("a"), params.get("b"), params.get("rho"),
                    params.get("m"), params.get("sigma"),
                    dte, forward, int(bool(arb_free)), int(bool(success)),
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def get_latest_svi_parameters(self, ticker: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM svi_parameters WHERE ticker=? ORDER BY ts DESC LIMIT 1",
                (ticker.upper(),),
            ).fetchone()
        return dict(row) if row else None

    def get_svi_history(self, ticker: str, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM svi_parameters WHERE ticker=? ORDER BY ts DESC LIMIT ?",
                (ticker.upper(), limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # GEX snapshots
    # ------------------------------------------------------------------
    def save_gex_snapshot(
        self,
        ticker: str,
        spot: float,
        net_gex: float,
        gamma_flip: float,
        regime: str,
        ts: Optional[float] = None,
    ) -> int:
        ts = ts if ts is not None else time.time()
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO gex_snapshots
                   (ticker, ts, spot, net_gex, gamma_flip, regime)
                   VALUES (?,?,?,?,?,?)""",
                (ticker.upper(), ts, spot, net_gex, gamma_flip, regime),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def get_gex_history(self, ticker: str, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM gex_snapshots WHERE ticker=? ORDER BY ts DESC LIMIT ?",
                (ticker.upper(), limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_price_series(self, ticker: str, limit: int = 1000) -> List[Dict[str, Any]]:
        """
        Return ascending price observations for portfolio risk analytics.

        Real spot values from GEX snapshots are preferred. If the store has no
        spot history yet, a compact synthetic series is reconstructed from
        recorded forward returns so VaR requests degrade gracefully.
        """
        ticker = ticker.upper()
        with self._lock:
            rows = self._conn.execute(
                """SELECT ts, spot FROM (
                       SELECT ts, spot
                       FROM gex_snapshots
                       WHERE ticker=? AND spot IS NOT NULL AND spot > 0
                       ORDER BY ts DESC
                       LIMIT ?
                   ) ORDER BY ts ASC""",
                (ticker, limit),
            ).fetchall()

        if rows:
            return [
                {"ts": float(r["ts"]), "price": float(r["spot"]), "source": "gex_snapshots"}
                for r in rows
            ]

        _, returns, timestamps = self.load_training_matrix(ticker, limit=limit)
        if len(returns) == 0:
            return []

        price = 1.0
        reconstructed: List[Dict[str, Any]] = []
        for ts, ret in zip(timestamps, returns):
            if np.isfinite(ret):
                price *= max(0.000001, 1.0 + float(ret))
                reconstructed.append(
                    {
                        "ts": float(ts),
                        "price": float(price),
                        "source": "training_samples_reconstructed",
                    }
                )
        return reconstructed

    # ------------------------------------------------------------------
    # Training samples (ML core)
    # ------------------------------------------------------------------
    def insert_training_sample(
        self,
        ticker: str,
        gex: float,
        vanna: float,
        cvd: float,
        obi: float,
        skew: float,
        fwd_return: float,
        ts: Optional[float] = None,
    ) -> int:
        ts = ts if ts is not None else time.time()
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO training_samples
                   (ticker, ts, gex, vanna, cvd, obi, skew, fwd_return)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (ticker.upper(), ts, gex, vanna, cvd, obi, skew, fwd_return),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def insert_training_samples(self, rows: List[Tuple]) -> int:
        """Bulk insert. Each row: (ticker, ts, gex, vanna, cvd, obi, skew, fwd_return)."""
        with self._lock:
            cur = self._conn.executemany(
                """INSERT INTO training_samples
                   (ticker, ts, gex, vanna, cvd, obi, skew, fwd_return)
                   VALUES (?,?,?,?,?,?,?,?)""",
                rows,
            )
            self._conn.commit()
            return cur.rowcount

    def load_training_matrix(
        self, ticker: Optional[str] = None, limit: Optional[int] = None
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        Returns ``(features, returns, timestamps)`` ready for the ExecutionOptimizer.

        features   : (N, 5) array -> GEX, Vanna, CVD, OBI, Skew
        returns    : (N,)  array  -> forward asset return per sample
        timestamps : (N,)  array  -> Unix seconds, ascending
        """
        sql = "SELECT ts, gex, vanna, cvd, obi, skew, fwd_return FROM training_samples"
        args: List[Any] = []
        if ticker:
            sql += " WHERE ticker=?"
            args.append(ticker.upper())
        sql += " ORDER BY ts ASC"
        if limit:
            sql += " LIMIT ?"
            args.append(limit)

        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        if not rows:
            empty = np.empty((0, 5)), np.empty((0,)), np.empty((0,))
            return empty

        timestamps = np.array([r["ts"] for r in rows], dtype=float)
        features = np.array(
            [[r["gex"], r["vanna"], r["cvd"], r["obi"], r["skew"]] for r in rows],
            dtype=float,
        )
        returns = np.array([r["fwd_return"] for r in rows], dtype=float)
        return features, returns, timestamps

    def training_sample_count(self, ticker: Optional[str] = None) -> int:
        if ticker:
            with self._lock:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS c FROM training_samples WHERE ticker=?",
                    (ticker.upper(),),
                ).fetchone()
        else:
            with self._lock:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS c FROM training_samples"
                ).fetchone()
        return int(row["c"])

    # ------------------------------------------------------------------
    # Optimization runs
    # ------------------------------------------------------------------
    def save_optimization_run(self, result: Dict[str, Any], ts: Optional[float] = None) -> int:
        ts = ts if ts is not None else time.time()
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO optimization_runs
                   (ts, weights, threshold, win_rate, profit, success, message)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    ts,
                    json.dumps(result.get("weights")),
                    result.get("threshold"),
                    result.get("optimized_win_rate"),
                    result.get("optimized_profit"),
                    int(bool(result.get("success"))),
                    str(result.get("message", "")),
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def get_latest_optimization(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM optimization_runs ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        out = dict(row)
        try:
            out["weights"] = json.loads(out["weights"]) if out["weights"] else None
        except (ValueError, TypeError):
            pass
        return out

    # ------------------------------------------------------------------
    # Cryptographic governance audit ledger
    # ------------------------------------------------------------------
    @staticmethod
    def _canonical_json(value: Optional[Dict[str, Any]]) -> str:
        return json.dumps(value or {}, sort_keys=True, separators=(",", ":"), default=str)

    @classmethod
    def compute_governance_state_hash(
        cls,
        timestamp: str,
        symbol: str,
        action_taken: str,
        data_lineage_source: str,
        active_trade_parameters: Optional[Dict[str, Any]] = None,
        execution_telemetry: Optional[Dict[str, Any]] = None,
    ) -> str:
        payload = "|".join(
            [
                timestamp,
                symbol.upper(),
                action_taken,
                data_lineage_source,
                cls._canonical_json(active_trade_parameters),
                cls._canonical_json(execution_telemetry),
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def record_governance_audit(
        self,
        symbol: str,
        action_taken: str,
        data_lineage_source: str,
        active_trade_parameters: Optional[Dict[str, Any]] = None,
        execution_telemetry: Optional[Dict[str, Any]] = None,
        timestamp: Optional[str] = None,
        transaction_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        timestamp = timestamp or datetime.now(timezone.utc).isoformat()
        transaction_id = transaction_id or str(uuid.uuid4())
        symbol = symbol.upper()
        active_json = self._canonical_json(active_trade_parameters)
        telemetry_json = self._canonical_json(execution_telemetry)
        state_hash = self.compute_governance_state_hash(
            timestamp,
            symbol,
            action_taken,
            data_lineage_source,
            active_trade_parameters,
            execution_telemetry,
        )

        with self._lock:
            self._conn.execute(
                """INSERT INTO governance_audit_trail
                   (transaction_id, timestamp, symbol, action_taken, data_lineage_source,
                    active_trade_parameters, execution_telemetry, cryptographic_state_hash)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    transaction_id,
                    timestamp,
                    symbol,
                    action_taken,
                    data_lineage_source,
                    active_json,
                    telemetry_json,
                    state_hash,
                ),
            )
            self._conn.commit()

        return {
            "transaction_id": transaction_id,
            "timestamp": timestamp,
            "symbol": symbol,
            "action_taken": action_taken,
            "data_lineage_source": data_lineage_source,
            "cryptographic_state_hash": state_hash,
        }

    def verify_governance_audit_integrity(self, limit: Optional[int] = 1000) -> Dict[str, Any]:
        args: List[Any] = []
        sql = "SELECT * FROM governance_audit_trail ORDER BY timestamp DESC"
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))

        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()

        invalid_records: List[Dict[str, Any]] = []
        for row in rows:
            active_params = json.loads(row["active_trade_parameters"] or "{}")
            telemetry = json.loads(row["execution_telemetry"] or "{}")
            expected = self.compute_governance_state_hash(
                row["timestamp"],
                row["symbol"],
                row["action_taken"],
                row["data_lineage_source"],
                active_params,
                telemetry,
            )
            if expected != row["cryptographic_state_hash"]:
                invalid_records.append(
                    {
                        "transaction_id": row["transaction_id"],
                        "symbol": row["symbol"],
                        "timestamp": row["timestamp"],
                        "expected_hash": expected,
                        "stored_hash": row["cryptographic_state_hash"],
                    }
                )

        total = len(rows)
        invalid = len(invalid_records)
        return {
            "status": "empty" if total == 0 else ("valid" if invalid == 0 else "tampered"),
            "total_checked": total,
            "valid_count": total - invalid,
            "invalid_count": invalid,
            "invalid_records": invalid_records,
        }

    # ------------------------------------------------------------------
    # Durable read-only macro fallback states
    # ------------------------------------------------------------------
    def save_macro_fallback_state(
        self,
        cache_key: str,
        payload: Dict[str, Any],
        provider: Optional[str] = None,
        updated_at: Optional[str] = None,
    ) -> None:
        """Persist the last usable analytical payload for outage recovery."""
        timestamp = updated_at or datetime.now(timezone.utc).isoformat()
        source = provider or str(payload.get("provider") or "unknown")
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        with self._lock:
            self._conn.execute(
                """INSERT INTO macro_fallback_states
                   (cache_key, provider, payload_json, updated_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(cache_key) DO UPDATE SET
                       provider=excluded.provider,
                       payload_json=excluded.payload_json,
                       updated_at=excluded.updated_at""",
                (str(cache_key), source, payload_json, timestamp),
            )
            self._conn.commit()

    def get_macro_fallback_state(self, cache_key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                """SELECT provider, payload_json, updated_at
                   FROM macro_fallback_states WHERE cache_key=?""",
                (str(cache_key),),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        payload.setdefault("provider", row["provider"])
        payload.setdefault("last_updated", row["updated_at"])
        payload["durable_snapshot_updated_at"] = row["updated_at"]
        return payload

    # ------------------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            self._conn.close()


# Convenience singleton used by the FastAPI app and background tasks.
_default_db: Optional[StateDatabase] = None


def get_state_db() -> StateDatabase:
    global _default_db
    if _default_db is None:
        from core.config import settings
        _default_db = StateDatabase(settings.DB_PATH)
    return _default_db


if __name__ == "__main__":
    # Smoke test against a throwaway DB file.
    import tempfile

    tmp = os.path.join(tempfile.gettempdir(), "tbbfx_state_test.db")
    db = StateDatabase(tmp)
    db.save_svi_parameters(
        "SPY",
        {"a": 0.04, "b": 0.1, "rho": -0.3, "m": 0.0, "sigma": 0.1},
        dte=7, forward=500.0, arb_free=True, success=True,
    )
    db.save_gex_snapshot("SPY", spot=500.0, net_gex=1.2e9, gamma_flip=498.5, regime="POSITIVE")
    db.insert_training_sample("SPY", gex=0.5, vanna=0.1, cvd=120.0, obi=0.3, skew=-0.2, fwd_return=0.0012)
    feats, rets, tss = db.load_training_matrix("SPY")
    print("latest SVI:", db.get_latest_svi_parameters("SPY"))
    print("gex history rows:", len(db.get_gex_history("SPY")))
    print("training matrix:", feats.shape, rets.shape, tss.shape)
    db.close()
    print("StateDatabase smoke test OK ->", tmp)
