"""Tests for controller.pricing."""

import json
from decimal import Decimal

from controller.pricing import CostEstimator

_SAMPLE_PRICE_LIST = [
    json.dumps(
        {
            "terms": {
                "OnDemand": {
                    "ABC.JRTCKXETXF": {
                        "priceDimensions": {
                            "ABC.JRTCKXETXF.6YS6EN2CT7": {
                                "pricePerUnit": {"USD": "0.0800000000"},
                            }
                        }
                    }
                }
            }
        }
    )
]


class _FakePricingClient:
    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc
        self.calls = 0

    def get_products(self, **kwargs):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self._response


class _FakeSession:
    def __init__(self, client):
        self._client = client

    def client(self, service_name, **kwargs):
        assert service_name == "pricing"
        return self._client


def test_price_per_gb_month_parses_a_successful_response():
    client = _FakePricingClient(response={"PriceList": _SAMPLE_PRICE_LIST})
    estimator = CostEstimator(_FakeSession(client))

    price = estimator.price_per_gb_month("us-east-1", "gp3")

    assert price == Decimal("0.08")


def test_price_per_gb_month_caches_a_successful_lookup():
    client = _FakePricingClient(response={"PriceList": _SAMPLE_PRICE_LIST})
    estimator = CostEstimator(_FakeSession(client))

    estimator.price_per_gb_month("us-east-1", "gp3")
    estimator.price_per_gb_month("us-east-1", "gp3")

    assert client.calls == 1


def test_price_per_gb_month_returns_none_and_does_not_raise_on_api_failure():
    client = _FakePricingClient(exc=RuntimeError("AccessDeniedException"))
    estimator = CostEstimator(_FakeSession(client))

    assert estimator.price_per_gb_month("us-east-1", "gp3") is None


def test_price_per_gb_month_returns_none_when_no_sku_matches():
    client = _FakePricingClient(response={"PriceList": []})
    estimator = CostEstimator(_FakeSession(client))

    assert estimator.price_per_gb_month("us-east-1", "gp3") is None


def test_price_per_gb_month_does_not_hammer_the_api_after_a_failure():
    client = _FakePricingClient(exc=RuntimeError("Throttling"))
    estimator = CostEstimator(_FakeSession(client))

    estimator.price_per_gb_month("us-east-1", "gp3")
    estimator.price_per_gb_month("us-east-1", "gp3")

    assert client.calls == 1


def test_price_per_gb_month_is_none_for_a_volume_with_no_known_type():
    estimator = CostEstimator(_FakeSession(_FakePricingClient()))

    assert estimator.price_per_gb_month("us-east-1", None) is None
