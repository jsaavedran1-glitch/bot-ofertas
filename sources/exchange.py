import requests, time

_cache = {"rate": None, "ts": 0}

def usd_to_cop() -> float:
    if time.time() - _cache["ts"] < 3600 and _cache["rate"]:
        return _cache["rate"]
    try:
        r = requests.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=5).json()
        rate = r["rates"]["COP"]
    except Exception:
        rate = _cache["rate"] or 4200.0
    _cache.update({"rate": rate, "ts": time.time()})
    return rate
