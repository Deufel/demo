# /// script
# requires-python = ">=3.12"
# dependencies = ["py-sse>=0.5.0", "html-tags>=0.4.4"]
# ///

import os
import time
import threading
from collections import deque
from py_sse import serve, Changes, LiveCounter, html
from html_tags import h, Safe
from html_tags import render as h_render


STICK = "https://cdn.jsdelivr.net/gh/Deufel/toolbox@d32d8da/css/style.css"
DATASTAR = "https://cdn.jsdelivr.net/gh/starfederation/datastar@v1.0.1/bundles/datastar.js"

SAMPLE_INTERVAL_S = 1.0 / 60.0
NET_HISTORY = 360

WINDOWS = (
    ("10s", 10),
    ("1m",  60),
    ("5m",  300),
    ("1h",  3600),
)
DEFAULT_WINDOW = "1m"
DISPLAY_AVG = 3
CHART_POINTS = 120


def alpha_for(window_seconds, sample_rate_hz=60):
    n = max(1, (window_seconds * sample_rate_hz) // 8)
    return 2.0 / (n + 1)


WINDOW_ALPHAS = {label: alpha_for(secs) for label, secs in WINDOWS}
WINDOW_STRIDE = {label: max(1, secs * 60 // CHART_POINTS) for label, secs in WINDOWS}

changes = Changes()
live = LiveCounter(soft_cap=3, min_poll_ms=1_000, max_poll_ms=10_000, ramp_users=20)

_state_lock = threading.Lock()
_state = {
    "ts": 0.0, "uptime_s": 0,
    "load_1": 0.0, "load_5": 0.0, "load_15": 0.0,
    "cpu_pct": 0.0,
    "cpu_recent": deque([0.0] * DISPLAY_AVG, maxlen=DISPLAY_AVG),
    "cpu_per_core": [],
    "core_recent": [],
    "mem_used": 0, "mem_total": 0, "mem_buffers": 0, "mem_cached": 0,
    "disk_used": 0, "disk_total": 0,
    "net_rx_per_s": 0, "net_tx_per_s": 0,
    "processes": [],
    "sampler_fps": 0.0, "sampler_tick_ms": 0.0,
    "ema_state": {label: [] for label, _ in WINDOWS},
    "chart_points": {label: [] for label, _ in WINDOWS},
    "chart_tick": {label: 0 for label, _ in WINDOWS},
    "history": {
        "cpu":    deque([0.0] * NET_HISTORY, maxlen=NET_HISTORY),
        "net_rx": deque([0]   * NET_HISTORY, maxlen=NET_HISTORY),
        "net_tx": deque([0]   * NET_HISTORY, maxlen=NET_HISTORY),
    },
}


# ─── /proc readers (unchanged) ────────────────────────────────────────

def _read(path):
    try:
        with open(path) as f: return f.read()
    except (FileNotFoundError, PermissionError):
        return ""

def _cpu_samples():
    out = []
    for line in _read("/proc/stat").splitlines():
        if not line.startswith("cpu"): break
        parts = line.split()
        try:
            fields = [int(x) for x in parts[1:]]
        except ValueError:
            continue
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
        total = sum(fields)
        out.append((idle, total))
    return out

def _meminfo():
    fields = {}
    for line in _read("/proc/meminfo").splitlines():
        if ":" not in line: continue
        k, v = line.split(":", 1)
        parts = v.strip().split()
        if parts and parts[0].isdigit():
            fields[k.strip()] = int(parts[0]) * 1024
    total = fields.get("MemTotal", 0)
    avail = fields.get("MemAvailable", fields.get("MemFree", 0))
    return total - avail, total, fields.get("Buffers", 0), fields.get("Cached", 0)

def _loadavg():
    parts = _read("/proc/loadavg").split()
    if len(parts) >= 3:
        try: return float(parts[0]), float(parts[1]), float(parts[2])
        except ValueError: pass
    return 0.0, 0.0, 0.0

def _uptime():
    parts = _read("/proc/uptime").split()
    if parts:
        try: return int(float(parts[0]))
        except ValueError: pass
    return 0

def _net_total():
    rx_total = tx_total = 0
    for line in _read("/proc/net/dev").splitlines()[2:]:
        if ":" not in line: continue
        name, rest = line.split(":", 1)
        if name.strip() == "lo": continue
        parts = rest.split()
        if len(parts) >= 9:
            try:
                rx_total += int(parts[0])
                tx_total += int(parts[8])
            except ValueError: pass
    return rx_total, tx_total

def _disk():
    try:
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        return total - free, total
    except OSError:
        return 0, 0

_proc_prev = {}
_proc_total_prev = 0

def _processes():
    global _proc_prev, _proc_total_prev
    samples = _cpu_samples()
    if not samples: return []
    _, cpu_total_now = samples[0]
    cpu_delta = max(1, cpu_total_now - _proc_total_prev)
    _proc_total_prev = cpu_total_now
    new_prev = {}
    out = []
    try:
        pids = [d for d in os.listdir("/proc") if d.isdigit()]
    except OSError:
        return []
    ncpu = max(1, len(samples) - 1)
    for pid_s in pids:
        try:
            with open(f"/proc/{pid_s}/stat") as f:
                line = f.read()
        except (FileNotFoundError, PermissionError):
            continue
        lparen = line.find("(")
        rparen = line.rfind(")")
        if lparen < 0 or rparen < 0: continue
        comm = line[lparen + 1:rparen]
        fields = line[rparen + 2:].split()
        if len(fields) < 22: continue
        try:
            utime = int(fields[11]); stime = int(fields[12])
            rss = int(fields[21]) * 4096
        except (ValueError, IndexError):
            continue
        cputime = utime + stime
        pid = int(pid_s)
        prev = _proc_prev.get(pid, cputime)
        new_prev[pid] = cputime
        delta = max(0, cputime - prev)
        cpu_pct = (delta / cpu_delta) * 100.0 * ncpu
        if cpu_pct > 0.01 or rss > 16 * 1024 * 1024:
            out.append((comm, cpu_pct, rss, pid))
    _proc_prev = new_prev
    out.sort(key=lambda p: -p[1])
    return out[:8]


# ─── sampler (unchanged) ──────────────────────────────────────────────

def _init_state_for_cores(n_cores, initial_value):
    for label, _ in WINDOWS:
        _state["ema_state"][label] = [initial_value] * n_cores
        _state["chart_points"][label] = [
            deque([initial_value] * CHART_POINTS, maxlen=CHART_POINTS)
            for _ in range(n_cores)
        ]
        _state["chart_tick"][label] = 0


def _advance_per_tick(per_core_values):
    for label, _ in WINDOWS:
        alpha = WINDOW_ALPHAS[label]
        stride = WINDOW_STRIDE[label]
        ema = _state["ema_state"][label]
        for i, raw in enumerate(per_core_values):
            if i < len(ema):
                ema[i] = ema[i] + alpha * (raw - ema[i])
        _state["chart_tick"][label] += 1
        if _state["chart_tick"][label] >= stride:
            _state["chart_tick"][label] = 0
            chart = _state["chart_points"][label]
            for i, v in enumerate(ema):
                if i < len(chart):
                    chart[i].append(v)


def sampler_loop():
    prev = _cpu_samples()
    prev_rx, prev_tx = _net_total()
    prev_t = time.monotonic()
    last_proc_scan = 0.0
    cached_procs = []
    tick_times = deque(maxlen=60)
    last_tick = time.monotonic()
    initialized = False

    while True:
        time.sleep(SAMPLE_INTERVAL_S)
        now_t = time.monotonic()
        dt = now_t - prev_t
        prev_t = now_t

        tick_dt = now_t - last_tick
        last_tick = now_t
        tick_times.append(tick_dt)
        avg_tick = sum(tick_times) / len(tick_times)
        measured_fps = (1.0 / avg_tick) if avg_tick > 0 else 0.0

        cur = _cpu_samples()
        cpu_pct = 0.0
        per_core = []
        if cur and prev and len(cur) == len(prev):
            for i, ((pi, pt), (ci, ct)) in enumerate(zip(prev, cur)):
                dt_idle = ci - pi
                dt_total = ct - pt
                pct = max(0.0, min(100.0, (1.0 - dt_idle / dt_total) * 100.0)) if dt_total > 0 else 0.0
                if i == 0:
                    cpu_pct = pct
                else:
                    per_core.append(pct)
        prev = cur

        mu, mt, mb, mc = _meminfo()
        rx, tx = _net_total()
        rx_per_s = max(0, int((rx - prev_rx) / dt)) if dt > 0 else 0
        tx_per_s = max(0, int((tx - prev_tx) / dt)) if dt > 0 else 0
        prev_rx, prev_tx = rx, tx
        du, dt_total_disk = _disk()
        l1, l5, l15 = _loadavg()
        up = _uptime()

        if now_t - last_proc_scan > 0.5:
            cached_procs = _processes()
            last_proc_scan = now_t

        with _state_lock:
            _state["ts"] = time.time()
            _state["uptime_s"] = up
            _state["load_1"] = l1; _state["load_5"] = l5; _state["load_15"] = l15
            _state["cpu_pct"] = cpu_pct
            _state["cpu_recent"].append(cpu_pct)
            _state["cpu_per_core"] = per_core

            if not initialized and per_core:
                _init_state_for_cores(len(per_core), per_core[0])
                for label, _ in WINDOWS:
                    for i, v in enumerate(per_core):
                        _state["ema_state"][label][i] = v
                        chart = _state["chart_points"][label][i]
                        chart.clear()
                        chart.extend([v] * CHART_POINTS)
                _state["core_recent"] = [
                    deque([v] * DISPLAY_AVG, maxlen=DISPLAY_AVG) for v in per_core
                ]
                initialized = True

            _advance_per_tick(per_core)

            if len(_state["core_recent"]) != len(per_core):
                _state["core_recent"] = [
                    deque([v] * DISPLAY_AVG, maxlen=DISPLAY_AVG) for v in per_core
                ]
            for recent, val in zip(_state["core_recent"], per_core):
                recent.append(val)

            _state["mem_used"] = mu; _state["mem_total"] = mt
            _state["mem_buffers"] = mb; _state["mem_cached"] = mc
            _state["disk_used"] = du; _state["disk_total"] = dt_total_disk
            _state["net_rx_per_s"] = rx_per_s; _state["net_tx_per_s"] = tx_per_s
            _state["processes"] = cached_procs
            _state["sampler_fps"] = measured_fps
            _state["sampler_tick_ms"] = avg_tick * 1000.0

            _state["history"]["cpu"].append(cpu_pct)
            _state["history"]["net_rx"].append(rx_per_s)
            _state["history"]["net_tx"].append(tx_per_s)

        changes.notify()


def snapshot(window):
    with _state_lock:
        chart = [list(d) for d in _state["chart_points"][window]]
        return {
            **{k: v for k, v in _state.items()
               if k not in ("ema_state", "chart_points", "chart_tick")},
            "cpu_recent": list(_state["cpu_recent"]),
            "cpu_per_core": list(_state["cpu_per_core"]),
            "core_recent": [list(d) for d in _state["core_recent"]],
            "processes": list(_state["processes"]),
            "chart": chart,
            "history": {k: list(v) for k, v in _state["history"].items()},
        }


def avg(values):
    return sum(values) / len(values) if values else 0.0


# ─── formatting ──────────────────────────────────────────────────────

def fmt_pct(pct):
    return f"{int(round(pct)):3d}%"


def fmt_bytes(n, width=6):
    f = float(n)
    for u in ("B", "K", "M", "G", "T"):
        if f < 1024:
            s = f"{int(f)}{u}" if u == "B" else f"{f:.1f}{u}"
            return f"{s:>{width}}"
        f /= 1024
    return f"{f:.1f}P".rjust(width)


def fmt_rate(n_per_s):
    return f"{fmt_bytes(n_per_s)}/s"


def fmt_uptime(s):
    days, s = divmod(int(s), 86400)
    hours, s = divmod(s, 3600)
    mins, _ = divmod(s, 60)
    if days:  return f"{days}d {hours}h {mins}m"
    if hours: return f"{hours}h {mins}m"
    return f"{mins}m"


def semantic_class(pct, warn=70, bad=90):
    if pct >= bad:  return "dgr"
    if pct >= warn: return "wrn"
    return "suc"


def window_seconds(name):
    return dict(WINDOWS).get(name, dict(WINDOWS)[DEFAULT_WINDOW])


def window_url(label):
    return "/" if label == DEFAULT_WINDOW else f"/{label}"


# ─── styles ──────────────────────────────────────────────────────────

PAGE_STYLE = """
@scope {
  :scope, :scope * { font-variant-numeric: tabular-nums }
  :scope { font-family: ui-monospace, SFMono-Regular, Menlo, monospace }
  :scope .mono { white-space: pre }

  :scope svg.spark {
    display: block;
    inline-size: 100%;
    block-size: 100%;
  }
  :scope .spark-short { block-size: 3lh }

  :scope .cpu-chart {
    aspect-ratio: 5 / 2;
    max-block-size: 30svh;
    inline-size: 100%;
    position: relative;
  }

  :scope table { inline-size: 100%; border-collapse: collapse; table-layout: fixed }
  :scope th {
    --type: -2; --fg: -0.5; text-transform: uppercase;
    text-align: start; padding: 0.2em 0.5em;
    border-block-end: 1px solid var(--border); font-weight: 500;
  }
  :scope td {
    padding: 0.1em 0.5em; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap;
  }
  :scope td.r, :scope th.r { text-align: end }

  :scope .logo-pill {
    align-items: center;
    padding: 0.25em 0.7em;
    border-radius: 999px;
    border: 1px solid var(--border);
  }
  :scope .logo-pill svg {
    inline-size: 1.1em; block-size: 1.1em;
    --fg: 0.5;
  }

  :scope .swatch {
    display: inline-block; inline-size: 0.9em; block-size: 0.9em;
    background: currentColor; border-radius: 0.2em;
    vertical-align: -0.1em;
  }

  :scope .tabs a[aria-current=page] {
    --fg: 0.9; font-weight: 600;
  }
}
"""


# ─── icons ───────────────────────────────────────────────────────────

_ICON_BASE = ('xmlns="http://www.w3.org/2000/svg" width="20" height="20" '
              'viewBox="0 0 24 24" fill="none" stroke="currentColor" '
              'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"')

icon_x = Safe(f'<svg {_ICON_BASE}><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>')

icon_moon = Safe(
    f'<svg {_ICON_BASE}><path d="M12 2v2"/>'
    '<path d="M14.837 16.385a6 6 0 1 1-7.223-7.222c.624-.147.97.66.715 1.248'
    'a4 4 0 0 0 5.26 5.259c.589-.255 1.396.09 1.248.715"/>'
    '<path d="M16 12a4 4 0 0 0-4-4"/><path d="m19 5-1.256 1.256"/>'
    '<path d="M20 12h2"/></svg>')

icon_font = Safe(
    f'<svg {_ICON_BASE}><path d="m15 16 2.536-7.328a1.02 1.02 1 0 1 1.928 0L22 16"/>'
    '<path d="M15.697 14h5.606"/>'
    '<path d="m2 16 4.039-9.69a.5.5 0 0 1 .923 0L11 16"/>'
    '<path d="M3.304 13h6.392"/></svg>')

icon_palette = Safe(
    '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" '
    'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M12 22a1 1 0 0 1 0-20 10 9 0 0 1 10 9 5 5 0 0 1-5 5h-2.25'
    'a1.75 1.75 0 0 0-1.4 2.8l.3.4a1.75 1.75 0 0 1-1.4 2.8z"/>'
    '<circle cx="13.5" cy="6.5" r=".5" fill="currentColor"/>'
    '<circle cx="17.5" cy="10.5" r=".5" fill="currentColor"/>'
    '<circle cx="6.5" cy="12.5" r=".5" fill="currentColor"/>'
    '<circle cx="8.5" cy="7.5" r=".5" fill="currentColor"/></svg>')

icon_logo = Safe(
    '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" '
    'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<line x1="10" x2="14" y1="2" y2="2"/>'
    '<line x1="12" x2="15" y1="14" y2="11"/>'
    '<circle cx="12" cy="14" r="8"/></svg>')


# ─── client script ───────────────────────────────────────────────────

CLIENT_FPS_SCRIPT = """
(function() {
  let last = performance.now(), ema = 60, frames = 0;
  function tick(now) {
    const dt = now - last;
    last = now;
    if (dt > 0 && dt < 1000) {
      ema = ema * 0.9 + (1000 / dt) * 0.1;
    }
    if (++frames % 30 === 0) {
      const el = document.getElementById('client-fps');
      if (el) el.textContent = String(Math.round(ema));
    }
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
})();
"""


# ─── components ──────────────────────────────────────────────────────

def sparkline(values, max_value=None, height=30, hue_shift=None, klass="spark-short"):
    if not values:
        return h.div({"class": f"stage {klass}"})
    if max_value is None:
        max_value = max(values) or 1
    if max_value <= 0:
        max_value = 1
    n = len(values)
    points = " ".join(
        f"{round(100 * i / max(1, n - 1))},{round(height - height * v / max_value)}"
        for i, v in enumerate(values))
    style = "--fg: 0.9"
    if hue_shift is not None:
        style += f"; --hue-shift: {hue_shift}"
    return h.div({"class": f"stage {klass}", "style": style},
        h.svg(
            {"viewBox": f"0 0 100 {height}", "preserveAspectRatio": "none",
             "class": "spark"},
            h.polyline({
                "points": points, "fill": "none", "stroke": "currentColor",
                "stroke-width": "0.8", "vector-effect": "non-scaling-stroke",
                "stroke-linejoin": "round",
            })))


def cpu_chart_with_legend(snap, window):
    n_cores = len(snap["cpu_per_core"])

    core_groups = []
    for i in range(n_cores):
        plotted = snap["chart"][i] if i < len(snap["chart"]) else []
        if not plotted:
            continue
        n = len(plotted)
        points = " ".join(
            f"{round(100 * j / max(1, n - 1))},{round(100 - v)}"
            for j, v in enumerate(plotted))
        hue = (i * 360 // max(1, n_cores)) % 360
        core_groups.append(
            h.g({"style": f"--fg: 0.85; --hue-shift: {hue}",
                 "stroke": "currentColor", "stroke-width": "1.2",
                 "fill": "none"},
                h.polyline({
                    "points": points,
                    "vector-effect": "non-scaling-stroke",
                    "stroke-linejoin": "round",
                })))

    gridlines = h.g({"stroke": "var(--border)", "stroke-width": "1"},
        h.line({"x1": "0", "y1": "25", "x2": "100", "y2": "25"}),
        h.line({"x1": "0", "y1": "50", "x2": "100", "y2": "50"}),
        h.line({"x1": "0", "y1": "75", "x2": "100", "y2": "75"}))

    legend_rows = [
        h.div({"class": "row", "style": "align-items: center"},
            h.span({"class": "swatch",
                    "style": f"--fg: 0.85; --hue-shift: {(i * 360 // max(1, n_cores)) % 360}; "
                             "color: currentColor"}),
            h.span({"style": "--type: -1"}, f"c{i}"),
            h.span({"class": "mono", "style": "--type: -1; --fg: -0.5; margin-inline-start: auto"},
                   fmt_pct(avg(snap["core_recent"][i]) if i < len(snap["core_recent"]) else 0)))
        for i in range(n_cores)
    ]

    return h.div({"class": "card stage column"},
        h.div({"class": "spread"},
            h.span({"style": "--type: -2; --fg: -0.5; text-transform: uppercase"},
                   f"cpu · {n_cores} cores · smoothed over {window}"),
            h.span({"class": "mono", "style": "--type: -1"},
                   fmt_pct(avg(snap["cpu_recent"])))),
        h.div({"class": "hud-overlay cpu-chart"},
            h.svg(
                {"viewBox": "0 0 100 100", "preserveAspectRatio": "none",
                 "class": "spark"},
                gridlines,
                *core_groups),
            h.div({"class": "↖ stage glass column",
                   "style": "padding: 0.4em 0.6em; border: 1px solid var(--border); "
                            "border-radius: var(--cfg-radius); align-items: stretch; "
                            "min-inline-size: 7em"},
                *legend_rows)))


HUE_MEM, HUE_DISK = 30, 60
HUE_NET_RX, HUE_NET_TX = 180, 210


def stat_card(label, primary, tag_value=None, tag_kind="suc",
              extra=None, hue_shift=None):
    style = f"--hue-shift: {hue_shift}" if hue_shift is not None else None
    attrs = {"class": "card stage column"}
    if style:
        attrs["style"] = style
    children = [
        h.div({"class": "spread"},
            h.span({"style": "--type: -2; --fg: -0.5; text-transform: uppercase"}, label),
            h.span({"class": f"tag {tag_kind}"}, tag_value) if tag_value is not None else "")
    ]
    children.append(h.span({"style": "--type: 1", "class": "mono"}, primary))
    if extra:
        children.append(h.span({"style": "--type: -2; --fg: -0.5"}, extra))
    return h.div(attrs, *children)


def stats_row(snap, window):
    mem_pct  = (snap["mem_used"]  / snap["mem_total"]  * 100.0) if snap["mem_total"]  else 0.0
    disk_pct = (snap["disk_used"] / snap["disk_total"] * 100.0) if snap["disk_total"] else 0.0
    net_samples = min(NET_HISTORY, window_seconds(window) * 60)

    return h.div({"class": "grid", "style": "--grid-min: 11rem"},
        stat_card(
            "memory",
            f"{fmt_bytes(snap['mem_used'])} / {fmt_bytes(snap['mem_total'])}",
            tag_value=fmt_pct(mem_pct),
            tag_kind=semantic_class(mem_pct),
            extra=f"buf {fmt_bytes(snap['mem_buffers'])} · cache {fmt_bytes(snap['mem_cached'])}",
            hue_shift=HUE_MEM),
        stat_card(
            "disk",
            f"{fmt_bytes(snap['disk_used'])} / {fmt_bytes(snap['disk_total'])}",
            tag_value=fmt_pct(disk_pct),
            tag_kind=semantic_class(disk_pct, 80, 95),
            hue_shift=HUE_DISK),
        h.div({"class": "card stage column",
               "style": f"--hue-shift: {HUE_NET_RX}"},
            h.span({"style": "--type: -2; --fg: -0.5; text-transform: uppercase"}, "net rx"),
            h.span({"style": "--type: 1", "class": "mono"}, fmt_rate(snap['net_rx_per_s'])),
            h.div({"class": "tablet desktop"},
                sparkline(snap["history"]["net_rx"][-net_samples:], height=20))),
        h.div({"class": "card stage column",
               "style": f"--hue-shift: {HUE_NET_TX}"},
            h.span({"style": "--type: -2; --fg: -0.5; text-transform: uppercase"}, "net tx"),
            h.span({"style": "--type: 1", "class": "mono"}, fmt_rate(snap['net_tx_per_s'])),
            h.div({"class": "tablet desktop"},
                sparkline(snap["history"]["net_tx"][-net_samples:], height=20))))


def process_table(snap):
    return h.div({"class": "card stage column"},
        h.span({"style": "--type: -2; --fg: -0.5; text-transform: uppercase"},
               f"top {len(snap['processes'])} processes"),
        h.table(
            Safe('<colgroup>'
                 '<col style="inline-size: 5em">'
                 '<col>'
                 '<col style="inline-size: 4.5em">'
                 '<col style="inline-size: 5em">'
                 '</colgroup>'),
            h.thead(h.tr(
                h.th("pid"), h.th("name"),
                h.th({"class": "r"}, "cpu%"), h.th({"class": "r"}, "rss"))),
            h.tbody(
                *[h.tr(
                    h.td({"style": "--fg: -0.5"}, str(p[3])),
                    h.td(p[0]),
                    h.td({"class": "r mono"}, fmt_pct(p[1])),
                    h.td({"class": "r mono"}, fmt_bytes(p[2])))
                  for p in snap["processes"]])))


# ─── chrome (rendered same on every fat morph) ───────────────────────
# All theme/style signals use the $_ prefix → local-only, not synced to
# backend. This is correct for pure UI state per Datastar conventions.

def banner():
    return h.section(
        {"class": "pg-banner lcr wrn card stage"},
        h.small({"style": "--fg: -0.6; --type: -1"}, "may 2026"),
        h.span("live demo — multi-tab to see polling fallback"),
        h.button(
            {"class": "icon-btn stage", "aria-label": "Dismiss",
             "data-on:click": "el.closest('.pg-banner').remove()"},
            icon_x))


def header(current_window, snap):
    window_tabs = h.nav({"class": "tabs underline"},
        *[h.a({
            "href": window_url(label),
            "aria-current": "page" if label == current_window else None,
        }, label) for label, _ in WINDOWS])

    uptime_text = f"up {fmt_uptime(snap['uptime_s'])}" if snap.get('uptime_s') else "—"

    return h.header({"class": "pg-header spread"},
        h.a({"href": "/", "class": "row logo-pill stage glass",
             "style": "text-decoration: none; color: inherit"},
            icon_logo,
            h.strong("Onit"),
            h.span({"style": "--type: -2; --fg: -0.5; margin-inline-start: 0.5em"},
                   uptime_text)),
        h.div({"class": "row"},
            h.span({"style": "--type: -2; --fg: -0.5"}, "sample window"),
            window_tabs,
            h.button({
                "class": "icon-btn", "aria-label": "Cycle color palette",
                "title": "Cycle color palette",
                "data-on:click":
                    "$_hue = ($_hue + 30) % 360, "
                    "document.body.style.setProperty('--hue', $_hue)",
            }, icon_palette),
            h.button({
                "class": "icon-btn", "aria-label": "Toggle theme",
                "data-on:click":
                    "$_theme = $_theme === 'light' ? 'dark' : 'light', "
                    "document.body.setAttribute('data-ui-theme', $_theme)",
            }, icon_moon),
            h.button({
                "class": "icon-btn", "aria-label": "Cycle font size",
                "data-on:click":
                    "$_type = $_type === 'sm' ? 'md' : ($_type === 'md' ? 'lg' : 'sm'), "
                    "document.body.setAttribute('data-ui-type', $_type)",
            }, icon_font)))


def footer(snap, count, mode):
    has_data = bool(snap.get('ts'))
    return h.footer({"class": "pg-footer spread"},
        h.div({"class": "row"},
            h.span({"class": f"tag {'suc' if mode == 'live' else 'inf'}"}, mode)
                if has_data else h.span({"style": "--fg: -0.5"}, "connecting…"),
            h.span(f"{count} viewer{'s' if count != 1 else ''}") if has_data else "",
            h.span({"style": "--fg: -0.5"}, "·") if has_data else "",
            h.span({"class": "mono"}, f"sampler {snap['sampler_fps']:.0f}Hz") if has_data else "",
            h.span({"class": "mono"}, f"tick {snap['sampler_tick_ms']:.1f}ms") if has_data else "",
            h.span({"style": "--fg: -0.5"}, "· client "),
            h.span({"id": "client-fps", "class": "mono",
                    "data-ignore-morph": ""}, "—"),
            h.span({"style": "--fg: -0.5"}, "fps")),
        h.div({"class": "row", "style": "--fg: -0.5"},
            h.span("py_sse + stick.css + datastar"),
            h.span("·"),
            h.span("github.com/Deufel")))


# ─── fat morph: the entire page body, every frame ────────────────────
# This is the Datastar pattern: send the whole document on every update.
# Idiomorph reconciles it efficiently. Brotli compresses against the
# previous frame's bytes. Because 99% of the document is identical
# tick-to-tick, the compressed payload approaches the size of just the
# changed bytes.

def body_content(snap, count, mode, window, interval_ms=None):
    """Everything inside <body>. Same shape for initial render and for
    every SSE patch. Fat morph friendly."""
    dashboard_attrs = {"id": "dashboard", "class": "column"}
    if mode == "poll" and interval_ms is not None:
        dashboard_attrs[f"data-on-interval__duration.{interval_ms}ms"] = (
            f"@get('/dashboard/{window}')"
        )

    return [
        banner(),
        header(window, snap),
        h.main({"class": "pg-main"},
            h.div(dashboard_attrs,
                stats_row(snap, window),
                cpu_chart_with_legend(snap, window),
                process_table(snap))),
        footer(snap, count, mode),
        h.script(CLIENT_FPS_SCRIPT),
    ]


def page(window, snap=None, count=0, mode="live", interval_ms=None):
    """Full page render. `snap` may be None for initial render before
    any sampler data; in that case we render a placeholder dashboard.

    The body is identical in structure between initial render and SSE
    patches — fat morph friendly. Theme/style state is local-only via
    $_ prefixed signals."""
    if snap is None:
        # Pre-data placeholder — same structure, empty values
        snap = {
            "uptime_s": 0, "cpu_pct": 0, "cpu_per_core": [], "cpu_recent": [],
            "core_recent": [], "chart": [],
            "mem_used": 0, "mem_total": 1, "mem_buffers": 0, "mem_cached": 0,
            "disk_used": 0, "disk_total": 1,
            "net_rx_per_s": 0, "net_tx_per_s": 0, "processes": [],
            "sampler_fps": 0, "sampler_tick_ms": 0, "ts": 0,
            "history": {"cpu": [], "net_rx": [], "net_tx": []},
        }

    return h.html({"id":"html"},
        h.head(
            h.title(f"vps monitor — {window}"),
            h.meta(charset="utf-8"),
            h.meta(name="viewport", content="width=device-width, initial-scale=1"),
            h.link(rel="stylesheet", href=STICK),
            h.script(type="module", src=DATASTAR),
            h.style(PAGE_STYLE)),
        h.body({
            "class": "page stage",
            # Local-only signals (underscore prefix). Initial values
            # only — they live in the browser.
            "data-signals": '{"_hue":0,"_theme":"dark","_type":"md"}',
            # The initial GET that subscribes to the SSE stream.
            "data-on-load": f"@get('/stream/{window}')",
        }, *body_content(snap, count, mode, window, interval_ms)))


# ─── routes ──────────────────────────────────────────────────────────

def sse_event_patch_full_page(html_str):
    """Send a full-page patch. Datastar's morph reconciles by id at
    every level — the body element matches, idiomorph diffs the
    subtree. Brotli compresses against prior frames."""
    return f"event: datastar-patch-elements\ndata: elements {html_str}"


def make_page_handler(window):
    """Initial page render — full HTML, no streaming data yet. The
    body's data-on-load fires the SSE subscription."""
    def handler(req):
        return html(h_render(page(window)))
    return handler


def make_stream_handler(window):
    """SSE stream. Every frame sends the entire <html> document with
    a stable id so idiomorph morphs in place. Brotli compresses against
    prior frames. Polling fallback kicks in past the LiveCounter cap —
    same handler, same render path, just a different mode flag that
    bakes data-on-interval into the dashboard wrapper."""
    def handler(req):
        resource = f"stream-{window}"

        def render_frame(snap, count, mode, interval=None):
            doc = h.html({"id": "html", "data-ui-theme": "dark"},
                h.head(
                    h.title(f"vps monitor — {window}"),
                    h.meta(charset="utf-8"),
                    h.meta(name="viewport", content="width=device-width, initial-scale=1"),
                    h.link(rel="stylesheet", href=STICK),
                    h.script(type="module", src=DATASTAR),
                    h.style(PAGE_STYLE)),
                h.body({
                    "class": "page stage",
                    "data-signals": '{"_hue":0,"_theme":"dark","_type":"md"}',
                }, *body_content(snap, count, mode, window, interval)))
            return sse_event_patch_full_page(h_render(doc))

        # Polling fallback: above the live cap, send a one-shot frame
        # with data-on-interval set, and close. The browser will fire
        # another @get('/stream/{window}') after the interval; that
        # request gets the latest live/poll decision afresh.
        if not live.should_be_live(resource):
            snap = snapshot(window)
            count = live.count(resource)
            interval = live.poll_interval_ms(resource)
            yield render_frame(snap, count, "poll", interval)
            return

        # Live path: hold the connection open. The LiveCounter's join()
        # context manager registers this viewer; if the connection drops
        # the count goes back down via the context exit. If the count
        # is later under cap again, polling clients get promoted to live
        # the next time their interval fires.
        with live.join(resource):
            count = live.count(resource)
            yield render_frame(snapshot(window), count, "live")
            while True:
                changes.wait(timeout=10)
                try:
                    count = live.count(resource)
                    yield render_frame(snapshot(window), count, "live")
                except (OSError, BrokenPipeError):
                    return

    return handler


def get_healthz(req):
    return (200, [("content-type", "text/plain")], b"ok")


ROUTES = [("GET", "/healthz", get_healthz)]
for label, _ in WINDOWS:
    page_path = "/" if label == DEFAULT_WINDOW else f"/{label}"
    ROUTES.append(("GET", page_path, make_page_handler(label)))
    ROUTES.append(("GET", f"/stream/{label}", make_stream_handler(label)))


if __name__ == "__main__":
    threading.Thread(target=sampler_loop, daemon=True, name="sampler").start()
    serve(ROUTES,
          host=os.environ.get("HOST", "0.0.0.0"),
          port=int(os.environ.get("PORT", "8001")))
