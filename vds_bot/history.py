import io
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    MOSCOW = ZoneInfo("Europe/Moscow")
except Exception:
    MOSCOW = timezone(timedelta(hours=3))

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False

_CHART_LOCK = threading.Lock()


class TelemetryHistory:
    def __init__(self, path: Path, retention_days: int = 90):
        self.path = Path(path)
        self.retention_days = retention_days
        self.lock = threading.Lock()
        self.last_cleanup = 0
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS telemetry (
                        ts INTEGER PRIMARY KEY,
                        host TEXT,
                        cpu_load REAL,
                        cpu_temp REAL,
                        gpu_load REAL,
                        gpu_temp REAL,
                        gpu_power REAL,
                        ram_percent REAL,
                        disk_c_percent REAL,
                        download_mbps REAL,
                        upload_mbps REAL,
                        idle_seconds INTEGER,
                        user_active INTEGER
                    )
                    """
                )
        finally:
            conn.close()

    @staticmethod
    def _number(value):
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def insert(self, payload):
        def _get_dict(obj, key):
            val = obj.get(key) if isinstance(obj, dict) else None
            return val if isinstance(val, dict) else {}

        cpu = _get_dict(payload, "cpu")
        gpu = _get_dict(payload, "gpu")
        memory = _get_dict(payload, "memory")
        network = _get_dict(payload, "network")
        disks_raw = payload.get("disks") if isinstance(payload, dict) else []
        disks = [d for d in disks_raw if isinstance(d, dict)] if isinstance(disks_raw, list) else []

        disk_c = next(
            (
                disk
                for disk in disks
                if str(disk.get("drive", "")).upper().startswith("C:")
            ),
            disks[0] if disks else {},
        )

        raw_ts = payload.get("ts") if isinstance(payload, dict) else None
        if raw_ts is None and isinstance(payload, dict):
            raw_ts = payload.get("timestamp")
        try:
            ts_val = int(float(raw_ts)) if raw_ts is not None else int(time.time())
        except (ValueError, TypeError):
            ts_val = int(time.time())

        row = (
            ts_val,
            str((payload.get("host") if isinstance(payload, dict) else "") or ""),
            self._number(cpu.get("load_percent")),
            self._number(cpu.get("temp_c")),
            self._number(gpu.get("load_percent")),
            self._number(gpu.get("temp_c")),
            self._number(gpu.get("power_w")),
            self._number(memory.get("used_percent")),
            self._number(disk_c.get("used_percent")),
            self._number(network.get("download_mbps")),
            self._number(network.get("upload_mbps")),
            payload.get("idle_seconds") if isinstance(payload, dict) else None,
            1 if (isinstance(payload, dict) and payload.get("user_active")) else 0,
        )

        with self.lock:
            conn = self._connect()
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO telemetry (
                            ts, host, cpu_load, cpu_temp, gpu_load, gpu_temp,
                            gpu_power, ram_percent, disk_c_percent,
                            download_mbps, upload_mbps, idle_seconds, user_active
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        row,
                    )
            finally:
                conn.close()

        if time.time() - self.last_cleanup > 86400:
            self.cleanup()

    def cleanup(self):
        cutoff = int(time.time()) - self.retention_days * 86400
        with self.lock:
            conn = self._connect()
            try:
                with conn:
                    conn.execute("DELETE FROM telemetry WHERE ts < ?", (cutoff,))
                    conn.execute("PRAGMA optimize")
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn.close()
        self.last_cleanup = time.time()

    @staticmethod
    def _bucket_size(period_seconds):
        if period_seconds <= 6 * 3600:
            return 60
        if period_seconds <= 2 * 86400:
            return 300
        if period_seconds <= 14 * 86400:
            return 1800
        return 7200

    def rows(self, period_seconds):
        start = int(time.time()) - period_seconds
        bucket = self._bucket_size(period_seconds)
        with self.lock:
            conn = self._connect()
            try:
                return conn.execute(
                    """
                    SELECT
                        (ts / ?) * ? AS bucket_ts,
                        AVG(cpu_load),
                        AVG(cpu_temp),
                        AVG(gpu_load),
                        AVG(gpu_temp),
                        AVG(gpu_power),
                        AVG(ram_percent),
                        AVG(disk_c_percent),
                        AVG(download_mbps),
                        AVG(upload_mbps),
                        AVG(user_active)
                    FROM telemetry
                    WHERE ts >= ?
                    GROUP BY bucket_ts
                    ORDER BY bucket_ts
                    """,
                    (bucket, bucket, start),
                ).fetchall()
            finally:
                conn.close()

    def stats(self):
        with self.lock:
            conn = self._connect()
            try:
                count, first_ts, last_ts = conn.execute(
                    "SELECT COUNT(*), MIN(ts), MAX(ts) FROM telemetry"
                ).fetchone()
                online_seconds, active_seconds = conn.execute(
                    """
                    SELECT
                        COALESCE(SUM(
                            CASE
                                WHEN delta BETWEEN 1 AND 60 THEN delta
                                ELSE 0
                            END
                        ), 0),
                        COALESCE(SUM(
                            CASE
                                WHEN user_active = 1 AND delta BETWEEN 1 AND 60
                                THEN delta
                                ELSE 0
                            END
                        ), 0)
                    FROM (
                        SELECT
                            user_active,
                            LEAD(ts) OVER (ORDER BY ts) - ts AS delta
                        FROM telemetry
                        WHERE ts >= ?
                    )
                    """,
                    (int(time.time()) - 86400,),
                ).fetchone()
                return {
                    "count": count or 0,
                    "first_ts": first_ts,
                    "last_ts": last_ts,
                    "online_seconds_24h": int(online_seconds or 0),
                    "active_seconds_24h": int(active_seconds or 0),
                }
            finally:
                conn.close()

    def render_chart(self, period_seconds, title):
        if not HAS_MATPLOTLIB:
            raise RuntimeError("Компонент matplotlib не установлен")
        rows = self.rows(period_seconds)
        if len(rows) < 2:
            raise ValueError("Недостаточно данных: подожди хотя бы пару минут")

        bucket = self._bucket_size(period_seconds)
        row_by_time = {row[0]: row for row in rows}
        first_bucket = rows[0][0]
        now_bucket = (int(time.time()) // bucket) * bucket
        last_bucket = max(rows[-1][0], now_bucket)
        rows = [
            row_by_time.get(
                timestamp,
                (
                    timestamp,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                ),
            )
            for timestamp in range(first_bucket, last_bucket + bucket, bucket)
        ]

        times = [
            datetime.fromtimestamp(row[0], MOSCOW)
            for row in rows
        ]

        with _CHART_LOCK:
            plt.style.use("dark_background")
            figure, axes = plt.subplots(
                4,
                1,
                figsize=(12, 10),
                sharex=True,
                constrained_layout=True,
            )
            try:
                figure.patch.set_facecolor("#111827")
                figure.suptitle(
                    f"PC CONTROL — {title}",
                    fontsize=17,
                    fontweight="bold",
                )

                for axis in axes:
                    axis.set_facecolor("#111827")
                    axis.grid(True, color="#334155", alpha=0.45)
                    axis.spines[:].set_color("#475569")

                axes[0].plot(times, [row[1] for row in rows], label="CPU", color="#22d3ee")
                axes[0].plot(times, [row[3] for row in rows], label="GPU", color="#a78bfa")
                axes[0].fill_between(
                    times,
                    0,
                    [100 if (row[10] or 0) >= 0.5 else 0 for row in rows],
                    color="#22c55e",
                    alpha=0.08,
                    label="Пользователь активен",
                )
                axes[0].fill_between(
                    times,
                    0,
                    [100 if row[1] is not None else 0 for row in rows],
                    color="#38bdf8",
                    alpha=0.04,
                    label="ПК включён",
                )
                axes[0].set_ylabel("Нагрузка, %")
                axes[0].set_ylim(0, 105)
                axes[0].legend(loc="upper left", ncol=4)

                axes[1].plot(
                    times,
                    [row[2] for row in rows],
                    label="CPU",
                    color="#fb923c",
                )
                axes[1].plot(
                    times,
                    [row[4] for row in rows],
                    label="GPU",
                    color="#f43f5e",
                )
                axes[1].set_ylabel("Температура, °C")
                axes[1].legend(loc="upper left", ncol=2)

                axes[2].plot(
                    times,
                    [row[6] for row in rows],
                    label="RAM",
                    color="#34d399",
                )
                axes[2].plot(
                    times,
                    [row[7] for row in rows],
                    label="Диск C:",
                    color="#facc15",
                )
                axes[2].set_ylabel("Использовано, %")
                axes[2].set_ylim(0, 105)
                axes[2].legend(loc="upper left", ncol=2)

                axes[3].plot(
                    times,
                    [row[8] for row in rows],
                    label="Скачивание",
                    color="#38bdf8",
                )
                axes[3].plot(
                    times,
                    [row[9] for row in rows],
                    label="Отдача",
                    color="#f472b6",
                )
                axes[3].set_ylabel("Сеть, Мбит/с")
                axes[3].legend(loc="upper left", ncol=2)

                locator = mdates.AutoDateLocator(minticks=4, maxticks=10)
                axes[3].xaxis.set_major_locator(locator)
                axes[3].xaxis.set_major_formatter(
                    mdates.ConciseDateFormatter(locator, tz=MOSCOW)
                )

                output = io.BytesIO()
                figure.savefig(
                    output,
                    format="png",
                    dpi=130,
                    facecolor=figure.get_facecolor(),
                )
                output.seek(0)
                return output.getvalue(), len(rows)
            finally:
                plt.close(figure)
