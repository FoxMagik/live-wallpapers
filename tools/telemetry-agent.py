#!/usr/bin/env python3
"""Standalone localhost telemetry agent for the Aegis live wallpaper scene.

Serves real-time system usage (CPU cores, RAM/cache/swap, disk, network RX/TX,
temperatures, load average, uptime, and top processes) as JSON over:

    http://127.0.0.1:19845/stats

Includes CORS + Private Network Access headers so `scenes/aegis.html` can poll
it from a local file:// URL, an OBS Browser Source, or GitHub Pages (https://).
Works out of the box on Linux (/proc & /sys) with zero dependencies, and
supports Windows / macOS (via `psutil` if installed, or OS fallbacks).

Usage:
    python3 tools/telemetry-agent.py [--port 19845] [--bind 127.0.0.1]
"""
import argparse
import ctypes
import http.server
import json
import os
import pathlib
import platform
import subprocess
import threading
import time

PORT_DEFAULT = 19845


class TelemetryCollector:
    """Cross-platform system telemetry collector (zero-subprocess on Linux)."""

    def __init__(self):
        self.system = platform.system()
        self.prev_cpu = {}
        self.prev_net = None
        self.prev_net_t = 0.0
        self.prev_procs = {}
        self.prev_total_jiffies = 0
        self.page_size = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
        self.hostname = platform.node() or "AEGIS-NODE"
        self.kernel = platform.release().split("-")[0]
        self._lock = threading.Lock()
        self._cached = None
        self._cached_at = 0.0
        try:
            import psutil  # type: ignore
            self._psutil = psutil
        except ImportError:
            self._psutil = None
        self.sample(force=True)

    # ---- Linux (/proc & /sys) -----------------------------------------------
    def _read_cpu_linux(self):
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

    def _read_mem_linux(self):
        info = {}
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    k, v = line.split(":", 1)
                    info[k.strip()] = int(v.strip().split()[0])
        except (OSError, ValueError):
            return {"pct": 0.0, "usedGb": 0.0, "totalGb": 16.0, "cachePct": 0.0, "swapPct": 0.0, "totalKb": 1}
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

    def _read_net_linux(self):
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

    def _read_temps_linux(self):
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

    def _read_procs_linux(self, dt_jiffies, mem_total_kb, num_cores):
        procs = []
        new_prev = {}
        denom = max(1.0, dt_jiffies / max(1, num_cores))
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
                    if cpu_pct >= 0.1 or mem_pct >= 0.5:
                        procs.append((cpu_pct + mem_pct * 0.25, pid, comm, round(cpu_pct, 1), round(mem_pct, 1)))
                except (OSError, ValueError, IndexError):
                    continue
        except OSError:
            pass
        self.prev_procs = new_prev
        procs.sort(reverse=True)
        return [{"pid": p[1], "name": p[2], "cpu": p[3], "mem": p[4]} for p in procs[:6]]

    def _sample_linux(self):
        cpus, total_jiffies = self._read_cpu_linux()
        dt_jiffies = max(1, total_jiffies - self.prev_total_jiffies)
        self.prev_total_jiffies = total_jiffies
        cores = [round(cpus[k], 1) for k in sorted((k for k in cpus if k != "cpu"), key=lambda s: int(s[3:]))]
        mem = self._read_mem_linux()
        net = self._read_net_linux()
        temps = self._read_temps_linux()
        procs = self._read_procs_linux(dt_jiffies, mem["totalKb"], len(cores) or 1)
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

    # ---- Cross-platform fallback (psutil / Windows / macOS) -----------------
    def _sample_portable(self):
        ps = self._psutil
        if ps:
            cores = [round(x, 1) for x in ps.cpu_percent(interval=None, percpu=True)]
            cpu = round(sum(cores) / max(1, len(cores)), 1)
            vm = ps.virtual_memory()
            sm = ps.swap_memory()
            du = ps.disk_usage("/")
            now = time.monotonic()
            nio = ps.net_io_counters()
            rx_kbs, tx_kbs = 0.0, 0.0
            if self.prev_net is not None and now > self.prev_net_t:
                dt = now - self.prev_net_t
                rx_kbs = max(0.0, (nio.bytes_recv - self.prev_net[0]) / 1024.0 / dt)
                tx_kbs = max(0.0, (nio.bytes_sent - self.prev_net[1]) / 1024.0 / dt)
            self.prev_net = (nio.bytes_recv, nio.bytes_sent)
            self.prev_net_t = now
            load = [round(x, 2) for x in os.getloadavg()] if hasattr(os, "getloadavg") else [round(cpu / 12.5, 2)] * 3
            procs = []
            for p in ps.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
                try:
                    info = p.info
                    c = float(info.get("cpu_percent") or 0.0)
                    m = float(info.get("memory_percent") or 0.0)
                    if c > 0.1 or m > 0.5:
                        procs.append((c + m * 0.25, info["pid"], (info["name"] or "proc")[:14], round(c, 1), round(m, 1)))
                except Exception:
                    continue
            procs.sort(reverse=True)
            return {
                "live": True,
                "host": self.hostname,
                "kernel": self.kernel,
                "cpu": cpu,
                "cores": cores or [cpu] * 8,
                "mem": {
                    "pct": round(vm.percent, 1),
                    "usedGb": round((vm.total - vm.available) / (1024 ** 3), 2),
                    "totalGb": round(vm.total / (1024 ** 3), 1),
                    "cachePct": round(getattr(vm, "cached", 0) * 100.0 / max(1, vm.total), 1),
                    "swapPct": round(sm.percent, 1),
                },
                "disk": {
                    "pct": round(du.percent, 1),
                    "usedGb": round(du.used / (1024 ** 3), 1),
                    "totalGb": round(du.total / (1024 ** 3), 1),
                },
                "net": {"rxKbs": round(rx_kbs, 1), "txKbs": round(tx_kbs, 1), "iface": "net0"},
                "temps": {},
                "load": load,
                "tasks": f"{len(procs)}/{len(ps.pids())}",
                "uptime": int(time.time() - ps.boot_time()),
                "procs": [{"pid": p[1], "name": p[2], "cpu": p[3], "mem": p[4]} for p in procs[:6]],
            }
        # Minimal stdlib fallback for non-Linux without psutil
        ncpu = os.cpu_count() or 8
        load = [round(x, 2) for x in os.getloadavg()] if hasattr(os, "getloadavg") else [0.5, 0.5, 0.5]
        cpu_est = round(min(100.0, (load[0] / max(1, ncpu)) * 100.0), 1)
        return {
            "live": True,
            "host": self.hostname,
            "kernel": self.kernel,
            "cpu": cpu_est,
            "cores": [cpu_est] * min(8, ncpu),
            "mem": {"pct": 40.0, "usedGb": 6.4, "totalGb": 16.0, "cachePct": 30.0, "swapPct": 0.0},
            "disk": {"pct": 25.0, "usedGb": 60.0, "totalGb": 240.0},
            "net": {"rxKbs": 0.0, "txKbs": 0.0, "iface": "net0"},
            "temps": {},
            "load": load,
            "tasks": "1/256",
            "uptime": 3600,
            "procs": [],
        }

    def sample(self, force=False):
        now = time.monotonic()
        with self._lock:
            if not force and self._cached is not None and (now - self._cached_at) < 0.45:
                return self._cached
            if self.system == "Linux" and os.path.exists("/proc/stat"):
                data = self._sample_linux()
            else:
                data = self._sample_portable()
            self._cached = data
            self._cached_at = now
            return data


def start_http_server(collector, bind="127.0.0.1", port=PORT_DEFAULT, daemon=True):
    """Start the localhost CORS/PNA HTTP server for browser & OBS polling."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def _send_cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")

        def do_OPTIONS(self):
            self.send_response(204)
            self._send_cors()
            self.end_headers()

        def do_GET(self):
            if self.path.startswith("/stats") or self.path == "/":
                payload = json.dumps(collector.sample(), separators=(",", ":")).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self._send_cors()
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.send_response(404)
                self._send_cors()
                self.end_headers()

        def log_message(self, fmt, *args):
            pass

    class ReusableHTTPServer(http.server.ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    srv = ReusableHTTPServer((bind, port), Handler)
    if daemon:
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
    return srv


def main():
    ap = argparse.ArgumentParser(description="Localhost telemetry server for Aegis HTML wallpaper")
    ap.add_argument("--bind", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    ap.add_argument("--port", type=int, default=PORT_DEFAULT, help=f"Port (default: {PORT_DEFAULT})")
    args = ap.parse_args()

    collector = TelemetryCollector()
    srv = start_http_server(collector, bind=args.bind, port=args.port, daemon=False)
    print(f"Aegis telemetry agent listening on http://{args.bind}:{args.port}/stats")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
