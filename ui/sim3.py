# -*- coding: utf-8 -*-
"""
sim3.py: EV Charger Simulator (OCPP 1.6 Client + Car Sim WebSocket Server)
Supports CLI arguments for CAR_SERVER_PORT and CSMS_URL.
"""

import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
import websockets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
_LOGGER = logging.getLogger("EV_Sim")

# Defaults
CSMS_URL = "ws://127.0.0.1:9000/ocpp/CP001"
CAR_SERVER_PORT = 9001
TICK_INTERVAL_SEC = 2.0

# Check command line args
if len(sys.argv) > 1:
    CAR_SERVER_PORT = int(sys.argv[1])
if len(sys.argv) > 2:
    CSMS_URL = sys.argv[2]

STATE = {
    "charging": False,
    "power_kw": 0.0,
    "soc": None,  # Percentage (%)
    "target_soc": 100.0,  # Percentage (%)
    "battery_capacity_kwh": 60.0,  # Energy capacity (kWh)
    "connector_id": 1,
    "transaction_id": None,
    "csms_ws": None,
    "car_ws": None,
    "soc_min": 20.0,  # Percentage (%)
    "soc_max": 100.0,  # Percentage (%)
    "target_time": "",  # Stored as absolute UTC ISO timestamp
}

_VEHICLE_CONNECTED_EVENT = asyncio.Event()


def iso_timestamp():
    return datetime.now(timezone.utc).isoformat()


async def notify_car_of_control():
    if STATE["car_ws"] and STATE["soc"] is not None:
        payload = {
            "charging": STATE["charging"],
            "power_kw": STATE["power_kw"],
            "soc": STATE["soc"],  # %
        }
        try:
            await STATE["car_ws"].send(json.dumps(payload))
        except (websockets.exceptions.ConnectionClosed, AttributeError):
            STATE["car_ws"] = None


async def reset_and_notify_disconnect():
    """Resets local state and sends StatusNotification(Available) to CSMS."""
    _LOGGER.warning(
        "🚗 Vehicle disconnected. Resetting state & sending StatusNotification -> Available"
    )

    # Send StopTransaction if a transaction was active
    if STATE["csms_ws"] and STATE["transaction_id"] is not None:
        stop_payload = {
            "transactionId": STATE["transaction_id"],
            "timestamp": iso_timestamp(),
            "meterStop": int((STATE["soc"] or 0) * 10),
            "reason": "EVDisconnected",
        }
        await send_call(STATE["csms_ws"], "StopTransaction", stop_payload)

    # Reset internal state
    STATE["charging"] = False
    STATE["power_kw"] = 0.0
    STATE["soc"] = None
    STATE["transaction_id"] = None
    STATE["target_time"] = ""
    _VEHICLE_CONNECTED_EVENT.clear()

    # Notify CSMS that charger connector is back to Available
    if STATE["csms_ws"]:
        await send_status_notification(STATE["csms_ws"], "Available")


async def car_server_handler(ws):
    global STATE
    _LOGGER.info(f"🚗 Car script linked on port {CAR_SERVER_PORT}.")
    STATE["car_ws"] = ws

    try:
        async for message in ws:
            try:
                data = json.loads(message)
                if data.get("action") == "connect":
                    if "soc_at_start" in data:
                        STATE["soc"] = float(data["soc_at_start"])

                    # Accept either battery_kwh or battery_capacity key safely
                    if "battery_kwh" in data:
                        STATE["battery_capacity_kwh"] = float(data["battery_kwh"])
                    elif "battery_capacity" in data:
                        STATE["battery_capacity_kwh"] = float(data["battery_capacity"])

                    if "soc_min" in data:
                        STATE["soc_min"] = float(data["soc_min"])
                    if "soc_max" in data:
                        STATE["soc_max"] = float(data["soc_max"])
                    if "target_value" in data:
                        STATE["target_soc"] = float(data["target_value"])
                    if "target_time" in data:
                        STATE["target_time"] = data["target_time"]

                    _LOGGER.info(
                        f"🚗 Car loaded -> Battery Capacity: {STATE['battery_capacity_kwh']} kWh | "
                        f"SoC: {STATE['soc']}% | Bounds: [{STATE['soc_min']}% - {STATE['soc_max']}%] | "
                        f"Target SoC: {STATE['target_soc']}% | Departure Timestamp (UTC): {STATE['target_time']}"
                    )
                    _VEHICLE_CONNECTED_EVENT.set()
                    await notify_car_of_control()

                elif data.get("action") == "disconnect":
                    await reset_and_notify_disconnect()

                elif "soc" in data:
                    STATE["soc"] = float(data["soc"])

            except Exception as e:
                _LOGGER.error(f"Error parsing car script data: {e}")
    except websockets.exceptions.ConnectionClosed:
        _LOGGER.warning("Car simulation script disconnected unexpectedly.")
    finally:
        STATE["car_ws"] = None
        if _VEHICLE_CONNECTED_EVENT.is_set():
            await reset_and_notify_disconnect()


async def csms_receiver(ws):
    global STATE
    _LOGGER.info("Downstream CSMS tracking task listening...")
    try:
        async for raw_message in ws:
            try:
                msg = json.loads(raw_message)
                if not isinstance(msg, list) or len(msg) < 3:
                    continue

                msg_type, msg_id = msg[0], msg[1]

                if msg_type == 2:  # CALL from CSMS
                    action = msg[2]
                    payload = msg[3] if len(msg) > 3 else {}

                    _LOGGER.info(f"📩 [RECV CSMS CALL] {action} -> {payload}")

                    if action in ["RemoteStartTransaction", "SetChargingProfile"]:
                        profile = payload.get("chargingProfile", payload)
                        schedule = profile.get("chargingSchedule", {})
                        periods = schedule.get("chargingSchedulePeriod", [])

                        allocated_power = float(periods[0].get("limit", 11.0)) if periods else 11.0
                        _LOGGER.info(f"⚡ Setting target power to: {allocated_power} kW")

                        STATE["power_kw"] = allocated_power
                        STATE["charging"] = True
                        if STATE["transaction_id"] is None:
                            STATE["transaction_id"] = 1001

                        await ws.send(json.dumps([3, msg_id, {"status": "Accepted"}]))
                        await notify_car_of_control()
                        await send_status_notification(ws, "Charging")

                    elif action == "RemoteStopTransaction":
                        _LOGGER.info("🛑 Halt command received.")
                        STATE["charging"] = False
                        STATE["power_kw"] = 0.0
                        await ws.send(json.dumps([3, msg_id, {"status": "Accepted"}]))
                        await notify_car_of_control()
                        await send_status_notification(ws, "Finishing")

                    else:
                        await ws.send(json.dumps([3, msg_id, {"status": "Accepted"}]))

            except Exception as ex:
                _LOGGER.error(f"Error handling downstream action: {ex}")
    except websockets.exceptions.ConnectionClosed:
        _LOGGER.warning("CSMS connection closed.")


async def send_call(ws, action, payload):
    msg_id = f"sim-{int(time.time() * 1000)}"
    envelope = [2, msg_id, action, payload]
    _LOGGER.info(f"Transmitting client message: {action}")
    await ws.send(json.dumps(envelope))
    return msg_id


async def send_status_notification(ws, status_value):
    payload = {
        "connectorId": STATE["connector_id"],
        "errorCode": "NoError",
        "status": status_value,
        "timestamp": iso_timestamp(),
    }
    if status_value == "Preparing" and STATE["soc"] is not None:
        payload["soc_at_start"] = float(STATE["soc"])
        payload["soc_min"] = float(STATE["soc_min"])
        payload["soc_max"] = float(STATE["soc_max"])
        payload["battery_capacity_kwh"] = float(STATE["battery_capacity_kwh"])
        payload["target_value"] = float(STATE["target_soc"])
        payload["target_time"] = str(STATE["target_time"])

    await send_call(ws, "StatusNotification", payload)


async def central_charger_loop():
    global STATE
    _LOGGER.info(f"Connecting to CSMS at: {CSMS_URL}")

    async for ws in websockets.connect(CSMS_URL, subprotocols=["ocpp1.6"]):
        STATE["csms_ws"] = ws
        _LOGGER.info("Network handshake completed successfully.")

        receiver_task = asyncio.create_task(csms_receiver(ws))

        await send_call(
            ws,
            "BootNotification",
            {
                "chargePointModel": "FlexSim-v3",
                "chargePointVendor": "HobbyistCorp",
                "firmwareVersion": "3.2.0",
            },
        )
        await asyncio.sleep(1)
        await send_status_notification(ws, "Available")

        last_heartbeat = time.time()

        try:
            while True:
                # If no car is connected, idle until one arrives
                if not _VEHICLE_CONNECTED_EVENT.is_set():
                    await asyncio.sleep(0.5)
                    continue

                # Transition to Preparing on vehicle connection
                _LOGGER.info("🚙 Vehicle confirmed! Status -> 'Preparing'...")
                await send_status_notification(ws, "Preparing")

                # Active session tick loop
                while _VEHICLE_CONNECTED_EVENT.is_set():
                    await asyncio.sleep(TICK_INTERVAL_SEC)
                    now = time.time()

                    if now - last_heartbeat >= 30:
                        await send_call(ws, "Heartbeat", {})
                        last_heartbeat = now

                    await notify_car_of_control()

                    if STATE["charging"] and STATE["power_kw"] > 0 and STATE["soc"] is not None:
                        if STATE["soc"] < STATE["target_soc"]:
                            hours_per_tick = TICK_INTERVAL_SEC / 3600.0
                            added_kwh = STATE["power_kw"] * hours_per_tick
                            added_soc = (added_kwh / STATE["battery_capacity_kwh"]) * 100.0
                            STATE["soc"] = min(STATE["soc"] + added_soc, STATE["target_soc"])
                            _LOGGER.info(
                                f"⚡ Charging active: {STATE['power_kw']:.2f} kW | SoC: {STATE['soc']:.3f}%"
                            )
                        else:
                            _LOGGER.info("🔋 Battery target reached.")
                            STATE["charging"] = False
                            STATE["power_kw"] = 0.0
                            await send_status_notification(ws, "Finishing")

                    # Periodic MeterValues
                    if _VEHICLE_CONNECTED_EVENT.is_set() and STATE["soc"] is not None:
                        meter_payload = {
                            "connectorId": STATE["connector_id"],
                            "meterValue": [
                                {
                                    "timestamp": iso_timestamp(),
                                    "sampledValue": [
                                        {
                                            "value": f"{STATE['power_kw']:.2f}",
                                            "context": "Sample.Periodic",
                                            "format": "Raw",
                                            "measurand": "Power.Active.Import",
                                            "unit": "kW",
                                        },
                                        {
                                            "value": f"{STATE['soc']:.1f}",
                                            "context": "Sample.Periodic",
                                            "format": "Raw",
                                            "measurand": "SoC",
                                            "unit": "Percent",
                                        },
                                    ],
                                }
                            ],
                        }
                        if STATE["transaction_id"] is not None:
                            meter_payload["transactionId"] = STATE["transaction_id"]
                        await send_call(ws, "MeterValues", meter_payload)

        except websockets.exceptions.ConnectionClosed:
            _LOGGER.warning("Lost CSMS connection. Retrying...")
        finally:
            receiver_task.cancel()
            STATE["csms_ws"] = None


async def main():
    _LOGGER.info(f"Starting Charger WebSocket Server on port {CAR_SERVER_PORT}...")
    car_server = await websockets.serve(car_server_handler, "0.0.0.0", CAR_SERVER_PORT)
    await central_charger_loop()
    await car_server.wait_closed()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        _LOGGER.info("Charger simulator terminated.")
