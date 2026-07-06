#!/usr/bin/env python3
"""statz — minimal TUI status board for a Proxmox cluster + Prometheus (lighthouse) metrics.

Usage:
    python statz.py                    # run the dashboard
    python statz.py --discover ups     # list Prometheus metric names matching a keyword
    python statz.py --config PATH      # use an alternate config file

Keys: q = quit
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import termios
import threading
import time
import tty
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib  # type: ignore

ACCENT = "cyan"
BORDER = "grey37"
DIM = "grey58"


# ── config ────────────────────────────────────────────────────────────────────

def load_config(path: str | None) -> dict:
    candidates = [Path(path)] if path else [
        Path(__file__).parent / "statz.toml",
        Path.home() / ".config" / "statz" / "statz.toml",
    ]
    for p in candidates:
        if p.is_file():
            with open(p, "rb") as f:
                return tomllib.load(f)
    sys.exit(f"statz: no config found (looked for {', '.join(str(c) for c in candidates)})")


# ── clients ───────────────────────────────────────────────────────────────────

class Proxmox:
    """Tiny async Proxmox VE API client (ticket or API-token auth)."""

    def __init__(self, cfg: dict):
        self.base = cfg["host"].rstrip("/")
        self.user = cfg.get("user", "root@pam")
        self.password = cfg.get("password", "")
        self.token_id = cfg.get("token_id")
        self.token_secret = cfg.get("token_secret")
        self.http = httpx.AsyncClient(verify=cfg.get("verify_ssl", True), timeout=8.0)
        self.ticket: str | None = None

    async def _login(self) -> None:
        r = await self.http.post(
            f"{self.base}/api2/json/access/ticket",
            data={"username": self.user, "password": self.password},
        )
        r.raise_for_status()
        self.ticket = r.json()["data"]["ticket"]

    async def get(self, path: str) -> Any:
        if self.token_id and self.token_secret:
            headers = {"Authorization": f"PVEAPIToken={self.token_id}={self.token_secret}"}
            r = await self.http.get(f"{self.base}/api2/json{path}", headers=headers)
        else:
            if self.ticket is None:
                await self._login()
            r = await self.http.get(
                f"{self.base}/api2/json{path}", cookies={"PVEAuthCookie": self.ticket}
            )
            if r.status_code == 401:  # ticket expired (2h lifetime) — re-login once
                await self._login()
                r = await self.http.get(
                    f"{self.base}/api2/json{path}", cookies={"PVEAuthCookie": self.ticket}
                )
        r.raise_for_status()
        return r.json()["data"]


class Prom:
    """Async Prometheus instant-query client (direct or via Grafana datasource proxy)."""

    def __init__(self, cfg: dict):
        self.url = cfg.get("prometheus_url", "").rstrip("/")
        auth = None
        if cfg.get("username"):
            auth = (cfg["username"], cfg.get("password", ""))
        headers = {}
        if cfg.get("grafana_token"):
            headers["Authorization"] = f"Bearer {cfg['grafana_token']}"
        self.http = httpx.AsyncClient(timeout=8.0, auth=auth, headers=headers, verify=False)

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    async def query(self, q: str, url: str | None = None) -> list[tuple[dict, float]]:
        base = (url or self.url).rstrip("/")
        r = await self.http.get(f"{base}/api/v1/query", params={"query": q})
        r.raise_for_status()
        body = r.json()
        if body.get("status") != "success":
            raise RuntimeError(body.get("error", "prometheus query failed"))
        return [(s["metric"], float(s["value"][1])) for s in body["data"]["result"]]

    async def metric_names(self) -> list[str]:
        r = await self.http.get(f"{self.url}/api/v1/label/__name__/values")
        r.raise_for_status()
        return r.json().get("data", [])


# ── state ─────────────────────────────────────────────────────────────────────

@dataclass
class State:
    cluster_name: str = "—"
    quorate: bool | None = None
    nodes: list[dict] = field(default_factory=list)          # cluster/resources type=node
    node_status: dict[str, dict] = field(default_factory=dict)  # node -> /nodes/X/status
    guests: list[dict] = field(default_factory=list)         # qemu + lxc
    stats: list[tuple[str, str, float | None]] = field(default_factory=list)  # label, value, frac
    node_extra: dict[str, dict] = field(default_factory=dict)  # node -> {watts, temp}
    targets: tuple[int, int] | None = None                   # up, total
    errors: dict[str, str] = field(default_factory=dict)
    last_fetch: float = 0.0


async def fetch_proxmox(px: Proxmox, state: State) -> None:
    try:
        cstatus, resources = await asyncio.gather(
            px.get("/cluster/status"), px.get("/cluster/resources")
        )
        for item in cstatus:
            if item.get("type") == "cluster":
                state.cluster_name = item.get("name", "—")
                state.quorate = bool(item.get("quorate"))
        state.nodes = sorted(
            (r for r in resources if r["type"] == "node"), key=lambda n: n["node"]
        )
        state.guests = [r for r in resources if r["type"] in ("qemu", "lxc")]

        async def nstat(name: str):
            try:
                return name, await px.get(f"/nodes/{name}/status")
            except Exception:
                return name, None

        pairs = await asyncio.gather(
            *(nstat(n["node"]) for n in state.nodes if n.get("status") == "online")
        )
        state.node_status = {name: st for name, st in pairs if st}
        state.errors.pop("proxmox", None)
    except Exception as e:
        state.errors["proxmox"] = f"{type(e).__name__}: {e}"


async def fetch_metrics(prom: Prom, cfg: dict, state: State) -> None:
    if not prom.enabled:
        return
    try:
        mcfg = cfg.get("metrics", {})
        stats: list[tuple[str, str, float | None]] = []
        for spec in mcfg.get("stat", []):
            try:
                series = await prom.query(spec["query"], spec.get("url"))
                if not series:
                    stats.append((spec["label"], "n/a", None))
                    continue
                value = series[0][1] if len(series) == 1 else sum(v for _, v in series)
                unit = spec.get("unit", "")
                prec = spec.get("precision", 1 if value % 1 else 0)
                frac = None
                if spec.get("max"):
                    frac = max(0.0, min(1.0, value / spec["max"]))
                stats.append((spec["label"], f"{value:.{prec}f}{unit}", frac))
            except Exception:
                stats.append((spec["label"], "err", None))
        state.stats = stats

        # per-node overlays (watts / temperature) matched by node name in labels
        extra: dict[str, dict] = {}
        for key, qname in (("watts", "node_power_query"), ("temp", "node_temp_query")):
            q = mcfg.get(qname)
            if not q:
                continue
            try:
                for labels, v in await prom.query(q):
                    blob = " ".join(str(x) for x in labels.values())
                    for name in (n["node"] for n in state.nodes):
                        if name in blob:
                            extra.setdefault(name, {})[key] = v
            except Exception:
                pass
        state.node_extra = extra

        ups = await prom.query("up")
        state.targets = (sum(1 for _, v in ups if v == 1), len(ups))
        state.errors.pop("metrics", None)
    except Exception as e:
        state.errors["metrics"] = f"{type(e).__name__}: {e}"


# ── formatting helpers ────────────────────────────────────────────────────────

def fmt_bytes(n: float) -> str:
    for unit in ("B", "K", "M", "G", "T", "P"):
        if abs(n) < 1024 or unit == "P":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}P"


def fmt_uptime(sec: float) -> str:
    sec = int(sec)
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def frac_color(frac: float) -> str:
    return "green" if frac < 0.70 else "yellow" if frac < 0.90 else "red"


def bar(frac: float, width: int = 10) -> Text:
    frac = max(0.0, min(1.0, frac))
    filled = round(frac * width)
    t = Text()
    t.append("▰" * filled, style=frac_color(frac))
    t.append("▱" * (width - filled), style="grey30")
    return t


# ── rendering ─────────────────────────────────────────────────────────────────

def render_header(state: State) -> Text:
    t = Text(no_wrap=True)
    t.append(" ▍STATZ ", style=f"bold {ACCENT}")
    t.append(f" {state.cluster_name} ", style="bold white")
    if state.quorate is not None:
        t.append("● quorate  " if state.quorate else "● NO QUORUM  ",
                 style="green" if state.quorate else "bold red")
    online = sum(1 for n in state.nodes if n.get("status") == "online")
    total = len(state.nodes)
    t.append(f"{online}/{total} nodes ", style="green" if online == total else "bold red")
    t.append(" · ", style=DIM)
    t.append(datetime.now().strftime("%H:%M:%S"), style=DIM)
    return t


def render_node(node: dict, status: dict | None, extra: dict | None = None) -> Panel:
    name = node["node"]
    online = node.get("status") == "online"
    title = Text()
    title.append("● ", style="green" if online else "red")
    title.append(name, style="bold white")

    if not online:
        return Panel(Text("offline", style="bold red"), title=title,
                     border_style="red", padding=(0, 1))

    grid = Table.grid(padding=(0, 1))
    grid.add_column(style=DIM, no_wrap=True)
    grid.add_column(no_wrap=True)

    cpu = node.get("cpu", 0.0)
    mem_frac = node.get("mem", 0) / max(node.get("maxmem", 1), 1)
    row_cpu = Text.assemble(bar(cpu), f" {cpu * 100:4.0f}% ", (f"{node.get('maxcpu', '?')}c", DIM))
    row_mem = Text.assemble(
        bar(mem_frac), f" {mem_frac * 100:4.0f}% ",
        (f"{fmt_bytes(node.get('mem', 0))}/{fmt_bytes(node.get('maxmem', 0))}", DIM),
    )
    grid.add_row("cpu", row_cpu)
    grid.add_row("mem", row_mem)

    if status:
        load = status.get("loadavg", ["?", "?", "?"])
        grid.add_row("load", Text(" ".join(str(x) for x in load[:3])))
        root = status.get("rootfs", {})
        if root.get("total"):
            rf = root.get("used", 0) / root["total"]
            grid.add_row("disk", Text.assemble(bar(rf), f" {rf * 100:4.0f}%"))

    if extra:
        t = Text()
        if "watts" in extra:
            t.append(f"{extra['watts']:.1f} W", style="bold white")
        if "temp" in extra:
            if t:
                t.append(" · ", style=DIM)
            temp = extra["temp"]
            t.append(f"{temp:.0f}°C",
                     style="green" if temp < 70 else "yellow" if temp < 85 else "bold red")
        grid.add_row("pwr", t)

    grid.add_row("up", Text(fmt_uptime(node.get("uptime", 0)), style=DIM))
    return Panel(grid, title=title, border_style=BORDER, padding=(0, 1))


def render_guests(state: State, height: int) -> Panel:
    guests = sorted(
        state.guests,
        key=lambda g: (g.get("status") != "running", -(g.get("cpu") or 0)),
    )
    running = sum(1 for g in guests if g.get("status") == "running")

    table = Table.grid(padding=(0, 1))
    table.add_column(width=2)                       # status dot
    table.add_column(style="white", no_wrap=True, overflow="ellipsis", max_width=22)
    table.add_column(style=DIM, no_wrap=True)       # type
    table.add_column(style=DIM, no_wrap=True)       # node
    table.add_column(justify="right", no_wrap=True) # cpu
    table.add_column(justify="right", style=DIM, no_wrap=True)  # mem

    max_rows = max(height - 3, 1)
    for g in guests[:max_rows]:
        run = g.get("status") == "running"
        dot = Text("●", style="green" if run else "grey35")
        cpu = f"{(g.get('cpu') or 0) * 100:.0f}%" if run else "—"
        mem = fmt_bytes(g.get("mem", 0)) if run else "—"
        table.add_row(
            dot,
            Text(g.get("name", str(g.get("vmid"))), style="white" if run else "grey50"),
            "vm" if g["type"] == "qemu" else "ct",
            g.get("node", ""),
            Text(cpu, style=frac_color(g.get("cpu") or 0) if run else "grey35"),
            mem,
        )
    hidden = len(guests) - max_rows
    if hidden > 0:
        table.add_row("", Text(f"+{hidden} more…", style=DIM), "", "", "", "")

    title = Text.assemble(("guests ", "bold white"), (f"{running}/{len(guests)} running", DIM))
    return Panel(table, title=title, border_style=BORDER, padding=(0, 1))


def render_power(state: State, prom_enabled: bool) -> Panel:
    grid = Table.grid(padding=(0, 1))
    grid.add_column(style=DIM, no_wrap=True)
    grid.add_column(justify="right", style="bold white", no_wrap=True)
    grid.add_column(no_wrap=True)

    if not prom_enabled:
        grid.add_row("", Text("metrics disabled", style=DIM), "")
    elif not state.stats and "metrics" not in state.errors:
        grid.add_row("", Text("…", style=DIM), "")
    for label, value, frac in state.stats:
        grid.add_row(label, value, bar(frac, 8) if frac is not None else Text(""))
    if state.targets:
        up, total = state.targets
        grid.add_row(
            "targets",
            Text(f"{up}/{total} up", style="green" if up == total else "bold yellow"),
            Text(""),
        )

    return Panel(grid, title=Text("power · lighthouse", style="bold white"),
                 border_style=BORDER, padding=(0, 1))


def render_footer(state: State, interval: float) -> Text:
    t = Text(no_wrap=True)
    age = time.monotonic() - state.last_fetch if state.last_fetch else None
    t.append(f" refresh {interval:g}s", style=DIM)
    if age is not None:
        t.append(f" · updated {age:.0f}s ago", style=DIM)
    t.append(" · q quit", style=DIM)
    for src, err in state.errors.items():
        t.append(f"  ⚠ {src}: {err[:60]}", style="bold red")
    return t


def render(state: State, console: Console, prom_enabled: bool, interval: float) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=1),
        Layout(name="nodes", size=8),
        Layout(name="body", ratio=1),
        Layout(name="footer", size=1),
    )
    layout["header"].update(render_header(state))
    if state.nodes:
        layout["nodes"].split_row(
            *(Layout(render_node(n, state.node_status.get(n["node"]),
                                 state.node_extra.get(n["node"]))) for n in state.nodes)
        )
    else:
        layout["nodes"].update(
            Panel(Text("connecting to proxmox…", style=DIM), border_style=BORDER)
        )
    body_height = max(console.size.height - 10, 4)
    layout["body"].split_row(
        Layout(render_guests(state, body_height), ratio=2),
        Layout(render_power(state, prom_enabled), ratio=1),
    )
    layout["footer"].update(render_footer(state, interval))
    return layout


# ── input ─────────────────────────────────────────────────────────────────────

def key_listener(stop: threading.Event) -> None:
    if not sys.stdin.isatty():
        return
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while not stop.is_set():
            ch = sys.stdin.read(1)
            if ch in ("q", "Q", "\x03"):
                stop.set()
                return
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ── main ──────────────────────────────────────────────────────────────────────

async def run(cfg: dict) -> None:
    console = Console()
    state = State()
    px = Proxmox(cfg["proxmox"])
    prom = Prom(cfg.get("metrics", {}))
    interval = float(cfg.get("app", {}).get("refresh_seconds", 3))

    stop = threading.Event()
    threading.Thread(target=key_listener, args=(stop,), daemon=True).start()

    async def poll() -> None:
        while not stop.is_set():
            await asyncio.gather(
                fetch_proxmox(px, state), fetch_metrics(prom, cfg, state)
            )
            state.last_fetch = time.monotonic()
            await asyncio.sleep(interval)

    poller = asyncio.create_task(poll())
    try:
        with Live(console=console, screen=True, auto_refresh=False) as live:
            while not stop.is_set():
                live.update(render(state, console, prom.enabled, interval), refresh=True)
                await asyncio.sleep(0.5)
    finally:
        stop.set()
        poller.cancel()
        await px.http.aclose()
        await prom.http.aclose()


async def discover(cfg: dict, keyword: str) -> None:
    prom = Prom(cfg.get("metrics", {}))
    if not prom.enabled:
        sys.exit("statz: no [metrics].prometheus_url configured")
    names = await prom.metric_names()
    hits = [n for n in names if keyword.lower() in n.lower()]
    print("\n".join(hits) if hits else f"no metric names matching {keyword!r}")
    await prom.http.aclose()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", help="path to statz.toml")
    ap.add_argument("--discover", metavar="KEYWORD",
                    help="list Prometheus metric names matching KEYWORD, then exit")
    args = ap.parse_args()
    cfg = load_config(args.config)
    try:
        if args.discover:
            asyncio.run(discover(cfg, args.discover))
        else:
            asyncio.run(run(cfg))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
