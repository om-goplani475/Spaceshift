"""Best-effort monthly EBS cost estimates via the AWS Pricing API.

Dashboard-render-time only - never called from discovery or resize, so a
slow or failing Pricing API can never affect an actual resize decision.
Prices are public AWS list-price catalog data, not account data, so the
client authenticates as the controller's own base identity (see
aws_session.build_base_session), not a per-account assumed role - no
customer has to grant pricing:GetProducts on their cross-account trust
role for a cosmetic estimate.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from controller.aws_session import RETRY_CONFIG

logger = logging.getLogger(__name__)

# The Pricing API's GetProducts endpoint only exists in us-east-1,
# regardless of which region the priced resource actually lives in.
_PRICING_API_REGION = "us-east-1"

_POSITIVE_CACHE_TTL = timedelta(hours=24)
_NEGATIVE_CACHE_TTL = timedelta(hours=1)


@dataclass
class _CacheEntry:
    price: Decimal | None
    fetched_at: datetime


class CostEstimator:
    """Caches $/GB-month by (region, volume_type), fetched from the AWS
    Pricing API on demand. Every public method is best-effort: on any
    failure (missing permission, throttling, no matching SKU, malformed
    response) it logs once per key and returns None - it never raises."""

    def __init__(self, base_session, cache_ttl: timedelta = _POSITIVE_CACHE_TTL) -> None:
        self._base_session = base_session
        self._cache_ttl = cache_ttl
        self._cache: dict[tuple[str, str], _CacheEntry] = {}
        self._warned: set[tuple[str, str]] = set()
        self._lock = threading.Lock()
        self._client = None

    def price_per_gb_month(self, region: str, volume_type: str | None) -> Decimal | None:
        if not region or not volume_type:
            return None
        key = (region, volume_type)
        now = datetime.now(UTC)
        with self._lock:
            entry = self._cache.get(key)
            if entry is not None:
                ttl = self._cache_ttl if entry.price is not None else _NEGATIVE_CACHE_TTL
                if now - entry.fetched_at < ttl:
                    return entry.price

        price = self._fetch_price(region, volume_type)
        with self._lock:
            self._cache[key] = _CacheEntry(price=price, fetched_at=now)
        return price

    def _fetch_price(self, region: str, volume_type: str) -> Decimal | None:
        key = (region, volume_type)
        try:
            client = self._get_client()
            response = client.get_products(
                ServiceCode="AmazonEC2",
                Filters=[
                    {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Storage"},
                    {"Type": "TERM_MATCH", "Field": "volumeApiName", "Value": volume_type},
                    {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
                ],
                MaxResults=1,
            )
            price = _parse_price(response.get("PriceList", []))
        except Exception:
            self._warn_once(
                key,
                "could not fetch EBS price for region=%s volume_type=%s (missing "
                "pricing:GetProducts? throttled? unrecognized type/region?) - cost estimates "
                "will omit this volume",
            )
            return None

        if price is None:
            self._warn_once(key, "no AWS Pricing API match for region=%s volume_type=%s")
        return price

    def _warn_once(self, key: tuple[str, str], message: str) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        logger.warning(message, *key)

    def _get_client(self):
        if self._client is None:
            self._client = self._base_session.client("pricing", region_name=_PRICING_API_REGION, config=RETRY_CONFIG)
        return self._client


def _parse_price(price_list: list[str]) -> Decimal | None:
    if not price_list:
        return None
    try:
        product = json.loads(price_list[0])
        on_demand = product["terms"]["OnDemand"]
        term = next(iter(on_demand.values()))
        dimension = next(iter(term["priceDimensions"].values()))
        return Decimal(dimension["pricePerUnit"]["USD"])
    except (KeyError, StopIteration, IndexError, TypeError, ValueError, InvalidOperation, json.JSONDecodeError):
        return None
