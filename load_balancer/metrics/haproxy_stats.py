"""Phase 7: HAProxy stats CSV -> proxy-shaped run snapshot.

The combined CSV (load_balancer/metrics/wrk_parser.py) expects a snapshot
dict shaped like collect_run_snapshot()'s output. HAProxy's stats CSV is
the only machine-readable source for its per-run counters, so this module
parses it and synthesizes that shape:

- proxy.counters from the client-facing row: the frontend's req_tot and
  hrsp_* classes when a frontend pxname is given (counts responses that got
  no server too), otherwise the BACKEND row,
- proxy.latency_ms empty: HAProxy exposes averages, not percentiles; the
  fair cross-implementation latency numbers are wrk-side (documented in
  docs/metric_definitions.md),
- backends[] from the server rows (stot, scur, UP/DOWN, addr).
"""

import csv
import io
import time


def _int(value: str) -> int:
    return int(value) if value not in (None, "", "-") else 0


def parse_haproxy_stats_csv(text: str) -> dict:
    """Parse a HAProxy `;csv` export into frontends/backends/servers dicts."""
    # HAProxy prefixes the first header cell with "# "; DictReader would
    # otherwise expose it as "# pxname".
    first_line, sep, rest = text.partition("\n")
    if first_line.startswith("# "):
        text = first_line[2:] + sep + rest
    reader = csv.DictReader(io.StringIO(text))
    out = {"frontends": {}, "backends": {}, "servers": {}}
    for row in reader:
        pxname = (row.get("pxname") or "").strip()
        svname = (row.get("svname") or "").strip()
        if not pxname or not svname:
            continue
        record = {
            "status": (row.get("status") or "").strip(),
            "check_status": (row.get("check_status") or "").strip(),
            "check_code": (row.get("check_code") or "").strip(),
            "check_health": _int(row.get("check_health")),
            "chkfail": _int(row.get("chkfail")),
            "chkdown": _int(row.get("chkdown")),
            "stot": _int(row.get("stot")),
            "req_tot": _int(row.get("req_tot")),
            "scur": _int(row.get("scur")),
            "smax": _int(row.get("smax")),
            "lastchg": _int(row.get("lastchg")),
            "addr": (row.get("addr") or "").strip(),
            "status_counts": {
                f"{n}xx": _int(row.get(f"hrsp_{n}xx")) for n in (1, 2, 3, 4, 5)
            },
        }
        if svname == "FRONTEND":
            out["frontends"][pxname] = record
        elif svname == "BACKEND":
            out["backends"][pxname] = record
        else:
            out["servers"].setdefault(pxname, {})[svname] = record
    return out


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def synthesize_snapshot(
    stats: dict,
    strategy_name: str,
    pxname: str = "be_roundrobin",
    captured_at: str | None = None,
    frontend_pxname: str | None = None,
) -> dict:
    """Build the collect_run_snapshot()-shaped dict from parsed stats.

    Latency percentiles are intentionally empty: HAProxy's stats page
    reports averages (qtime/ctime/rtime/ttime), not distributions, and the
    cross-implementation latency comparison uses wrk-side numbers.
    """
    servers = stats["servers"].get(pxname, {})
    backend_row = stats["backends"].get(pxname, {})
    if pxname not in stats["backends"]:
        raise ValueError(
            f"HAProxy stats have no backend {pxname!r}; "
            f"available: {sorted(stats['backends'])}"
        )
    if frontend_pxname is not None and frontend_pxname not in stats["frontends"]:
        raise ValueError(
            f"HAProxy stats have no frontend {frontend_pxname!r}; "
            f"available: {sorted(stats['frontends'])}"
        )
    frontend_row = (
        stats["frontends"].get(frontend_pxname, {}) if frontend_pxname else {}
    )
    # Client-facing counters: a FRONTEND row (preferred) or the BACKEND row
    # counts requests that never got a server (all DOWN / queue), while the
    # sum of server stot only covers delivered requests.
    client_row = frontend_row or backend_row
    total = sum(s["stot"] for s in servers.values())
    requests_total = client_row.get("req_tot") or backend_row.get("stot", 0)
    status_counts = dict(client_row.get("status_counts", {}))
    # A FRONTEND row's stot counts client connections; a backend/server
    # stot counts sessions (one per request here because of http-server-close).
    connections = frontend_row.get("stot", 0)

    backends = []
    for backend_id, server in servers.items():
        host, _, port = server["addr"].rpartition(":")
        backends.append(
            {
                "backend_id": backend_id,
                "host": host,
                "port": _int(port),
                # "UP 2/3" / "UP 1/3" still receive traffic while the fall
                # counter drains; "DOWN 1/2" does not yet receive traffic.
                "healthy": server["status"].startswith("UP"),
                "total_requests": server["stot"],
                "active_requests": server["scur"],
                "share_pct": (
                    round(100.0 * server["stot"] / total, 2) if total else 0.0
                ),
                # stats CSV has no transition history; acceptance polling
                # records DOWN/UP timestamps (see scripts/haproxy_acceptance.py).
                "state_transitions": [],
            }
        )

    return {
        "meta": {
            "strategy": strategy_name,
            "run_started_at": captured_at or _utc_now(),
            "source": "haproxy_stats",
            "pxname": pxname,
        },
        "proxy": {
            "counters": {
                "requests_total": requests_total,
                "connections_total": connections,
                "responses_5xx": status_counts.get("5xx", 0),
                # HAProxy has no equivalent for internal errors that never
                # became a status (docs/metric_definitions.md).
                "errors": None,
                # class aggregates: the comparable form (docs/metric_definitions.md)
                "status_counts": dict(status_counts),
            },
            "latency_ms": {
                "samples": 0,
                "mean_ms": None,
                "p50": None,
                "p95": None,
                "p99": None,
            },
        },
        "backends": backends,
    }
