#!/usr/bin/env python3
"""Simple Tkinter GUI for simtemp

Features:
- Reads packed binary `simtemp_sample` records from /dev/simtemp
- Shows current temperature, timestamp, and alert status
- Plots a small scrolling history (text-based) and numeric display
- Controls for sampling_ms, threshold_mC, and mode via sysfs under
  /sys/class/simtemp/simtemp/ if present, else tries /sys/devices/platform/nxp_simtemp/

Run: python3 user/gui/app.py

Quick start (evaluator-friendly)
- Enable access (preferred wrapper, from repo root):
    ./setup_access.py
  This will attempt a graphical privilege prompt (pkexec) or print a sudo fallback.

- Run the GUI (no root needed once access is configured):
    python3 user/gui/app.py

- For local testing without the device, run the simulator mode:
    python3 user/gui/app.py --simulate
"""

import os
import struct
import threading
import time
from datetime import datetime
import argparse
import tkinter as tk
from tkinter import ttk, messagebox
import subprocess
import shutil
import sys

SAMPLE_FMT = "=QiI"  # u64 timestamp_ns, s32 temp_mC, u32 flags 
SAMPLE_SIZE = struct.calcsize(SAMPLE_FMT)
DEV_PATH = "/dev/simtemp"
SYS_CLASS_PATH = "/sys/class/simtemp/simtemp"
SYS_PLATFORM_PATH = "/sys/devices/platform/nxp_simtemp"

MODES = ["normal", "ramp", "noisy"]

class SimTempReader(threading.Thread):
    """Background thread that reads samples from the char device and pushes them to a queue."""
    def __init__(self, update_callback, stop_event):
        super().__init__(daemon=True)
        self.update_callback = update_callback
        self.stop_event = stop_event
        self.fd = None

    def open_device(self):
        try:
            self.fd = open(DEV_PATH, "rb", buffering=0)
        except Exception as e:
            # device not available
            self.fd = None

    def run(self):
        if not os.path.exists(DEV_PATH):
            # try a short retry loop
            for _ in range(5):
                if self.stop_event.is_set():
                    return
                time.sleep(0.5)
                if os.path.exists(DEV_PATH):
                    break
        self.open_device()
        if not self.fd:
            # notify GUI
            self.update_callback(None, error="/dev/simtemp not available")
            return

        while not self.stop_event.is_set():
            try:
                data = self.fd.read(SAMPLE_SIZE)
                if not data or len(data) < SAMPLE_SIZE:
                    # short read: wait a bit and retry
                    time.sleep(0.1)
                    continue
                ts_ns, temp_mC, flags = struct.unpack(SAMPLE_FMT, data)
                self.update_callback((ts_ns, temp_mC, flags))
            except Exception as e:
                self.update_callback(None, error=str(e))
                time.sleep(0.5)

    def close(self):
        try:
            if self.fd:
                self.fd.close()
                self.fd = None
        except Exception:
            pass


class SimTempSimulator(threading.Thread):
    """Simulator thread that generates samples locally (for GUI testing)."""
    def __init__(self, update_callback, stop_event, sampling_ms=1000, threshold_mC=45000, mode=0):
        super().__init__(daemon=True)
        self.update_callback = update_callback
        self.stop_event = stop_event
        self.sampling_ms = sampling_ms
        self.threshold_mC = threshold_mC
        self.mode = mode
        self.ramp = 0

    def run(self):
        while not self.stop_event.is_set():
            ts_ns = int(time.time() * 1e9)
            if self.mode == 1:  # ramp
                temp_mC = 25000 + ((self.ramp * 200) % 40000)
            elif self.mode == 2:  # noisy
                temp_mC = 30000 + ((self.ramp * 37) % 4001) - 2000
            else:
                temp_mC = 30000 + (self.ramp % 20000)
            flags = 1  # NEW_SAMPLE
            if temp_mC >= self.threshold_mC:
                flags |= 0x2
            self.ramp += 1
            self.update_callback((ts_ns, temp_mC, flags))
            # sleep honoring sampling_ms but wake early if stopped
            for _ in range(max(1, int(self.sampling_ms / 100))):
                if self.stop_event.is_set():
                    break
                time.sleep(self.sampling_ms / 1000.0 / max(1, int(self.sampling_ms / 100)))


class SimTempGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("simtemp GUI")
        self.stop_event = threading.Event()
        self.reader = None

        # state
        self.history = []  # list of (ts, temp_c, alert)

        # top frame: numeric display
        top = ttk.Frame(root, padding=8)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="Temperature:").grid(row=0, column=0, sticky="w")
        self.temp_var = tk.StringVar(value="-- °C")
        ttk.Label(top, textvariable=self.temp_var, font=(None, 16)).grid(row=0, column=1, sticky="w")

        ttk.Label(top, text="Alert:").grid(row=1, column=0, sticky="w")
        self.alert_var = tk.StringVar(value="No")
        ttk.Label(top, textvariable=self.alert_var, font=(None, 12)).grid(row=1, column=1, sticky="w")

        ttk.Label(top, text="Last update:").grid(row=2, column=0, sticky="w")
        self.last_var = tk.StringVar(value="--")
        ttk.Label(top, textvariable=self.last_var).grid(row=2, column=1, sticky="w")

        # middle frame: history listbox
        mid = ttk.Frame(root, padding=(8,0,8,0))
        mid.grid(row=1, column=0, sticky="nsew")
        root.rowconfigure(1, weight=1)
        self.history_box = tk.Listbox(mid, height=10)
        self.history_box.pack(fill="both", expand=True)

        # bottom frame: controls
        bot = ttk.Frame(root, padding=8)
        bot.grid(row=2, column=0, sticky="ew")
        bot.columnconfigure(1, weight=1)

        ttk.Label(bot, text="sampling_ms:").grid(row=0, column=0, sticky="w")
        self.sampling_var = tk.StringVar(value="")
        self.sampling_entry = ttk.Entry(bot, textvariable=self.sampling_var, width=12)
        self.sampling_entry.grid(row=0, column=1, sticky="w")
        ttk.Button(bot, text="Set", command=self.set_sampling).grid(row=0, column=2, sticky="w")

        ttk.Label(bot, text="threshold_mC:").grid(row=1, column=0, sticky="w")
        self.threshold_var = tk.StringVar(value="")
        self.threshold_entry = ttk.Entry(bot, textvariable=self.threshold_var, width=12)
        self.threshold_entry.grid(row=1, column=1, sticky="w")
        ttk.Button(bot, text="Set", command=self.set_threshold).grid(row=1, column=2, sticky="w")

        ttk.Label(bot, text="mode:").grid(row=2, column=0, sticky="w")
        self.mode_var = tk.StringVar(value=MODES[0])
        self.mode_combo = ttk.Combobox(bot, values=MODES, textvariable=self.mode_var, state="readonly", width=12)
        self.mode_combo.grid(row=2, column=1, sticky="w")
        ttk.Button(bot, text="Set", command=self.set_mode).grid(row=2, column=2, sticky="w")

        # admin access buttons (non-privileged UI; actions run the audited installer via polkit)
        admin_frame = ttk.Frame(bot)
        admin_frame.grid(row=0, column=3, rowspan=3, sticky="ne", padx=(8,0))
        ttk.Button(admin_frame, text="Enable Access (admin)", command=self.enable_access_admin).pack(fill="x", pady=(0,4))
        ttk.Button(admin_frame, text="Disable Access (admin)", command=self.disable_access_admin).pack(fill="x")
        ttk.Button(admin_frame, text="Show Access Commands", command=self.show_access_commands).pack(fill="x", pady=(4,0))

        # status bar
        self.status_var = tk.StringVar(value="OK")
        status = ttk.Label(root, textvariable=self.status_var, relief="sunken", anchor="w")
        status.grid(row=3, column=0, sticky="ew")

        # start reader
        self.start_reader()
        # refresh sysfs fields once
        self.refresh_sysfs()

        root.protocol("WM_DELETE_WINDOW", self._on_close)

    def start_reader(self):
        if self.reader:
            return
        self.reader = SimTempReader(self._on_sample, self.stop_event)
        self.reader.start()
        self.status_var.set("Reading from /dev/simtemp...")

    def stop_reader(self):
        if not self.reader:
            return
        self.stop_event.set()
        self.reader.close()
        self.reader.join(timeout=1.0)
        self.reader = None

    def _on_sample(self, sample, error=None):
        # called from reader thread
        if error:
            self.root.after(0, self._show_error, error)
            return
        if not sample:
            return
        ts_ns, temp_mC, flags = sample
        ts = datetime.utcfromtimestamp(ts_ns / 1e9).isoformat() + "Z"
        temp_c = temp_mC / 1000.0
        alert = bool(flags & 0x2)
        self.history.append((ts, temp_c, alert))
        if len(self.history) > 200:
            self.history.pop(0)
        # schedule UI update in main thread
        self.root.after(0, self._update_ui, ts, temp_c, alert)

    def _show_error(self, msg):
        self.status_var.set(f"Error: {msg}")
        messagebox.showerror("simtemp GUI", msg)

    def _update_ui(self, ts, temp_c, alert):
        self.temp_var.set(f"{temp_c:.3f} °C")
        self.alert_var.set("YES" if alert else "No")
        self.last_var.set(ts)
        # update history box
        self.history_box.insert(0, f"{ts} temp={temp_c:.3f}C alert={1 if alert else 0}")
        if self.history_box.size() > 200:
            self.history_box.delete(200, tk.END)

    def refresh_sysfs(self):
        base = SYS_CLASS_PATH if os.path.isdir(SYS_CLASS_PATH) else SYS_PLATFORM_PATH
        try:
            with open(os.path.join(base, "sampling_ms"), "r") as f:
                self.sampling_var.set(f.read().strip())
        except Exception:
            self.sampling_var.set("")
        try:
            with open(os.path.join(base, "threshold_mC"), "r") as f:
                self.threshold_var.set(f.read().strip())
        except Exception:
            self.threshold_var.set("")
        try:
            with open(os.path.join(base, "mode"), "r") as f:
                self.mode_var.set(f.read().strip())
        except Exception:
            self.mode_var.set(MODES[0])

    def _show_text_modal(self, title, text, confirm_label=None):
        """Show a modal dialog with scrollable text. If confirm_label is provided,
        returns True when the user confirms, False otherwise."""
        dlg = tk.Toplevel(self.root)
        dlg.title(title)
        dlg.transient(self.root)
        dlg.grab_set()
        txt = tk.Text(dlg, wrap="none", height=20, width=80)
        txt.insert("1.0", text)
        txt.configure(state="disabled")
        txt.grid(row=0, column=0, columnspan=2, sticky="nsew")
        scr_y = ttk.Scrollbar(dlg, orient="vertical", command=txt.yview)
        scr_y.grid(row=0, column=2, sticky="ns")
        txt.configure(yscrollcommand=scr_y.set)
        btn_frame = ttk.Frame(dlg)
        btn_frame.grid(row=1, column=0, columnspan=3, sticky="e", pady=6)
        result = {"confirmed": False}

        def on_confirm():
            result["confirmed"] = True
            dlg.destroy()

        def on_close():
            dlg.destroy()

        if confirm_label:
            ttk.Button(btn_frame, text=confirm_label, command=on_confirm).pack(side="right", padx=(4,0))
        ttk.Button(btn_frame, text="Close", command=on_close).pack(side="right")

        self.root.wait_window(dlg)
        return result["confirmed"]

    def _run_subprocess_threaded(self, cmd, on_done):
        """Run subprocess in a thread and call on_done(returncode, stdout, stderr) in main thread."""
        def target():
            try:
                p = subprocess.run(cmd, capture_output=True, text=True)
                rc = p.returncode
                out = p.stdout
                err = p.stderr
            except Exception as e:
                rc = 1
                out = ""
                err = str(e)
            self.root.after(0, on_done, rc, out, err)
        threading.Thread(target=target, daemon=True).start()

    def enable_access_admin(self):
        """Dry-run the installer and, after confirmation, run it via pkexec (or show fallback)."""
        # determine paths: prefer the top-level wrapper if present, otherwise use the extras installer
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
        extras_installer = os.path.abspath(os.path.join(repo_root, 'extras', 'scripts', 'install_simtemp_access.py'))
        wrapper = os.path.abspath(os.path.join(repo_root, 'scripts', 'setup_access.py'))

        installer = extras_installer
        if not os.path.exists(installer):
            messagebox.showerror("simtemp GUI", f"Installer not found: {installer}")
            return

        # dry-run uses the extras installer (it supports --dry-run)
        try:
            p = subprocess.run([sys.executable, extras_installer, '--dry-run'], capture_output=True, text=True)
            dryout = p.stdout + ("\n" + p.stderr if p.stderr else "")
        except Exception as e:
            dryout = f"Failed to run dry-run: {e}"

        ok = self._show_text_modal("Enable Access — dry run (output)", dryout, confirm_label="Proceed and authenticate")
        if not ok:
            return

        # If the evaluator-friendly wrapper exists in the repo root, run it (it handles pkexec/sudo itself).
        if os.path.exists(wrapper):
            cmd = [sys.executable, wrapper]
            self.status_var.set("Requesting admin privileges via wrapper...")
            def done(rc, out, err):
                if rc == 0:
                    messagebox.showinfo('simtemp GUI', 'Access enabled successfully')
                    self.refresh_sysfs()
                    self.status_var.set('Access enabled')
                else:
                    messagebox.showerror('simtemp GUI', f'Enable failed (rc={rc})\n{out}\n{err}')
                    self.status_var.set('Enable failed')
            self._run_subprocess_threaded(cmd, done)
            return

        # fallback to trying pkexec directly against the extras installer
        pkexec_path = shutil.which('pkexec')
        if pkexec_path:
            cmd = ['pkexec', sys.executable, installer]
            self.status_var.set("Requesting admin privileges...")
            def done(rc, out, err):
                if rc == 0:
                    messagebox.showinfo('simtemp GUI', 'Access enabled successfully')
                    self.refresh_sysfs()
                    self.status_var.set('Access enabled')
                else:
                    messagebox.showerror('simtemp GUI', f'Enable failed (rc={rc})\n{out}\n{err}')
                    self.status_var.set('Enable failed')
            self._run_subprocess_threaded(cmd, done)
        else:
            # fallback: show exact command for user to run
            cmds = f"sudo {sys.executable} {installer}\n"
            messagebox.showinfo('simtemp GUI', f"pkexec not found — run the following as root in a terminal:\n\n{cmds}")

    def disable_access_admin(self):
        """Dry-run the uninstaller and, after confirmation, run it via pkexec (or show fallback)."""
        # determine paths: prefer the top-level wrapper if present, otherwise use the extras uninstaller
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
        extras_uninstaller = os.path.abspath(os.path.join(repo_root, 'extras', 'scripts', 'uninstall_simtemp_access.py'))
        wrapper = os.path.abspath(os.path.join(repo_root, 'scripts', 'teardown_access.py'))

        uninstaller = extras_uninstaller
        if not os.path.exists(uninstaller):
            messagebox.showerror("simtemp GUI", f"Uninstaller not found: {uninstaller}")
            return

        # dry-run uses the extras uninstaller (it supports --dry-run)
        try:
            p = subprocess.run([sys.executable, extras_uninstaller, '--dry-run'], capture_output=True, text=True)
            dryout = p.stdout + ("\n" + p.stderr if p.stderr else "")
        except Exception as e:
            dryout = f"Failed to run dry-run: {e}"

        ok = self._show_text_modal("Disable Access — dry run (output)", dryout, confirm_label="Proceed and authenticate")
        if not ok:
            return

        # If the evaluator-friendly wrapper exists, run it (it handles pkexec/sudo itself).
        if os.path.exists(wrapper):
            cmd = [sys.executable, wrapper]
            self.status_var.set("Requesting admin privileges via wrapper...")
            def done(rc, out, err):
                if rc == 0:
                    messagebox.showinfo('simtemp GUI', 'Access disabled and state restored')
                    self.refresh_sysfs()
                    self.status_var.set('Access disabled')
                else:
                    messagebox.showerror('simtemp GUI', f'Disable failed (rc={rc})\n{out}\n{err}')
                    self.status_var.set('Disable failed')
            self._run_subprocess_threaded(cmd, done)
            return

        # fallback: try pkexec directly against the extras uninstaller
        pkexec_path = shutil.which('pkexec')
        if pkexec_path:
            cmd = ['pkexec', sys.executable, uninstaller]
            self.status_var.set("Requesting admin privileges...")
            def done(rc, out, err):
                if rc == 0:
                    messagebox.showinfo('simtemp GUI', 'Access disabled and state restored')
                    self.refresh_sysfs()
                    self.status_var.set('Access disabled')
                else:
                    messagebox.showerror('simtemp GUI', f'Disable failed (rc={rc})\n{out}\n{err}')
                    self.status_var.set('Disable failed')
            self._run_subprocess_threaded(cmd, done)
        else:
            cmds = f"sudo {sys.executable} {uninstaller}\n"
            messagebox.showinfo('simtemp GUI', f"pkexec not found — run the following as root in a terminal:\n\n{cmds}")

    def show_access_commands(self):
        """Show manual quick-test and recommended group/udev commands and allow copying to clipboard."""
        base = SYS_CLASS_PATH if os.path.isdir(SYS_CLASS_PATH) else SYS_PLATFORM_PATH
        sampling = os.path.join(base, "sampling_ms")
        threshold = os.path.join(base, "threshold_mC")
        mode = os.path.join(base, "mode")
        dev = DEV_PATH

        text = f"""Quick insecure test (temporary, NOT recommended for production):

    sudo chmod 0666 {sampling}
    sudo chmod 0666 {threshold}
    sudo chmod 0666 {mode}
    sudo chmod 0666 {dev}

Revert the quick test (example):

    sudo chmod 0644 {sampling}
    sudo chmod 0644 {threshold}
    sudo chmod 0644 {mode}
    sudo chmod 0644 {dev}

Safer, recommended (group-based) approach:

    # create a dedicated group (one-time)
    sudo groupadd -f simtemp
    sudo usermod -a -G simtemp $USER  # re-login or run `newgrp simtemp`

    # set group ownership and restrictive group permissions
    sudo chgrp simtemp {dev}
    sudo chmod 0660 {dev}
    sudo chgrp simtemp {sampling}
    sudo chmod 0660 {sampling}
    sudo chgrp simtemp {threshold}
    sudo chmod 0660 {threshold}
    sudo chgrp simtemp {mode}
    sudo chmod 0660 {mode}

Persistence:
    - For the /dev node, add a udev rule (e.g. /etc/udev/rules.d/99-simtemp.rules) to set group/mode when the device is created.
    - For sysfs attrs, use a systemd tmpfiles.d entry or a boot script to set group/mode after driver bind/resume.

Automated safe installer (recommended locally):
    Use the evaluator-friendly wrappers at the repo root (preferred):

        ./setup_access.py --dry-run (not supported by wrapper; wrapper will run installer)
        ./setup_access.py        # apply (will prompt or print sudo fallback)
        ./teardown_access.py     # restore recorded state (will prompt or print sudo fallback)

    Or use the extras scripts directly (they support --dry-run):

        extras/scripts/install_simtemp_access.py --dry-run   # preview changes
        sudo extras/scripts/install_simtemp_access.py       # apply (records prior state)
        sudo extras/scripts/uninstall_simtemp_access.py     # restore recorded state

The GUI's admin buttons prefer the repo-root wrappers when present and otherwise fall back to pkexec against the extras scripts. Marker file used by installer: /var/lib/simtemp/access_state.json

"""

        # create modal dialog with copy button
        dlg = tk.Toplevel(self.root)
        dlg.title("Access setup commands")
        dlg.transient(self.root)
        dlg.grab_set()
        txt = tk.Text(dlg, wrap="none", height=22, width=88)
        txt.insert("1.0", text)
        txt.configure(state="disabled")
        txt.grid(row=0, column=0, columnspan=2, sticky="nsew")
        scr_y = ttk.Scrollbar(dlg, orient="vertical", command=txt.yview)
        scr_y.grid(row=0, column=2, sticky="ns")
        txt.configure(yscrollcommand=scr_y.set)

        def do_copy():
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(text)
                messagebox.showinfo('simtemp GUI', 'Commands copied to clipboard')
            except Exception as e:
                messagebox.showerror('simtemp GUI', f'Copy failed: {e}')

        btn_frame = ttk.Frame(dlg)
        btn_frame.grid(row=1, column=0, columnspan=3, sticky="e", pady=6)
        ttk.Button(btn_frame, text="Copy to clipboard", command=do_copy).pack(side="right", padx=(4,0))
        ttk.Button(btn_frame, text="Close", command=dlg.destroy).pack(side="right")
        self.root.wait_window(dlg)

    def write_sysfs(self, name, value):
        base = SYS_CLASS_PATH if os.path.isdir(SYS_CLASS_PATH) else SYS_PLATFORM_PATH
        path = os.path.join(base, name)
        try:
            with open(path, "w") as f:
                f.write(str(value) + "\n")
            self.status_var.set(f"Wrote {name}={value}")
        except Exception as e:
            self.status_var.set(f"Failed to write {path}: {e}")
            messagebox.showerror("simtemp GUI", f"Failed to write {path}: {e}")

    def set_sampling(self):
        v = self.sampling_var.get().strip()
        if not v.isdigit():
            messagebox.showerror("simtemp GUI", "sampling_ms must be an integer")
            return
        self.write_sysfs("sampling_ms", v)

    def set_threshold(self):
        v = self.threshold_var.get().strip()
        try:
            int(v)
        except Exception:
            messagebox.showerror("simtemp GUI", "threshold_mC must be an integer")
            return
        self.write_sysfs("threshold_mC", v)

    def set_mode(self):
        v = self.mode_var.get().strip()
        if v not in MODES:
            messagebox.showerror("simtemp GUI", f"mode must be one of {MODES}")
            return
        self.write_sysfs("mode", v)

    def _on_close(self):
        self.stop_reader()
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description="simtemp GUI")
    parser.add_argument("--simulate", action="store_true", help="Run GUI with an internal simulator instead of /dev/simtemp")
    parser.add_argument("--sim-sampling", type=int, default=1000, help="Simulator sampling_ms (ms)")
    parser.add_argument("--sim-threshold", type=int, default=45000, help="Simulator threshold_mC")
    parser.add_argument("--sim-mode", type=int, default=0, choices=[0,1,2], help="Simulator mode: 0=normal,1=ramp,2=noisy")
    args = parser.parse_args()

    root = tk.Tk()
    app = SimTempGUI(root)
    if args.simulate:
        # replace reader with simulator
        app.stop_reader()
        app.stop_event.clear()
        sim = SimTempSimulator(app._on_sample, app.stop_event, sampling_ms=args.sim_sampling, threshold_mC=args.sim_threshold, mode=args.sim_mode)
        app.reader = sim
        sim.start()
        app.status_var.set("Running in simulate mode")

    root.mainloop()


if __name__ == "__main__":
    main()
