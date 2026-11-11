"""Firebird 2.5 Prometheus Exporter - Remote-Variante fuer den PMM-Server.

Pro Firebird-Instanz laeuft ein eigener Exporter-Container auf dem PMM-Server.
Der Exporter greift per TCP (Standardport 3050) remote auf die Datenbank zu und
wird vom lokalen pmm-agent ueber einen eigenen Port abgefragt.

Alle Metriken werden wie bisher in metrics.json definiert (gleiches Format,
Modi: scalar / row / labeled). Der Code enthaelt nur die Exporter-eigenen
Metriken (firebird_up, Scrape-Dauer, Instanz-Info).

Konfiguration: Umgebungsvariablen (haben Vorrang) oder optional config.json.

  Umgebungsvariable        config.json                 Default
  -----------------------  --------------------------  -----------------
  FB_HOST                  database.host               (Pflicht)
  FB_PORT                  database.port               3050
  FB_DATABASE              database.path               (Pflicht, Pfad/Alias)
  FB_USER                  database.user               SYSDBA
  FB_PASSWORD              database.password           (Pflicht)
  FB_PASSWORD_FILE         -                           (alternativ zu FB_PASSWORD,
                                                        z. B. Docker Secret)
  FB_CHARSET               database.charset            UTF8
  FB_CONNECT_TIMEOUT       database.connect_timeout    5 (Sekunden, TCP-Vorpruefung)
  EXPORTER_PORT            server.port                 9103
  EXPORTER_LISTEN_ADDR     server.listen_address       0.0.0.0
  SCRAPE_CACHE_SECONDS     server.scrape_interval_seconds  15
  INSTANCE_NAME            instance.name               FB_HOST
  METRICS_FILE             -                           metrics.json
  CONFIG_FILE              -                           config.json (optional)
"""
import json
import os
import signal
import socket
import threading
import time

import fdb
from prometheus_client import REGISTRY, start_http_server
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

MODES = ("scalar", "row", "labeled")


# --------------------------------------------------------------------------
# Konfiguration
# --------------------------------------------------------------------------
def load_json_file(filename):
    """Laedt eine JSON-Datei und meldet fehlende Dateien klar."""
    if not os.path.exists(filename):
        raise FileNotFoundError(f"Die Datei {filename} wurde nicht gefunden!")
    with open(filename, "r", encoding="utf-8") as f:
        return json.load(f)


def _pick(env_name, file_value, default=None):
    """Umgebungsvariable vor config.json vor Default."""
    val = os.environ.get(env_name)
    if val is not None and val != "":
        return val
    if file_value is not None:
        return file_value
    return default


def load_settings():
    """Baut die Gesamtkonfiguration aus Umgebung und optionaler config.json."""
    cfg = {}
    cfg_file = os.environ.get("CONFIG_FILE", "config.json")
    if os.path.exists(cfg_file):
        cfg = load_json_file(cfg_file)
    db = cfg.get("database", {})
    srv = cfg.get("server", {})

    password = os.environ.get("FB_PASSWORD")
    pw_file = os.environ.get("FB_PASSWORD_FILE")
    if not password and pw_file:
        with open(pw_file, "r", encoding="utf-8") as f:
            password = f.read().strip()
    if not password:
        password = db.get("password")

    settings = {
        "host": _pick("FB_HOST", db.get("host")),
        "port": int(_pick("FB_PORT", db.get("port"), 3050)),
        "database": _pick("FB_DATABASE", db.get("path")),
        "user": _pick("FB_USER", db.get("user"), "SYSDBA"),
        "password": password,
        "charset": _pick("FB_CHARSET", db.get("charset"), "UTF8"),
        "connect_timeout": float(_pick("FB_CONNECT_TIMEOUT", db.get("connect_timeout"), 5)),
        "exporter_port": int(_pick("EXPORTER_PORT", srv.get("port"), 9103)),
        "listen_addr": _pick("EXPORTER_LISTEN_ADDR", srv.get("listen_address"), "0.0.0.0"),
        "cache_seconds": float(_pick("SCRAPE_CACHE_SECONDS",
                                     srv.get("scrape_interval_seconds"), 15)),
        "metrics_file": os.environ.get("METRICS_FILE", "metrics.json"),
    }
    settings["instance_name"] = _pick("INSTANCE_NAME", cfg.get("instance", {}).get("name"),
                                      settings["host"])

    missing = [k for k in ("host", "database", "password") if not settings[k]]
    if missing:
        raise ValueError(
            "Pflichtangaben fehlen: " + ", ".join(missing) +
            " (FB_HOST, FB_DATABASE, FB_PASSWORD bzw. FB_PASSWORD_FILE setzen)")
    return settings


def build_dsn(host, port, database):
    """Firebird-2.5-DSN fuer Remote-Zugriff: host/port:pfad_oder_alias."""
    return f"{host}/{port}:{database}"


# --------------------------------------------------------------------------
# Metrik-Definitionen (unveraendert zum bisherigen Exporter)
# --------------------------------------------------------------------------
def load_metric_definitions(cfg):
    """Prueft metrics.json beim Start und ueberspringt deaktivierte Eintraege."""
    definitions = []
    for i, m in enumerate(cfg.get("metrics", [])):
        if m.get("enabled", True) is False:
            continue
        mode = m.get("mode", "scalar")
        if mode not in MODES:
            raise ValueError(f"metrics[{i}]: unbekannter mode '{mode}'")
        if "sql" not in m:
            raise ValueError(f"metrics[{i}]: 'sql' fehlt")
        if mode in ("scalar", "labeled") and "name" not in m:
            raise ValueError(f"metrics[{i}]: 'name' fehlt")
        if mode == "row" and not m.get("columns"):
            raise ValueError(f"metrics[{i}]: 'columns' fehlt")
        if mode == "labeled" and not m.get("labels"):
            raise ValueError(f"metrics[{i}]: 'labels' fehlt")
        definitions.append(m)
    return definitions


def new_family(kind, name, help_text, labels=None):
    """Counter ohne _created-Metrik, Gauge unveraendert."""
    labels = labels or []
    if kind == "counter":
        base = name[:-6] if name.endswith("_total") else name
        return CounterMetricFamily(base, help_text, labels=labels)
    return GaugeMetricFamily(name, help_text, labels=labels)


def run_metric(cur, m):
    """Fuehrt eine Metrik-Definition aus und liefert Metric-Families."""
    cur.execute(m["sql"])
    mode = m.get("mode", "scalar")
    help_text = m.get("help", m.get("name", ""))

    if mode == "scalar":
        row = cur.fetchone()
        if not row or row[0] is None:
            return []
        fam = new_family(m.get("type", "gauge"), m["name"], help_text)
        fam.add_metric([], float(row[0]))
        return [fam]

    if mode == "row":
        row = cur.fetchone()
        if not row:
            return []
        out = []
        for i, col in enumerate(m["columns"]):
            if i < len(row) and row[i] is not None:
                fam = new_family(col.get("type", "gauge"), col["name"],
                                 col.get("help", col["name"]))
                fam.add_metric([], float(row[i]))
                out.append(fam)
        return out

    # labeled
    labels = m["labels"]
    n = len(labels)
    fam = new_family(m.get("type", "gauge"), m["name"], help_text, labels)
    for row in cur.fetchall():
        if len(row) > n and row[n] is not None:
            fam.add_metric([str(x).strip() for x in row[:n]], float(row[n]))
    return [fam]


# --------------------------------------------------------------------------
# Collector
# --------------------------------------------------------------------------
class RemoteFirebirdCollector:
    """Fragt eine remote Firebird-Instanz bei jedem Scrape ab (mit kurzem Cache)."""

    def __init__(self, settings, definitions):
        self.s = settings
        self.definitions = definitions
        self.dsn = build_dsn(settings["host"], settings["port"], settings["database"])
        self._lock = threading.Lock()
        self._cache = None
        self._cache_ts = 0.0

    def describe(self):
        # Verhindert, dass die Registry beim Registrieren schon scrapt.
        return []

    def collect(self):
        with self._lock:
            now = time.time()
            if self._cache is None or now - self._cache_ts >= self.s["cache_seconds"]:
                self._cache = self._scrape()
                self._cache_ts = time.time()
            families = self._cache
        for fam in families:
            yield fam

    def _tcp_check(self):
        """Schnelle Erreichbarkeitspruefung, damit ein toter Host nicht den
        langen OS-Timeout von fdb.connect() abwartet."""
        with socket.create_connection((self.s["host"], self.s["port"]),
                                      timeout=self.s["connect_timeout"]):
            pass

    def _scrape(self):
        start = time.time()
        families = []
        up = 0
        failed = 0
        con = None
        try:
            self._tcp_check()
            con = fdb.connect(
                dsn=self.dsn,
                user=self.s["user"],
                password=self.s["password"],
                charset=self.s["charset"],
            )
            up = 1
            for m in self.definitions:
                cur = con.cursor()
                try:
                    families.extend(run_metric(cur, m))
                except Exception as err:  # eine kaputte Query stoppt die anderen nicht
                    failed += 1
                    label = m.get("name") or "row-query"
                    print(f"[{self.s['instance_name']}] Fehler bei Metrik '{label}': {err}",
                          flush=True)
                finally:
                    cur.close()
        except Exception as err:
            print(f"[{self.s['instance_name']}] Fehler bei der Datenbankverbindung "
                  f"({self.s['host']}:{self.s['port']}): {err}", flush=True)
        finally:
            if con:
                try:
                    con.close()
                except Exception:
                    pass

        g = GaugeMetricFamily("firebird_up", "1 wenn die Verbindung zu Firebird klappt")
        g.add_metric([], up)
        families.append(g)
        g = GaugeMetricFamily("firebird_scrape_failed_metrics",
                              "Anzahl fehlgeschlagener Metrik-Queries im letzten Scrape")
        g.add_metric([], failed)
        families.append(g)
        g = GaugeMetricFamily("firebird_scrape_duration_seconds", "Dauer des letzten Scrapes")
        g.add_metric([], time.time() - start)
        families.append(g)
        g = GaugeMetricFamily("firebird_exporter_info",
                              "Konstante 1; Labels beschreiben die abgefragte Instanz",
                              labels=["instance_name", "fb_host", "fb_port", "fb_database"])
        g.add_metric([self.s["instance_name"], self.s["host"], str(self.s["port"]),
                      self.s["database"]], 1)
        families.append(g)
        return families


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main():
    settings = load_settings()
    definitions = load_metric_definitions(load_json_file(settings["metrics_file"]))

    REGISTRY.register(RemoteFirebirdCollector(settings, definitions))
    start_http_server(settings["exporter_port"], addr=settings["listen_addr"])

    print(f"Firebird 2.5 Remote-Exporter '{settings['instance_name']}' auf "
          f"{settings['listen_addr']}:{settings['exporter_port']} gestartet.", flush=True)
    print(f"Ziel: {settings['host']}:{settings['port']} / {settings['database']} "
          f"(User {settings['user']})", flush=True)
    print(f"{len(definitions)} Metrik-Definitionen geladen, Cache "
          f"{settings['cache_seconds']}s.", flush=True)

    # Sauberes Beenden bei 'docker stop' (SIGTERM) und Strg+C.
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()
    print("Exporter wird beendet.", flush=True)


if __name__ == "__main__":
    main()
