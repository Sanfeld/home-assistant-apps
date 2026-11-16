#!/usr/bin/env python3
"""Wi-Fi Signal Monitor – Home Assistant app (add-on).

Polls a UniFi OS console (UCG Max, UDM, ...) for every connected Wi-Fi client,
flags weak / critical / poor-experience connections with debouncing, and
publishes the results to Home Assistant via MQTT discovery (REST fallback),
HA events (`wifi_monitor_alert`) and a persistent notification.
"""

from __future__ import annotations

import csv
import fnmatch
import http.cookiejar
import json
import logging
import os
import re
import signal
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

OPTIONS_PATH = os.environ.get("OPTIONS_PATH", "/data/options.json")
STATE_PATH = os.environ.get("STATE_PATH", "/data/state.json")
SHARE_DIR = os.environ.get("SHARE_DIR", "/share/wifi_monitor")
SUPERVISOR_URL = os.environ.get("SUPERVISOR_URL", "http://supervisor")
def _supervisor_token() -> str:
    """The token is an env var, but s6-overlay keeps it in a file when the env isn't passed through."""
    for name in ("SUPERVISOR_TOKEN", "HASSIO_TOKEN"):
        if os.environ.get(name):
            return os.environ[name]
        for base in ("/run/s6/container_environment", "/var/run/s6/container_environment"):
            try:
                with open(os.path.join(base, name), encoding="utf-8") as f:
                    token = f.read().strip()
                if token:
                    return token
            except OSError:
                continue
    return ""


SUPERVISOR_TOKEN = _supervisor_token()

APP_VERSION = "1.1.0"
DISCOVERY_PREFIX = "homeassistant"
BASE_TOPIC = "wifi_monitor"
AVAIL_TOPIC = f"{BASE_TOPIC}/status"
FORGET_AFTER_DAYS = 7          # remove per-client sensors for clients gone this long
LEVEL_RANK = {"ok": 0, "poor": 1, "weak": 2, "critical": 3}
SELECT_NONE = "— choose a client —"
BANDS = {"ng": "2.4 GHz", "na": "5 GHz", "6e": "6 GHz"}

log = logging.getLogger("wifi_monitor")
STOP = threading.Event()


# --------------------------------------------------------------------------- utils
def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def slugify(text: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    return text or "client"


def mac_id(mac: str) -> str:
    return (mac or "").lower().replace(":", "")


def load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path: str, data) -> None:
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError as err:
        log.warning("Could not save %s: %s", path, err)


# --------------------------------------------------------------------------- UniFi
class UniFiError(Exception):
    pass


class UniFiClient:
    """Minimal UniFi OS client: API key (X-API-KEY) or local-account login."""

    def __init__(self, opts: dict):
        host = opts["unifi_host"].strip().removeprefix("https://").removeprefix("http://").rstrip("/")
        port = int(opts.get("unifi_port") or 443)
        self.base = f"https://{host}" + ("" if port == 443 else f":{port}")
        self.base = os.environ.get("UNIFI_BASE_URL", self.base)  # test hook
        self.site = opts.get("site") or "default"
        self.api_key = (opts.get("api_key") or "").strip()
        self.username = (opts.get("username") or "").strip()
        self.password = opts.get("password") or ""
        if not self.api_key and not (self.username and self.password):
            raise UniFiError("Set either api_key or username + password in the app configuration")
        ctx = ssl.create_default_context()
        if not opts.get("verify_ssl"):
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self.cookies = http.cookiejar.CookieJar()
        handlers = [urllib.request.HTTPCookieProcessor(self.cookies)]
        if self.base.startswith("https"):
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self.opener = urllib.request.build_opener(*handlers)
        self.csrf = None
        self.logged_in = False

    def _request(self, method: str, path: str, body=None, timeout=15):
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["X-API-KEY"] = self.api_key
        elif self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        return self.opener.open(req, timeout=timeout)

    def login(self) -> None:
        try:
            resp = self._request("POST", "/api/auth/login",
                                 {"username": self.username, "password": self.password, "remember": True})
            self.csrf = resp.headers.get("X-CSRF-Token") or resp.headers.get("X-Updated-CSRF-Token")
            resp.read()
            self.logged_in = True
            log.info("Logged in to UniFi console as %s", self.username)
        except urllib.error.HTTPError as err:
            raise UniFiError(f"UniFi login failed (HTTP {err.code}) – check username/password") from err
        except (urllib.error.URLError, OSError) as err:
            raise UniFiError(f"Cannot reach UniFi console at {self.base}: {err}") from err

    def get(self, endpoint: str) -> list:
        path = f"/proxy/network/api/s/{self.site}/{endpoint}"
        for attempt in (1, 2):
            if not self.api_key and not self.logged_in:
                self.login()
            try:
                with self._request("GET", path) as resp:
                    payload = json.loads(resp.read().decode() or "{}")
                    if resp.headers.get("X-Updated-CSRF-Token"):
                        self.csrf = resp.headers["X-Updated-CSRF-Token"]
                break
            except urllib.error.HTTPError as err:
                if err.code in (401, 403) and not self.api_key and attempt == 1:
                    log.info("UniFi session expired, logging in again")
                    self.logged_in = False
                    continue
                if err.code in (401, 403):
                    raise UniFiError(f"UniFi refused the request (HTTP {err.code}) – check the API key / account rights") from err
                if err.code == 404:
                    raise UniFiError(f"UniFi endpoint not found (HTTP 404) – is the site name '{self.site}' right?") from err
                raise UniFiError(f"UniFi HTTP error {err.code} on {endpoint}") from err
            except (urllib.error.URLError, OSError, ValueError) as err:
                raise UniFiError(f"Cannot read {endpoint} from {self.base}: {err}") from err
        meta = payload.get("meta", {})
        if meta.get("rc") not in (None, "ok"):
            raise UniFiError(f"UniFi returned an error for {endpoint}: {meta.get('msg')}")
        return payload.get("data", []) or []


# --------------------------------------------------------------------------- Home Assistant
class HomeAssistant:
    """Supervisor-proxied Core API: events, services, REST states."""

    def __init__(self):
        self.enabled = bool(SUPERVISOR_TOKEN)

    def _call(self, method: str, path: str, body=None):
        if not self.enabled:
            return None
        headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}", "X-Supervisor-Token": SUPERVISOR_TOKEN}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            SUPERVISOR_URL + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as err:
            detail = err.read().decode(errors="ignore")[:200]
            log.warning("Supervisor call %s %s failed: HTTP %s %s", method, path, err.code, detail)
            return None
        except (urllib.error.URLError, OSError, ValueError) as err:
            log.warning("Supervisor call %s %s failed: %s", method, path, err)
            return None

    def mqtt_service(self, opts: dict):
        """MQTT settings: manual options first, else the Supervisor's MQTT service (with retries)."""
        if opts.get("mqtt_host"):
            log.info("Using MQTT broker from app options: %s", opts["mqtt_host"])
            return {"host": opts["mqtt_host"], "port": opts.get("mqtt_port") or 1883,
                    "username": opts.get("mqtt_username") or None, "password": opts.get("mqtt_password") or None}
        if not self.enabled:
            log.warning("SUPERVISOR_TOKEN is not set – can't ask the Supervisor for MQTT settings")
            return None
        for attempt in range(1, 7):
            res = self._call("GET", "/services/mqtt")
            data = (res or {}).get("data")
            if data and data.get("host"):
                log.info("Found MQTT broker %s:%s via the Supervisor", data["host"], data.get("port"))
                return data
            log.info("MQTT service not available yet (attempt %d/6, reply: %s)", attempt,
                     json.dumps(res)[:200] if res is not None else "none")
            time.sleep(5)
        return None

    def fire_event(self, event_type: str, data: dict):
        self._call("POST", f"/core/api/events/{event_type}", data)

    def service(self, domain: str, service: str, data: dict):
        self._call("POST", f"/core/api/services/{domain}/{service}", data)

    def set_state(self, entity_id: str, state, attributes: dict):
        self._call("POST", f"/core/api/states/{entity_id}", {"state": state, "attributes": attributes})


# --------------------------------------------------------------------------- MQTT
class Mqtt:
    def __init__(self, cfg: dict, on_command):
        import paho.mqtt.client as mqtt  # imported lazily so logic can be tested without paho
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="wifi_monitor")
        if cfg.get("username"):
            self.client.username_pw_set(cfg["username"], cfg.get("password"))
        if cfg.get("ssl"):
            self.client.tls_set()
        self.client.will_set(AVAIL_TOPIC, "offline", retain=True)
        self.on_command = on_command
        self.connected = threading.Event()
        self.on_connect_hooks = []
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = lambda *a: self.connected.clear()
        self.client.on_message = self._on_message
        self.client.connect_async(cfg["host"], int(cfg.get("port", 1883)), keepalive=60)
        self.client.loop_start()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            log.error("MQTT connection refused: %s", reason_code)
            return
        log.info("Connected to MQTT broker")
        self.connected.set()
        client.subscribe(f"{BASE_TOPIC}/set/#")
        client.subscribe(f"{DISCOVERY_PREFIX}/status")
        client.publish(AVAIL_TOPIC, "online", retain=True)
        for hook in self.on_connect_hooks:
            hook()

    def _on_message(self, client, userdata, msg):
        payload = msg.payload.decode(errors="ignore")
        if msg.topic == f"{DISCOVERY_PREFIX}/status":
            if payload == "online":
                for hook in self.on_connect_hooks:
                    hook()
            return
        self.on_command(msg.topic.split("/")[-1], payload)

    def publish(self, topic: str, payload, retain: bool = False):
        if not isinstance(payload, str):
            payload = json.dumps(payload)
        self.client.publish(topic, payload, retain=retain)

    def stop(self):
        try:
            self.client.publish(AVAIL_TOPIC, "offline", retain=True)
            time.sleep(0.3)
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- Monitor
class Monitor:
    def __init__(self, opts: dict, ha: HomeAssistant, mqtt: Mqtt | None, unifi=None):
        self.opts = opts
        self.ha = ha
        self.mqtt = mqtt
        self.unifi = unifi or UniFiClient(opts)
        st = load_json(STATE_PATH, {})
        self.clients: dict = st.get("clients", {})       # mac -> tracking record
        self.overrides: dict = st.get("overrides", {})   # thresholds changed from HA
        self.ignored_macs: set = set(st.get("ignored_macs", []))  # ignored from the dashboard
        self.lock = threading.RLock()
        self.select_options = None                        # last published select options
        self.announced: set = set()                       # macs with discovery published this run
        self.last_error = None
        self.last_summary = None
        self.notification_text = None
        if mqtt:
            mqtt.on_connect_hooks.append(self.publish_discovery)
        os.makedirs(SHARE_DIR, exist_ok=True)

    # thresholds (HA number entities can override the app options)
    @property
    def warn_dbm(self) -> int:
        return int(self.overrides.get("warn_dbm", self.opts["warn_dbm"]))

    @property
    def critical_dbm(self) -> int:
        return int(self.overrides.get("critical_dbm", self.opts["critical_dbm"]))

    @property
    def min_satisfaction(self) -> int:
        return int(self.overrides.get("min_satisfaction", self.opts["min_satisfaction"]))

    def mac_by_name(self, name: str):
        name = (name or "").strip()
        for mac, rec in self.clients.items():
            if rec.get("name") == name:
                return mac
        return None

    def handle_command(self, key: str, payload: str):
        with self.lock:
            self._handle_command(key, payload)

    def _handle_command(self, key: str, payload: str):
        if key in ("ignore_client", "unignore_client"):
            mac = self.mac_by_name(payload)
            if not mac:
                self.publish_selects(force=True)
                return
            name = self.clients[mac].get("name")
            if key == "ignore_client":
                self.ignored_macs.add(mac)
                log.info("Now ignoring %s (%s) – set from Home Assistant", name, mac)
            else:
                self.ignored_macs.discard(mac)
                log.info("No longer ignoring %s (%s)", name, mac)
            self.save_state()
            self.publish_selects(force=True)
            return
        if key not in ("warn_dbm", "critical_dbm", "min_satisfaction"):
            return
        try:
            self.overrides[key] = int(float(payload))
        except ValueError:
            return
        log.info("Threshold %s set to %s from Home Assistant", key, self.overrides[key])
        self.save_state()
        self.publish_thresholds()

    def save_state(self):
        save_json(STATE_PATH, {"clients": self.clients, "overrides": self.overrides,
                               "ignored_macs": sorted(self.ignored_macs)})

    def ignored_by(self, mac: str, name: str):
        """'dashboard', 'app setting' or None."""
        if mac in self.ignored_macs:
            return "dashboard"
        return "app setting" if self.ignored(mac, name) else None

    def ignored(self, mac: str, name: str) -> bool:
        if mac in self.ignored_macs:
            return True
        for pat in self.opts.get("ignore_clients") or []:
            pat = pat.strip().lower()
            if pat and (fnmatch.fnmatch(mac.lower(), pat) or fnmatch.fnmatch(name.lower(), pat)):
                return True
        return False

    def classify(self, sig, sat) -> str:
        level = "ok"
        if sig is not None:
            if sig < self.critical_dbm:
                level = "critical"
            elif sig < self.warn_dbm:
                level = "weak"
        if level == "ok" and sat is not None and self.min_satisfaction > 0 and sat < self.min_satisfaction:
            level = "poor"
        return level

    # ---------------------------------------------------------------- poll
    def poll(self) -> None:
        with self.lock:
            self._poll()

    def _poll(self) -> None:
        try:
            stations = self.unifi.get("stat/sta")
            devices = self.unifi.get("stat/device")
        except UniFiError as err:
            if str(err) != self.last_error:
                log.error("%s", err)
            self.last_error = str(err)
            self.publish_console_status(False, str(err))
            return
        if self.last_error:
            log.info("UniFi console reachable again")
        self.last_error = None
        self.publish_console_status(True, None)

        ap_names = {d.get("mac", "").lower(): d.get("name") or d.get("model") or d.get("mac")
                    for d in devices if d.get("type") == "uap" or d.get("radio_table")}
        seen = set()
        now = time.time()
        rows = []
        events = []
        n_bad = self.opts["bad_polls_before_alert"]
        n_good = self.opts["good_polls_before_clear"]

        for sta in stations:
            if sta.get("is_wired", False) or not sta.get("essid"):
                continue
            mac = (sta.get("mac") or "").lower()
            if not mac:
                continue
            name = sta.get("name") or sta.get("hostname") or (
                f"{sta.get('oui')} {mac[-5:]}" if sta.get("oui") else mac)
            sig = sta.get("signal")
            if sig is None and sta.get("rssi") is not None:
                sig = int(sta["rssi"]) - 95  # UniFi rssi is signal above a -95 dBm floor
            sat = sta.get("satisfaction")
            if sat is not None and (sat < 0 or sat > 100):
                sat = None
            ap_mac = (sta.get("ap_mac") or "").lower()
            row = {
                "mac": mac,
                "name": name,
                "signal": sig,
                "satisfaction": sat,
                "ap": ap_names.get(ap_mac, ap_mac or "unknown"),
                "band": BANDS.get(sta.get("radio"), sta.get("radio") or "?"),
                "channel": sta.get("channel"),
                "ssid": sta.get("essid"),
                "ip": sta.get("ip"),
                "tx_rate_mbps": round(sta["tx_rate"] / 1000) if sta.get("tx_rate") else None,
                "rx_rate_mbps": round(sta["rx_rate"] / 1000) if sta.get("rx_rate") else None,
                "uptime_s": sta.get("uptime"),
            }
            ignored = self.ignored(mac, name)
            level = "ok" if ignored else self.classify(sig, sat)
            row["level"] = level
            row["ignored"] = ignored
            seen.add(mac)

            rec = self.clients.setdefault(mac, {"bad": 0, "good": 0, "alert": None, "since": None})
            rec.update({"name": name, "last_seen": now, "row": row})
            if level != "ok":
                rec["bad"] += 1
                rec["good"] = 0
            else:
                rec["good"] += 1
                rec["bad"] = 0

            if ignored and rec["alert"] is not None:
                events.append(("cleared", row | {"was": rec["alert"], "since": rec["since"]}))
                rec["alert"] = None
                rec["since"] = None
            elif rec["alert"] is None and level != "ok" and rec["bad"] >= n_bad:
                rec["alert"] = level
                rec["since"] = now_iso()
                rec["worst"] = sig
                events.append(("new", row))
            elif rec["alert"] is not None:
                if level == "ok" and rec["good"] >= n_good:
                    events.append(("cleared", row | {"was": rec["alert"], "since": rec["since"]}))
                    rec["alert"] = None
                    rec["since"] = None
                elif level != "ok":
                    if sig is not None and (rec.get("worst") is None or sig < rec["worst"]):
                        rec["worst"] = sig
                    if LEVEL_RANK[level] > LEVEL_RANK[rec["alert"]]:
                        rec["alert"] = level
                        events.append(("escalated", row))
            rows.append(row)

        # clients that left
        for mac, rec in list(self.clients.items()):
            if mac in seen:
                continue
            if rec.get("alert"):
                events.append(("disconnected", (rec.get("row") or {"mac": mac, "name": rec.get("name")})
                               | {"was": rec["alert"], "since": rec.get("since")}))
                rec["alert"] = None
                rec["since"] = None
            rec["bad"] = rec["good"] = 0
            if rec.get("online", True):
                rec["online"] = False
                self.publish_client_offline(mac)
            if now - rec.get("last_seen", now) > FORGET_AFTER_DAYS * 86400:
                self.forget_client(mac)
                del self.clients[mac]
        for mac in seen:
            self.clients[mac]["online"] = True

        self.save_state()
        rows.sort(key=lambda r: (r["signal"] is None, r["signal"] if r["signal"] is not None else 0))
        self.publish(rows)
        for kind, row in events:
            self.emit_event(kind, row)
        self.update_notification()

    # ---------------------------------------------------------------- outputs
    def alerts(self) -> list:
        out = []
        for mac, rec in self.clients.items():
            if rec.get("alert") and rec.get("online", True):
                row = rec.get("row") or {}
                out.append({
                    "name": row.get("name", rec.get("name")), "mac": mac,
                    "level": rec["alert"], "current": row.get("level"),
                    "signal": row.get("signal"), "worst_signal": rec.get("worst"),
                    "satisfaction": row.get("satisfaction"), "ap": row.get("ap"),
                    "band": row.get("band"), "channel": row.get("channel"),
                    "ssid": row.get("ssid"), "since": rec.get("since"),
                })
        out.sort(key=lambda a: (-LEVEL_RANK[a["level"]], a["signal"] if a["signal"] is not None else 0))
        return out

    def build_summary(self, rows: list) -> dict:
        active = [r for r in rows if not r["ignored"]]
        sigs = [r["signal"] for r in active if r["signal"] is not None]
        alerts = self.alerts()
        aps = {}
        for r in rows:
            a = aps.setdefault(r["ap"], {"ap": r["ap"], "clients": 0, "weak": 0, "_sum": 0, "_n": 0,
                                         "bands": {}})
            a["clients"] += 1
            a["bands"][r["band"]] = a["bands"].get(r["band"], 0) + 1
            if r["level"] in ("weak", "critical"):
                a["weak"] += 1
            if r["signal"] is not None:
                a["_sum"] += r["signal"]
                a["_n"] += 1
        ap_list = []
        for a in sorted(aps.values(), key=lambda x: x["ap"]):
            a["avg_signal"] = round(a.pop("_sum") / a["_n"]) if a["_n"] else None
            a.pop("_n")
            ap_list.append(a)
        worst = next((r for r in active if r["signal"] is not None), None)  # rows are sorted weakest first
        return {
            "bad_connections": len(alerts),
            "critical": sum(1 for a in alerts if a["level"] == "critical"),
            "weak": sum(1 for a in alerts if a["level"] == "weak"),
            "poor": sum(1 for a in alerts if a["level"] == "poor"),
            "problem": "ON" if alerts else "OFF",
            "wifi_clients": len(rows),
            "worst_signal": worst["signal"] if worst else None,
            "worst_client": worst["name"] if worst else None,
            "average_signal": round(sum(sigs) / len(sigs)) if sigs else None,
            "below_warning_now": sum(1 for r in active if r["level"] in ("weak", "critical")),
            "last_poll": now_iso(),
            "alerts": alerts,
            "access_points": ap_list,
            "clients": [{"name": r["name"], "mac": r["mac"], "signal": r["signal"],
                         "satisfaction": r["satisfaction"], "ap": r["ap"], "band": r["band"],
                         "level": r["level"], "ignored": r["ignored"]} for r in rows],
            "ignored_clients": [{"name": r["name"], "mac": r["mac"], "signal": r["signal"], "ap": r["ap"],
                                 "by": self.ignored_by(r["mac"], r["name"])}
                                for r in rows if r["ignored"]],
            "thresholds": {"warn_dbm": self.warn_dbm, "critical_dbm": self.critical_dbm,
                           "min_satisfaction": self.min_satisfaction},
        }

    def publish(self, rows: list):
        summary = self.build_summary(rows)
        self.last_summary = summary
        if self.mqtt and self.mqtt.connected.is_set():
            self.mqtt.publish(f"{BASE_TOPIC}/summary", {k: v for k, v in summary.items()
                                                        if k not in ("alerts", "access_points", "clients", "ignored_clients")},
                              retain=True)
            self.mqtt.publish(f"{BASE_TOPIC}/alerts", {"alerts": summary["alerts"],
                                                       "thresholds": summary["thresholds"]}, retain=True)
            self.mqtt.publish(f"{BASE_TOPIC}/clients", {"clients": summary["clients"],
                                                        "access_points": summary["access_points"],
                                                        "ignored_clients": summary["ignored_clients"]},
                              retain=True)
            self.publish_selects()
            if self.opts.get("per_client_sensors"):
                for r in rows:
                    self.publish_client(r)
        else:
            self.publish_rest(summary)

    def publish_rest(self, s: dict):
        """Fallback without MQTT: plain entities via the REST API (lost on HA restart until next poll)."""
        self.ha.set_state("sensor.wifi_monitor_bad_connections", s["bad_connections"], {
            "friendly_name": "Wi-Fi bad connections", "icon": "mdi:wifi-alert",
            "unit_of_measurement": "clients", "state_class": "measurement",
            "alerts": s["alerts"], "thresholds": s["thresholds"]})
        self.ha.set_state("sensor.wifi_monitor_wifi_clients", s["wifi_clients"], {
            "friendly_name": "Wi-Fi clients", "icon": "mdi:devices", "unit_of_measurement": "clients",
            "state_class": "measurement", "clients": s["clients"], "access_points": s["access_points"]})
        for key, label in (("worst_signal", "Wi-Fi worst signal"), ("average_signal", "Wi-Fi average signal")):
            self.ha.set_state(f"sensor.wifi_monitor_{key}", s[key] if s[key] is not None else "unknown", {
                "friendly_name": label, "unit_of_measurement": "dBm", "device_class": "signal_strength",
                "state_class": "measurement", "client": s["worst_client"] if key == "worst_signal" else None})
        self.ha.set_state("binary_sensor.wifi_monitor_problem", s["problem"].lower(), {
            "friendly_name": "Wi-Fi connection problem", "device_class": "problem"})

    def client_entity(self, mac: str, name: str):
        return f"{DISCOVERY_PREFIX}/sensor/wifi_monitor/{mac_id(mac)}/config"

    def publish_client(self, r: dict):
        mac = r["mac"]
        rec = self.clients.get(mac, {})
        if mac not in self.announced or rec.get("announced_name") != r["name"]:
            first = "announced_name" not in rec
            cfg = {
                "name": f"{r['name']} signal",
                "unique_id": f"wifi_monitor_{mac_id(mac)}",
                "state_topic": f"{BASE_TOPIC}/client/{mac_id(mac)}",
                "value_template": "{{ value_json.signal }}",
                "json_attributes_topic": f"{BASE_TOPIC}/client/{mac_id(mac)}",
                "unit_of_measurement": "dBm",
                "device_class": "signal_strength",
                "state_class": "measurement",
                "entity_category": "diagnostic",
                "availability_mode": "all",
                "availability": [
                    {"topic": AVAIL_TOPIC},
                    {"topic": f"{BASE_TOPIC}/client/{mac_id(mac)}/availability"},
                ],
                "device": self.device_info(),
            }
            if first:
                cfg["default_entity_id"] = f"sensor.wifi_signal_{slugify(r['name'])}"
            self.mqtt.publish(self.client_entity(mac, r["name"]), cfg, retain=True)
            rec["announced_name"] = r["name"]
            self.announced.add(mac)
        self.mqtt.publish(f"{BASE_TOPIC}/client/{mac_id(mac)}/availability", "online", retain=True)
        self.mqtt.publish(f"{BASE_TOPIC}/client/{mac_id(mac)}", r, retain=True)

    def publish_client_offline(self, mac: str):
        if self.mqtt and self.mqtt.connected.is_set() and self.opts.get("per_client_sensors"):
            self.mqtt.publish(f"{BASE_TOPIC}/client/{mac_id(mac)}/availability", "offline", retain=True)

    def forget_client(self, mac: str):
        log.info("Removing sensor for %s (not seen for %d days)", mac, FORGET_AFTER_DAYS)
        if self.mqtt and self.mqtt.connected.is_set():
            self.mqtt.publish(self.client_entity(mac, ""), "", retain=True)
            for t in (f"{BASE_TOPIC}/client/{mac_id(mac)}", f"{BASE_TOPIC}/client/{mac_id(mac)}/availability"):
                self.mqtt.publish(t, "", retain=True)

    def publish_console_status(self, ok: bool, error):
        if self.mqtt and self.mqtt.connected.is_set():
            self.mqtt.publish(f"{BASE_TOPIC}/console", {"state": "ON" if ok else "OFF", "error": error},
                              retain=True)
        elif not ok:
            self.ha.set_state("binary_sensor.wifi_monitor_unifi_console", "off", {
                "friendly_name": "Wi-Fi monitor UniFi connection", "device_class": "connectivity",
                "error": error})

    def emit_event(self, kind: str, row: dict):
        data = {"type": kind, **{k: row.get(k) for k in (
            "name", "mac", "signal", "satisfaction", "level", "ap", "band", "channel", "ssid")}}
        if "was" in row:
            data["was"] = row["was"]
            data["since"] = row.get("since")
        verb = {"new": "has a bad connection", "escalated": "got worse", "cleared": "recovered",
                "disconnected": "disconnected while flagged"}[kind]
        data["message"] = (f"{data['name']} {verb}: {data['signal']} dBm"
                           f" on {data['ap']} ({data['band']})" if data.get("signal") is not None
                           else f"{data['name']} {verb}")
        log.info("%s", data["message"])
        self.ha.fire_event("wifi_monitor_alert", data)
        self.log_csv(data)

    def log_csv(self, data: dict):
        path = os.path.join(SHARE_DIR, "alerts.csv")
        new = not os.path.exists(path)
        try:
            with open(path, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["time", "event", "name", "mac", "level", "signal_dbm", "satisfaction",
                                "ap", "band", "channel", "ssid"])
                w.writerow([now_iso(), data["type"], data["name"], data["mac"], data.get("level"),
                            data.get("signal"), data.get("satisfaction"), data.get("ap"), data.get("band"),
                            data.get("channel"), data.get("ssid")])
        except OSError as err:
            log.warning("Could not write %s: %s", path, err)

    def update_notification(self):
        if not self.opts.get("persistent_notification"):
            return
        alerts = self.alerts()
        if not alerts:
            text = None
        else:
            icon = {"critical": "🔴", "weak": "🟠", "poor": "🟡"}
            lines = []
            for a in alerts:
                exp = f", {a['satisfaction']}% experience" if a["satisfaction"] is not None else ""
                lines.append(f"{icon[a['level']]} **{a['name']}** – {a['signal']} dBm{exp}"
                             f" · {a['ap']} ({a['band']})")
            text = "\n".join(lines)
        if text == self.notification_text:
            return
        self.notification_text = text
        if text is None:
            self.ha.service("persistent_notification", "dismiss", {"notification_id": "wifi_monitor"})
        else:
            self.ha.service("persistent_notification", "create", {
                "notification_id": "wifi_monitor",
                "title": f"Wi-Fi: {len(alerts)} bad connection{'s' if len(alerts) != 1 else ''}",
                "message": text,
            })

    # ---------------------------------------------------------------- discovery
    def device_info(self) -> dict:
        return {"identifiers": ["wifi_monitor"], "name": "Wi-Fi Signal Monitor",
                "manufacturer": "Home Assistant app", "model": "UniFi Wi-Fi signal monitor",
                "sw_version": APP_VERSION}

    def publish_discovery(self):
        if not self.mqtt:
            return
        dev = self.device_info()
        avail = {"availability_topic": AVAIL_TOPIC}
        s = f"{BASE_TOPIC}/summary"

        def sensor(key, name, **extra):
            cfg = {"name": name, "unique_id": f"wifi_monitor_{key}",
                   "default_entity_id": f"sensor.wifi_monitor_{key}", "state_topic": s,
                   "value_template": f"{{{{ value_json.{key} }}}}", "device": dev, **avail, **extra}
            self.mqtt.publish(f"{DISCOVERY_PREFIX}/sensor/wifi_monitor/{key}/config", cfg, retain=True)

        sensor("bad_connections", "Bad connections", icon="mdi:wifi-alert", unit_of_measurement="clients",
               state_class="measurement", json_attributes_topic=f"{BASE_TOPIC}/alerts")
        sensor("wifi_clients", "Wi-Fi clients", icon="mdi:devices", unit_of_measurement="clients",
               state_class="measurement", json_attributes_topic=f"{BASE_TOPIC}/clients")
        sensor("worst_signal", "Worst signal", unit_of_measurement="dBm", device_class="signal_strength",
               state_class="measurement",
               json_attributes_topic=s, json_attributes_template="{{ {'client': value_json.worst_client} | tojson }}")
        sensor("average_signal", "Average signal", unit_of_measurement="dBm",
               device_class="signal_strength", state_class="measurement")
        sensor("critical", "Critical connections", icon="mdi:wifi-strength-alert-outline",
               unit_of_measurement="clients", state_class="measurement")
        sensor("weak", "Weak connections", icon="mdi:wifi-strength-1-alert", unit_of_measurement="clients",
               state_class="measurement")
        sensor("poor", "Poor experience", icon="mdi:emoticon-sad-outline", unit_of_measurement="clients",
               state_class="measurement")
        sensor("last_poll", "Last poll", device_class="timestamp", entity_category="diagnostic")

        self.mqtt.publish(f"{DISCOVERY_PREFIX}/binary_sensor/wifi_monitor/problem/config", {
            "name": "Connection problem", "unique_id": "wifi_monitor_problem",
            "default_entity_id": "binary_sensor.wifi_monitor_problem", "state_topic": s,
            "value_template": "{{ value_json.problem }}", "device_class": "problem", "device": dev, **avail,
        }, retain=True)
        self.mqtt.publish(f"{DISCOVERY_PREFIX}/binary_sensor/wifi_monitor/unifi_console/config", {
            "name": "UniFi console", "unique_id": "wifi_monitor_unifi_console",
            "default_entity_id": "binary_sensor.wifi_monitor_unifi_console",
            "state_topic": f"{BASE_TOPIC}/console", "value_template": "{{ value_json.state }}",
            "json_attributes_topic": f"{BASE_TOPIC}/console", "device_class": "connectivity",
            "entity_category": "diagnostic", "device": dev, **avail,
        }, retain=True)

        for key, name, lo, hi in (("warn_dbm", "Warning threshold", -95, -40),
                                  ("critical_dbm", "Critical threshold", -100, -45),
                                  ("min_satisfaction", "Minimum experience", 0, 100)):
            self.mqtt.publish(f"{DISCOVERY_PREFIX}/number/wifi_monitor/{key}/config", {
                "name": name, "unique_id": f"wifi_monitor_{key}",
                "default_entity_id": f"number.wifi_monitor_{key}",
                "command_topic": f"{BASE_TOPIC}/set/{key}", "state_topic": f"{BASE_TOPIC}/thresholds",
                "value_template": f"{{{{ value_json.{key} }}}}", "min": lo, "max": hi, "step": 1,
                "mode": "box", "unit_of_measurement": "%" if key == "min_satisfaction" else "dBm",
                "entity_category": "config", "device": dev, **avail,
            }, retain=True)
        self.announced.clear()
        self.select_options = None
        self.publish_thresholds()

    def publish_selects(self, force: bool = False):
        """Two dropdowns in HA: add a client to / remove it from the dashboard ignore list."""
        if not (self.mqtt and self.mqtt.connected.is_set()):
            return
        online = sorted({rec.get("name") for rec in self.clients.values()
                         if rec.get("online", True) and rec.get("name")}, key=str.lower)
        add = [n for n in online if not self.ignored(self.mac_by_name(n) or "", n)]
        remove = sorted({self.clients[m].get("name") for m in self.ignored_macs
                         if m in self.clients and self.clients[m].get("name")}, key=str.lower)
        opts = (tuple(add), tuple(remove))
        if opts != self.select_options or force:
            dev = self.device_info()
            for key, name, icon, options in (
                    ("ignore_client", "Ignore a client", "mdi:eye-off-outline", add),
                    ("unignore_client", "Stop ignoring a client", "mdi:eye-outline", remove)):
                self.mqtt.publish(f"{DISCOVERY_PREFIX}/select/wifi_monitor/{key}/config", {
                    "name": name, "unique_id": f"wifi_monitor_{key}",
                    "default_entity_id": f"select.wifi_monitor_{key}",
                    "command_topic": f"{BASE_TOPIC}/set/{key}",
                    "state_topic": f"{BASE_TOPIC}/select_state",
                    "options": [SELECT_NONE] + options, "icon": icon,
                    "device": dev, "availability_topic": AVAIL_TOPIC,
                }, retain=True)
            self.select_options = opts
            time.sleep(0.2)  # let HA apply the new options before resetting the state
        self.mqtt.publish(f"{BASE_TOPIC}/select_state", SELECT_NONE, retain=True)

    def publish_thresholds(self):
        if self.mqtt and self.mqtt.connected.is_set():
            self.mqtt.publish(f"{BASE_TOPIC}/thresholds", {"warn_dbm": self.warn_dbm,
                                                           "critical_dbm": self.critical_dbm,
                                                           "min_satisfaction": self.min_satisfaction},
                              retain=True)


# --------------------------------------------------------------------------- main
def main() -> int:
    opts = load_json(OPTIONS_PATH, None)
    if opts is None:
        print(f"Cannot read {OPTIONS_PATH}", file=sys.stderr)
        return 1
    logging.basicConfig(level=getattr(logging, opts.get("log_level", "info").upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    if opts["critical_dbm"] >= opts["warn_dbm"]:
        log.warning("critical_dbm (%s) should be lower than warn_dbm (%s)", opts["critical_dbm"], opts["warn_dbm"])

    ha = HomeAssistant()
    mqtt = None
    monitor_ref = {}
    cfg = ha.mqtt_service(opts)
    if cfg:
        try:
            mqtt = Mqtt(cfg, lambda k, p: monitor_ref["m"].handle_command(k, p))
            mqtt.connected.wait(10)
        except Exception as err:  # noqa: BLE001
            log.error("MQTT setup failed (%s) – falling back to REST sensors", err)
            mqtt = None
    else:
        log.warning("No MQTT broker found – using basic REST sensors (install Mosquitto for full features)")

    try:
        monitor = Monitor(opts, ha, mqtt)
    except UniFiError as err:
        log.error("%s", err)
        return 1
    monitor_ref["m"] = monitor
    if mqtt and mqtt.connected.is_set():
        monitor.publish_discovery()

    def stop(*_):
        STOP.set()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    log.info("Wi-Fi Signal Monitor %s started – polling %s every %ss (warn < %s dBm, critical < %s dBm)",
             APP_VERSION, monitor.unifi.base, opts["poll_interval_seconds"], monitor.warn_dbm, monitor.critical_dbm)
    interval = opts["poll_interval_seconds"]
    while not STOP.is_set():
        started = time.monotonic()
        try:
            monitor.poll()
        except Exception:  # noqa: BLE001 – never let one bad poll kill the app
            log.exception("Unexpected error during poll")
        wait = interval if not monitor.last_error else min(interval * 2, 300)
        STOP.wait(max(1, wait - (time.monotonic() - started)))

    if mqtt:
        mqtt.stop()
    log.info("Stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
