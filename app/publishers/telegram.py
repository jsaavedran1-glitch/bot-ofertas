"""Mirror deals to a public Telegram channel (Bot API). Optional: off when the token/chat aren't set."""
from __future__ import annotations

from decimal import Decimal
from html import escape
import json
from pathlib import Path

import requests

from app.models import DealObservation, from_minor, money, store_name, to_minor


class TelegramPublisher:
    def __init__(self, bot_token: str, chat_id: str, session: requests.Session | None = None) -> None:
        self.base = f"https://api.telegram.org/bot{bot_token}"
        self.chat_id = chat_id
        self.session = session or requests.Session()

    def send_photo(self, image: Path, caption: str) -> None:
        with image.open("rb") as handle:
            self._post("sendPhoto", data={"chat_id": self.chat_id, "caption": caption, "parse_mode": "HTML"},
                       files={"photo": (image.name, handle, "image/png")})

    def send_album(self, images: list[Path], caption: str) -> None:
        handles = [p.open("rb") for p in images]
        try:
            media = [{"type": "photo", "media": f"attach://p{i}"} for i in range(len(images))]
            media[0].update({"caption": caption, "parse_mode": "HTML"})
            self._post("sendMediaGroup", data={"chat_id": self.chat_id, "media": json.dumps(media)},
                       files={f"p{i}": (p.name, h, "image/png") for i, (p, h) in enumerate(zip(images, handles))})
        finally:
            for h in handles:
                h.close()

    def _post(self, method: str, **kwargs) -> None:
        # Never let the bot token reach logs or exceptions.
        try:
            response = self.session.post(f"{self.base}/{method}", timeout=(8, 60), **kwargs)
        except requests.RequestException:
            raise RuntimeError(f"Telegram {method}: no se pudo conectar.") from None
        if not response.ok:
            raise RuntimeError(f"Telegram {method}: HTTP {response.status_code}.")


def _price(minor: int, currency: str, rate: Decimal | None) -> str:
    if currency == "USD" and rate:
        return money(to_minor(from_minor(minor, "USD") * rate, "COP"), "COP")
    return money(minor, currency)


def deal_caption(deal: DealObservation, rate: Decimal | None) -> str:
    lines = [f"🔥 <b>{escape(deal.title)}</b>", f"🏪 {escape(store_name(deal.source))}"]
    before = f" (antes <s>{_price(deal.original_price_minor, deal.currency, rate)}</s>)" if deal.original_price_minor else ""
    lines.append(f"💥 <b>{_price(deal.price_minor, deal.currency, rate)}</b>{before} -{deal.discount_pct}%")
    lines.append(f'👉 <a href="{escape(deal.url, quote=True)}">Ver la oferta</a>')
    if deal.currency == "USD":
        lines.append("<i>Valor aproximado en COP; envío e impuestos se confirman en la tienda.</i>")
    if deal.affiliate:
        lines.append("<i>Enlace afiliado: podemos recibir una comisión sin costo para ti.</i>")
    return "\n".join(lines)[:1024]


def top_caption(deals: list[DealObservation], rate: Decimal | None) -> str:
    numbers = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣")
    lines = [f"🔥 <b>TOP {len(deals)} OFERTAS DE HOY</b>", ""]
    for n, d in zip(numbers, deals):
        title = d.title if len(d.title) <= 45 else d.title[:44] + "…"
        lines.append(f'{n} <a href="{escape(d.url, quote=True)}">{escape(title)}</a>')
        lines.append(f"   💥 <b>{_price(d.price_minor, d.currency, rate)}</b> -{d.discount_pct}% · {escape(store_name(d.source))}")
    return "\n".join(lines)[:1024]
