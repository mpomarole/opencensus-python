# Copyright 2019, OpenCensus Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import datetime
import json
import logging
import os
import platform
import threading

import requests

from opencensus.ext.azure.common.transport import _requests_lock, _requests_map
from opencensus.ext.azure.common.version import __version__ as ext_version
from opencensus.metrics.export.gauge import DerivedLongGauge, LongGauge
from opencensus.metrics.label_key import LabelKey
from opencensus.metrics.label_value import LabelValue

_AIMS_URI = "http://169.254.169.254/metadata/instance/compute"
_AIMS_API_VERSION = "api-version=2017-12-01"
_AIMS_FORMAT = "format=json"

_DEFAULT_STATS_CONNECTION_STRING = "InstrumentationKey=c4a29126-a7cb-47e5-b348-11414998b11e;IngestionEndpoint=https://dc.services.visualstudio.com/"  # noqa: E501
_DEFAULT_STATS_SHORT_EXPORT_INTERVAL = 900  # 15 minutes
_DEFAULT_STATS_LONG_EXPORT_INTERVAL = 86400  # 24 hours

_ATTACH_METRIC_NAME = "Attach"
_REQ_SUC_COUNT_NAME = "Request Success Count"

_RP_NAMES = ["appsvc", "function", "vm", "unknown"]

_logger = logging.getLogger(__name__)


def _get_stats_connection_string():
    cs_env = os.environ.get("APPLICATION_INSIGHTS_STATS_CONNECTION_STRING")
    if cs_env:
        return cs_env
    else:
        return _DEFAULT_STATS_CONNECTION_STRING


def _get_stats_short_export_interval():
    ei_env = os.environ.get("APPLICATION_INSIGHTS_STATS_SHORT_EXPORT_INTERVAL")
    if ei_env:
        return int(ei_env)
    else:
        return _DEFAULT_STATS_SHORT_EXPORT_INTERVAL


def _get_stats_long_export_interval():
    ei_env = os.environ.get("APPLICATION_INSIGHTS_STATS_LONG_EXPORT_INTERVAL")
    if ei_env:
        return int(ei_env)
    else:
        return _DEFAULT_STATS_LONG_EXPORT_INTERVAL


_STATS_CONNECTION_STRING = _get_stats_connection_string()
_STATS_SHORT_EXPORT_INTERVAL = _get_stats_short_export_interval()
_STATS_LONG_EXPORT_INTERVAL = _get_stats_long_export_interval()
_STATS_LONG_INTERVAL_THRESHOLD = _STATS_LONG_EXPORT_INTERVAL / _STATS_SHORT_EXPORT_INTERVAL  # noqa: E501


def _get_common_properties():
    properties = []
    properties.append(
        LabelKey("rp", 'name of the rp, e.g. appsvc, vm, function, aks, etc.'))
    properties.append(LabelKey("attach", 'codeless or sdk'))
    properties.append(LabelKey("cikey", 'customer ikey'))
    properties.append(LabelKey("runtimeVersion", 'Python version'))
    properties.append(LabelKey("os", 'os of application being instrumented'))
    properties.append(LabelKey("language", 'Python'))
    properties.append(LabelKey("version", 'sdkVersion - version of the ext'))
    return properties


def _get_attach_properties():
    properties = _get_common_properties()
    properties.insert(1, LabelKey("rpid", 'unique id of rp'))
    return properties


def _get_network_properties():
    properties = _get_common_properties()
    return properties


def _get_success_count_value():
    with _requests_lock:
        interval_count = _requests_map.get('success', 0) \
                    - _requests_map.get('last_success', 0)
        _requests_map['last_success'] = _requests_map.get('success', 0)
        return interval_count


class _StatsbeatMetrics:

    def __init__(self, instrumentation_key):
        self._instrumentation_key = instrumentation_key
        self._stats_lock = threading.Lock()
        self._vm_data = {}
        self._vm_retry = True
        self._rp = _RP_NAMES[3]
        self._os_type = platform.system()
        # Attach metrics - metrics related to rp (resource provider)
        self._attach_metric = LongGauge(
            _ATTACH_METRIC_NAME,
            'Statsbeat metric related to rp integrations',
            'count',
            _get_attach_properties(),
        )
        # Keep track of how many iterations until long export
        self._long_threshold_count = 0
        # Network metrics - metrics related to request calls to Breeze
        self._network_metrics = {}
        # Map of gauge function -> metric
        # Gauge function is the callback used to populate the metric value
        self._network_metrics[_get_success_count_value] = DerivedLongGauge(
            _REQ_SUC_COUNT_NAME,
            'Request success count',
            'count',
            _get_network_properties(),
        )

    # Metrics that are sent on application start
    def get_initial_metrics(self):
        stats_metrics = []
        if self._attach_metric:
            attach_metric = self._get_attach_metric()
            if attach_metric:
                stats_metrics.append(attach_metric)
        return stats_metrics

    # Metrics sent every statsbeat interval
    def get_metrics(self):
        metrics = []
        try:
            # Initial metrics use the long export interval
            # Only export once long count hits threshold
            with self._stats_lock:
                self._long_threshold_count = self._long_threshold_count + 1
                if self._long_threshold_count >= _STATS_LONG_INTERVAL_THRESHOLD:  # noqa: E501
                    metrics.extend(self.get_initial_metrics())
                    self._long_threshold_count = 0
            network_metrics = self._get_network_metrics()
            metrics.extend(network_metrics)
        except Exception as ex:
            _logger.warning('Error while exporting stats metrics %s.', ex)

        return metrics

    def _get_network_metrics(self):
        properties = self._get_common_properties()
        metrics = []
        for fn, metric in self._network_metrics.items():
            # NOTE: A time series is a set of unique label values
            # If the label values ever change, a separate time series will be
            # created, however, `_get_properties()` should never change
            metric.create_time_series(properties, fn)
            stats_metric = metric.get_metric(datetime.datetime.utcnow())
            # Don't export if value is 0
            if stats_metric.time_series[0].points[0].value.value != 0:
                metrics.append(stats_metric)
        return metrics

    def _get_attach_metric(self):
        properties = []
        rp = ''
        rpId = ''
        # rp, rpId
        if os.environ.get("WEBSITE_SITE_NAME") is not None:
            # Web apps
            rp = _RP_NAMES[0]
            rpId = '{}/{}'.format(
                        os.environ.get("WEBSITE_SITE_NAME"),
                        os.environ.get("WEBSITE_HOME_STAMPNAME", '')
            )
        elif os.environ.get("FUNCTIONS_WORKER_RUNTIME") is not None:
            # Function apps
            rp = _RP_NAMES[1]
            rpId = os.environ.get("WEBSITE_HOSTNAME")
        elif self._vm_retry and self._get_azure_compute_metadata():
            # VM
            rp = _RP_NAMES[2]
            rpId = '{}//{}'.format(
                        self._vm_data.get("vmId", ''),
                        self._vm_data.get("subscriptionId", ''))
            self._os_type = self._vm_data.get("osType", '')
        else:
            # Not in any rp or VM metadata failed
            rp = _RP_NAMES[3]
            rpId = _RP_NAMES[3]

        self._rp = rp
        properties.extend(self._get_common_properties())
        properties.insert(1, LabelValue(rpId))  # rpid
        self._attach_metric.get_or_create_time_series(properties)
        return self._attach_metric.get_metric(datetime.datetime.utcnow())

    def _get_common_properties(self):
        properties = []
        properties.append(LabelValue(self._rp))  # rp
        properties.append(LabelValue("sdk"))  # attach type
        properties.append(LabelValue(self._instrumentation_key))  # cikey
        # runTimeVersion
        properties.append(LabelValue(platform.python_version()))
        properties.append(LabelValue(self._os_type or platform.system()))  # os
        properties.append(LabelValue("python"))  # language
        # version
        properties.append(
            LabelValue(ext_version))
        return properties

    def _get_azure_compute_metadata(self):
        try:
            request_url = "{0}?{1}&{2}".format(
                _AIMS_URI, _AIMS_API_VERSION, _AIMS_FORMAT)
            response = requests.get(
                request_url, headers={"MetaData": "True"}, timeout=5.0)
        except (requests.exceptions.ConnectionError, requests.Timeout):
            # Not in VM
            self._vm_retry = False
            return False
        except requests.exceptions.RequestException:
            self._vm_retry = True  # retry
            return False

        try:
            text = response.text
            self._vm_data = json.loads(text)
        except Exception:  # pylint: disable=broad-except
            # Error in reading response body, retry
            self._vm_retry = True
            return False

        # Vm data is perpetually updated
        self._vm_retry = True
        return True
