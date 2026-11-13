"""PlayStation (PSN) gift card deals at Newegg, read from DealNews' Newegg feed.

Newegg blocks automated requests, so the deals come from DealNews, which lists each Newegg promotion with
its price, card value and promo code. Only fresh items count (gift card promos are short), and a deal is
re-checked against the feed before it is published. These codes only work on U.S. PSN accounts: every
post says so.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
import re
import xml.etree.ElementTree as ET

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.models import DealObservation, DiscountEvidence, to_minor
from app.sources.base import DealSource, SourceError

FEED = "https://www.dealnews.com/s504/Newegg/?rss=1"
MAX_AGE_HOURS = 72
REGION_NOTE = "Código digital por correo · válida solo en cuentas PSN de Estados Unidos"

_NS = "https://www.dealnews.com/ns/rss/1.0.htm"
_PLAYSTATION = re.compile(r"\b(playstation|psn|ps plus|ps5)\b", re.IGNORECASE)
_CARD = re.compile(r"gift ?card|e-?gift|store card|membership|wallet", re.IGNORECASE)
_VALUE_FOR_PRICE = re.compile(r"\$([\d,]+(?:\.\d+)?)\D[^$]*?\bfor \$([\d,]+(?:\.\d+)?)", re.IGNORECASE)
_PROMO_CODE = re.compile(r'(?:promo |coupon )?code\s+"([A-Z0-9-]{4,20})"', re.IGNORECASE)
_DEAL_ID = re.compile(r"/(\d{6,})\.html")


def _usd(text: str) -> float:
    return float(text.replace(",", ""))


class NeweggPlayStationCardSource(DealSource):
    source_name = "newegg"
    source_names = {"newegg"}
    min_discount_pct = 5  # gift cards rarely drop more than 10-15%: any real saving is news
    min_savings_cop = 10_000

    def __init__(self, session: requests.Session | None = None, now=None) -> None:
        self.session = session or requests.Session()
        self.session.mount("https://", HTTPAdapter(max_retries=Retry(total=2, backoff_factor=0.4,
                                                                     status_forcelist=(429, 500, 502, 503, 504))))
        self.session.headers["User-Agent"] = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        )
        self.now = now or (lambda: datetime.now(timezone.utc))

    def fetch(self) -> list[DealObservation]:
        try:
            resp = self.session.get(FEED, timeout=(5, 20))
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
        except (requests.RequestException, ET.ParseError) as exc:
            raise SourceError(f"DealNews (Newegg) no respondió: {type(exc).__name__}") from None
        return [d for item in root.iter("item") if (d := self._parse(item))]

    def revalidate(self, deal: DealObservation) -> DealObservation | None:
        """Still listed, still fresh and at the same price."""
        if deal.source != self.source_name:
            return None
        try:
            current = {d.external_id: d for d in self.fetch()}
        except SourceError:
            return None
        return current.get(deal.external_id)

    def _parse(self, item: ET.Element) -> DealObservation | None:
        if (item.findtext(f"{{{_NS}}}retailer") or "").strip().lower() != "newegg":
            return None
        title = (item.findtext("title") or "").strip()
        description = re.sub(r"<[^>]+>", " ", unescape(item.findtext("description") or ""))
        if not (_PLAYSTATION.search(title) and _CARD.search(title)) or "expired" in (title + description).lower():
            return None
        try:
            published = parsedate_to_datetime(item.findtext("pubDate") or "")
        except (TypeError, ValueError):
            return None
        if self.now() - published > timedelta(hours=MAX_AGE_HOURS):
            return None
        match = _VALUE_FOR_PRICE.search(title)
        link = (item.findtext("link") or "").split("?", 1)[0]
        deal_id = _DEAL_ID.search(link)
        if not match or not deal_id:
            return None
        value, price = _usd(match.group(1)), _usd(match.group(2))
        if price <= 0 or value <= price:
            return None
        image = ""
        for media in item.iter("{http://search.yahoo.com/mrss/}content"):
            image = (media.get("url") or "").split("?", 1)[0]
            if image:
                image += "?h=600&w=600"
                break
        code = _PROMO_CODE.search(description)
        note = (f"Con el código {code.group(1)} · " if code else "") + REGION_NOTE
        clean_title = re.sub(r"\s*\+\s*(email|digital) delivery.*$", "", title, flags=re.IGNORECASE)
        clean_title = re.sub(r"\s+for\s+\$[\d,.]+$", "", clean_title, flags=re.IGNORECASE).strip()
        return DealObservation(
            source=self.source_name,
            external_id=f"dn_{deal_id.group(1)}",
            title=clean_title,
            price_minor=to_minor(price, "USD"),
            original_price_minor=to_minor(value, "USD"),  # the balance you get, not a former price
            evidence=DiscountEvidence.GIFT_CARD_VALUE,
            currency="USD",
            url=link,
            image_url=image,
            available=True,
            shipping_note=note,
            observed_at=self.now(),
        )
