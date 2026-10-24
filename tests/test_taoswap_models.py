from copy import deepcopy
from decimal import Decimal
import json

import pytest

from taoswap_models import (
    Subnet, ValidationError, parse_subnets, parse_validator_page, to_decimal,
)


@pytest.fixture
def payload():
    return {
        'netuid': 2,
        'results': [{
            'uid': 0, 'hotkey': 'synthetic-hotkey',
            'identity': {'name': 'synthetic', 'additional': {'tags': []}},
            'stake': Decimal('123.456789012345678901'), 'vtrust': 0,
            'dividends': 0.25, 'take': 0, 'childkey_take': 0,
        }],
        'pagination': {
            'page': 1, 'page_size': 1, 'total_count': 1,
            'reachable_count': 1, 'total_pages': 1,
            'has_next': False, 'has_previous': False,
        },
        'yields_as_of': None,
    }


def test_subnet_normalization_and_nullable_name():
    data = {'results': [{'id': 0, 'name': None}, {'id': 2, 'name': 'synthetic'}]}
    before = deepcopy(data)
    assert parse_subnets(data) == (Subnet(0, None), Subnet(2, 'synthetic'))
    assert data == before
    assert parse_subnets({'results': []}) == ()


@pytest.mark.parametrize('netuid', [0, 2])
def test_duplicate_subnets_are_rejected_without_exposing_values(netuid):
    data = {'results': [
        {'id': netuid, 'name': 'synthetic-sensitive-first'},
        {'id': netuid, 'name': 'synthetic-sensitive-second'},
    ]}
    before = deepcopy(data)
    with pytest.raises(ValidationError) as error:
        parse_subnets(data)
    assert str(error.value) == 'subnets.results[1].id: duplicate subnet ID'
    assert data == before


@pytest.mark.parametrize('value', [True, False, -1, 1.5, 2.0, '2', None])
def test_invalid_ids(value, payload):
    with pytest.raises(ValidationError, match=r'\.id:'):
        parse_subnets({'results': [{'id': value, 'name': None}]})
    with pytest.raises(ValidationError, match='requested_netuid:'):
        parse_validator_page(payload, value)
    payload['netuid'] = value
    with pytest.raises(ValidationError, match=r'validators\.netuid:'):
        parse_validator_page(payload, 2)


@pytest.mark.parametrize('row', [{}, {'id': 2}, {'name': None}])
def test_missing_subnet_fields(row):
    with pytest.raises(ValidationError, match='required field is missing'):
        parse_subnets({'results': [row]})


@pytest.mark.parametrize('name', [2, False, {}, []])
def test_invalid_subnet_name(name):
    with pytest.raises(ValidationError, match=r'\.name:'):
        parse_subnets({'results': [{'id': 2, 'name': name}]})


@pytest.mark.parametrize('bad', [None, [], 'synthetic', True])
def test_invalid_envelopes(bad):
    with pytest.raises(ValidationError):
        parse_subnets(bad)
    with pytest.raises(ValidationError):
        parse_validator_page(bad, 2)


@pytest.mark.parametrize('bad', [None, {}, 'synthetic', True, [None], [[]]])
def test_invalid_results(bad, payload):
    with pytest.raises(ValidationError):
        parse_subnets({'results': bad})
    payload['results'] = bad
    with pytest.raises(ValidationError):
        parse_validator_page(payload, 2)


@pytest.mark.parametrize('key', ['netuid', 'results', 'pagination', 'yields_as_of'])
def test_missing_page_fields(key, payload):
    del payload[key]
    with pytest.raises(ValidationError, match=f'validators.{key}: required'):
        parse_validator_page(payload, 2)


def test_missing_subnet_results():
    with pytest.raises(ValidationError, match='subnets.results: required'):
        parse_subnets({})


@pytest.mark.parametrize('key', [
    'uid', 'hotkey', 'identity', 'stake', 'vtrust', 'dividends', 'take', 'childkey_take',
])
def test_missing_snapshot_fields(key, payload):
    del payload['results'][0][key]
    with pytest.raises(ValidationError, match=rf'\[0\].{key}: required'):
        parse_validator_page(payload, 2)


@pytest.mark.parametrize('value', [True, -1, 1.5, '0', None])
def test_invalid_uid(value, payload):
    payload['results'][0]['uid'] = value
    with pytest.raises(ValidationError, match=r'\.uid:'):
        parse_validator_page(payload, 2)


@pytest.mark.parametrize('key,value', [
    ('hotkey', None), ('hotkey', 2), ('hotkey', True),
    ('identity', []), ('identity', 'synthetic'), ('identity', False),
])
def test_invalid_hotkey_or_identity(key, value, payload):
    payload['results'][0][key] = value
    with pytest.raises(ValidationError, match=rf'\.{key}:'):
        parse_validator_page(payload, 2)


def test_snapshot_decimals_zero_identity_and_no_mutation(payload):
    before = deepcopy(payload)
    page = parse_validator_page(payload, 2)
    row = page.records[0]
    assert row.netuid == 2 and row.uid == 0
    assert row.stake == Decimal('123.456789012345678901')
    assert row.dividends == Decimal('0.25')
    for key in ('stake', 'vtrust', 'dividends', 'take', 'childkey_take'):
        assert isinstance(getattr(row, key), Decimal)
    assert row.vtrust == row.take == row.childkey_take == Decimal(0)
    assert not hasattr(row, 'chk_take')
    assert not hasattr(row, 'score_current')
    assert not hasattr(row, 'metrics_history')
    row.identity['additional']['tags'].append('copy-only')
    assert payload == before
    payload['results'][0]['identity'] = None
    assert parse_validator_page(payload, 2).records[0].identity is None


@pytest.mark.parametrize('value,expected', [
    (0, '0'), (-2, '-2'), (0.1, '0.1'),
    (Decimal('0.123456789012345678901'), '0.123456789012345678901'),
])
def test_decimal_numbers(value, expected):
    assert to_decimal(value) == Decimal(expected)


@pytest.mark.parametrize('value', [True, False, None, [], {}, float('nan'),
                                        float('inf'), float('-inf'), Decimal('NaN'),
                                        Decimal('sNaN'), Decimal('Infinity')])
def test_invalid_decimal_numbers(value):
    with pytest.raises(ValidationError):
        to_decimal(value)


@pytest.mark.parametrize('value', ['0', '-2.50', '1e-9'])
def test_decimal_strings_require_explicit_permission(value):
    with pytest.raises(ValidationError):
        to_decimal(value)
    assert to_decimal(value, allow_string=True) == Decimal(value)


@pytest.mark.parametrize('value', ['', 'NaN', 'Infinity', ' 1', '1_000', 'secret'])
def test_invalid_decimal_strings(value):
    with pytest.raises(ValidationError):
        to_decimal(value, allow_string=True)


@pytest.mark.parametrize('key', ['stake', 'vtrust', 'dividends', 'take', 'childkey_take'])
@pytest.mark.parametrize('value', [None, True, '0', float('nan'), float('inf')])
def test_invalid_metric_values(key, value, payload):
    payload['results'][0][key] = value
    with pytest.raises(ValidationError, match=rf'\.{key}:'):
        parse_validator_page(payload, 2)


def test_exact_json_decimal_parsing_without_http():
    decoded = json.loads('{"stake": 0.123456789012345678901}', parse_float=Decimal)
    assert to_decimal(decoded['stake']) == Decimal('0.123456789012345678901')


def test_response_netuid_must_match(payload):
    with pytest.raises(ValidationError, match='does not match request context'):
        parse_validator_page(payload, 5)


@pytest.mark.parametrize('key', [
    'page', 'page_size', 'total_count', 'reachable_count', 'total_pages',
    'has_next', 'has_previous',
])
def test_missing_pagination_fields(key, payload):
    del payload['pagination'][key]
    with pytest.raises(ValidationError, match='required field is missing'):
        parse_validator_page(payload, 2)


@pytest.mark.parametrize('changes', [
    {'page': 0}, {'page': True}, {'page_size': 0}, {'page_size': '1'}, {'page_size': 501},
    {'total_count': -1}, {'reachable_count': False}, {'total_pages': 0},
    {'reachable_count': 2}, {'has_next': 0}, {'has_previous': 1},
    {'total_count': 2, 'reachable_count': 2},
])
def test_invalid_pagination(changes, payload):
    payload['pagination'].update(changes)
    with pytest.raises(ValidationError):
        parse_validator_page(payload, 2)


@pytest.mark.parametrize('bad', [None, [], True])
def test_invalid_pagination_object(bad, payload):
    payload['pagination'] = bad
    with pytest.raises(ValidationError, match='pagination: expected an object'):
        parse_validator_page(payload, 2)


def test_partial_page_collection_and_truncation_are_valid(payload):
    payload['pagination'].update(total_count=10, reachable_count=10,
                                 total_pages=10, has_next=True)
    assert len(parse_validator_page(payload, 2).records) == 1
    payload['pagination'].update(total_count=10001, reachable_count=10000,
                                 total_pages=10000)
    assert parse_validator_page(payload, 2).reachable_count < 10001
    payload['pagination'].update(page=10000, has_next=False, has_previous=True)
    assert parse_validator_page(payload, 2).has_next is False


def test_short_last_page_is_valid(payload):
    payload['pagination'].update(page=2, page_size=2, total_count=3,
                                 reachable_count=3, total_pages=2, has_previous=True)
    assert len(parse_validator_page(payload, 2).records) == 1


@pytest.mark.parametrize('pages', [0, 1])
def test_valid_empty_page(payload, pages):
    payload['results'] = []
    payload['pagination'].update(total_count=0, reachable_count=0, total_pages=pages)
    assert parse_validator_page(payload, 2).records == ()


def test_empty_page_does_not_imply_empty_collection(payload):
    payload['results'] = []
    page = parse_validator_page(payload, 2)
    assert page.records == ()
    assert page.total_count == 1


def test_empty_out_of_range_page_is_not_arbitrarily_rejected(payload):
    payload['results'] = []
    payload['pagination'].update(page=2, has_previous=True)
    assert parse_validator_page(payload, 2).records == ()


def test_short_nonfinal_page_is_not_rejected(payload):
    payload['pagination'].update(page_size=2, total_count=4, reachable_count=4,
                                 total_pages=2, has_next=True)
    assert len(parse_validator_page(payload, 2).records) == 1


def test_total_pages_is_not_derived_from_total_count(payload):
    payload['pagination']['total_pages'] = 2
    page = parse_validator_page(payload, 2)
    assert page.total_pages == 2
    assert page.reachable_count == 1


@pytest.mark.parametrize('has_next,has_previous', [(True, False), (False, True)])
def test_navigation_flags_are_preserved_not_recomputed(has_next, has_previous, payload):
    payload['pagination'].update(has_next=has_next, has_previous=has_previous)
    page = parse_validator_page(payload, 2)
    assert page.has_next is has_next
    assert page.has_previous is has_previous


def test_offset_guard_can_end_pagination_before_total_count(payload):
    payload['pagination'].update(
        page=21, page_size=500, total_count=20000, reachable_count=10500,
        total_pages=21, has_next=False, has_previous=True,
    )
    page = parse_validator_page(payload, 2)
    assert (page.page - 1) * page.page_size == 10000
    assert page.reachable_count < page.total_count
    assert not page.has_next
    assert not hasattr(page, 'complete')
    assert not hasattr(page, 'is_complete')


@pytest.mark.parametrize('reachable,pages', [(2, 1), (0, 1), (1, 0)])
def test_documented_reachable_count_formula_is_enforced(reachable, pages, payload):
    payload['pagination'].update(reachable_count=reachable, total_pages=pages)
    with pytest.raises(ValidationError, match=r'pagination\.reachable_count:'):
        parse_validator_page(payload, 2)


def test_page_cannot_exceed_page_size(payload):
    payload['results'].append(deepcopy(payload['results'][0]))
    payload['pagination'].update(total_count=2, reachable_count=2,
                                 total_pages=2, has_next=True)
    with pytest.raises(ValidationError, match='exceeds page_size or reachable_count'):
        parse_validator_page(payload, 2)


def test_page_cannot_exceed_reachable_count(payload):
    payload['results'].append(deepcopy(payload['results'][0]))
    payload['pagination']['page_size'] = 2
    with pytest.raises(ValidationError, match='exceeds page_size or reachable_count'):
        parse_validator_page(payload, 2)


@pytest.mark.parametrize('value', [None, '', 'synthetic-date-without-timezone'])
def test_nullable_yields_as_of_is_preserved(value, payload):
    payload['yields_as_of'] = value
    assert parse_validator_page(payload, 2).yields_as_of == value


def test_invalid_yields_as_of(payload):
    payload['yields_as_of'] = 0
    with pytest.raises(ValidationError, match='yields_as_of:'):
        parse_validator_page(payload, 2)


def test_safe_errors_and_repr(payload):
    assert 'synthetic-hotkey' not in repr(parse_validator_page(payload, 2))
    payload['results'][0]['stake'] = 'synthetic-sensitive'
    with pytest.raises(ValidationError) as error:
        parse_validator_page(payload, 2)
    assert str(error.value) == 'validators.results[0].stake: expected a finite number'
    assert 'synthetic' not in str(error.value)
