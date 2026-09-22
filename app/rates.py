from __future__ import annotations

from decimal import Decimal
import requests


class ExchangeRateError(RuntimeError):
    pass


class ExchangeRateProvider:
    def __init__(self, configured_rate: str = "", session: requests.Session | None = None) -> None:
        self.configured_rate = configured_rate
        self.session = session or requests.Session()

    def usd_to_cop(self) -> Decimal | None:
        if self.configured_rate:
            rate = Decimal(self.configured_rate)
            if rate <= 0:
                raise ExchangeRateError("USD_COP_RATE debe ser mayor que cero.")
            return rate
        try:
            response = self.session.get("https://open.er-api.com/v6/latest/USD", timeout=(5, 15))
            response.raise_for_status()
            rate = Decimal(str(response.json()["rates"]["COP"]))
            if rate <= 0:
                raise ValueError("tasa inválida")
            return rate
        except (requests.RequestException, KeyError, TypeError, ValueError):
            return None
