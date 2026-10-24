from copy import deepcopy
from decimal import Decimal
import socket

import pytest
import requests

import taoswap_collection
from taoswap_client import ClientError, ValidatorCollection
from taoswap_collection import CollectionStartError, collect_subnet_validators
from taoswap_models import Subnet, ValidatorPage, ValidatorSnapshot


class FakeClient:
    def __init__(self, subnets, pages=None):
        self.subnets = subnets
        self.pages = pages or {}
        self.calls = []

    def fetch_subnets(self):
        self.calls.append(('subnets',))
        if isinstance(self.subnets, Exception):
            raise self.subnets
        return self.subnets

    def fetch_validator_page(self, netuid, *, page, page_size):
        self.calls.append(('validators', netuid, page, page_size))
        result = self.pages[(netuid, page)]
        if isinstance(result, Exception):
            raise result
        assert result.page_size == page_size
        return result


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Real network operations are forbidden')
    monkeypatch.setattr(socket, 'socket', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(requests.Session, 'send', forbidden)


def snapshot(netuid, uid=0, identity=None):
    return ValidatorSnapshot(
        netuid, uid, 'synthetic-hotkey', identity,
        Decimal('0.123456789012345678901'), Decimal(0), Decimal(0), Decimal(0), Decimal(0),
    )


def one_page(netuid, records=None):
    records = (snapshot(netuid),) if records is None else records
    return ValidatorPage(netuid, records, 1, 1, len(records), len(records), 1,
                         False, False, None)


def test_deterministic_order_and_input_preservation():
    subnets = [Subnet(5, 'synthetic-five'), Subnet(0, None), Subnet(2, 'synthetic-two')]
    pages = {(subnet.netuid, 1): one_page(subnet.netuid) for subnet in subnets}
    before = deepcopy((subnets, pages))
    client = FakeClient(subnets, pages)
    result = collect_subnet_validators(client, page_size=1)
    assert [subnet.netuid for subnet in result.processed_subnets] == [0, 2, 5]
    assert client.calls == [('subnets',), ('validators', 0, 1, 1),
                            ('validators', 2, 1, 1), ('validators', 5, 1, 1)]
    assert (subnets, pages) == before
    assert result.total_records_available == result.total_records_observed == 3
    assert result.total_pages_received == 3
    assert all(item.collection.status == 'unknown' for item in result.collections)
    assert result.status == 'unknown' and not result.subnet_list_completeness_guaranteed


def test_empty_list_has_no_validator_traversal():
    client = FakeClient(())
    result = collect_subnet_validators(client)
    assert client.calls == [('subnets',)]
    assert not result.processed_subnets and not result.collections and not result.errors
    assert result.total_records_available == result.total_records_observed == 0
    assert result.total_pages_received == 0
    assert result.status == 'unknown' and result.reason == 'empty_subnet_list'


@pytest.mark.parametrize('netuid', [0, 2])
def test_duplicate_models_even_beyond_limit_stop_before_traversal(netuid):
    client = FakeClient([Subnet(netuid, None), Subnet(netuid, 'synthetic-sensitive')])
    with pytest.raises(CollectionStartError) as error:
        collect_subnet_validators(client, max_subnets=1)
    assert error.value.category == 'duplicate_netuid'
    assert 'synthetic' not in str(error.value)
    assert client.calls == [('subnets',)]


@pytest.mark.parametrize('subnets', [None, iter(()), [None], [Subnet(True, None)],
                                  [Subnet(-1, None)], [Subnet(2, 5)]])
def test_invalid_prepared_subnet_models(subnets):
    client = FakeClient(subnets)
    with pytest.raises(CollectionStartError):
        collect_subnet_validators(client)
    assert client.calls == [('subnets',)]


def test_list_failure_is_fatal_safe_and_not_retried():
    client = FakeClient(ClientError('synthetic-secret-body', kind='http', status_code=503))
    with pytest.raises(CollectionStartError) as error:
        collect_subnet_validators(client)
    assert error.value.category == 'http' and error.value.status_code == 503
    assert 'synthetic' not in str(error.value) and error.value.__suppress_context__
    assert client.calls == [('subnets',)]


def test_subnet_failure_preserves_other_results_and_partial_diagnostics():
    pages = {
        (0, 1): one_page(0),
        (2, 1): ValidatorPage(2, (snapshot(2),), 1, 1, 2, 2, 2, True, False, None),
        (2, 2): ClientError('synthetic-secret', kind='timeout'),
        (5, 1): one_page(5),
    }
    client = FakeClient([Subnet(5, None), Subnet(2, None), Subnet(0, None)], pages)
    result = collect_subnet_validators(client, page_size=1, max_pages=2)
    assert [item.subnet.netuid for item in result.collections] == [0, 5]
    assert len(result.errors) == 1
    error = result.errors[0]
    assert error.subnet.netuid == 2 and error.category == 'timeout'
    assert error.pages_received == error.records_observed == 1
    assert result.total_records_available == 2
    assert result.total_records_observed == result.total_pages_received == 3
    assert result.status == 'incomplete' and result.reason == 'subnet_errors'
    assert client.calls == [('subnets',), ('validators', 0, 1, 1),
                            ('validators', 2, 1, 1), ('validators', 2, 2, 1),
                            ('validators', 5, 1, 1)]


def test_incomplete_and_unknown_are_preserved():
    pages = {(0, 1): one_page(0),
             (2, 1): ValidatorPage(2, (snapshot(2),), 1, 1, 3, 1, 1, False, False, None)}
    result = collect_subnet_validators(FakeClient([Subnet(0, None), Subnet(2, None)], pages),
                                       page_size=1)
    assert [item.collection.status for item in result.collections] == ['unknown', 'incomplete']
    assert result.collections[1].collection.reason == 'offset_truncated_collection'
    assert result.status == 'incomplete' and result.reason == 'subnet_collections_incomplete'


def test_zero_validators_is_distinct_from_empty_list_and_api_failure():
    client = FakeClient([Subnet(2, None)], {(2, 1): one_page(2, ())})
    result = collect_subnet_validators(client, page_size=1)
    assert len(result.collections) == 1 and not result.errors
    assert result.total_records_available == result.total_records_observed == 0
    assert result.total_pages_received == 1
    assert result.status == 'unknown' and result.reason == 'no_validator_records'


def test_max_subnets_selects_sorted_prefix_and_limits_calls():
    subnets = [Subnet(5, None), Subnet(2, None), Subnet(0, None)]
    client = FakeClient(subnets, {(0, 1): one_page(0), (2, 1): one_page(2)})
    result = collect_subnet_validators(client, max_subnets=2, max_pages=1, page_size=1)
    assert [subnet.netuid for subnet in result.processed_subnets] == [0, 2]
    assert result.subnets_reported == 3 and result.subnets_skipped == 1
    assert result.status == 'incomplete' and result.reason == 'max_subnets_reached'
    assert len(client.calls) == 1 + 2 * 1


@pytest.mark.parametrize('name', ['max_subnets', 'max_pages', 'page_size'])
@pytest.mark.parametrize('value', [0, -1, True, False, 1.5, '1'])
def test_invalid_limits_before_any_client_calls(name, value):
    client = FakeClient(())
    with pytest.raises(ValueError):
        collect_subnet_validators(client, **{name: value})
    assert client.calls == []


def test_invalid_page_size_before_any_client_calls():
    client = FakeClient(())
    with pytest.raises(ValueError):
        collect_subnet_validators(client, page_size=501)
    assert not client.calls


def test_decimal_identity_and_records_are_not_reinterpreted():
    identity = {'name': 'synthetic', 'additional': {'tags': []}}
    record = snapshot(2, identity=identity)
    source = one_page(2, (record,))
    before = deepcopy(source)
    result = collect_subnet_validators(FakeClient([Subnet(2, None)], {(2, 1): source}),
                                       page_size=1)
    observed = result.collections[0].collection.records[0]
    assert observed is record and observed.identity is identity
    assert observed.stake is record.stake
    assert observed.stake == Decimal('0.123456789012345678901')
    assert source == before


def test_nested_complete_still_cannot_certify_global_completeness(monkeypatch):
    collection = ValidatorCollection((), 1, 0, 0, 0, 1, 'complete', 'synthetic')
    monkeypatch.setattr(taoswap_collection, 'collect_validators', lambda *a, **kw: collection)
    result = collect_subnet_validators(FakeClient([Subnet(2, None)]))
    assert result.collections[0].collection is collection
    assert result.status == 'unknown' and not result.subnet_list_completeness_guaranteed


def test_full_request_budget_including_failed_attempts():
    subnets = [Subnet(0, None), Subnet(2, None)]
    pages = {}
    for subnet in subnets:
        pages[(subnet.netuid, 1)] = ValidatorPage(
            subnet.netuid, (snapshot(subnet.netuid),), 1, 1, 2, 2, 2, True, False, None,
        )
        pages[(subnet.netuid, 2)] = ClientError('synthetic', kind='http', status_code=500)
    client = FakeClient(subnets, pages)
    result = collect_subnet_validators(client, max_subnets=2, max_pages=2, page_size=1)
    assert len(client.calls) == 1 + 2 * 2
    assert result.total_pages_received == result.total_records_observed == 2
    assert result.total_records_available == 0 and len(result.errors) == 2


def test_max_pages_is_forwarded_to_existing_collector():
    subnets = [Subnet(0, None), Subnet(2, None)]
    pages = {
        (subnet.netuid, 1): ValidatorPage(
            subnet.netuid, (snapshot(subnet.netuid),), 1, 1, 3, 3, 3, True, False, None,
        ) for subnet in subnets
    }
    client = FakeClient(subnets, pages)
    result = collect_subnet_validators(client, max_subnets=2, max_pages=1, page_size=1)
    assert len(client.calls) == 1 + 2 * 1
    assert result.total_pages_received == 2 and result.status == 'incomplete'
    assert all(item.collection.reason == 'max_pages_reached' for item in result.collections)


def test_unrecognized_error_fields_cannot_leak_into_diagnostics():
    client = FakeClient(ClientError('synthetic-secret', kind='synthetic-secret',
                                   status_code='synthetic-secret'))
    with pytest.raises(CollectionStartError) as error:
        collect_subnet_validators(client)
    assert error.value.category == 'client_error' and error.value.status_code is None
    assert 'synthetic' not in str(error.value)
