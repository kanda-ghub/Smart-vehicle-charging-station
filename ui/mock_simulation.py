import json
import os
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone, time as dt_time
import tkinter as tk
from tkinter import ttk, messagebox

EEST_TZ = timezone(timedelta(hours=3))


class StatusLED(tk.Canvas):
    """Custom Tkinter Widget representing a colored LED status light."""

    COLORS = {
        "OFF": ("#3a3a3a", "#222222"),      # Dark Gray
        "IDLE": ("#ffcc00", "#997a00"),     # Yellow
        "WAITING": ("#ff9900", "#995c00"),  # Orange (Scheduled arrival)
        "PLUGGED": ("#00ff66", "#008833"),  # Green
    }

    def __init__(self, parent, size=22, **kwargs):
        super().__init__(parent, width=size, height=size, highlightthickness=0, **kwargs)
        self.size = size
        self.state = "OFF"
        self.draw()

    def draw(self):
        self.delete("all")
        fill, outline = self.COLORS.get(self.state, self.COLORS["OFF"])
        padding = 3
        self.create_oval(
            padding,
            padding,
            self.size - padding,
            self.size - padding,
            fill=fill,
            outline=outline,
            width=2,
        )

    def set_status(self, state):
        if state in self.COLORS and self.state != state:
            self.state = state
            self.draw()


class VerticalConsoleDualEVUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Multi-EV Station Controller (Vertical Telemetry)")
        self.root.geometry("1400x1020")
        self.root.minsize(1100, 850)

        self.log_queue = queue.Queue()
        self.processes = {}  # Keys: 'chg1', 'chg2', 'car1', 'car2'
        self.scheduled_timers = {}  # Holds timer objects for pending scheduled plug-ins
        self.log_files = {}  # Holds open file handles for logging process output

        # Intercept window close for graceful teardown
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        self.init_ui()
        self.root.after(100, self.process_queue_logs)

    def init_ui(self):
        # Top Global Control Bar & Common Building Office Hours
        top_bar = ttk.Frame(self.root, padding="10")
        top_bar.pack(fill=tk.X)

        btn_frame = ttk.Frame(top_bar)
        btn_frame.pack(side=tk.LEFT)

        # Primary Scenario Button
        ttk.Button(
            btn_frame,
            text="🚀 Start Scenario",
            command=self.start_scenario,
        ).pack(side=tk.LEFT, padx=5)

        ttk.Button(
            btn_frame, text="⚡ Start Both Chargers", command=self.start_both_chargers
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            btn_frame, text="🔌 Schedule/Plug Both Cars", command=self.plug_both_cars
        ).pack(side=tk.LEFT, padx=5)

        ttk.Button(top_bar, text="■ Stop / Unplug Everything", command=self.stop_all).pack(
            side=tk.RIGHT, padx=5
        )

        hours_lf = ttk.LabelFrame(
            top_bar, text=" 🏢 Building Operational Hours (Global) ", padding="5"
        )
        hours_lf.pack(side=tk.LEFT, padx=20)

        ttk.Label(hours_lf, text="Facility Start (Hour 0-23):").pack(side=tk.LEFT, padx=(5, 2))
        self.field_global_office_start = ttk.Entry(hours_lf, width=5)
        self.field_global_office_start.insert(0, "0")
        self.field_global_office_start.pack(side=tk.LEFT, padx=(0, 10))

        ttk.Label(hours_lf, text="Facility End (Hour 0-23):").pack(side=tk.LEFT, padx=(5, 2))
        self.field_global_office_end = ttk.Entry(hours_lf, width=5)
        self.field_global_office_end.insert(0, "23")
        self.field_global_office_end.pack(side=tk.LEFT, padx=(0, 5))

        # Main Panel Frame holding Spot 1 and Spot 2
        main_panel = ttk.Frame(self.root, padding="10")
        main_panel.pack(fill=tk.BOTH, expand=True)

        col1 = ttk.Frame(main_panel)
        col1.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))

        col2 = ttk.Frame(main_panel)
        col2.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(5, 0))

        # Build Spot 1 Frame
        self.build_spot_panel(
            col1,
            spot_id=1,
            prefix="car1",
            default_ws="ws://localhost:9001",
            default_csms="ws://127.0.0.1:9000/ocpp/CP001",
            default_capacity="60.0",
            default_soc_start="32.0",
            default_arr_hour="20",
            default_dep_hour="7",
        )

        # Build Spot 2 Frame
        self.build_spot_panel(
            col2,
            spot_id=2,
            prefix="car2",
            default_ws="ws://localhost:9002",
            default_csms="ws://127.0.0.1:9000/ocpp/CP002",
            default_capacity="75.0",
            default_soc_start="42",
            default_arr_hour="11",
            default_dep_hour="17",
        )

    def build_spot_panel(
        self,
        parent,
        spot_id,
        prefix,
        default_ws,
        default_csms,
        default_capacity,
        default_soc_start,
        default_arr_hour,
        default_dep_hour,
    ):
        spot_frame = ttk.LabelFrame(
            parent,
            text=f" EV Spot {spot_id} (Port {default_ws.split(':')[-1]}) ",
            padding="10",
        )
        spot_frame.pack(fill=tk.BOTH, expand=True)

        # Status Header Bar
        act_frame = ttk.Frame(spot_frame)
        act_frame.pack(fill=tk.X, pady=(0, 10))

        led_frame = ttk.Frame(act_frame)
        led_frame.pack(side=tk.LEFT, padx=(0, 10))

        led = StatusLED(led_frame, size=22)
        led.pack(side=tk.LEFT, padx=(0, 5))
        setattr(self, f"led_{spot_id}", led)

        lbl_status = ttk.Label(led_frame, text="OFFLINE", font=("Segoe UI", 9, "bold"))
        lbl_status.pack(side=tk.LEFT)
        setattr(self, f"lbl_status_{spot_id}", lbl_status)

        # Action Buttons
        btn_chg = ttk.Button(
            act_frame,
            text="1. Start Charger",
            command=lambda: self.start_charger(spot_id, prefix),
        )
        btn_chg.pack(side=tk.LEFT, padx=3, fill=tk.X, expand=True)

        btn_plug = ttk.Button(
            act_frame, text="2. 🔌 Auto Plug-In", command=lambda: self.schedule_or_plug_car(spot_id, prefix)
        )
        btn_plug.pack(side=tk.LEFT, padx=3, fill=tk.X, expand=True)

        btn_unplug = ttk.Button(
            act_frame, text="3. 🛑 Unplug", command=lambda: self.unplug_car(spot_id)
        )
        btn_unplug.pack(side=tk.LEFT, padx=3, fill=tk.X, expand=True)

        # Configuration Container
        cfg_container = ttk.Frame(spot_frame)
        cfg_container.pack(fill=tk.X, pady=(0, 10))

        # Network Settings
        chg_lf = ttk.LabelFrame(cfg_container, text=" Network Configuration ", padding="8")
        chg_lf.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))
        self.add_field(chg_lf, "CSMS Endpoint:", f"{prefix}_csms_url", default_csms, row=0)
        self.add_field(
            chg_lf, "Local WS Port:", f"{prefix}_charger_port", default_ws.split(":")[-1], row=1
        )
        self.add_field(chg_lf, "Car WS URL:", f"{prefix}_ws_url", default_ws, row=2)
        chg_lf.columnconfigure(1, weight=1)

        # Vehicle Parameters & Schedule Form
        car_lf = ttk.LabelFrame(
            cfg_container, text=" Vehicle Parameters & Schedule ", padding="8"
        )
        car_lf.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(5, 0))
        self.add_field(
            car_lf, "battery_capacity (kWh):", f"{prefix}_battery_capacity", default_capacity, row=0
        )
        self.add_field(
            car_lf, "soc_at_start (%):", f"{prefix}_soc_at_start", default_soc_start, row=1
        )
        self.add_field(car_lf, "soc_min (%):", f"{prefix}_soc_min", "20.0", row=2)
        self.add_field(car_lf, "soc_max (%):", f"{prefix}_soc_max", "90.0", row=3)
        self.add_field(car_lf, "target_value (%):", f"{prefix}_target_value", "90.0", row=4)
        self.add_field(
            car_lf, "Arrival Hour (0-23):", f"{prefix}_arrival_hour", default_arr_hour, row=5
        )
        self.add_field(
            car_lf, "Departure Hour (0-23):", f"{prefix}_departure_hour", default_dep_hour, row=6
        )
        car_lf.columnconfigure(1, weight=1)

        # Vertical Stacking Consoles
        consoles_frame = ttk.Frame(spot_frame)
        consoles_frame.pack(fill=tk.BOTH, expand=True)

        # Charger Log
        chg_console_lf = ttk.LabelFrame(consoles_frame, text=" ⚡ Charger Log ", padding="5")
        chg_console_lf.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(0, 4))

        chg_console = tk.Text(
            chg_console_lf,
            wrap=tk.WORD,
            bg="#181818",
            fg="#00e676",
            insertbackground="white",
            font=("Consolas", 8),
            height=8,
        )
        chg_sb = ttk.Scrollbar(chg_console_lf, command=chg_console.yview)
        chg_console.configure(yscrollcommand=chg_sb.set)
        chg_sb.pack(side=tk.RIGHT, fill=tk.Y)
        chg_console.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        setattr(self, f"console_chg_{spot_id}", chg_console)

        # Car Log
        car_console_lf = ttk.LabelFrame(consoles_frame, text=" 🚗 Car Log ", padding="5")
        car_console_lf.pack(side=tk.BOTTOM, fill=tk.BOTH, expand=True, pady=(4, 0))

        car_console = tk.Text(
            car_console_lf,
            wrap=tk.WORD,
            bg="#181818",
            fg="#00b0ff",
            insertbackground="white",
            font=("Consolas", 8),
            height=8,
        )
        car_sb = ttk.Scrollbar(car_console_lf, command=car_console.yview)
        car_console.configure(yscrollcommand=car_sb.set)
        car_sb.pack(side=tk.RIGHT, fill=tk.Y)
        car_console.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        setattr(self, f"console_car_{spot_id}", car_console)

    def add_field(self, parent, label_text, var_name, default_val, row):
        ttk.Label(parent, text=label_text).grid(row=row, column=0, sticky=tk.W, pady=2)
        entry = ttk.Entry(parent)
        entry.insert(0, default_val)
        entry.grid(row=row, column=1, sticky=tk.EW, padx=(5, 0), pady=2)
        setattr(self, f"field_{var_name}", entry)

    def update_spot_led(self, spot_id):
        chg_active = (
            f"chg{spot_id}" in self.processes and self.processes[f"chg{spot_id}"].poll() is None
        )
        car_active = (
            f"car{spot_id}" in self.processes and self.processes[f"car{spot_id}"].poll() is None
        )
        is_scheduled = spot_id in self.scheduled_timers

        led = getattr(self, f"led_{spot_id}")
        lbl = getattr(self, f"lbl_status_{spot_id}")

        if car_active:
            led.set_status("PLUGGED")
            lbl.config(text="PLUGGED IN", foreground="#00aa44")
        elif is_scheduled:
            led.set_status("WAITING")
            lbl.config(text="WAITING ARRIVAL", foreground="#ff9900")
        elif chg_active:
            led.set_status("IDLE")
            lbl.config(text="CHARGER READY", foreground="#cc9900")
        else:
            led.set_status("OFF")
            lbl.config(text="OFFLINE", foreground="#777777")

    def parse_hour(self, val_str, default_hour):
        try:
            h = int(val_str.strip())
            return max(0, min(23, h))
        except Exception:
            return default_hour

    def parse_float(self, val_str, default_val):
        try:
            val = float(val_str.strip())
            return max(0.0, val)
        except Exception:
            return default_val

    def extract_car_params(self, spot_id, prefix):
        now = datetime.now(EEST_TZ)

        arr_hour = self.parse_hour(
            getattr(self, f"field_{prefix}_arrival_hour").get(), 8
        )
        dep_hour = self.parse_hour(
            getattr(self, f"field_{prefix}_departure_hour").get(), 17
        )

        # Standard arrival date calculation for today
        arr_dt = datetime.combine(
            now.date(), dt_time(hour=arr_hour, minute=0), tzinfo=EEST_TZ
        )

        # Handle Overnight logic: if departure hour <= arrival hour, departure is next day
        if dep_hour <= arr_hour:
            dep_dt = datetime.combine(
                now.date() + timedelta(days=1), dt_time(hour=dep_hour, minute=0), tzinfo=EEST_TZ
            )
        else:
            dep_dt = datetime.combine(
                now.date(), dt_time(hour=dep_hour, minute=0), tzinfo=EEST_TZ
            )

        # Automatically roll forward day-by-day until the departure window is in the future
        while dep_dt <= now:
            arr_dt += timedelta(days=1)
            dep_dt += timedelta(days=1)

        default_cap = 60.0 if spot_id == 1 else 75.0
        battery_cap = self.parse_float(
            getattr(self, f"field_{prefix}_battery_capacity").get(), default_cap
        )

        soc_start = min(
            100.0,
            self.parse_float(getattr(self, f"field_{prefix}_soc_at_start").get(), 42.0),
        )
        soc_min = min(
            100.0, self.parse_float(getattr(self, f"field_{prefix}_soc_min").get(), 20.0)
        )
        soc_max = min(
            100.0, self.parse_float(getattr(self, f"field_{prefix}_soc_max").get(), 100.0)
        )
        target_val = min(
            100.0,
            self.parse_float(getattr(self, f"field_{prefix}_target_value").get(), 100.0),
        )

        params = {
            "charger_ws_url": getattr(self, f"field_{prefix}_ws_url").get(),
            "action": "connect",
            "battery_capacity": battery_cap,
            "soc_at_start": soc_start,
            "soc_min": soc_min,
            "soc_max": soc_max,
            "target_value": target_val,
            "target_time": dep_dt.isoformat(),
        }
        return params, arr_dt, dep_dt

    def start_charger(self, spot_id, prefix):
        proc_key = f"chg{spot_id}"
        if proc_key in self.processes and self.processes[proc_key].poll() is None:
            self.log_message(spot_id, "chg", "[SYS] Charger is already running.")
            return

        port = getattr(self, f"field_{prefix}_charger_port").get()
        csms = getattr(self, f"field_{prefix}_csms_url").get()
        cmd = [sys.executable, "-u", "sim3.py", port, csms]

        self.spawn_monitored_process(cmd, "CHARGER", proc_key, spot_id, target_type="chg")

    def schedule_or_plug_car(self, spot_id, prefix):
        car_key = f"car{spot_id}"
        if car_key in self.processes and self.processes[car_key].poll() is None:
            self.log_message(spot_id, "car", "[SYS] Car is already plugged in!")
            return

        if spot_id in self.scheduled_timers:
            self.log_message(spot_id, "car", "[SYS] Car plug-in is already scheduled!")
            return

        params, arr_dt, dep_dt = self.extract_car_params(spot_id, prefix)
        if params is None:
            return

        now = datetime.now(EEST_TZ)
        delay_sec = (arr_dt - now).total_seconds()

        if delay_sec > 0:
            self.log_message(
                spot_id,
                "car",
                f"[SYS] ⏰ SCHEDULED: Plug-in at {arr_dt.strftime('%d-%b %H:%M:%S')} | Unplug: {dep_dt.strftime('%d-%b %H:%M:%S')} (in {delay_sec:.1f}s)",
            )
            # Schedule execution when arrival time is hit
            t = threading.Timer(
                delay_sec, lambda: self.execute_plug_in(spot_id, prefix, params, arr_dt, dep_dt)
            )
            self.scheduled_timers[spot_id] = t
            t.start()
            self.update_spot_led(spot_id)
        else:
            # Arrival time has already arrived or passed: plug in immediately
            self.execute_plug_in(spot_id, prefix, params, arr_dt, dep_dt)

    def execute_plug_in(self, spot_id, prefix, params, arr_dt, dep_dt):
        # Remove timer reference
        if spot_id in self.scheduled_timers:
            del self.scheduled_timers[spot_id]

        cfg_path = f"active_{prefix}_config.json"
        try:
            with open(cfg_path, "w") as f:
                json.dump(params, f, indent=4)
        except Exception as e:
            self.log_message(spot_id, "car", f"[SYS ERROR] Failed saving car config: {e}")
            return

        duration_sec = max(5.0, (dep_dt - arr_dt).total_seconds())

        cmd = [
            sys.executable,
            "-u",
            "car_sim.py",
            cfg_path,
            "--duration",
            f"{duration_sec:.1f}",
        ]

        overnight_tag = " [OVERNIGHT]" if dep_dt.day != arr_dt.day else ""
        self.log_message(
            spot_id,
            "car",
            f"[SYS] 🔌 ARRIVAL TIME REACHED! Auto-plugging vehicle{overnight_tag} | Arrival: {arr_dt.strftime('%Y-%m-%d %H:%M')} | Departure: {dep_dt.strftime('%Y-%m-%d %H:%M')} ({duration_sec / 3600:.1f} hrs total)",
        )
        self.spawn_monitored_process(cmd, "CAR", f"car{spot_id}", spot_id, target_type="car")

    def unplug_car(self, spot_id):
        # Cancel any scheduled future plug-in timer
        if spot_id in self.scheduled_timers:
            self.scheduled_timers[spot_id].cancel()
            del self.scheduled_timers[spot_id]
            self.log_message(spot_id, "car", "[SYS] 🛑 Canceled scheduled plug-in timer.")

        car_key = f"car{spot_id}"
        if car_key in self.processes and self.processes[car_key].poll() is None:
            self.log_message(spot_id, "car", "[SYS] 🛑 Manually unplugging vehicle...")
            self.processes[car_key].terminate()
        else:
            self.log_message(spot_id, "car", "[SYS] Vehicle is not currently plugged in.")
        self.update_spot_led(spot_id)

    def start_both_chargers(self):
        self.start_charger(1, "car1")
        self.start_charger(2, "car2")

    def plug_both_cars(self):
        self.schedule_or_plug_car(1, "car1")
        self.schedule_or_plug_car(2, "car2")

    def start_scenario(self):
        """Starts both chargers immediately and schedules both cars according to arrival hours."""
        self.log_message(1, "chg", "======== 🚀 STARTING FULL SCENARIO ========")
        self.log_message(2, "chg", "======== 🚀 STARTING FULL SCENARIO ========")
        self.start_both_chargers()
        self.plug_both_cars()

    def stop_all(self):
        for spot_id in [1, 2]:
            if spot_id in self.scheduled_timers:
                self.scheduled_timers[spot_id].cancel()
            self.log_message(spot_id, "chg", "[SYS] Terminating charger session...")
            self.log_message(spot_id, "car", "[SYS] Terminating vehicle session...")

        self.scheduled_timers.clear()

        for key, proc in list(self.processes.items()):
            if proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
        self.processes.clear()
        
        # Close any active log files safely
        for key, f_obj in list(self.log_files.items()):
            try:
                f_obj.close()
            except Exception:
                pass
        self.log_files.clear()

        self.update_spot_led(1)
        self.update_spot_led(2)

    def spawn_monitored_process(self, cmd, tag, proc_key, spot_id, target_type):
        try:
            # Create a log filename containing the current date (YYYY-MM-DD)
            current_date_str = datetime.now(EEST_TZ).strftime("%Y-%m-%d")
            log_filename = f"{target_type}{spot_id}_{current_date_str}.log"
            
            # Open file handle for appending process output
            log_file = open(log_filename, "a", encoding="utf-8")
            self.log_files[proc_key] = log_file

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            self.processes[proc_key] = proc
            threading.Thread(
                target=self.stream_process_output,
                args=(proc, tag, spot_id, target_type, log_file),
                daemon=True,
            ).start()
            self.log_message(spot_id, target_type, f"[SYS] Started process [{tag}] (Logging to {log_filename})")
            self.update_spot_led(spot_id)
        except Exception as e:
            self.log_message(
                spot_id, target_type, f"[SYS ERROR] Failed starting [{tag}]: {e}"
            )

    def stream_process_output(self, proc, tag, spot_id, target_type, log_file):
        for line in proc.stdout:
            # Write to disk log file
            try:
                timestamped_line = f"[{datetime.now(EEST_TZ).strftime('%Y-%m-%d %H:%M:%S')}] {line}"
                log_file.write(timestamped_line)
                log_file.flush()
            except Exception:
                pass
            # Push to UI queue
            self.log_queue.put((spot_id, target_type, f"[{tag}] {line}"))
            
        proc.wait()
        
        exit_msg = f"[SYS] Process [{tag}] exited.\n"
        try:
            log_file.write(f"[{datetime.now(EEST_TZ).strftime('%Y-%m-%d %H:%M:%S')}] {exit_msg}")
            log_file.flush()
            log_file.close()
        except Exception:
            pass

        self.log_queue.put((spot_id, target_type, exit_msg))

        # Automatically reschedule for the next day when a car simulation finishes
        if target_type == "car":
            prefix = "car1" if spot_id == 1 else "car2"
            self.root.after(1000, lambda: self.auto_reschedule_next_day(spot_id, prefix))

    def auto_reschedule_next_day(self, spot_id, prefix):
        car_key = f"car{spot_id}"
        if car_key not in self.processes or self.processes[car_key].poll() is not None:
            if spot_id not in self.scheduled_timers:
                self.log_message(spot_id, "car", "[SYS] 🔄 Session completed. Automatically scheduling next cycle for tomorrow...")
                self.schedule_or_plug_car(spot_id, prefix)

    def process_queue_logs(self):
        while True:
            try:
                spot_id, target_type, line = self.log_queue.get_nowait()
                console = getattr(self, f"console_{target_type}_{spot_id}", None)
                if console:
                    console.insert(tk.END, line)
                    console.see(tk.END)
                self.log_queue.task_done()
                self.update_spot_led(spot_id)
            except queue.Empty:
                break
        self.root.after(100, self.process_queue_logs)

    def log_message(self, spot_id, target_type, message):
        console = getattr(self, f"console_{target_type}_{spot_id}", None)
        if console:
            console.insert(tk.END, f"{message}\n")
            console.see(tk.END)

    def on_closing(self):
        self.stop_all()
        time.sleep(0.2)
        self.root.destroy()


if __name__ == "__main__":
    try:
        from ctypes import windll

        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    root = tk.Tk()
    style = ttk.Style()
    if "clam" in style.theme_names():
        style.theme_use("clam")

    app = VerticalConsoleDualEVUI(root)
    root.mainloop()
