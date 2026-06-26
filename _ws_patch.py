# Parche para compatibilidad websocket-client en Python 3.12+
import threading

if not hasattr(threading.Thread, 'isAlive'):
    threading.Thread.isAlive = threading.Thread.is_alive


# Keepalive por defecto para conexiones websocket largas (pyRofex).
try:
    import websocket

    _ORIG_RUN_FOREVER = getattr(websocket.WebSocketApp, "run_forever", None)
    _PATCHED_FLAG = "_a3_keepalive_patched"

    if _ORIG_RUN_FOREVER is not None and not getattr(websocket.WebSocketApp, _PATCHED_FLAG, False):
        def _run_forever_with_keepalive(self, *args, **kwargs):
            kwargs["ping_interval"] = 20
            kwargs["ping_timeout"] = 10
            return _ORIG_RUN_FOREVER(self, *args, **kwargs)

        websocket.WebSocketApp.run_forever = _run_forever_with_keepalive
        setattr(websocket.WebSocketApp, _PATCHED_FLAG, True)
except Exception:
    pass
