# -*- coding: utf-8 -*-
import asyncio
import websockets
import json
import sys
import os
import argparse
from datetime import datetime, timedelta, timezone

EEST_TZ = timezone(timedelta(hours=3))

def parse_args():
    parser = argparse.ArgumentParser(description="EV Car Simulator Client")
    parser.add_argument("config_file", help="Path to JSON configuration file")
    parser.add_argument("--duration", type=float, default=0.0, help="Auto-unplug duration in seconds (0 = run until stopped/full)")
    return parser.parse_args()

def load_car_specs(config_path):
    specs = {
        "charger_ws_url": "ws://localhost:9001",
        "action": "connect",
        "soc_at_start": 20.0,
        "soc_min": 20.0,
        "soc_max": 90.0,
        "target_value": 30.0,
        "target_time": ""
    }

    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                specs.update(json.load(f))
            print(f"[CAR] Loaded config from {config_path}", flush=True)
        except Exception as e:
            print(f"[ERROR] Failed to parse JSON config: {e}", flush=True)

    return specs

async def listen_messages(ws):
    """Listens for telemetry updates from the charger."""
    try:
        async for message in ws:
            data = json.loads(message)
            charging = data.get("charging", False)
            power_kw = data.get("power_kw", 0.0)
            soc = data.get("soc", 0.0)
            
            if charging:
                print(f"[CAR UPDATE] Status: Charging | Drawing: {power_kw:.2f} kW | Current SoC: {soc:.2f}%", flush=True)
            else:
                if soc >= 100.0:
                    print(f"[CAR UPDATE] Status: Battery Full (100%). Disconnecting...", flush=True)
                    break
                else:
                    print(f"[CAR UPDATE] Status: Connected/Idle | Current SoC: {soc:.2f}%", flush=True)
    except websockets.exceptions.ConnectionClosed:
        pass

async def auto_unplug_timer(duration):
    """Sleeps for the set duration and triggers disconnection."""
    print(f"[TIMER] Auto-unplug timer set for {duration:.1f} seconds.", flush=True)
    await asyncio.sleep(duration)
    print(f"\n[TIMER] ⏱ Duration elapsed ({duration}s)! Executing automatic unplug...", flush=True)

async def run_car_simulation(config_path, duration):
    car_specs = load_car_specs(config_path)
    charger_url = car_specs.pop("charger_ws_url", "ws://localhost:9001")

    # Retry configuration to handle the initial charger startup race condition
    max_retries = 10
    retry_delay = 0.5
    ws = None

    print(f"[CAR] Manually plugging into EV Charger at {charger_url}...", flush=True)
    
    for attempt in range(max_retries):
        try:
            ws = await websockets.connect(charger_url)
            break
        except (ConnectionRefusedError, OSError):
            if attempt < max_retries - 1:
                await asyncio.sleep(retry_delay)
            else:
                print(f"[ERROR] Could not connect to charger at {charger_url} after {max_retries} attempts. Ensure the charger script is running.", flush=True)
                print("[CAR] Session ended. Car unplugged.", flush=True)
                return
        except Exception as e:
            print(f"[ERROR] Session error: {e}", flush=True)
            print("[CAR] Session ended. Car unplugged.", flush=True)
            return

    try:
        print(f"[CAR] Plugged in. Transmitting battery profile and constraints...", flush=True)
        await ws.send(json.dumps(car_specs))
        
        listen_task = asyncio.create_task(listen_messages(ws))
        
        if duration > 0:
            timer_task = asyncio.create_task(auto_unplug_timer(duration))
            
            # Wait until either the timer elapses OR the connection/listener finishes
            done, pending = await asyncio.wait(
                [listen_task, timer_task],
                return_when=asyncio.FIRST_COMPLETED
            )
            
            for task in pending:
                task.cancel()
        else:
            await listen_task

        # Transmit final disconnect frame before shutting down
        print("[CAR] Sending disconnect frame to charger...", flush=True)
        try:
            await ws.send(json.dumps({"action": "disconnect"}))
            await asyncio.sleep(0.2)
        except websockets.exceptions.ConnectionClosed:
            pass

    except Exception as e:
        print(f"[ERROR] Session error: {e}", flush=True)
    finally:
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        print("[CAR] Session ended. Car unplugged.", flush=True)

if __name__ == "__main__":
    args = parse_args()
    try:
        asyncio.run(run_car_simulation(args.config_file, args.duration))
    except KeyboardInterrupt:
        sys.exit(0)
