# -*- coding: utf-8 -*-
import asyncio
import requests
from datetime import datetime, timezone, timedelta

from fledge.common import logger

_LOGGER = logger.setup(__name__)

__version__ = "3.2"

# -----------------------------------------------------------------------------
# CONFIG
# -----------------------------------------------------------------------------
_DEFAULT_CONFIG = {
    "plugin": {
        "description": "FlexMeasures north plugin",
        "type": "string",
        "default": "flex",
        "readonly": "true"
    },

    "flexMeasuresUrl": {
        "description": "FlexMeasures API URL",
        "type": "string",
        "default": "http://localhost:5000/api",
        "order": "1",
        "displayName": "FlexMeasures URL"
    },

    "apiToken": {
        "description": "FlexMeasures API token",
        "type": "string",
        "default": "",
        "order": "2",
        "displayName": "API Token"
    },

    "timeout": {
        "description": "HTTP timeout in seconds",
        "type": "integer",
        "default": "5",
        "order": "3",
        "displayName": "Timeout"
    }
}

# -----------------------------------------------------------------------------
# SENSOR MAPPING
# -----------------------------------------------------------------------------
SENSOR_MAP = {
    ("PV_BESS", "bess_power_kW"): 4,
    ("PV_BESS", "SoC_percent"): 5,
    ("PV_BESS", "load_kw"): 17,
    ("PV_Power", "power_kW"): 3,
    ("Grid_Power", "grid_power_kW"): 6,
    ("CP001", "power_kW"): 1,
    ("CP001", "soc"): 14,
    ("CP002", "power_kW"): 15,
    ("CP002", "soc"): 16
}

UNIT_MAP = {
    "power_kW": "kW",
    "load_kw": "kW",
    "grid_power_kW": "kW",
    "bess_power_kW": "kW",
    "SoC_percent": "%",
    "soc": "%"
}

# -----------------------------------------------------------------------------
# HELPERS
# -----------------------------------------------------------------------------
def utc_now():
    """Returns the current local time (UTC+3) rounded down to 15-min boundary."""
    local_tz = timezone(timedelta(hours=3))
    now = datetime.now(local_tz)
    minute_15 = (now.minute // 15) * 15
    return now.replace(minute=minute_15, second=0, microsecond=0).isoformat()

def fifteen_minute_bucket(ts):
    """
    Round timestamp down to nearest 15-minute bucket (00, 15, 30, 45).
    """
    try:
        if not ts:
            return utc_now()

        if isinstance(ts, str):
            ts = ts.replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts)
        else:
            return utc_now()

        # Floor minutes to nearest 15-minute mark
        minute_15 = (dt.minute // 15) * 15
        dt = dt.replace(minute=minute_15, second=0, microsecond=0)

        return dt.isoformat()

    except Exception as ex:
        _LOGGER.warning("Invalid timestamp '{}': {}".format(ts, ex))
        return utc_now()

# -----------------------------------------------------------------------------
# PLUGIN INFO
# -----------------------------------------------------------------------------
def plugin_info():
    return {
        "name": "flex",
        "version": __version__,
        "type": "north",
        "mode": "none",
        "interface": "1.0",
        "config": _DEFAULT_CONFIG
    }

# -----------------------------------------------------------------------------
# INIT
# -----------------------------------------------------------------------------
def plugin_init(config):
    handle = {
        "flex_url": config["flexMeasuresUrl"]["value"].rstrip("/"),
        "api_token": config["apiToken"]["value"],
        "timeout": int(config["timeout"]["value"])
    }

    _LOGGER.info("FlexMeasures plugin initialized")
    return handle

# -----------------------------------------------------------------------------
# START
# -----------------------------------------------------------------------------
def plugin_start(handle):
    _LOGGER.info("FlexMeasures plugin started")
    return handle

# -----------------------------------------------------------------------------
# SEND
# -----------------------------------------------------------------------------
async def plugin_send(handle, readings, stream_id=None):

    if not readings:
        return (0, 0, 0)

    sent = 0
    dropped = 0
    last_id = 0

    try:
        grouped = {}

        # -------------------------------------------------------------
        # GROUP BY SENSOR + 15-MINUTE BUCKET
        # -------------------------------------------------------------
        for r in readings:

            asset = r.get("asset_code")
            data = r.get("reading", {})
            ts = r.get("ts")

            # track last reading id (for Fledge buffer bookkeeping)
            last_id = r.get("id", last_id)

            bucket = fifteen_minute_bucket(ts)

            for key, value in data.items():

                _LOGGER.debug(f"====> Looking for tuple {asset} {key}...")
                sensor_id = SENSOR_MAP.get((asset, key))
                if not sensor_id:                     
                    continue

                unit = UNIT_MAP.get(key, "")

                grouped.setdefault(sensor_id, {})
                grouped[sensor_id].setdefault(bucket, {
                    "values": [],
                    "unit": unit
                })

                grouped[sensor_id][bucket]["values"].append(value)

        # -------------------------------------------------------------
        # SEND TO FLEXMEASURES
        # -------------------------------------------------------------
        loop = asyncio.get_event_loop()

        for sensor_id, buckets in grouped.items():

            url = f"{handle['flex_url']}/sensors/{sensor_id}/data"

            headers = {
                "Authorization": f"{handle['api_token']}",
                "Content-Type": "application/json"
            }

            for bucket_ts, data in buckets.items():

                values = data["values"]
                avg_value = sum(values) / float(len(values))

                payload = {
                    "type": "PostSensorDataRequest",
                    "start": bucket_ts,
                    "duration": "PT15M",  # <--- UPDATED to 15 minutes
                    "unit": data["unit"],
                    "values": [round(avg_value, 2)]
                }

                try:
                    response = await loop.run_in_executor(
                        None,
                        lambda: requests.post(
                            url,
                            json=payload,
                            headers=headers,
                            timeout=handle["timeout"]
                        )
                    )

                    if response.status_code in (200, 201):
                        sent += 1
                        _LOGGER.debug(
                            f"Sensor {sensor_id}: sent 15-min avg {avg_value}"
                        )
                    else:
                        dropped += 1
                        _LOGGER.error(
                            f"FlexMeasures ERROR {response.status_code}: {response.text}"
                        )

                except Exception as ex:
                    dropped += 1
                    _LOGGER.error(
                        f"HTTP POST failed for sensor {sensor_id}: {ex}"
                    )

        return (sent, dropped, last_id)

    except Exception as ex:
        _LOGGER.error(f"plugin_send exception: {ex}")
        return (0, len(readings), last_id)

# -----------------------------------------------------------------------------
# POLL
# -----------------------------------------------------------------------------
def plugin_poll(handle):
    return None

# -----------------------------------------------------------------------------
# SHUTDOWN
# -----------------------------------------------------------------------------
def plugin_shutdown(handle):
    _LOGGER.info("FlexMeasures plugin shutdown")
