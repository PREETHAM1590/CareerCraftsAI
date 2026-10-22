"""Generate one Grafana dashboard per server (APP, INFRA, COMPUTERS), never combined.

Run from this directory: python gen_dashboards.py grafana/dashboards
"""

import json
import os
import sys

PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}
# Loki logs this one line at error level whenever Grafana cancels a query it
# no longer needs (panel refresh, dashboard closed). Match only that line, so
# any other scheduler_processor.go error still shows.
QUERY_CANCEL_NOISE = '!~ "caller=scheduler_processor[.]go:[0-9]+ .*code = Canceled"'
# Alloy logs this when a container is removed between its discovery and its
# inspect (deploys on app-vm). Match only that pairing, so other Alloy errors
# still show.
INSPECT_RACE_NOISE = (
    '!~ "msg=\\"error inspecting Docker container\\".*No such container"'
)
ERROR_NOISE = f"{QUERY_CANCEL_NOISE} {INSPECT_RACE_NOISE}"
PCT = [
    {"color": "green", "value": None},
    {"color": "orange", "value": 70},
    {"color": "red", "value": 90},
]
UP = [{"color": "red", "value": None}, {"color": "green", "value": 1}]
NET = 'device!~"lo|veth.*|docker.*|br-.*"'


class Dash:
    def __init__(self):
        self.panels, self.y, self._id = [], 0, 0

    def nid(self):
        self._id += 1
        return self._id

    def row(self, title):
        self.panels.append(
            {
                "type": "row",
                "title": title,
                "id": self.nid(),
                "collapsed": False,
                "gridPos": {"h": 1, "w": 24, "x": 0, "y": self.y},
                "panels": [],
            }
        )
        self.y += 1

    def ts(
        self,
        title,
        targets,
        x,
        w,
        h=8,
        unit="short",
        ds=PROM,
        desc="",
        max_=None,
        stack=False,
    ):
        d = {
            "unit": unit,
            "custom": {
                "lineWidth": 1,
                "fillOpacity": 10,
                "stacking": {"mode": "normal" if stack else "none"},
            },
        }
        if max_ is not None:
            d.update(min=0, max=max_)
        self.panels.append(
            {
                "type": "timeseries",
                "title": title,
                "id": self.nid(),
                "datasource": ds,
                "description": desc,
                "gridPos": {"h": h, "w": w, "x": x, "y": self.y},
                "fieldConfig": {"defaults": d, "overrides": []},
                "options": {
                    "legend": {
                        "displayMode": "table",
                        "placement": "bottom",
                        "calcs": ["lastNotNull", "max"],
                    },
                    "tooltip": {"mode": "multi", "sort": "desc"},
                },
                "targets": [
                    dict(t, refId=chr(65 + i), datasource=ds)
                    for i, t in enumerate(targets)
                ],
            }
        )

    def stat(self, title, expr, x, w, unit="short", h=4, steps=None, legend=""):
        self.panels.append(
            {
                "type": "stat",
                "title": title,
                "id": self.nid(),
                "datasource": PROM,
                "gridPos": {"h": h, "w": w, "x": x, "y": self.y},
                "fieldConfig": {
                    "defaults": {
                        "unit": unit,
                        "thresholds": {
                            "mode": "absolute",
                            "steps": steps or [{"color": "green", "value": None}],
                        },
                    },
                    "overrides": [],
                },
                "options": {
                    "reduceOptions": {"calcs": ["lastNotNull"]},
                    "colorMode": "background",
                    "textMode": "value",
                },
                "targets": [
                    {
                        "expr": expr,
                        "refId": "A",
                        "legendFormat": legend,
                        "datasource": PROM,
                    }
                ],
            }
        )

    def logs(self, title, expr, x, w, h=10):
        self.panels.append(
            {
                "type": "logs",
                "title": title,
                "id": self.nid(),
                "datasource": LOKI,
                "gridPos": {"h": h, "w": w, "x": x, "y": self.y},
                "options": {
                    "showTime": True,
                    "wrapLogMessage": True,
                    "sortOrder": "Descending",
                },
                "targets": [{"refId": "A", "datasource": LOKI, "expr": expr}],
            }
        )


def host_section(d, i, name):
    """Server resources for one instance."""
    sel = f'instance="{i}"'
    d.row(f"{name}: server resources")
    d.stat(
        "CPU used",
        f'100 - avg(rate(node_cpu_seconds_total{{{sel},mode="idle"}}[2m])) * 100',
        0,
        4,
        "percent",
        steps=PCT,
    )
    d.stat(
        "Memory used",
        f"(1 - node_memory_MemAvailable_bytes{{{sel}}} / "
        f"node_memory_MemTotal_bytes{{{sel}}}) * 100",
        4,
        4,
        "percent",
        steps=PCT,
    )
    d.stat(
        "Disk / used",
        f'100 - node_filesystem_avail_bytes{{{sel},mountpoint="/"}} / '
        f'node_filesystem_size_bytes{{{sel},mountpoint="/"}} * 100',
        8,
        4,
        "percent",
        steps=PCT,
    )
    d.stat(
        "Load 1m / cores",
        # Both sides by instance, or the divisor has no labels to match on.
        f"sum by (instance) (node_load1{{{sel}}}) / "
        f'count by (instance) (node_cpu_seconds_total{{{sel},mode="idle"}})',
        12,
        4,
        "percentunit",
        steps=[
            {"color": "green", "value": None},
            {"color": "orange", "value": 0.8},
            {"color": "red", "value": 1.2},
        ],
    )
    d.stat("CPU cores", f'count(node_cpu_seconds_total{{{sel},mode="idle"}})', 16, 4)
    d.stat("Total memory", f"node_memory_MemTotal_bytes{{{sel}}}", 20, 4, "bytes")
    d.y += 4
    d.ts(
        "CPU %",
        [
            {
                "expr": f'100 - avg(rate(node_cpu_seconds_total{{{sel},mode="idle"}}[1m])) * 100',
                "legendFormat": "used",
            },
            {
                "expr": f'avg(rate(node_cpu_seconds_total{{{sel},mode="iowait"}}[1m])) * 100',
                "legendFormat": "iowait",
            },
            {
                "expr": f'avg(rate(node_cpu_seconds_total{{{sel},mode="steal"}}[1m])) * 100',
                "legendFormat": "steal",
            },
        ],
        0,
        12,
        unit="percent",
        max_=100,
    )
    d.ts(
        "Memory",
        [
            {
                "expr": f"node_memory_MemTotal_bytes{{{sel}}} - node_memory_MemAvailable_bytes{{{sel}}}",
                "legendFormat": "used",
            },
            {
                "expr": f"node_memory_MemAvailable_bytes{{{sel}}}",
                "legendFormat": "available",
            },
            {
                "expr": f"node_memory_SwapTotal_bytes{{{sel}}} - node_memory_SwapFree_bytes{{{sel}}}",
                "legendFormat": "swap used",
            },
        ],
        12,
        12,
        unit="bytes",
    )
    d.y += 8
    d.ts(
        "Network in / out",
        [
            {
                "expr": f"sum(rate(node_network_receive_bytes_total{{{sel},{NET}}}[1m]))",
                "legendFormat": "in",
            },
            {
                "expr": f"-sum(rate(node_network_transmit_bytes_total{{{sel},{NET}}}[1m]))",
                "legendFormat": "out",
            },
        ],
        0,
        12,
        unit="Bps",
    )
    d.ts(
        "Disk I/O",
        [
            {
                "expr": f"sum(rate(node_disk_read_bytes_total{{{sel}}}[1m]))",
                "legendFormat": "read",
            },
            {
                "expr": f"-sum(rate(node_disk_written_bytes_total{{{sel}}}[1m]))",
                "legendFormat": "write",
            },
        ],
        12,
        12,
        unit="Bps",
    )
    d.y += 8


def container_section(d, i, name):
    sel = f'instance="{i}",name!=""'
    d.row(f"{name}: containers")
    d.ts(
        "Container CPU (cores)",
        [
            {
                "expr": f"sum by (name) (rate(container_cpu_usage_seconds_total{{{sel}}}[1m]))",
                "legendFormat": "{{name}}",
            }
        ],
        0,
        12,
        h=10,
    )
    d.ts(
        "Container memory (working set)",
        [
            {
                "expr": f"sum by (name) (container_memory_working_set_bytes{{{sel}}})",
                "legendFormat": "{{name}}",
            }
        ],
        12,
        12,
        h=10,
        unit="bytes",
    )
    d.y += 10
    d.ts(
        "Container memory % of its limit",
        [
            {
                "expr": f"sum by (name) (container_memory_working_set_bytes{{{sel}}}) / "
                f"sum by (name) (container_spec_memory_limit_bytes{{{sel}}} > 0) * 100",
                "legendFormat": "{{name}}",
            }
        ],
        0,
        12,
        unit="percent",
        desc="Near 100% the kernel OOM-kills the container.",
    )
    d.ts(
        "Container CPU % of its cpus: limit",
        [
            {
                "expr": f"sum by (name) (rate(container_cpu_usage_seconds_total{{{sel}}}[1m])) / "
                f"sum by (name) (container_spec_cpu_quota{{{sel}}} / "
                f"container_spec_cpu_period{{{sel}}}) * 100",
                "legendFormat": "{{name}}",
            }
        ],
        12,
        12,
        unit="percent",
        desc="Only containers with a cpus: limit. 100% means the container is throttled.",
    )
    d.y += 8


def logs_section(d, i, name):
    d.row(f"{name}: logs")
    d.ts(
        "Log lines per service (1m)",
        [
            {
                "expr": f'sum by (service) (count_over_time({{instance="{i}",service=~".+"}} [1m]))',
                "legendFormat": "{{service}}",
            }
        ],
        0,
        8,
        ds=LOKI,
    )
    d.ts(
        "Error log lines per service (5m)",
        [
            {
                "expr": f'sum by (service) (count_over_time({{instance="{i}",service=~".+"}} '
                f'| level=~"error|critical|fatal" {ERROR_NOISE} [5m]))',
                "legendFormat": "{{service}}",
            }
        ],
        8,
        16,
        ds=LOKI,
    )
    d.y += 8
    d.logs(
        f"Recent errors on {name}",
        f'{{instance="{i}",service=~".+"}} | level=~"error|critical|fatal" {ERROR_NOISE}',
        0,
        24,
    )
    d.y += 10


def app_extras(d):
    nginx = '{job="nginx",instance="app-vm"} | json | host!="grafana.careercraftsai.me"'
    d.row("APP: HTTP requests (host nginx, end to end)")
    d.ts(
        "Requests per second",
        [
            {
                "expr": f"sum by (host) (count_over_time({nginx} [1m])) / 60",
                "legendFormat": "{{host}}",
            }
        ],
        0,
        8,
        unit="reqps",
        ds=LOKI,
    )
    d.ts(
        "Latency p50 / p95 / p99",
        [
            {
                "expr": f"quantile_over_time(0.50, {nginx} | unwrap rt [1m]) by (host)",
                "legendFormat": "p50 {{host}}",
            },
            {
                "expr": f"quantile_over_time(0.95, {nginx} | unwrap rt [1m]) by (host)",
                "legendFormat": "p95 {{host}}",
            },
            {
                "expr": f"quantile_over_time(0.99, {nginx} | unwrap rt [1m]) by (host)",
                "legendFormat": "p99 {{host}}",
            },
        ],
        8,
        8,
        unit="s",
        ds=LOKI,
    )
    d.ts(
        "Errors per second (5xx, 429)",
        [
            {
                "expr": f'sum by (host) (count_over_time({nginx} | status=~"5.." [1m])) / 60',
                "legendFormat": "5xx {{host}}",
            },
            {
                "expr": f'sum by (host) (count_over_time({nginx} | status="429" [1m])) / 60',
                "legendFormat": "429 {{host}}",
            },
        ],
        16,
        8,
        unit="reqps",
        ds=LOKI,
    )
    d.y += 8
    d.panels.append(
        {
            "type": "table",
            "title": "Slowest API endpoints (p95, last 15m)",
            "id": d.nid(),
            "datasource": LOKI,
            "gridPos": {"h": 9, "w": 24, "x": 0, "y": d.y},
            "fieldConfig": {"defaults": {"unit": "s"}, "overrides": []},
            "targets": [
                {
                    "refId": "A",
                    "datasource": LOKI,
                    "queryType": "instant",
                    "expr": 'topk(20, quantile_over_time(0.95, {job="nginx",instance="app-vm"} '
                    '| json | host=~"api.*|app.*" | label_format path=`{{ '
                    'regexReplaceAll "[0-9a-f]{8}-[0-9a-f-]{27}" .path ":id" }}` '
                    "| unwrap rt [15m]) by (method, path))",
                }
            ],
        }
    )
    d.y += 9

    d.row("APP: Postgres and Redis")
    d.stat("Postgres up", "pg_up", 0, 4, steps=UP)
    d.stat("PG connections", "sum(pg_stat_activity_count)", 4, 4)
    d.stat(
        "PG cache hit",
        "sum(rate(pg_stat_database_blks_hit[5m])) / (sum(rate("
        "pg_stat_database_blks_hit[5m])) + sum(rate(pg_stat_database_blks_read[5m])))",
        8,
        4,
        "percentunit",
    )
    d.stat("Redis up", "redis_up", 12, 4, steps=UP)
    d.stat("Redis memory", "redis_memory_used_bytes", 16, 4, "bytes")
    d.stat("Redis clients", "redis_connected_clients", 20, 4)
    d.y += 4
    d.ts(
        "Postgres connections by state",
        [
            {
                "expr": 'sum by (state) (pg_stat_activity_count{state!=""})',
                "legendFormat": "{{state}}",
            }
        ],
        0,
        8,
        stack=True,
    )
    d.ts(
        "Postgres transactions/s",
        [
            {
                "expr": "sum(rate(pg_stat_database_xact_commit[1m]))",
                "legendFormat": "commits",
            },
            {
                "expr": "sum(rate(pg_stat_database_xact_rollback[1m]))",
                "legendFormat": "rollbacks",
            },
            {
                "expr": "sum(rate(pg_stat_database_deadlocks[1m]))",
                "legendFormat": "deadlocks",
            },
        ],
        8,
        8,
    )
    d.ts(
        "Redis commands/s, hits, misses",
        [
            {
                "expr": "rate(redis_commands_processed_total[1m])",
                "legendFormat": "commands/s",
            },
            {"expr": "rate(redis_keyspace_hits_total[1m])", "legendFormat": "hits/s"},
            {
                "expr": "rate(redis_keyspace_misses_total[1m])",
                "legendFormat": "misses/s",
            },
        ],
        16,
        8,
    )
    d.y += 8

    workers = (
        'instance="app-vm",'
        'name=~".*(temporal-worker|notification-worker|sandbox-server|temporal-1).*"'
    )
    d.row("APP: workers and auto-apply (temporal-worker, sandbox-server)")
    d.ts(
        "Worker CPU (cores)",
        [
            {
                "expr": f"sum by (name) (rate(container_cpu_usage_seconds_total{{{workers}}}[1m]))",
                "legendFormat": "{{name}}",
            }
        ],
        0,
        8,
    )
    d.ts(
        "Worker memory",
        [
            {
                "expr": f"sum by (name) (container_memory_working_set_bytes{{{workers}}})",
                "legendFormat": "{{name}}",
            }
        ],
        8,
        8,
        unit="bytes",
    )
    d.ts(
        "Auto-apply log activity (5m)",
        [
            {
                "expr": 'sum by (service) (count_over_time({instance="app-vm",'
                'service=~"temporal-worker|sandbox-server|backend"} '
                '|~ "(?i)auto.?apply|apply_task|submission" [5m]))',
                "legendFormat": "{{service}}",
            }
        ],
        16,
        8,
        ds=LOKI,
    )
    d.y += 8


def computers_extras(d):
    browsers = (
        'count(container_memory_working_set_bytes{instance="computers-vm",'
        'name=~"careercraft-sandbox-computer-u-.*"}) or vector(0)'
    )
    d.row("COMPUTERS: user browsers (gVisor) and relay")
    d.stat("Running browsers", browsers, 0, 6)
    d.stat(
        "Browsers / cap",
        f'({browsers}) / scalar(computers_relay_capacity_sessions{{instance="computers-vm"}})',
        6,
        6,
        "percentunit",
        steps=[
            {"color": "green", "value": None},
            {"color": "orange", "value": 0.75},
            {"color": "red", "value": 1},
        ],
    )
    # Allow for the 15s poll + 5s timeout and 15s scrape; expire a stopped poller.
    d.stat(
        "Relay up",
        'max(computers_relay_up{instance="computers-vm",job="relay"} '
        "and on(instance, job) (time() - node_textfile_mtime_seconds{"
        'instance="computers-vm",job="relay",file="/relay-health/relay.prom"} < 60)) '
        "or vector(0)",
        12,
        6,
        steps=UP,
    )
    d.stat(
        "Cap (SANDBOX_MAX_SESSIONS)",
        'computers_relay_capacity_sessions{instance="computers-vm"}',
        18,
        6,
    )
    d.y += 4
    d.ts(
        "Browsers vs cap",
        [
            {"expr": browsers, "legendFormat": "browser containers"},
            {
                "expr": 'computers_relay_active_sessions{instance="computers-vm"}',
                "legendFormat": "relay active sessions",
            },
            {
                "expr": 'computers_relay_capacity_sessions{instance="computers-vm"}',
                "legendFormat": "cap",
            },
        ],
        0,
        24,
        desc="CPU is the limit: 4 cores saturate around 12 active browsers "
        "(docs/reports/2026-10-22-computers-production.md).",
    )
    d.y += 8


def dashboard(uid, title, d):
    return {
        "uid": uid,
        "title": title,
        "tags": ["careercraft"],
        "timezone": "browser",
        "refresh": "30s",
        "time": {"from": "now-1h", "to": "now"},
        "schemaVersion": 39,
        "editable": False,
        "panels": d.panels,
        "templating": {"list": []},
    }


out = sys.argv[1]
app = Dash()
host_section(app, "app-vm", "APP")
container_section(app, "app-vm", "APP")
app_extras(app)
logs_section(app, "app-vm", "APP")
infra = Dash()
host_section(infra, "infra-vm", "INFRA")
container_section(infra, "infra-vm", "INFRA")
logs_section(infra, "infra-vm", "INFRA")
computers = Dash()
host_section(computers, "computers-vm", "COMPUTERS")
computers_extras(computers)
container_section(computers, "computers-vm", "COMPUTERS")
logs_section(computers, "computers-vm", "COMPUTERS")
for name, uid, title, d in [
    (
        "careercraft-app-server.json",
        "careercraft-app",
        "CareerCraft · APP server (app-vm)",
        app,
    ),
    (
        "careercraft-infra-server.json",
        "careercraft-infra",
        "CareerCraft · INFRA server (infra-vm: Nango, Ollama)",
        infra,
    ),
    (
        "careercraft-computers-server.json",
        "careercraft-computers",
        "CareerCraft · COMPUTERS server (computers-vm)",
        computers,
    ),
]:
    with open(os.path.join(out, name), "w", encoding="utf-8") as fh:
        json.dump(dashboard(uid, title, d), fh, indent=1)
    print(name, len(d.panels), "panels")
