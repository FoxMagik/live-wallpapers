#!/usr/bin/python3
"""livewall host: show an HTML scene as a live desktop background on Hyprland.

One borderless WebKitGTK view per monitor on the wlr-layer-shell BOTTOM layer
(above the regular wallpaper, below every window), click-through, no keyboard.
Watches Hyprland's event socket and calls window.livewall.pause()/resume() in
the page when the desktop is covered, mirroring owe's [pause] settings in
~/.config/owe/config.toml (fullscreen / occupied_workspace).

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


class Host:
    def __init__(self, url, fps):
        self.url = url
        self.fps = fps
        self.policy = load_pause_policy()
        self.views = {}  # (x, y) -> (window, webview, paused-state)
        self.pending = False
        display = Gdk.Display.get_default()
        for i in range(display.get_n_monitors()):
            self.add_monitor(display.get_monitor(i))
        GLib.timeout_add(1500, self.recompute)  # settle once pages have loaded
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
