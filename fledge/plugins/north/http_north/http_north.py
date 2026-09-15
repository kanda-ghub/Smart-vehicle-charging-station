# -*- coding: utf-8 -*-

# FLEDGE_BEGIN
# See: http://fledge-iot.readthedocs.io/
# FLEDGE_END

""" HTTP North plugin"""

import aiohttp
import asyncio
import json
import logging
import base64

import numpy as np

from fledge.common import logger
from fledge.plugins.north.common.common import *

__author__ = "Ashish Jabble, Praveen Garg"
__copyright__ = "Copyright (c) 2018 Dianomic Systems"
__license__ = "Apache 2.0"
__version__ = "${VERSION}"

_LOGGER = logger.setup(__name__, level=logging.INFO)


http_north = None
config = ""

_CONFIG_CATEGORY_NAME = "HTTP"
_CONFIG_CATEGORY_DESCRIPTION = "HTTP North Plugin"

_DEFAULT_CONFIG = {
    'plugin': {
         'description': 'HTTP North Plugin',
         'type': 'string',
         'default': 'http_north',
         'readonly': 'true'
    },
    'url': {
        'description': 'Destination URL',
        'type': 'string',
        'default': 'http://localhost:6683/sensor-reading',
        'order': '1',
        'displayName': 'URL'
    },
    "source": {
         "description": "Source of data to be sent on the stream. May be either readings or statistics.",
         "type": "enumeration",
         "default": "readings",
         "options": ["readings", "statistics"],
         'order': '2',
         'displayName': 'Source'
    },
    "verifySSL": {
        "description": "Verify SSL certificate",
        "type": "boolean",
        "default": "false",
        'order': '3',
        'displayName': 'Verify SSL'
    }
}


def plugin_info():
    return {
        'name': 'http',
        'version': '3.1.0',
        'type': 'north',
        'mode': 'none',
        'interface': '1.0',
        'config': _DEFAULT_CONFIG
    }


def plugin_init(data):
    global http_north, config
    http_north = HttpNorthPlugin()
    config = data
    return config


async def plugin_send(data, payload, stream_id):
    # stream_id (log?)
    try:
        is_data_sent, new_last_object_id, num_sent = await http_north.send_payloads(payload)
    except asyncio.CancelledError:
        pass
    else:
        return is_data_sent, new_last_object_id, num_sent


def plugin_shutdown(data):
    pass


# TODO: North plugin can not be reconfigured? (per callback mechanism)
def plugin_reconfigure():
    pass


# https://stackoverflow.com/questions/3488934/simplejson-and-numpy-array/24375113#24375113

class NumpyEncoder(json.JSONEncoder):

    def default(self, obj):
        """If input object is an ndarray it will be converted into a dict 
        holding dtype, shape and the data
        """
        if isinstance(obj, np.ndarray):
            obj_data = np.ascontiguousarray(obj).data
            data_list = obj_data.tolist()
            return dict(__ndarray__=data_list,
                        dtype=str(obj.dtype),
                        shape=obj.shape)
        # Let the base class default method raise the TypeError
        super(NumpyEncoder, self).default(obj)


class NumpyEncoderBase64(json.JSONEncoder):

    def default(self, obj):
        """If input object is an ndarray it will be converted into a dict 
        holding dtype, shape and the data
        """
        if isinstance(obj, np.ndarray):
            obj_data = np.ascontiguousarray(obj).data
            data_list = base64.b64encode(obj_data)
            if isinstance(data_list, bytes):
                data_list = data_list.decode(encoding='UTF-8')
            return dict(__ndarray__=data_list,
                        dtype=str(obj.dtype),
                        shape=obj.shape)

        # Let the base class default method raise the TypeError
        super(NumpyEncoderBase64, self).default(obj)


class HttpNorthPlugin(object):
    """ North HTTP Plugin """

    def __init__(self):
        self.event_loop = asyncio.get_event_loop()

    async def send_payloads(self, payloads):
        is_data_sent = False
        last_object_id = 0
        num_sent = 0
        try:
            payload_block = list()

            for p in payloads:
                last_object_id = p["id"]
                read = dict()
                read["asset"] = p['asset_code']
                read["readings"] = p['reading']
                for k, v in read['readings'].items():
                    if isinstance(v, np.ndarray):
                        serialized_data_base64 = json.dumps(v, cls=NumpyEncoderBase64)
                        read['readings'][k] = serialized_data_base64

                read["timestamp"] = p['user_ts']
                payload_block.append(read)

            num_sent = await self._send_payloads(payload_block)
            is_data_sent = True
        except Exception as ex:
            _LOGGER.exception("Data could not be sent, %s", str(ex))

        return is_data_sent, last_object_id, num_sent

    async def _send_payloads(self, payload_block):
        """ send a list of block payloads"""

        num_count = 0
        try:
            verify_ssl = False if config["verifySSL"]['value'] == 'false' else True
            url = config['url']['value']
            connector = aiohttp.TCPConnector(verify_ssl=verify_ssl)
            async with aiohttp.ClientSession(connector=connector) as session:
                result = await self._send(url, payload_block, session)
        except:
            pass
        else: 
            num_count += len(payload_block)
        return num_count

    async def _send(self, url, payload, session):
        """ Send the payload, using provided socket session """
        headers = {'content-type': 'application/json'}
        async with session.post(url, data=json.dumps(payload), headers=headers) as resp:
            result = await resp.text()
            status_code = resp.status
            if status_code in range(400, 500):
                _LOGGER.error("Bad request error code: %d, reason: %s", status_code, resp.reason)
                raise Exception
            if status_code in range(500, 600):
                _LOGGER.error("Server error code: %d, reason: %s", status_code, resp.reason)
                raise Exception
            return result
