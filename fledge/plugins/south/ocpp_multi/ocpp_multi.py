# -*- coding: utf-8 -*-
"""
Fledge South Plugin: OCPP 1.6 Server (Control-Enabled for FlexMeasures Schedules)
Enhanced with Multi-Charger Aggregated Load Tracking, SoC percentages, Battery Capacity, and State-Change Filtering
"""

import asyncio
import threading
import json
from datetime import datetime, timezone, timedelta
from collections import deque

from fledge.common import logger
from fledge.services.south import exceptions
import websockets

__version__ = "3.5"
_LOGGER = logger.setup(__name__)

_DEFAULT_CONFIG = {
    "plugin": {
        "description": "OCPP 1.6 Server Plugin with Control API Support",
        "type": "string",
        "default": "ocpp_multi",
        "displayName": "Plugin Name"
    },
    "host": {
        "description": "Bind address",
        "type": "string",
        "default": "0.0.0.0",
        "displayName": "Host IP"
    },
    "port": {
        "description": "WebSocket port",
        "type": "integer",
        "default": "9000",
        "displayName": "Port"
    },
    "pollInterval": {
        "description": "Polling interval (seconds)",
        "type": "integer",
        "default": "1",
        "displayName": "Poll Interval"
    }
}

_event_queue = deque()
_queue_lock = threading.Lock()

# Global map to store active connections: { "cp_id": ws_connection_object }
_ACTIVE_CONNECTIONS = {}
_connections_lock = threading.Lock()

# Thread-safe tracker for tracking individual charger loads to compute total aggregate load
_CHARGER_LOADS = {}
_loads_lock = threading.Lock()

# Thread-safe state tracker to ensure status is only forwarded on a literal change
_LAST_CHARGER_STATUS = {}
_status_lock = threading.Lock()

# Reference to the background async event loop
_SERVER_LOOP = None

def utc_now():
    local_tz = timezone(timedelta(hours=3))
    return datetime.now(local_tz).isoformat(timespec='milliseconds')

def parse_cp_id(path):
    if path:
        return path.strip("/").split("/")[-1]
    return "unknown"

def enqueue_reading(reading):
    with _queue_lock:
        _event_queue.append(reading)

# -----------------------------------------------------------------------------
# OCPP HANDLER
# -----------------------------------------------------------------------------
async def handle_ocpp(ws, path):
    cp_id = parse_cp_id(path)
    _LOGGER.info(f"OCPP client connected: {cp_id}")
    
    with _connections_lock:
        _ACTIVE_CONNECTIONS[cp_id] = ws

    try:
        async for message in ws:
            try:
                msg = json.loads(message)
                if not isinstance(msg, list) or len(msg) < 3:
                    continue

                msg_type = msg[0]
                msg_id = msg[1]

                if msg_type != 2:
                    continue

                action = msg[2]
                payload = msg[3] if len(msg) > 3 else {}
                _LOGGER.debug(f"{cp_id} -> {action}")

                response_payload = {}

                if action == "BootNotification":
                    response_payload = {
                        "status": "Accepted",
                        "currentTime": utc_now(),
                        "interval": 10
                    }
                elif action == "Heartbeat":
                    response_payload = {
                        "currentTime": utc_now()
                    }
                elif action == "StatusNotification":
                    status = payload.get("status")
                    
                    # Deduplicate status triggers to prevent message flooding
                    should_send = False
                    with _status_lock:
                        if _LAST_CHARGER_STATUS.get(cp_id) != status and status != "Charging":
                            _LAST_CHARGER_STATUS[cp_id] = status
                            should_send = True
                    
                    if should_send:
                        _LOGGER.info(f"⚡ State change detected on {cp_id}: -> {status}")
                        
                        # Pack standard parameters
                        reading_data = {
                            "status": status, 
                            "asset_id": cp_id
                        }
                        
                        # Extract SoC values as percentages
                        reading_data["soc_at_start"] = float(payload.get("soc_at_start", 0.0))
                        reading_data["soc_min"] = float(payload.get("soc_min", 0.0))
                        reading_data["soc_max"] = float(payload.get("soc_max", 100.0))
                        if "soc" in payload:
                            reading_data["soc"] = float(payload.get("soc", 0.0))
                            
                        # Extract battery capacity (in kWh) and safely parse as float
                        reading_data["battery_capacity_kwh"] = float(
                            payload.get("battery_capacity_kwh", payload.get("battery_capacity", 0.0))
                        )

                        # Extract target constraints
                        reading_data["target_value"] = float(payload.get("target_value", 0.0))
                        reading_data["target_time"] = payload.get("target_time", "")
                        reading_data["start_time"] = payload.get("start_time", "")

                        _LOGGER.info(f"**************** Reading data {reading_data}")

                        # Broadcast a single combined packet to the storage layer queue
                        enqueue_reading({
                            "asset_code": "EV_Charger_Status",
                            "reading": reading_data,
                            "ts": utc_now()
                        })
                    else:
                        _LOGGER.debug(f"State duplicate suppressed for {cp_id} ({status})")

                elif action == "StartTransaction":
                    response_payload = {
                        "transactionId": 1,
                        "idTagInfo": {"status": "Accepted"}
                    }
                elif action == "MeterValues":
                    mv_list = payload.get("meterValue", [])
                    for mv in mv_list:
                        sampled = mv.get("sampledValue", [])
                        power, soc = None, None
                        
                        for v in sampled:
                            try:
                                unit = v.get("unit")
                                measurand = v.get("measurand", "")
                                value = float(v.get("value", 0))
                                
                                if unit == "kW" or "Power" in measurand:
                                    power = value
                                elif unit in ("Percent", "%") or "SoC" in measurand:
                                    soc = value
                            except Exception:
                                continue

                        if power is not None or soc is not None:
                            timestamp_now = utc_now()
                            reading = {
                                "asset_code": cp_id,
                                "reading": {},
                                "ts": timestamp_now
                            }
                            
                            if power is not None:
                                reading["reading"]["power_kW"] = -power
                                
                                # Track individual load and compute aggregate real-time total
                                with _loads_lock:
                                    _CHARGER_LOADS[cp_id] = power
                                    total_ev_load = sum(_CHARGER_LOADS.values())
                                
                                # Enqueue the sum as a unique asset reading for the Control Pipeline to catch
                                enqueue_reading({
                                    "asset_code": "EV_Total_Load",
                                    "reading": {"power_kW": total_ev_load},
                                    "ts": timestamp_now
                                })

                            if soc is not None:
                                reading["reading"]["soc"] = soc

                            enqueue_reading(reading)

                response = [3, msg_id, response_payload]
                await ws.send(json.dumps(response))

            except Exception as ex:
                _LOGGER.error(f"OCPP message error: {ex}")

    except websockets.exceptions.ConnectionClosed:
        _LOGGER.warning(f"OCPP client disconnected: {cp_id}")
    finally:
        with _connections_lock:
            if _ACTIVE_CONNECTIONS.get(cp_id) == ws:
                del _ACTIVE_CONNECTIONS[cp_id]
        
        # Reset tracking footprints upon hard disconnection
        with _loads_lock:
            if cp_id in _CHARGER_LOADS:
                del _CHARGER_LOADS[cp_id]
                enqueue_reading({
                    "asset_code": "EV_Total_Load",
                    "reading": {"power_kW": sum(_CHARGER_LOADS.values())},
                    "ts": utc_now()
                })
                
        with _status_lock:
            if cp_id in _LAST_CHARGER_STATUS:
                del _LAST_CHARGER_STATUS[cp_id]

def start_server(host, port):
    global _SERVER_LOOP
    _LOGGER.info(f"Starting OCPP server on {host}:{port}")
    _SERVER_LOOP = asyncio.new_event_loop()
    asyncio.set_event_loop(_SERVER_LOOP)
    
    server = websockets.serve(handle_ocpp, host, port, subprotocols=["ocpp1.6"])
    _SERVER_LOOP.run_until_complete(server)
    _SERVER_LOOP.run_forever()

# -----------------------------------------------------------------------------
# PLUGIN API
# -----------------------------------------------------------------------------
def plugin_info():
    return {
        "name": "ocpp_multi",
        "version": __version__,
        "mode": "poll|control", 
        "type": "south",
        "interface": "2.0.0",
        "config": _DEFAULT_CONFIG
    }

def plugin_init(config):
    host = config.get("host", {}).get("value", _DEFAULT_CONFIG["host"]["default"])
    port = int(config.get("port", {}).get("value", _DEFAULT_CONFIG["port"]["default"]))

    thread = threading.Thread(target=start_server, args=(host, port))
    thread.daemon = True
    thread.start()
    _LOGGER.info("OCPP control-enabled server thread started with aggregator functionality")
    
    handle = {"config": config}
    return handle

def plugin_poll(handle):
    try:
        readings = []
        with _queue_lock:
            while _event_queue:
                readings.append(_event_queue.popleft())
        return readings
    except Exception as ex:
        raise exceptions.DataRetrievalError(ex)

# -----------------------------------------------------------------------------
# CONTROL API INTERFACE
# -----------------------------------------------------------------------------
def plugin_operation(handle, operation, parameters):
    """
    Fledge calls this when a Control Pipeline operation is triggered.
    """
    global _SERVER_LOOP
    try:
        _LOGGER.info(f"Operation: {operation}, parameters: {parameters}")
        _LOGGER.info(f"📥 Raw parameters received from Fledge: {parameters} (Type: {type(parameters).__name__})")
        
        # --- PARAMETER NORMALIZATION ---
        params = {}
        if isinstance(parameters, list):
            for item in parameters:
                if isinstance(item, dict):
                    params.update(item)
                elif isinstance(item, tuple) and len(item) == 2:
                    params[item[0]] = item[1]
        elif isinstance(parameters, dict):
            params = parameters
        elif parameters is None:
            params = {}        

        # Extract fields from safely normalized dictionary
        cp_id = params.get("charge_point_id", "CP001")
        power_limit = float(params.get("power_kw", 11.0))
        
        with _connections_lock:
            ws = _ACTIVE_CONNECTIONS.get(cp_id)
            
        if not ws:
            _LOGGER.error(f"Control operation failed: Charge point {cp_id} is not connected.")
            return False

        if _SERVER_LOOP is None:
            _LOGGER.error("Control operation failed: WebSocket server event loop is uninitialized.")
            return False

        # Build standard OCPP downstream message envelope
        ocpp_call = [
            2,
            f"flex-msg-{int(datetime.now().timestamp())}",
            operation,
            {
                "idTag": "FLEXMEASURES_BATCH",
                "connectorId": 1,
                "chargingProfile": {
                    "chargingProfileId": 42,
                    "stackLevel": 0,
                    "chargingProfilePurpose": "TxProfile",
                    "chargingProfileKind": "Absolute",
                    "chargingSchedule": {
                        "chargingRateUnit": "kW",
                        "chargingSchedulePeriod": [
                            {"startPeriod": 0, "limit": power_limit}
                        ]
                    }
                }
            }
        ]

        # Dispatch via the running server thread
        asyncio.run_coroutine_threadsafe(
            ws.send(json.dumps(ocpp_call)), 
            _SERVER_LOOP
        )
        _LOGGER.info(f"Successfully deployed FlexMeasures schedule profile down to {cp_id}")
        return True

    except Exception as e:
        _LOGGER.error(f"Failed to execute control operation: {str(e)}")
        return False

def plugin_reconfigure(handle, new_config):
    _LOGGER.info("OCPP server plugin reconfigured")
    handle["config"] = new_config
    return handle

def plugin_shutdown(handle):
    global _SERVER_LOOP
    _LOGGER.info("Shutting down OCPP server")
    if _SERVER_LOOP and _SERVER_LOOP.is_running():
        _SERVER_LOOP.call_soon_threadsafe(_SERVER_LOOP.stop)
