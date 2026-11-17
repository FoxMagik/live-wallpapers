#!/usr/bin/python3
"""livewall host: show an HTML scene as a live desktop background on Hyprland.

One borderless WebKitGTK view per monitor on the wlr-layer-shell BOTTOM layer
(above the regular wallpaper, below every window), click-through, no keyboard.
Watches Hyprland's event socket and calls window.livewall.pause()/resume() in
the page when the desktop is covered, mirroring owe's [pause] settings in
~/.config/owe/config.toml (fullscreen / occupied_workspace).

Also streams lightweight live system telemetry (/proc & /sys) once per second
while unpaused to scenes that implement `window.livewall.pushStats(data)` or
`window.onLivewallStats(data)`.

usage: livewall.py <scene.html | URL> [--fps 30]
"""
import argparse
import json
import os
import pathlib
import signal
import socket
import subprocess
import threading
import time
import tomllib

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("WebKit2", "4.1")
gi.require_version("GtkLayerShell", "0.1")
import cairo  # noqa: E402
from gi.repository import Gdk, GLib, Gtk, GtkLayerShell, WebKit2  # noqa: E402

PAUSE_EVENTS = {
    "workspace", "workspacev2", "focusedmon", "focusedmonv2", "openwindow", "closewindow",
    "movewindow", "movewindowv2", "fullscreen", "changefloatingmode", "minimized",
    "activespecial", "activespecialv2", "monitoradded", "monitorremoved",
}


def load_pause_policy():
    policy = {"fullscreen": True, "occupied_workspace": True}
    cfg = pathlib.Path.home() / ".config/owe/config.toml"
    try:
        with cfg.open("rb") as f:
            policy.update({k: v for k, v in tomllib.load(f).get("pause", {}).items() if k in policy})
    except (OSError, tomllib.TOMLDecodeError):
        pass
    if os.environ.get("LIVEWALL_ALWAYS_ANIMATE") == "1":
        policy = {"fullscreen": False, "occupied_workspace": False}
    return policy


def hyprctl(*args):
    try:
        return json.loads(subprocess.run(["hyprctl", "-j", *args], capture_output=True, text=True, timeout=2).stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return []


class TelemetryCollector:
    """Zero-subprocess /proc & /sys telemetry reader for HUD scenes."""

    def __init__(self):
        self.prev_cpu = {}
        self.prev_net = None
        self.prev_net_t = 0.0
        self.prev_procs = {}
        self.prev_total_jiffies = 0
        self.page_size = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
        un = os.uname()
        self.hostname = un.nodename
        self.kernel = un.release.split("-")[0]
        self.sample()  # prime deltas

    def _read_cpu(self):
        out = {}
        total_jiffies = 0
        try:
            with open("/proc/stat", "r", encoding="utf-8") as f:
                for line in f:
                    if not line.startswith("cpu"):
                        break
                    parts = line.split()
                    name = parts[0]
                    vals = [int(x) for x in parts[1:]]
                    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
                    tot = sum(vals)
                    if name == "cpu":
                        total_jiffies = tot
                    prev = self.prev_cpu.get(name)
                    self.prev_cpu[name] = (tot, idle)
                    if prev:
                        dtot = tot - prev[0]
                        didle = idle - prev[1]
                        out[name] = max(0.0, min(100.0, (dtot - didle) * 100.0 / dtot)) if dtot > 0 else 0.0
                    else:
                        out[name] = 0.0
        except OSError:
            pass
        return out, total_jiffies

    def _read_mem(self):
        info = {}
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    k, v = line.split(":", 1)
                    info[k.strip()] = int(v.strip().split()[0])
        except (OSError, ValueError):
            return {"pct": 0, "usedGb": 0, "totalGb": 16, "cachePct": 0, "swapPct": 0, "totalKb": 1}
        tot = max(1, info.get("MemTotal", 1))
        avail = info.get("MemAvailable", tot)
        used = max(0, tot - avail)
        cache = info.get("Cached", 0) + info.get("Buffers", 0)
        stot = info.get("SwapTotal", 0)
        sfree = info.get("SwapFree", 0)
        return {
            "pct": round(used * 100.0 / tot, 1),
            "usedGb": round(used / 1048576.0, 2),
            "totalGb": round(tot / 1048576.0, 1),
            "cachePct": round(cache * 100.0 / tot, 1),
            "swapPct": round((stot - sfree) * 100.0 / stot, 1) if stot > 0 else 0.0,
            "totalKb": tot,
        }

    def _read_net(self):
        now = time.monotonic()
        rx_tot, tx_tot = 0, 0
        primary_iface = "eth0"
        best_bytes = -1
        try:
            with open("/proc/net/dev", "r", encoding="utf-8") as f:
                for line in list(f)[2:]:
                    if ":" not in line:
                        continue
                    iface, rest = line.split(":", 1)
                    iface = iface.strip()
                    if iface == "lo":
                        continue
                    fields = [int(x) for x in rest.split()]
                    rx, tx = fields[0], fields[8]
                    rx_tot += rx
                    tx_tot += tx
                    if rx + tx > best_bytes:
                        best_bytes = rx + tx
                        primary_iface = iface
        except (OSError, ValueError, IndexError):
            pass
        rx_kbs, tx_kbs = 0.0, 0.0
        if self.prev_net is not None and now > self.prev_net_t:
            dt = now - self.prev_net_t
            rx_kbs = max(0.0, (rx_tot - self.prev_net[0]) / 1024.0 / dt)
            tx_kbs = max(0.0, (tx_tot - self.prev_net[1]) / 1024.0 / dt)
        self.prev_net = (rx_tot, tx_tot)
        self.prev_net_t = now
        return {"rxKbs": round(rx_kbs, 1), "txKbs": round(tx_kbs, 1), "iface": primary_iface}

    def _read_temps(self):
        temps = {}
        try:
            for hw in pathlib.Path("/sys/class/hwmon").iterdir():
                try:
                    name = (hw / "name").read_text(encoding="utf-8").strip()
                    raw = int((hw / "temp1_input").read_text(encoding="utf-8").strip()) / 1000.0
                except (OSError, ValueError):
                    continue
                if raw <= 0 or raw > 150:
                    continue
                if name in ("coretemp", "k10temp", "zenpower", "cpu_thermal"):
                    temps["cpu"] = round(raw, 1)
                elif name == "nvme":
                    temps["nvme"] = round(raw, 1)
                elif "wifi" in name:
                    temps["wifi"] = round(raw, 1)
                elif "pch" in name or "smm" in name or "acpitz" in name:
                    temps.setdefault("sys", round(raw, 1))
        except OSError:
            pass
        return temps

    def _read_procs(self, dt_jiffies, mem_total_kb, num_cores):
        procs = []
        new_prev = {}
        denom = max(1, dt_jiffies) / max(1, num_cores)
        try:
            for entry in os.scandir("/proc"):
                if not entry.name.isdigit():
                    continue
                pid = int(entry.name)
                try:
                    with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as f:
                        s = f.read()
                    rpar = s.rfind(")")
                    comm = s[s.find("(") + 1:rpar][:14]
                    fields = s[rpar + 2:].split()
                    utime = int(fields[11])
                    stime = int(fields[12])
                    rss_kb = (int(fields[21]) * self.page_size) // 1024
                    ticks = utime + stime
                    new_prev[pid] = ticks
                    prev_ticks = self.prev_procs.get(pid, ticks)
                    cpu_pct = max(0.0, min(100.0, (ticks - prev_ticks) * 100.0 / denom))
                    mem_pct = max(0.0, min(100.0, rss_kb * 100.0 / mem_total_kb))
                    if cpu_pct >= 0.1 or mem_pct >= 0.6:
                        procs.append((cpu_pct + mem_pct * 0.25, pid, comm, round(cpu_pct, 1), round(mem_pct, 1)))
                except (OSError, ValueError, IndexError):
                    continue
        except OSError:
            pass
        self.prev_procs = new_prev
        procs.sort(reverse=True)
        return [{"pid": p[1], "name": p[2], "cpu": p[3], "mem": p[4]} for p in procs[:6]]

    def sample(self):
        cpus, total_jiffies = self._read_cpu()
        dt_jiffies = max(1, total_jiffies - self.prev_total_jiffies)
        self.prev_total_jiffies = total_jiffies
        cores = [round(cpus[k], 1) for k in sorted((k for k in cpus if k != "cpu"), key=lambda s: int(s[3:]))]
        mem = self._read_mem()
        net = self._read_net()
        temps = self._read_temps()
        procs = self._read_procs(dt_jiffies, mem["totalKb"], len(cores) or 1)
        mem.pop("totalKb", None)

        disk = {"pct": 0.0, "usedGb": 0.0, "totalGb": 100.0}
        try:
            st = os.statvfs("/")
            tot = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
            used = max(0, tot - free)
            if tot > 0:
                disk = {
                    "pct": round(used * 100.0 / tot, 1),
                    "usedGb": round(used / (1024 ** 3), 1),
                    "totalGb": round(tot / (1024 ** 3), 1),
                }
        except OSError:
            pass

        load = [0.0, 0.0, 0.0]
        tasks = "1/256"
        try:
            parts = pathlib.Path("/proc/loadavg").read_text(encoding="utf-8").split()
            load = [round(float(parts[0]), 2), round(float(parts[1]), 2), round(float(parts[2]), 2)]
            tasks = parts[3]
        except (OSError, ValueError, IndexError):
            pass

        uptime = 0
        try:
            uptime = int(float(pathlib.Path("/proc/uptime").read_text(encoding="utf-8").split()[0]))
        except (OSError, ValueError, IndexError):
            pass

        return {
            "live": True,
            "host": self.hostname,
            "kernel": self.kernel,
            "cpu": round(cpus.get("cpu", 0.0), 1),
            "cores": cores,
            "mem": mem,
            "disk": disk,
            "net": net,
            "temps": temps,
            "load": load,
            "tasks": tasks,
            "uptime": uptime,
            "procs": procs,
        }


class Host:
    def __init__(self, url, fps):
        self.url = url
        self.fps = fps
        self.policy = load_pause_policy()
        self.views = {}  # (x, y) -> [window, webview, paused-state]
        self.pending = False
        self.telemetry = TelemetryCollector()
        display = Gdk.Display.get_default()
        for i in range(display.get_n_monitors()):
            self.add_monitor(display.get_monitor(i))
        GLib.timeout_add(1500, self.recompute)  # settle once pages have loaded
        GLib.timeout_add(1000, self.push_telemetry)
        threading.Thread(target=self.watch_hyprland, daemon=True).start()

    def add_monitor(self, mon):
        geo = mon.get_geometry()
        win = Gtk.Window()
        GtkLayerShell.init_for_window(win)
        GtkLayerShell.set_monitor(win, mon)
        GtkLayerShell.set_layer(win, GtkLayerShell.Layer.BOTTOM)
        GtkLayerShell.set_namespace(win, "livewall")
        GtkLayerShell.set_exclusive_zone(win, -1)
        GtkLayerShell.set_keyboard_mode(win, GtkLayerShell.KeyboardMode.NONE)
        for edge in (GtkLayerShell.Edge.TOP, GtkLayerShell.Edge.BOTTOM, GtkLayerShell.Edge.LEFT, GtkLayerShell.Edge.RIGHT):
            GtkLayerShell.set_anchor(win, edge, True)

        settings = WebKit2.Settings()
        settings.set_enable_developer_extras(False)
        settings.set_hardware_acceleration_policy(WebKit2.HardwareAccelerationPolicy.ALWAYS)
        settings.set_allow_file_access_from_file_urls(True)
        if os.environ.get("LIVEWALL_DEBUG") == "1":
            settings.set_enable_write_console_messages_to_stdout(True)
        view = WebKit2.WebView.new_with_settings(settings)
        view.set_background_color(Gdk.RGBA(0, 0, 0, 1))
        view.connect("context-menu", lambda *a: True)
        sep = "&" if "?" in self.url else "?"
        view.load_uri(f"{self.url}{sep}fps={self.fps}")
        win.add(view)
        win.connect("realize", lambda w: w.input_shape_combine_region(cairo.Region()))
        win.show_all()
        win.input_shape_combine_region(cairo.Region())  # click-through
        self.views[(geo.x, geo.y)] = [win, view, None]

    def push_telemetry(self):
        active_views = [entry[1] for entry in self.views.values() if not entry[2]]
        if active_views:
            payload = json.dumps(self.telemetry.sample(), separators=(",", ":"))
            js = f"window.livewall && window.livewall.pushStats && window.livewall.pushStats({payload})"
            for view in active_views:
                view.evaluate_javascript(js, -1, None, None, None, None, None)
        return True

    # --- pause policy ---------------------------------------------------------
    def recompute(self):
        self.pending = False
        monitors = hyprctl("monitors")
        clients = hyprctl("clients")
        for m in monitors:
            key = (m.get("x"), m.get("y"))
            if key not in self.views:
                continue
            ws_ids = {m.get("activeWorkspace", {}).get("id")}
            special = m.get("specialWorkspace", {}).get("id", 0)
            if special:
                ws_ids.add(special)
            on_screen = [c for c in clients
                         if c.get("mapped", True) and not c.get("hidden")
                         and c.get("workspace", {}).get("id") in ws_ids]
            covered = False
            if self.policy["occupied_workspace"] and on_screen:
                covered = True
            if self.policy["fullscreen"] and any(c.get("fullscreen") for c in on_screen):
                covered = True
            self.set_paused(key, covered)
        return False

    def set_paused(self, key, paused):
        entry = self.views[key]
        if entry[2] == paused:
            return
        entry[2] = paused
        js = "window.livewall && window.livewall.%s()" % ("pause" if paused else "resume")
        entry[1].evaluate_javascript(js, -1, None, None, None, None, None)

    def schedule(self):
        if not self.pending:
            self.pending = True
            GLib.timeout_add(60, self.recompute)
        return False

    def watch_hyprland(self):
        sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
        runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        if not sig:
            return
        path = f"{runtime}/hypr/{sig}/.socket2.sock"
        while True:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.connect(path)
                    buf = b""
                    while chunk := s.recv(4096):
                        buf += chunk
                        *lines, buf = buf.split(b"\n")
                        if any(l.split(b">>", 1)[0].decode(errors="ignore") in PAUSE_EVENTS for l in lines):
                            GLib.idle_add(self.schedule)
            except OSError:
                pass
            threading.Event().wait(2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scene")
    ap.add_argument("--fps", type=int, default=30)
    args = ap.parse_args()
    url = args.scene
    if "://" not in url:
        url = pathlib.Path(url).resolve().as_uri()
    if not GtkLayerShell.is_supported():
        raise SystemExit("livewall: compositor lacks wlr-layer-shell")
    Host(url, args.fps)
    try:
        from gi.repository import GLibUnix
        add_signal = GLibUnix.signal_add
    except ImportError:
        add_signal = GLib.unix_signal_add
    for sig in (signal.SIGTERM, signal.SIGINT):
        add_signal(GLib.PRIORITY_DEFAULT, sig, Gtk.main_quit)
    Gtk.main()


if __name__ == "__main__":
    main()
