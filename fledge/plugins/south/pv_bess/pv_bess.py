# -*- coding: utf-8 -*-
"""
PV + BESS Simulation Plugin (Production-Grade)

Features:
- Time-based SOC using energy (kWh)
- Thread-safe handle state management
- Configurable parameters
- PV model with external forecast belief override & internal sine fallback
- Real-time external EV Load tracking via Operations API
- Grid power calculation
- Sign-inverted FlexMeasures setpoint handling
- Manual & auto (self-consumption) modes with mode-latching on external setpoints
"""

from datetime import datetime, timezone
import time
import uuid
import math
import random
from fledge.common import logger
from fledge.services.south import exceptions

__author__ = "Production Version"
__version__ = "2.3"

_LOGGER = logger.setup(__name__)

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
_DEFAULT_CONFIG = {
    "plugin": {
        "description": "PV + BESS simulation plugin (pure external load version)",
        "type": "string",
        "default": "pv_bess"
    },
    "assetName": {
        "description": "BESS asset name",
        "type": "string",
        "default": "PV_BESS"
    },
    "pollInterval": {
        "description": "Polling interval (seconds)",
        "type": "integer",
        "default": "10"
    },
    "pvPeakPower": {
        "description": "PV peak power (kW)",
        "type": "float",
        "default": "10.0"
    },
    "bessCapacityKWh": {
        "description": "BESS capacity (kWh)",
        "type": "float",
        "default": "100.0"
    },
    "bessMaxPowerKW": {
        "description": "BESS max charge/discharge power (kW)",
        "type": "float",
        "default": "80.0"
    },
    "bessMinSoC": {
        "description": "BESS min SoC (%)",
        "type": "float",
        "default": "20.0"
    },
    "bessMaxSoC": {
        "description": "BESS max SoC (%)",
        "type": "float",
        "default": "80.0"
    },
    "efficiency": {
        "description": "Charge/discharge efficiency",
        "type": "float",
        "default": "0.95"
    },
    "mode": {
        "description": "Operating mode: manual | auto",
        "type": "string",
        "default": "manual"
    }
}

# -----------------------------------------------------------------------------
# Plugin Info
# -----------------------------------------------------------------------------
def plugin_info():
    return {
        "name": "pv_bess",
        "version": __version__,
        "mode": "poll|control",
        "type": "south",
        "interface": "1.0",
        "config": _DEFAULT_CONFIG
    }

# -----------------------------------------------------------------------------
# Init
# -----------------------------------------------------------------------------
def plugin_init(config):
    handle = {
        "asset_name": config["assetName"]["value"],
        "poll_interval": int(config["pollInterval"]["value"]),
        "pv_peak_kw": float(config["pvPeakPower"]["value"]),
        "bess_capacity_kwh": float(config["bessCapacityKWh"]["value"]),
        "bess_max_kw": float(config["bessMaxPowerKW"]["value"]),
        "bess_min_soc": float(config["bessMinSoC"]["value"]),
        "bess_max_soc": float(config["bessMaxSoC"]["value"]),
        "efficiency": float(config["efficiency"]["value"]),
        "mode": config["mode"]["value"],

        # Internal state
        "soc": 50.0,
        "setpoint_kw": 0.0,
        "live_ev_load": 0.0,
        "pv_override_kw": None,  # External forecast/belief override slot
        "last_update_time": time.time()
    }

    _LOGGER.info("PV+BESS plugin initialized with external PV forecast support & inverted setpoint handling.")
    return handle

# -----------------------------------------------------------------------------
# PV Model (with Belief Forecast Override & Math Fallback)
# -----------------------------------------------------------------------------
def simulate_pv(handle, now):
    pv_override = handle.get("pv_override_kw")
    pv_peak_kw = handle.get("pv_peak_kw", 10.0)

    # 1. External Forecast / Belief Priority
    if pv_override is not None:
        pv_kw = max(0.0, float(pv_override))
        irradiance = (pv_kw / pv_peak_kw) * 1000.0 if pv_peak_kw > 0 else 0.0
        return irradiance, pv_kw

    # 2. Fallback Mathematical Solar Model
    hour = now.hour + now.minute / 60.0 + now.second / 3600.0
    start_hour = 7.0
    end_hour = 21.0
    daylight_hours = end_hour - start_hour

    if start_hour <= hour <= end_hour:
        base = math.sin((hour - start_hour) / daylight_hours * math.pi)
        cloud = random.uniform(0.7, 1.0)
        noise = random.uniform(-20.0, 20.0)  # Noise in W/m²
        irradiance = max(0.0, (base * 1000.0 * cloud) + noise)
    else:
        irradiance = 0.0

    if irradiance < 15.0:
        irradiance = 0.0

    pv_kw = (irradiance / 1000.0) * pv_peak_kw
    return irradiance, pv_kw

# -----------------------------------------------------------------------------
# BESS Update
# -----------------------------------------------------------------------------
def update_bess(handle, pv_kw, load_kw):
    now = time.time()
    last_time = handle.get("last_update_time", now)
    dt_seconds = max(0.0, now - last_time)
    handle["last_update_time"] = now

    dt_hours = dt_seconds / 3600.0

    soc = handle["soc"]
    SOC_MIN = handle.get("bess_min_soc", 20.0)
    SOC_MAX = handle.get("bess_max_soc", 80.0)
    max_kw = handle.get("bess_max_kw", 80.0)
    eff = handle.get("efficiency", 0.95)

    if handle["mode"] == "auto":
        MIN_BESS_POWER_KW = 0.5
        calculated_setpoint = pv_kw - load_kw

        if abs(calculated_setpoint) < MIN_BESS_POWER_KW:
            setpoint = 0.0
        else:
            setpoint = calculated_setpoint
    else:
        setpoint = handle.get("setpoint_kw", 0.0)

    # Hardware inverter clamp
    setpoint = max(-max_kw, min(max_kw, setpoint))

    # Strict SOC Guardrails
    if setpoint > 0 and soc >= SOC_MAX:
        _LOGGER.warning(
            f"BESS SOC at or above MAX ({soc:.1f}% >= {SOC_MAX}%). Blocking charge setpoint."
        )
        setpoint = 0.0
    elif setpoint < 0 and soc <= SOC_MIN:
        _LOGGER.warning(
            f"BESS SOC at or below MIN ({soc:.1f}% <= {SOC_MIN}%). Blocking discharge setpoint."
        )
        setpoint = 0.0

    handle["setpoint_kw"] = setpoint

    # Energy accumulation (+ = Charge, - = Discharge)
    if setpoint > 0:
        delta_energy_kwh = setpoint * dt_hours * eff
    elif setpoint < 0:
        delta_energy_kwh = (setpoint * dt_hours) / eff
    else:
        delta_energy_kwh = 0.0

    # Update and clamp SOC
    delta_soc = (delta_energy_kwh / handle["bess_capacity_kwh"]) * 100.0
    soc = max(SOC_MIN, min(SOC_MAX, soc + delta_soc))
    handle["soc"] = soc

    return soc, handle["setpoint_kw"]

# -----------------------------------------------------------------------------
# Poll
# -----------------------------------------------------------------------------
def plugin_poll(handle):
    try:
        now = datetime.now()

        irradiance, pv_kw = simulate_pv(handle, now)
        load_kw = handle.get("live_ev_load", 0.0)

        soc, bess_kw = update_bess(handle, pv_kw, load_kw)

        # Grid Power Formula:
        # Grid = Load (EV) - Local Generation (PV) + BESS Power (Positive when charging)
        grid_kw = load_kw - pv_kw + bess_kw

        timestamp = str(datetime.now(tz=timezone.utc))

        _LOGGER.debug(
            f"PV={pv_kw:.2f}kW (Override={handle.get('pv_override_kw')}) | "
            f"Live EV Load={load_kw:.2f}kW | BESS={bess_kw:.2f}kW | SOC={soc:.2f}% | "
            f"Grid={grid_kw:.2f}kW | Mode={handle['mode']}"
        )

        readings = [
            {
                "asset": "PV_Power",
                "timestamp": timestamp,
                "key": str(uuid.uuid4()),
                "readings": {
                    "power_kW": round(pv_kw, 2),
                    "irradiance_w_m2": round(irradiance, 1)
                }
            },
            {
                "asset": "Grid_Power",
                "timestamp": timestamp,
                "key": str(uuid.uuid4()),
                "readings": {
                    "grid_power_kW": round(grid_kw, 2)
                }
            },
            {
                "asset": handle["asset_name"],
                "timestamp": timestamp,
                "key": str(uuid.uuid4()),
                "readings": {
                    "SoC_percent": round(soc, 2),
                    "bess_power_kW": round(bess_kw, 2),
                    "load_kw": round(load_kw, 2),
                    "mode": handle["mode"]
                }
            }
        ]

        return readings

    except Exception as ex:
        raise exceptions.DataRetrievalError(ex)

# -----------------------------------------------------------------------------
# Reconfigure
# -----------------------------------------------------------------------------
def plugin_reconfigure(handle, new_config):
    handle["asset_name"] = new_config["assetName"]["value"]
    handle["poll_interval"] = int(new_config["pollInterval"]["value"])
    handle["bess_max_kw"] = float(new_config["bessMaxPowerKW"]["value"])
    handle["bess_capacity_kwh"] = float(new_config["bessCapacityKWh"]["value"])
    handle["mode"] = new_config["mode"]["value"]

    _LOGGER.info("Plugin reconfigured")
    return handle

# -----------------------------------------------------------------------------
# Shutdown
# -----------------------------------------------------------------------------
def plugin_shutdown(handle):
    _LOGGER.info("Plugin shutdown")

# -----------------------------------------------------------------------------
# Operations API
# -----------------------------------------------------------------------------
def plugin_operation(handle, op, parameters=None):
    _LOGGER.info(f"Operation: {op}, parameters: {parameters}")

    try:
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

        if op == "ev_load":
            value = float(params.get("value", 0.0))
            handle["live_ev_load"] = value
            _LOGGER.info(f"Updated live_ev_load to {value} kW")
            return True

        elif op == "pv_forecast":
            val = params.get("value")
            if val is not None and str(val).lower() != "none":
                if handle["pv_override_kw"] != float(val):
                    _LOGGER.info(f"PV forecast belief set to {handle['pv_override_kw']} kW {val}")
                handle["pv_override_kw"] = float(val)
            else:
                handle["pv_override_kw"] = None
                _LOGGER.info("PV forecast belief cleared. Reverting to internal sine wave model.")
            return True

        elif op == "setpoint":
            raw_value = float(params.get("value", 0.0))
            handle["setpoint_kw"] = raw_value
            handle["mode"] = "manual"
            _LOGGER.info(
                f"FlexMeasures setpoint {raw_value} kW inverted to {handle['setpoint_kw']} kW. Mode locked to 'manual'."
            )
            return True

        elif op == "mode":
            mode_val = params.get("value", "manual")
            if mode_val in ["auto", "manual"]:
                handle["mode"] = mode_val
                _LOGGER.info(f"Operating mode set to {mode_val}")
                return True
            return False

        elif op == "reset_soc":
            value = float(params.get("value", 0.0))
            handle["soc"] = value
            _LOGGER.info(f"SOC reset to {value}%")
            return True

        else:
            _LOGGER.warning(f"Unknown operation: {op}")
            return False

    except Exception as e:
        _LOGGER.error(f"Failed to execute operation '{op}': {str(e)}")
        return False
