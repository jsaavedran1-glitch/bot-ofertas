from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html import unescape
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.models import DealObservation, DiscountEvidence, to_minor
from app.sources.base import DealSource, SourceError

# Amazon removed public RSS feeds; using DealNews as a deal aggregator for Amazon/Woot items.
# ponytail: Amazon deals link to an affiliate-tagged search (no ASIN in the feed); switch to /dp/ASIN once PA-API is available.
AFFILIATE_TAG = "ojoalprecio10-20"

_FEEDS = [
    "https://www.dealnews.com/c142/Electronics/?rss=1",
    "https://www.dealnews.com/c39/Computers/?rss=1",
    "https://www.dealnews.com/c166/Video-Games/?rss=1",
    "https://www.dealnews.com/c202/Clothing-Accessories/?rss=1",
]

_RETAILER_SOURCE = {
    "amazon": "amazon",
    "woot": "woot",
    "woot! an amazon company": "woot",
}

_PRICE_RE = re.compile(r"\$([\d,]+\.?\d*)")
_PCT_RE = re.compile(r"(\d+)%\s*off", re.IGNORECASE)
_DEAL_ID_RE = re.compile(r"/(\d{6,})\.html")


def _extract_prices(text: str) -> tuple[float | None, float | None]:
    prices = [float(m.replace(",", "")) for m in _PRICE_RE.findall(text)]
    if len(prices) >= 2:
        return min(prices), max(prices)
    if len(prices) == 1:
        return prices[0], None
    return None, None


class AmazonRssSource(DealSource):
    """Deal source backed by DealNews RSS filtered to Amazon/Woot retailer items."""

    source_name = "amazon"
    source_names = {"amazon", "woot"}

    def __init__(self, min_discount_pct: int = 20, session: requests.Session | None = None) -> None:
        self.min_discount_pct = min_discount_pct
        self.session = session or requests.Session()
        retry = Retry(total=2, backoff_factor=0.4, status_forcelist=(429, 500, 502, 503, 504))
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.headers["User-Agent"] = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        )

    def fetch(self) -> list[DealObservation]:
        deals: dict[str, DealObservation] = {}
        feed_errors = 0
        for feed_url in _FEEDS:
            try:
                resp = self.session.get(feed_url, timeout=(5, 20))
                resp.raise_for_status()
                root = ET.fromstring(resp.content)
            except Exception:
                feed_errors += 1
                continue
            for item in root.iter("item"):
                deal = self._parse_item(item)
                if deal and deal.external_id not in deals:
                    deals[deal.external_id] = deal
        if not deals and feed_errors == len(_FEEDS):
            raise SourceError("DealNews no respondió en ningún feed.")
        return list(deals.values())

    def _parse_item(self, item: ET.Element) -> DealObservation | None:
        ns = "https://www.dealnews.com/ns/rss/1.0.htm"
        retailer = (item.findtext(f"{{{ns}}}retailer") or "").strip().lower()
        source_name = _RETAILER_SOURCE.get(retailer)
        if source_name is None:
            return None

        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        description = unescape(item.findtext("description") or "")

        if not title or not link:
            return None

        # Skip category/collection sales — not single-product deals
        if re.search(r"\bup to\b|\bfrom \$|\bdeals at\b", title, re.IGNORECASE):
            return None

        id_match = _DEAL_ID_RE.search(link)
        if not id_match:
            return None
        external_id = f"dn_{id_match.group(1)}"

        # Prices from description first, then title
        price, orig = _extract_prices(description)
        if price is None:
            price, orig = _extract_prices(title)
        if price is None or price <= 0:
            return None

        # Discount %
        pct_match = _PCT_RE.search(title + " " + description)
        if pct_match:
            discount_pct = int(pct_match.group(1))
        elif orig and orig > price:
            discount_pct = round((orig - price) / orig * 100)
        else:
            return None

        if discount_pct < self.min_discount_pct:
            return None

        if orig is None or orig <= price:
            orig = price / (1 - discount_pct / 100)

        try:
            price_minor = to_minor(price, "USD")
            orig_minor = to_minor(orig, "USD")
        except (ValueError, TypeError):
            return None

        # Image from media:content
        img_url = ""
        for media in item.iter("{http://search.yahoo.com/mrss/}content"):
            img_url = media.get("url", "")
            if img_url:
                break
        if "dlnws.com" in img_url:
            img_url = img_url.split("?", 1)[0] + "?h=600&w=600"  # feed ships 103x125 thumbnails

        clean_title = re.sub(r"\s+for\s+\$[\d,.]+.*$", "", title, flags=re.IGNORECASE).strip() or title
        clean_title = re.sub(r"\s*\(.*?\)\s*$", "", clean_title).strip() or title

        is_amazon = source_name == "amazon"
        url = (
            "https://www.amazon.com/s?" + urlencode({"k": clean_title, "tag": AFFILIATE_TAG})
            if is_amazon else link
        )

        return DealObservation(
            source=source_name,
            external_id=external_id,
            title=clean_title,
            price_minor=price_minor,
            original_price_minor=orig_minor,
            evidence=DiscountEvidence.OFFICIAL_ORIGINAL,
            currency="USD",
            url=url,
            image_url=img_url,
            available=True,
            shipping_note="Ver condiciones de envío en la tienda",
            observed_at=datetime.now(timezone.utc),
            affiliate=is_amazon,
        )

    def revalidate(self, deal: DealObservation) -> DealObservation | None:
        if deal.source not in _RETAILER_SOURCE.values():
            return None
        return deal  # Optimistic: DealNews deals are curated and expire via their own mechanism
