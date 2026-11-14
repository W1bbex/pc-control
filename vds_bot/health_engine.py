import logging
import math
import time
from dataclasses import dataclass
from typing import Optional

LOG = logging.getLogger("pc-control-bot.health_engine")

PC_LIFECYCLE_COMPONENTS = ("windows_system", "windows_session", "windows_desktop", "telemetry")


def _parse_ts(ts):
    """Safely convert an incoming timestamp to a finite float or None."""
    if ts is None or isinstance(ts, bool):
        return None
    try:
        val = float(ts)
    except (ValueError, TypeError):
        return None
    return val if math.isfinite(val) else None


class PCState:
    READY = "READY"
    LOCKED = "LOCKED"
    BOOTING = "BOOTING"
    DEGRADED = "DEGRADED"
    SHUTDOWN_OBSERVED = "SHUTDOWN_OBSERVED"
    UNREACHABLE = "UNREACHABLE"


@dataclass
class PCStateInfo:
    state: str
    label: str
    reason: str
    telemetry_age: Optional[float]
    system_status: str
    desktop_status: str
    session_status: str
    esp_status: str
    mqtt_online: bool


class ComponentState:
    def __init__(self, name, stale_threshold=120.0):
        self.name = name
        self.status = "unknown"
        self.boot_id = None
        self.last_ts = 0.0
        self.local_updated_at = 0.0
        self.stale_threshold = stale_threshold

    def update(self, status, boot_id=None, ts=None, is_retained=False):
        now = time.time()

        # Protect against empty status
        if not status:
            status = "unknown"

        # Check reboot / boot_id transition before sequence check.
        # Keep the monotonic timestamp floor intact when boot_id matches (same OS boot session).
        if boot_id is not None:
            same_boot = self.boot_id is not None and boot_id == self.boot_id
            real_reboot = self.boot_id is not None and boot_id != self.boot_id
            cold_live_boot = self.boot_id is None and not is_retained and self.status in ("offline", "unknown")
            if not same_boot and (real_reboot or cold_live_boot):
                LOG.info(
                    "Component %s rebooted or woke from offline (boot_id: %s). Resetting timestamp sequence.",
                    self.name, boot_id
                )
                self.last_ts = 0.0
            self.boot_id = boot_id

        # Safely coerce ts to float to prevent TypeError '<' not supported between str and float
        parsed_ts = _parse_ts(ts)
        if ts is not None and parsed_ts is None:
            LOG.warning("Invalid non-numeric ts received for %s: %r", self.name, ts)
        ts = parsed_ts

        # Pre-NTP timestamps (e.g. ESP8266 boots before NTP sync and reports ts=0)
        if ts is not None and ts <= 0:
            if not is_retained:
                LOG.debug("Component %s reported ts=%.2f (pre-NTP live packet), mapping to local time %.2f", self.name, ts, now)
                ts = now
            else:
                LOG.debug("Component %s reported retained ts=%.2f (pre-NTP), ignoring timestamp", self.name, ts)
                ts = None

        # Check timestamp sequence: ignore out-of-order / older packets
        if ts is not None and self.last_ts is not None and self.last_ts > 0.0:
            if ts < self.last_ts:
                LOG.debug("Ignoring out-of-order packet for %s (ts=%.2f < last_ts=%.2f)", self.name, ts, self.last_ts)
                return False

        # Strict TS check for all components (except mqtt_broker/telegram which don't have TS)
        future_skew = False
        if self.name not in ("mqtt_broker", "telegram"):
            if ts is None:
                # Without TS, we cannot trust freshness, EXCEPT for offline (LWT)
                if status != "offline":
                    status = "unknown"
            else:
                age = now - ts
                if age < -600.0:
                    future_skew = True
                    if status in ("online", "unlocked", "locked", "fresh"):
                        status = "stale"
                elif age > self.stale_threshold:
                    if status in ("online", "unlocked", "locked", "fresh", "login_screen"):
                        status = "stale"

        self.status = status
        self._last_future_skew = future_skew
        if ts is not None and not future_skew:
            self.last_ts = ts
        self.local_updated_at = now
        return True

    def check_stale(self):
        if self.status in ("online", "unlocked", "locked", "fresh", "login_screen"):
            if time.time() - self.local_updated_at > self.stale_threshold:
                self.status = "stale"
                return True
        return False


class HealthEngine:
    def __init__(self):
        self.components = {
            "telegram": ComponentState("telegram", 60.0),
            "mqtt_broker": ComponentState("mqtt_broker", 30.0),
            "esp": ComponentState("esp", 120.0),
            "windows_system": ComponentState("windows_system", 45.0),
            "windows_session": ComponentState("windows_session", 45.0),
            "windows_desktop": ComponentState("windows_desktop", 45.0),
            "telemetry": ComponentState("telemetry", 45.0),
        }
        self.shutdown_observed_until = 0.0
        self.shutdown_observed_at = 0.0
        self.graceful_shutdown_until = 0.0
        self.graceful_shutdown_at = 0.0
        self.graceful_shutdown_reason = ""
        
    def update(self, component, status, boot_id=None, ts=None, is_retained=False):
        if component in self.components:
            comp = self.components[component]
            old_boot_id = comp.boot_id
            accepted = comp.update(status, boot_id, ts, is_retained)
            # Only live, accepted PC lifecycle components with valid timestamps may clear shutdown flags.
            # Auxiliary 24/7 components (esp, mqtt_broker, telegram) stay online while the PC is off.
            parsed_ts = _parse_ts(ts)
            invalid_ts = ts is not None and parsed_ts is None
            future_skew = getattr(comp, "_last_future_skew", False)
            if (
                component in PC_LIFECYCLE_COMPONENTS
                and accepted
                and not invalid_ts
                and not future_skew
                and status in ("online", "unlocked", "locked", "fresh")
            ):
                msg_ts = parsed_ts if (parsed_ts is not None and parsed_ts > 0) else (
                    comp.last_ts if comp.last_ts > 0 else time.time()
                )
                reboot_detected = bool(
                    boot_id and (
                        (old_boot_id and boot_id != old_boot_id)
                        or (old_boot_id is None and not is_retained)
                    )
                )
                if reboot_detected or msg_ts > self.shutdown_observed_at:
                    self.clear_shutdown_observed()
                if reboot_detected or msg_ts > self.graceful_shutdown_at:
                    self.clear_graceful_shutdown()
                else:
                    LOG.info(
                        "Ignored pre-shutdown heartbeat from %s (ts=%.2f <= shutdown_ts)",
                        component,
                        msg_ts,
                    )
            
    def get_status(self, component):
        return self.components[component].status if component in self.components else "unknown"

    def set_shutdown_observed(self, ttl=3600.0, now=None):
        ts = time.time() if now is None else float(now)
        self.shutdown_observed_at = ts
        self.shutdown_observed_until = ts + ttl
        LOG.info("Shutdown observed until %s", self.shutdown_observed_until)

    def clear_shutdown_observed(self):
        if self.shutdown_observed_until > 0.0:
            LOG.info("Clearing shutdown observed state")
            self.shutdown_observed_until = 0.0
            self.shutdown_observed_at = 0.0

    def set_graceful_shutdown(self, ts=None, ttl=2592000.0, reason="windows_shutdown"):
        now_ts = time.time() if ts is None else float(ts)
        self.graceful_shutdown_at = now_ts
        self.graceful_shutdown_until = now_ts + ttl
        self.graceful_shutdown_reason = reason
        if "windows_system" in self.components:
            self.components["windows_system"].status = "offline"
            self.components["windows_system"].last_ts = now_ts
        if "windows_desktop" in self.components:
            self.components["windows_desktop"].status = "offline"
            self.components["windows_desktop"].last_ts = now_ts
        if "windows_session" in self.components:
            self.components["windows_session"].status = "unknown"
        LOG.info("Graceful shutdown recorded at %.2f (reason=%s)", now_ts, reason)

    def clear_graceful_shutdown(self):
        if self.graceful_shutdown_until > 0.0:
            LOG.info("Clearing graceful shutdown state")
            self.graceful_shutdown_until = 0.0
            self.graceful_shutdown_at = 0.0
            self.graceful_shutdown_reason = ""
        
    def check_all_stale(self):
        changed = []
        for name, comp in self.components.items():
            if name == "mqtt_broker":
                continue
            if comp.check_stale():
                changed.append(name)
        return changed
        
    def set_all_unknown(self, exclude=None):
        exclude = exclude or []
        for name, comp in self.components.items():
            if name not in exclude:
                comp.status = "unknown"
        # When broker drops, shutdown_observed is also unprovable
        self.clear_shutdown_observed()

    def evaluate_pc_state(self, telemetry_age=None, now=None) -> PCStateInfo:
        now = time.time() if now is None else float(now)
        mqtt_status = self.get_status("mqtt_broker")
        mqtt_online = (mqtt_status == "online")
        sys_status = self.get_status("windows_system")
        desk_status = self.get_status("windows_desktop")
        sess_status = self.get_status("windows_session")
        esp_status = self.get_status("esp")
        telem_status = self.get_status("telemetry")

        # 1. If MQTT is not online:
        if not mqtt_online:
            return PCStateInfo(
                state=PCState.UNREACHABLE,
                label="⚪ Нет связи с системой",
                reason="Бот потерял связь с MQTT-брокером",
                telemetry_age=telemetry_age,
                system_status=sys_status,
                desktop_status=desk_status,
                session_status=sess_status,
                esp_status=esp_status,
                mqtt_online=False,
            )

        # 2. Check if SHUTDOWN_OBSERVED or GRACEFUL_SHUTDOWN is active within TTL
        offline_statuses = ("offline", "unknown", "stale", "shutdown", "graceful_shutdown")
        is_both_offline = (
            sys_status in offline_statuses
            and desk_status in offline_statuses
        )
        if is_both_offline:
            if now < self.shutdown_observed_until:
                return PCStateInfo(
                    state=PCState.SHUTDOWN_OBSERVED,
                    label="🔴 Выключен (наблюдается)",
                    reason="ПК перестал отвечать после принятой команды выключения",
                    telemetry_age=telemetry_age,
                    system_status=sys_status,
                    desktop_status=desk_status,
                    session_status=sess_status,
                    esp_status=esp_status,
                    mqtt_online=True,
                )
            if now < self.graceful_shutdown_until:
                return PCStateInfo(
                    state=PCState.SHUTDOWN_OBSERVED,
                    label="🔴 Выключен (штатно)",
                    reason="ПК штатно завершил работу через Windows",
                    telemetry_age=telemetry_age,
                    system_status=sys_status,
                    desktop_status=desk_status,
                    session_status=sess_status,
                    esp_status=esp_status,
                    mqtt_online=True,
                )

        if now < self.shutdown_observed_until and not is_both_offline:
            self.clear_shutdown_observed()
        if now < self.graceful_shutdown_until and not is_both_offline:
            self.clear_graceful_shutdown()

        # 3. If system agent is online:
        if sys_status == "online":
            if sess_status in ("locked", "login_screen"):
                return PCStateInfo(
                    state=PCState.LOCKED,
                    label="🟡 Экран входа",
                    reason="Экран Windows заблокирован или ожидает входа",
                    telemetry_age=telemetry_age,
                    system_status=sys_status,
                    desktop_status=desk_status,
                    session_status=sess_status,
                    esp_status=esp_status,
                    mqtt_online=True,
                )
            elif sess_status == "unlocked":
                telem_fresh = (telem_status == "fresh" and (telemetry_age is not None and telemetry_age <= 90.0))
                if desk_status == "online" and telem_fresh:
                    if esp_status in ("offline", "stale"):
                        return PCStateInfo(
                            state=PCState.DEGRADED,
                            label="🟡 ESP недоступна",
                            reason="Сеанс разблокирован, но модуль питания ESP8266 недоступен (отключен)",
                            telemetry_age=telemetry_age,
                            system_status=sys_status,
                            desktop_status=desk_status,
                            session_status=sess_status,
                            esp_status=esp_status,
                            mqtt_online=True,
                        )
                    return PCStateInfo(
                        state=PCState.READY,
                        label="🟢 Готов к работе",
                        reason="Сеанс разблокирован, desktop-агент онлайн, телеметрия свежая",
                        telemetry_age=telemetry_age,
                        system_status=sys_status,
                        desktop_status=desk_status,
                        session_status=sess_status,
                        esp_status=esp_status,
                        mqtt_online=True,
                    )
                else:
                    reasons = []
                    if desk_status != "online":
                        reasons.append(f"desktop-агент {desk_status}")
                    if esp_status in ("offline", "stale"):
                        reasons.append(f"модуль ESP8266 {esp_status}")
                    if telemetry_age is None:
                        reasons.append("телеметрия ещё не получена")
                    elif telemetry_age > 90.0 or telem_status != "fresh":
                        reasons.append(f"телеметрия устарела ({int(telemetry_age)}с)")
                    return PCStateInfo(
                        state=PCState.DEGRADED,
                        label="🟡 Ограниченная работа",
                        reason="Сеанс разблокирован, но " + ", ".join(reasons),
                        telemetry_age=telemetry_age,
                        system_status=sys_status,
                        desktop_status=desk_status,
                        session_status=sess_status,
                        esp_status=esp_status,
                        mqtt_online=True,
                    )
            else:
                # sess_status is no_console or unknown or booting
                return PCStateInfo(
                    state=PCState.BOOTING,
                    label="🟡 Загружается",
                    reason=f"Система отвечает, сессия: {sess_status}",
                    telemetry_age=telemetry_age,
                    system_status=sys_status,
                    desktop_status=desk_status,
                    session_status=sess_status,
                    esp_status=esp_status,
                    mqtt_online=True,
                )

        # 4. If system agent is NOT online (offline, stale, unknown),
        # but desktop agent is online or fresh telemetry is arriving:
        telem_fresh = (telem_status == "fresh" and (telemetry_age is not None and telemetry_age <= 90.0))
        if desk_status == "online" or telem_fresh:
            effective_sess = "unlocked" if sess_status in ("unknown", "", None) else sess_status
            reasons = [f"системный агент {sys_status}"]
            if esp_status in ("offline", "stale"):
                reasons.append(f"ESP8266 {esp_status}")
            return PCStateInfo(
                state=PCState.DEGRADED,
                label="🟡 Ограниченная работа",
                reason=f"ПК включён (desktop: {desk_status}), но " + ", ".join(reasons),
                telemetry_age=telemetry_age,
                system_status=sys_status,
                desktop_status=desk_status,
                session_status=effective_sess,
                esp_status=esp_status,
                mqtt_online=True,
            )

        # 5. Otherwise, connection lost without confirmed shutdown:
        return PCStateInfo(
            state=PCState.UNREACHABLE,
            label="⚪ Связь потеряна",
            reason="Связь потеряна, состояние ПК неизвестно (нет подтверждения выключения)",
            telemetry_age=telemetry_age,
            system_status=sys_status,
            desktop_status=desk_status,
            session_status=sess_status,
            esp_status=esp_status,
            mqtt_online=True,
        )

