"""Low-cardinality operational events; never include request data, identifiers or secrets."""
import json
import logging
import os
import shutil
import sys
import threading
import time
from collections import Counter

try:
    import resource
except ImportError:  # Windows has no resource module.
    resource = None


LOGGER = logging.getLogger('uvicorn.error.peergrade')


def emit(event, **fields):
    LOGGER.info(json.dumps({'event': event, **fields}, ensure_ascii=True, separators=(',', ':')))


def route_template(scope):
    """Only static application route templates, never request.url or path parameters."""
    route = scope.get('route')
    path = getattr(route, 'path', None)
    if path in {'/', '/healthz', '/assets'} or isinstance(path, str) and path.startswith('/api/'):
        return path
    return 'unmatched'


def resource_sample(engine):
    pool = engine.pool
    result = {'process_cpu_seconds': round(time.process_time(), 2), 'peak_rss_bytes': None,
              'disk_free_bytes': None, 'db_pool_checked_out': None, 'db_pool_size': None}
    if resource is not None:
        try:
            # Linux reports ru_maxrss in KiB; macOS reports bytes.
            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            result['peak_rss_bytes'] = int(rss if sys.platform == 'darwin' else rss * 1024)
        except (OSError, ValueError):
            pass
    elif os.name == 'nt':
        try:
            import ctypes
            from ctypes import wintypes
            class MemoryCounters(ctypes.Structure):
                _fields_ = [('cb', wintypes.DWORD), ('page_faults', wintypes.DWORD)] + [
                    (name, ctypes.c_size_t) for name in ('peak_working_set','working_set','quota_peak_paged_pool',
                    'quota_paged_pool','quota_peak_nonpaged_pool','quota_nonpaged_pool','pagefile','peak_pagefile')]
            stats = MemoryCounters()
            stats.cb = ctypes.sizeof(stats)
            handle = ctypes.windll.kernel32.GetCurrentProcess
            handle.restype = wintypes.HANDLE
            query = ctypes.windll.psapi.GetProcessMemoryInfo
            query.argtypes = [wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD]
            query.restype = wintypes.BOOL
            if query(handle(), ctypes.byref(stats), stats.cb):
                result['peak_rss_bytes'] = int(stats.peak_working_set)
        except (AttributeError, OSError, ValueError):
            pass
    try:
        result['disk_free_bytes'] = shutil.disk_usage(os.getcwd()).free
    except OSError:
        pass
    for field, method in (('db_pool_checked_out', 'checkedout'), ('db_pool_size', 'size')):
        query = getattr(pool, method, None)
        if callable(query):
            result[field] = query()
    return result


class ServiceMonitor:
    def __init__(self, interval=60):
        self.interval = interval
        self.next_sample = 0
        self.lock = threading.Lock()
        self.requests = Counter()
        self.model = Counter()

    def model_result(self, outcome, elapsed):
        outcome = outcome if outcome in {'success', 'failed', 'unavailable', 'expired'} else 'failed'
        with self.lock:
            self.model[outcome] += 1
        emit('model_check', outcome=outcome, duration_ms=max(0, round(elapsed * 1000)))

    def request_result(self, scope, status, elapsed, engine):
        with self.lock:
            self.requests[f'{status // 100}xx'] += 1
            now = time.monotonic()
            snapshot = None
            if now >= self.next_sample:
                self.next_sample = now + self.interval
                snapshot = {'requests': dict(self.requests), 'model_checks': dict(self.model)}
        if status >= 500:
            emit('service_error', route=route_template(scope), status=status,
                 duration_ms=max(0, round(elapsed * 1000)))
        if snapshot is not None:
            try:
                emit('resource_sample', **snapshot, **resource_sample(engine))
            except Exception:
                # Monitoring failure must not turn a valid response into an application error.
                emit('monitor_sample_unavailable')


monitor = ServiceMonitor()
