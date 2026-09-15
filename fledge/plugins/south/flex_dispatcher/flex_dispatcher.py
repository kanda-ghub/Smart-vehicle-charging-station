# -*- coding: utf-8 -*-
"""
FlexMeasures EMS Site-Wide Dispatcher - Fledge South Plugin (Production-Grade)

Features:
- Dynamically shifts focus from single BESS tracking to Site Asset orchestration.
- Generates schedule payloads that include EV sensors:
    * Full parameters (SoC targets, capacities) when CONNECTED.
    * Zero-capacity payload (consumption & production = "0 kW") when DISCONNECTED.
- Deferred unit conversion: Converts EV SoC % targets and boundaries to absolute kWh at payload construction time.
- Preserves explicit, fixed EV target arrival/completion timestamps without sliding across execution intervals.
- Conditional Office Hours constraint handling for optimization payloads with UI validity rules.
- Extracts bundled vehicle metadata constraints via control operations.
- Dynamic vehicle battery capacity received dynamically via EV status notification parameters.
- Multiplexes received multi-sensor schedules back into respective Fledge control nodes.
- Synchronizes FlexMeasures PV forecast beliefs directly to the PV+BESS simulation plugin.
- Full internal handling of local hardware time alignment (UTC+3 / EEST).
"""

import math
import json
import time
import requests
from datetime import datetime, timezone, timedelta
from fledge.common import logger

_LOGGER = logger.setup(__name__)
__version__ = "7.1"

# -----------------------------------------------------------------------------
# DYNAMIC CONFIGURATION SCHEMAS
# -----------------------------------------------------------------------------
_DEFAULT_CONFIG = {
    "plugin": {
        "description": "FlexMeasures EMS Dynamic Filter Dispatcher",
        "type": "string",
        "default": "flex_site_dynamic_dispatcher",
        "readonly": "true"
    },
    "flexMeasuresUrl": {
        "description": "FlexMeasures API URL Endpoint (v3_0)",
        "type": "string",
        "default": "http://localhost:5000/api/v3_0"
    },
    "apiToken": {
        "description": "FlexMeasures API access authorization token",
        "type": "string",
        "default": ""
    },
    "timeout": {
        "description": "HTTP client communication timeout in seconds",
        "type": "integer",
        "default": "5"
    },
    "scheduleTriggerInterval": {
        "description": "Optimization matrix recalculation loop interval in seconds",
        "type": "integer",
        "default": "900"
    },
    "scheduleHorizon": {
        "description": "Optimization horizon window duration",
        "type": "string",
        "default": "PT1H"
    },
    "siteAssetId": {
        "description": "FlexMeasures Master Site Asset or main optimization sensor ID node",
        "type": "integer",
        "default": "1"
    },
    "useOfficeHours": {
        "description": "Enable conditional office hours window restriction for scheduling",
        "type": "boolean",
        "default": "true"
    },
    "officeStartHour": {
        "description": "Start hour of daily office operations (0-23)",
        "type": "integer",
        "default": "8",
        "validity": 'useOfficeHours == "true"'
    },
    "officeEndHour": {
        "description": "End hour of daily office operations (0-23)",
        "type": "integer",
        "default": "17",
        "validity": 'useOfficeHours == "true"'
    },
    "fledgeCoreAdminUrl": {
        "description": "Fledge Administration API URL endpoint used for control operations",
        "type": "string",
        "default": "http://localhost:8081"
    },
    "bessSensorId": {
        "description": "BESS Sensor ID (Always included in the core platform scheduling routine)",
        "type": "string",
        "default": "4"
    },
    "bessCapacityKWh": {
        "description": "BESS capacity (kWh)",
        "type": "float",
        "default": "80.0"
    },
    "ev1SensorId": {
        "description": "FlexMeasures Sensor ID corresponding to EV Charger 1 (CP001)",
        "type": "string",
        "default": "1"
    },
    "ev2SensorId": {
        "description": "FlexMeasures Sensor ID corresponding to EV Charger 2 (CP002)",
        "type": "string",
        "default": "15"
    },
    "pvSensorId": {
        "description": "FlexMeasures Sensor ID for PV production/forecast data",
        "type": "string",
        "default": "3"
    },
    "pvBessControlSlug": {
        "description": "Fledge control script/operation slug for the PV_BESS plugin",
        "type": "string",
        "default": "pv_bess_operation"
    },
    "sensorRoutingMap": {
        "description": "JSON Mapping matching FlexMeasures Sensor IDs to Fledge Control Endpoint slugs",
        "type": "string",
        "default": '{"4": "pv_bess_setpoint", "1": "charge_schedule", "15": "charge_schedule"}'
    }
}

# -----------------------------------------------------------------------------
# PERSISTENT MULTI-ASSET STATE STRUCTURES
# -----------------------------------------------------------------------------
STATE = {
    "job_id": None,
    "last_trigger": 0,
    "last_dispatched_idx": -1,
    "schedule_start": None,
    "schedule_duration_min": 1,
    "matrix_size": 0,
    "cached_multi_schedules": {},
    # Hardware presence mapping updated dynamically via plugin_operation
    "EV_status": {"CP001": 0, "CP002": 0}, 
    # Persistent storage for vehicle raw metadata extracted during the notification phase
    "EV_car_metadata": {
        "CP001": {},
        "CP002": {}
    },
    "bess_soc_initialized": False,
    "bess_soc_kwh": 30.0,
    "bess_soc_pct": 37.5,
    "tz": timezone(timedelta(hours=3))  # Enforced Local Execution Context (UTC+3 / EEST)
}

# -----------------------------------------------------------------------------
# CORE HELPERS
# -----------------------------------------------------------------------------
def parse_bool(val) -> bool:
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ["true", "1", "yes"]

def parse_routing_map(raw_json_input):
    """Parses routing map JSON and explicitly coerces all keys/values to string."""
    if not raw_json_input:
        return {}
    try:
        data = json.loads(raw_json_input) if isinstance(raw_json_input, str) else raw_json_input
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
        return {}
    except (json.JSONDecodeError, TypeError, ValueError) as ex:
        _LOGGER.error(f"Failed to parse routing map JSON: {ex}")
        return {}

def headers(handle):
    return {
        "Authorization": handle["api_token"],
        "Content-Type": "application/json"
    }

def get_rounded_iso_now(resolution_minutes=15):
    now = datetime.now(STATE["tz"])
    minutes_to_add = math.ceil(now.minute / resolution_minutes) * resolution_minutes
    rounded = now.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=minutes_to_add)
    return rounded.isoformat()

def get_office_hours_window(handle):
    if not handle.get("use_office_hours", False):
        return None

    try:
        now = datetime.now(STATE["tz"])
        start_h = handle.get("office_start_hour", 8)
        end_h = handle.get("office_end_hour", 17)

        start_dt = now.replace(hour=start_h, minute=0, second=0, microsecond=0)
        end_dt = now.replace(hour=end_h, minute=0, second=0, microsecond=0)

        return {
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat()
        }
    except Exception as ex:
        _LOGGER.error(f"Failed to generate office hours window: {ex}")
        return None

def evaluate_and_route_setpoints(handle):
    try:
        if not STATE["cached_multi_schedules"] or not STATE["schedule_start"]:
            return

        now = datetime.now(STATE["tz"])
        delta = now - STATE["schedule_start"]
        elapsed_minutes = (delta.total_seconds() / 60.0) / 15

        if elapsed_minutes < 0:
            return

        idx = int(elapsed_minutes)

        if STATE.get("last_dispatched_idx") == idx:
            return

        dispatched_any = False

        for sensor_id, values in STATE["cached_multi_schedules"].items():
            str_sensor_id = str(sensor_id)
            if idx < 0 or idx >= len(values):
                continue
            
            target_setpoint = values[idx]
            _LOGGER.debug(f"Target setpoint for Sensor {str_sensor_id}: idx [{idx}] {target_setpoint} at time tick {now}")

            if str_sensor_id in handle["routing_map"]:
                endpoint_slug = handle["routing_map"][str_sensor_id]
                dispatch_single_target(handle, str_sensor_id, endpoint_slug, target_setpoint)
                dispatched_any = True
                
        if dispatched_any:
            STATE["last_dispatched_idx"] = idx
                
    except Exception as ex:
        _LOGGER.error(f"Failed to cleanly multiplex target setpoints array: {ex}")

def sync_pv_forecast_to_bess(handle):
    """Queries FlexMeasures for current PV forecast/belief and sends operation to PV_BESS plugin."""
    str_pv_sensor_id = handle.get("pv_sensor_id")
    if not str_pv_sensor_id:
        return

    # Instead of fetching from 'now', shift back by one interval step (15 minutes)
    current_slot_start = get_rounded_iso_now(resolution_minutes=15) - timedelta(minutes=15)

    url = f"{handle['base_url']}/sensors/{str_pv_sensor_id}/data"
    params = {
        "start": current_slot_start.isoformat(),
        "duration": "PT15M",
        "unit": "kW"
    }

    pv_val = None
    try:
        r = requests.get(url, headers=headers(handle), params=params, timeout=handle["timeout"])
        if r.status_code == 200:
            data = r.json()
            values = data.get("values", [])
            if values and values[0] is not None:
                pv_val = float(values[0])
                #_LOGGER.info(f"Retrieved PV forecast belief from FlexMeasures: {pv_val} kW")
        else:
            _LOGGER.warning(f"Failed to fetch PV data from FlexMeasures. Status: {r.status_code}")
    except Exception as ex:
        _LOGGER.error(f"Error querying PV forecast from FlexMeasures: {ex}")

    # 2. Issue Operation request to Fledge Core Control Hub for the PV_BESS plugin
    slug = handle.get("pv_bess_control_slug", "pv_bess_operation")
    control_url = f"{handle['fledge_core_url']}/fledge/control/request/{slug}"
    
    payload = {
        "value": str(pv_val)
    }

    try:
        r = requests.put(control_url, json=payload, timeout=handle["timeout"])
        if r.status_code not in [200, 201]:
            #_LOGGER.info(f"Successfully dispatched PV forecast ({pv_val} kW) to [{slug}] => {payload} {control_url}")
            _LOGGER.error(f"Fledge Control Hub rejected PV forecast update to [{slug}]. Status: {r.status_code}")
    except Exception as ex:
        _LOGGER.error(f"Failed to send PV forecast operation to [{slug}]: {ex}")

def to_kwh_str_from_percent(percent_val, capacity_kwh, default_percent=0.0):
    """Converts a percentage value (0-100) and battery capacity (kWh) to an absolute kWh string."""
    try:
        pct = float(percent_val) if percent_val is not None else default_percent
        cap = float(capacity_kwh) if (capacity_kwh is not None and float(capacity_kwh) > 0) else 50.0
        kwh = (pct / 100.0) * cap
        return f"{kwh:.2f} kWh"
    except (ValueError, TypeError):
        return "0.00 kWh"

# -----------------------------------------------------------------------------
# COMMUNICATIONS & CLIENT INTERFACES
# -----------------------------------------------------------------------------
def build_ev_payload(handle, sensor_id, cp_id, start_time):
    """Constructs the FlexMeasures payload for an EV Charger node:
    - If CONNECTED (EV_status == 1): Full asset model with capacity & SoC constraints.
    - If DISCONNECTED (EV_status == 0): Zeroed consumption/production capacities.
    """
    str_sensor_id = str(sensor_id)
    is_connected = STATE["EV_status"].get(cp_id, 0) == 1

    if not is_connected:
        _LOGGER.info(f"EV ({cp_id} / Sensor {str_sensor_id}) DISCONNECTED -> Sending 0 kW zeroed capacity payload.")
        return {
            "sensor": int(str_sensor_id),
            "consumption-capacity": "0 kW",
            "production-capacity": "0 kW"
        }

    # Connected state logic
    _LOGGER.info(f"EV ({cp_id} / Sensor {str_sensor_id}) CONNECTED -> Building active charging payload.")
    meta = STATE["EV_car_metadata"].get(cp_id, {})

    try:
        raw_cap = meta.get("capacity")
        capacity_kwh = float(raw_cap) if raw_cap is not None and float(raw_cap) > 0 else 50.0
    except (ValueError, TypeError):
        capacity_kwh = 50.0

    target_time_iso = meta.get("target_time")
    if not target_time_iso:
        try:
            horizon_start_dt = datetime.fromisoformat(start_time)
        except (ValueError, TypeError):
            horizon_start_dt = datetime.now(STATE["tz"])
        target_time_iso = (horizon_start_dt + timedelta(hours=8)).isoformat()

    try:
        start_dt = datetime.fromisoformat(start_time)
    except (ValueError, TypeError):
        start_dt = datetime.now(STATE["tz"])

    bess_target_iso = (start_dt + timedelta(hours=24)).isoformat()

    return {
        "sensor": int(str_sensor_id),
        "charging-efficiency": "95%",
        "consumption-capacity": "11.0 kW",
        "production-capacity": "0 kW",
        "soc-at-start": to_kwh_str_from_percent(meta.get("soc_at_start"), capacity_kwh, default_percent=20.0),
        "soc-min": to_kwh_str_from_percent(meta.get("soc_min"), capacity_kwh, default_percent=0.0),
        "soc-max": to_kwh_str_from_percent(meta.get("soc_max"), capacity_kwh, default_percent=100.0),
        "soc-targets": [
            {
                "datetime": target_time_iso,
                "value": to_kwh_str_from_percent(meta.get("target_value"), capacity_kwh, default_percent=100.0)
            },
            {
                "datetime": bess_target_iso,
                "value": to_kwh_str_from_percent(meta.get("target_value"), capacity_kwh, default_percent=100.0)
            }
        ]
    }

def trigger_site_schedule(handle):
    site_id = handle["site_asset_id"]
    url = f"{handle['base_url']}/assets/{site_id}/schedules/trigger" 

    # 1. Determine Optimization Horizon Start Time
    office_window = get_office_hours_window(handle)

    if handle.get("use_office_hours", False) and office_window: 
        start_time = office_window["start"]  
    else:
        now = datetime.now(STATE["tz"])
        minutes_to_add = 15 - (now.minute % 15)
        start_time = (now + timedelta(minutes=minutes_to_add)).replace(second=0, microsecond=0).isoformat()

    # 2. Compute BESS Target ISO string (24h from start_time)
    try:
        start_dt = datetime.fromisoformat(start_time)
    except (ValueError, TypeError):
        start_dt = datetime.now(STATE["tz"])

    bess_target_iso = (start_dt + timedelta(hours=24)).isoformat()

    # 3. Construct Active Assets Array with BESS payload
    bess_soc_kwh = STATE["bess_soc_kwh"]
    active_assets = [
        {
            "sensor": int(handle["bess_sensor_id"]), 
            "soc-at-start": f"{bess_soc_kwh:.2f} kWh",
            "soc-min": "5 kWh",
            "soc-max": "80 kWh",
            "power-capacity": "20 kW",
            "soc-targets": [
                {
                    "datetime": bess_target_iso,
                    "value": "76 kWh"
                }
            ]
        }
    ]

    # 4. Include EV Charger 1 Payload (Connected or Disconnected format)
    ev1_payload = build_ev_payload(handle, handle["ev1_sensor_id"], "CP001", start_time)
    active_assets.append(ev1_payload)

    # 5. Include EV Charger 2 Payload (Connected or Disconnected format)
    ev2_payload = build_ev_payload(handle, handle["ev2_sensor_id"], "CP002", start_time)
    active_assets.append(ev2_payload)

    payload = {
        "start": start_time,
        "duration": handle["schedule_horizon"],
        "flex-model": active_assets,
        "flex-context": {
            "site-power-capacity": "100.0 kW",
            "inflexible-device-sensors": [3],
            "consumption-price": {"sensor": 7}
        }
    }

    try:
        _LOGGER.info(f"Submitting trigger payload: {json.dumps(payload)}")
        r = requests.post(url, json=payload, headers=headers(handle), timeout=handle["timeout"])

        if r.status_code != 200:
            _LOGGER.error(f"FlexMeasures schedule trigger rejected. Status: {r.status_code}, Response: {r.text}")
            return None

        data = r.json()
        return data.get("schedule")

    except Exception as ex:
        _LOGGER.error(f"Exception encountered while submitting schedule payload: {ex}")
        return None

def poll_site_job(handle, job_id):
    # Always query BESS, EV1, and EV2 sensors
    active_sensor_ids = [
        str(handle["bess_sensor_id"]),
        str(handle["ev1_sensor_id"]),
        str(handle["ev2_sensor_id"])
    ]

    schedule_horizon = handle["schedule_horizon"]
    combined_schedules = []

    for sensor_id in active_sensor_ids:
        url = f"{handle['base_url']}/sensors/{sensor_id}/schedules/{job_id}?duration={schedule_horizon}"
        try:
            r = requests.get(url, headers=headers(handle), timeout=handle["timeout"])
            
            if r.status_code == 202:
                return None
            elif r.status_code != 200:
                _LOGGER.error(f"Error getting schedule for sensor {sensor_id} on job {job_id}: {r.status_code}")
                return None

            sensor_data = r.json()
            _LOGGER.info(f"Fetched schedule for sensor {sensor_id}: {json.dumps(sensor_data)}")
            combined_schedules.append({
                "sensor_id": str(sensor_id),
                "values": sensor_data.get("values", []),
                "start": sensor_data.get("start"),
                "duration": sensor_data.get("duration", "PT1M")
            })
        except Exception as ex:
            _LOGGER.error(f"Network failure while pulling schedule for sensor {sensor_id}: {ex}")
            return None

    return {"schedules": combined_schedules}

def cache_multi_schedule(result):
    try:
        schedules_list = result.get("schedules", [])
        if not schedules_list:
            return False

        temp_storage = {}
        global_start = None

        for item in schedules_list:
            sensor_str = str(item.get("sensor_id"))
            values = item.get("values", [])
            start = item.get("start")

            if not values or not start:
                continue

            temp_storage[sensor_str] = values
            
            if not global_start:
                global_start = datetime.fromisoformat(start).astimezone(STATE["tz"])
                
        if temp_storage:
            STATE["cached_multi_schedules"] = temp_storage
            STATE["schedule_start"] = global_start
            STATE["matrix_size"] = sum(len(v) for v in temp_storage.values())
            _LOGGER.info(f"Successfully cached matrix rows for {list(temp_storage.keys())} starting at {STATE['schedule_start']}.")
            return True
        return False
    except Exception as ex:
        _LOGGER.error(f"Failed to ingest composite multi-sensor response layout payload: {ex}")
        return False

def dispatch_single_target(handle, sensor_id, endpoint_slug, value):
    url = f"{handle['fledge_core_url']}/fledge/control/request/{endpoint_slug}"
    str_sensor_id = str(sensor_id)
    payload = {}
    
    if str_sensor_id == str(handle["bess_sensor_id"]):
        value = -value
        payload = {"value": str(value)}
    elif str_sensor_id in [str(handle["ev1_sensor_id"]), str(handle["ev2_sensor_id"])]:
        cp_id = "CP001" if str_sensor_id == str(handle["ev1_sensor_id"]) else "CP002"
        payload = {
            "charge_point_id": cp_id,
            "power_kw": str(max(0.0, float(value)))
        }
    else:
        return

    try:
        r = requests.put(url, json=payload, timeout=handle["timeout"])
        if r.status_code not in [200, 201]:
            _LOGGER.error(f"Fledge Core Control Hub rejected target [{endpoint_slug}] update matrix. Status: {r.status_code}")
    except Exception as ex:
        _LOGGER.error(f"Network processing failure writing out execution command to [{endpoint_slug}]: {ex}")

# -----------------------------------------------------------------------------
# FLEDGE NATIVE INTERFACE METHOD IMPLEMENTATIONS
# -----------------------------------------------------------------------------
def plugin_info():
    return {
        "name": "flex_site_dynamic_dispatcher",
        "version": __version__,
        "type": "south",
        "mode": "poll|control",
        "interface": "1.0",
        "config": _DEFAULT_CONFIG
    }

def plugin_init(config):
    routing_map = parse_routing_map(config.get("sensorRoutingMap", {}).get("value", "{}"))

    handle = {
        "base_url": config["flexMeasuresUrl"]["value"].rstrip("/"),
        "api_token": config["apiToken"]["value"],
        "timeout": int(config["timeout"]["value"]),
        "trigger_interval": int(config["scheduleTriggerInterval"]["value"]),
        "schedule_horizon": config["scheduleHorizon"]["value"],
        "site_asset_id": int(config["siteAssetId"]["value"]),
        "bess_sensor_id": str(config["bessSensorId"]["value"]),
        "bess_capacity_kWh": float(config["bessCapacityKWh"]["value"]),
        "ev1_sensor_id": str(config["ev1SensorId"]["value"]),
        "ev2_sensor_id": str(config["ev2SensorId"]["value"]),
        "pv_sensor_id": str(config.get("pvSensorId", {}).get("value", "3")),
        "pv_bess_control_slug": str(config.get("pvBessControlSlug", {}).get("value", "pv_bess_operation")),
        "fledge_core_url": config["fledgeCoreAdminUrl"]["value"].rstrip("/"),
        "routing_map": routing_map,
        "use_office_hours": parse_bool(config.get("useOfficeHours", {}).get("value", True)),
        "office_start_hour": int(config.get("officeStartHour", {}).get("value", 8)),
        "office_end_hour": int(config.get("officeEndHour", {}).get("value", 17))
    }
    return handle

def plugin_poll(handle):
    try:
        now = datetime.now(STATE["tz"]).timestamp()
        diff = now - STATE["last_trigger"]

        if STATE["job_id"] is None and diff >= handle["trigger_interval"] and STATE["bess_soc_initialized"] == True:
            job_id = trigger_site_schedule(handle)
            if job_id:
                STATE["job_id"] = job_id
                STATE["last_trigger"] = now

        if STATE["job_id"] is not None:
            result = poll_site_job(handle, STATE["job_id"])
            if result:
                if cache_multi_schedule(result):
                    STATE["job_id"] = None

        # Route setpoints for BESS and EV chargers
        evaluate_and_route_setpoints(handle)

        # Sync PV forecast belief to BESS plugin
        sync_pv_forecast_to_bess(handle)

        return [{
            "asset": "EMS_Site_Orchestrator_Status",
            "timestamp": datetime.now(STATE["tz"]).isoformat(),
            "readings": {
                "active_monitored_sensors": len(STATE["cached_multi_schedules"]),
                "matrix_data_points_count": STATE["matrix_size"],
                "ev1_state_flag": int(STATE["EV_status"]["CP001"]),
                "ev2_state_flag": int(STATE["EV_status"]["CP002"])
            }
        }]
    except Exception as ex:
        _LOGGER.error(f"Loop operational execution processing error occurred inside plugin_poll tracker frame: {ex}")
    return []

def plugin_operation(handle, operation, params):
    try:
        _LOGGER.info(f"operation {operation} params {params}")
        # --- Operation 1: Dedicated BESS State Update ---
        if operation == "update_bess_soc":
            unpacked_params = {}
            if isinstance(params, list):
                for item in params:
                    if isinstance(item, tuple) and len(item) == 2:
                        unpacked_params[item[0]] = item[1]
            elif isinstance(params, dict):
                unpacked_params = params

            bess_capacity_kwh = float(handle.get("bess_capacity_kWh", 80.0))
            
            soc_pct = (
                unpacked_params.get("soc")
                or unpacked_params.get("bess_soc")
                or unpacked_params.get("bess_soc_percent")
                or handle.get("soc")
            )

            if soc_pct is not None:
                soc_pct = float(soc_pct)
                soc_kwh = round((soc_pct / 100.0) * bess_capacity_kwh, 2)

                STATE["bess_soc_pct"] = soc_pct
                STATE["bess_soc_kwh"] = soc_kwh
                handle["soc"] = soc_pct
                STATE["bess_soc_initialized"] = True

                return True
            else:
                _LOGGER.warning("update_bess_soc operation received but no SOC parameter found.")
                return False

        # --- Operation 2: Dynamic EV SoC Update (Connected Only) ---
        elif operation == "update_ev_soc":
            unpacked_params = {}
            if isinstance(params, list):
                for item in params:
                    if isinstance(item, tuple) and len(item) == 2:
                        unpacked_params[item[0]] = item[1]
            elif isinstance(params, dict):
                unpacked_params = params

            charger_id = str(unpacked_params.get("charger_id"))

            if charger_id not in STATE["EV_status"]:
                _LOGGER.warning(f"update_ev_soc operation received for unknown charger_id: {charger_id}")
                return False

            is_connected = STATE["EV_status"].get(charger_id, 0) == 1

            if is_connected:
                raw_soc = unpacked_params.get("soc")
                if raw_soc is not None:
                    try:
                        soc_val = float(raw_soc)
                        if charger_id not in STATE["EV_car_metadata"]:
                            STATE["EV_car_metadata"][charger_id] = {}
                        
                        STATE["EV_car_metadata"][charger_id]["soc_at_start"] = soc_val
                        _LOGGER.info(f"Updated active SoC for connected EV {charger_id} to {soc_val}%.")

                        return True
                    except (ValueError, TypeError) as ex:
                        _LOGGER.error(f"Failed to parse SoC value '{raw_soc}' for {charger_id}: {ex}")
                        return False
                else:
                    _LOGGER.warning(f"update_ev_soc received for {charger_id} but no SoC parameter was provided.")
                    return False
            else:
                _LOGGER.info(f"EV {charger_id} is DISCONNECTED. Forcing soc_at_start to 0.")
                if charger_id in STATE["EV_car_metadata"]:
                    STATE["EV_car_metadata"][charger_id]["soc_at_start"] = 0
                return True

        # --- Operation 3: EV Status Changed ---
        elif operation == "ev_status_changed":
            unpacked_params = {}
            if isinstance(params, list):
                for item in params:
                    if isinstance(item, tuple) and len(item) == 2:
                        unpacked_params[item[0]] = item[1]
            elif isinstance(params, dict):
                unpacked_params = params

            charger_id = str(unpacked_params.get("charger_id", "CP001"))
            ev_status = str(unpacked_params.get("status", "DISCONNECTED"))
            
            is_connected = ev_status in ["Preparing"]
            new_status_bit = 1 if is_connected else 0
            
            if charger_id in STATE["EV_status"]:
                previous_status_bit = STATE["EV_status"][charger_id]
                has_toggled = (previous_status_bit != new_status_bit)
                STATE["EV_status"][charger_id] = new_status_bit
                
                metadata_updated = False
                
                if is_connected:
                    current_meta = STATE["EV_car_metadata"].get(charger_id, {})
                    
                    raw_cap = (
                        unpacked_params.get("battery_capacity_kwh")
                        or unpacked_params.get("battery_capacity")
                        or current_meta.get("capacity")
                    )
                    try:
                        cap = float(raw_cap) if raw_cap is not None and float(raw_cap) > 0 else 50.0
                    except (ValueError, TypeError):
                        cap = 50.0

                    soc_start = (
                        unpacked_params.get("soc_at_start")
                        or unpacked_params.get("soc")
                        or current_meta.get("soc_at_start")
                    )
                    soc_min = (
                        unpacked_params.get("soc_min")
                        if unpacked_params.get("soc_min") is not None
                        else current_meta.get("soc_min", 0)
                    )
                    soc_max = (
                        unpacked_params.get("soc_max")
                        if unpacked_params.get("soc_max") is not None
                        else current_meta.get("soc_max", 100)
                    )
                    target_val = (
                        unpacked_params.get("target_value")
                        if unpacked_params.get("target_value") is not None
                        else current_meta.get("target_value", 100)
                    )
                    target_time = (
                        unpacked_params.get("target_time")
                        or current_meta.get("target_time")
                    )

                    new_meta = {
                        "soc_at_start": soc_start,
                        "soc_min": soc_min,
                        "soc_max": soc_max,
                        "target_value": target_val,
                        "target_time": target_time,
                        "capacity": cap,
                        "connected_at": current_meta.get("connected_at") or get_rounded_iso_now(resolution_minutes=1)
                    }

                    if current_meta != new_meta:
                        metadata_updated = True

                    STATE["EV_car_metadata"][charger_id] = new_meta
                else:
                    if STATE["EV_car_metadata"][charger_id] != {}:
                        metadata_updated = True
                    STATE["EV_car_metadata"][charger_id] = {}

                if has_toggled or metadata_updated:
                    _LOGGER.info(f"EV Status/Metadata update for {charger_id}. Forcing immediate schedule recalculation.")
                    STATE["last_trigger"] = 0
                    STATE["job_id"] = None

                return True
            return False
    except Exception as ex:
        _LOGGER.error(f"Inbound management operation interface processing dropped an exception: {ex}")
    return False

def plugin_reconfigure(handle, new_config):
    routing_map = parse_routing_map(new_config.get("sensorRoutingMap", {}).get("value", "{}"))
    if not routing_map:
        routing_map = handle["routing_map"]

    handle["base_url"] = new_config["flexMeasuresUrl"]["value"].rstrip("/")
    handle["api_token"] = new_config["apiToken"]["value"]
    handle["timeout"] = int(new_config["timeout"]["value"])
    handle["trigger_interval"] = int(new_config["scheduleTriggerInterval"]["value"])
    handle["schedule_horizon"] = new_config["scheduleHorizon"]["value"]
    handle["site_asset_id"] = int(new_config["siteAssetId"]["value"])
    handle["bess_sensor_id"] = str(new_config["bessSensorId"]["value"])
    handle["bess_capacity_kWh"] = float(new_config["bessCapacityKWh"]["value"])
    handle["ev1_sensor_id"] = str(new_config["ev1SensorId"]["value"])
    handle["ev2_sensor_id"] = str(new_config["ev2SensorId"]["value"])
    handle["pv_sensor_id"] = str(new_config.get("pvSensorId", {}).get("value", handle.get("pv_sensor_id", "3")))
    handle["pv_bess_control_slug"] = str(new_config.get("pvBessControlSlug", {}).get("value", handle.get("pv_bess_control_slug", "pv_bess_operation")))
    handle["fledge_core_url"] = new_config["fledgeCoreAdminUrl"]["value"].rstrip("/")
    handle["routing_map"] = routing_map
    handle["use_office_hours"] = parse_bool(new_config.get("useOfficeHours", {}).get("value", handle["use_office_hours"]))
    handle["office_start_hour"] = int(new_config.get("officeStartHour", {}).get("value", handle["office_start_hour"]))
    handle["office_end_hour"] = int(new_config.get("officeEndHour", {}).get("value", handle["office_end_hour"]))

    return handle

def plugin_shutdown(handle):
    _LOGGER.info("FlexMeasures Dynamic Multi-Asset Site Dispatch Engine shutdown sequence finalized.")
