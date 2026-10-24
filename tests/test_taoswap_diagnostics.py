from copy import deepcopy
from dataclasses import replace
from decimal import Decimal, localcontext
import json
import socket

import pytest
import requests

from taoswap_client import ValidatorCollection
from taoswap_collection import MultiSubnetCollection, SubnetCollection, SubnetFailure
from taoswap_diagnostics import (
    DiagnosticSerializationError, build_diagnostic_report, diagnostic_json,
)
from taoswap_models import Subnet, ValidatorSnapshot


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Real network operations are forbidden')
    monkeypatch.setattr(socket, 'socket', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(requests.Session, 'send', forbidden)


@pytest.fixture
def result():
    subnet = Subnet(2, None)
    record = ValidatorSnapshot(
        2, 0, 'synthetic-private-hotkey',
        {'name': 'synthetic-private-identity', 'cookies': 'synthetic-cookie'},
        Decimal('0.123456789012345678901'), Decimal('0'), Decimal('-0.00'),
        Decimal('-2.500'), Decimal('1E-20'),
    )
    collection = ValidatorCollection(
        (record, record), 1, 1, 2, 2, 1, 'unknown', 'repeated_observation',
    )
    return MultiSubnetCollection(
        (subnet,), (SubnetCollection(subnet, collection),), (), 1, 0, 2, 2, 1,
        False, 'unknown', 'global_completeness_not_guaranteed',
    )


def nested_keys(value):
    if isinstance(value, dict):
        return set(value).union(*(nested_keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(nested_keys(item) for item in value))
    return set()


def test_deterministic_json_and_round_trip(result):
    text = diagnostic_json(result)
    assert text.encode('ascii') == diagnostic_json(result).encode('ascii')
    assert json.loads(text) == build_diagnostic_report(result)
    assert text == json.dumps(json.loads(text), sort_keys=True, ensure_ascii=True,
                              separators=(',', ':'), allow_nan=False)


def test_exact_decimal_strings_and_original_metric_names(result):
    row = json.loads(diagnostic_json(result))['collections'][0]['records'][0]
    assert row == {
        'netuid': 2, 'uid': 0, 'stake': '0.123456789012345678901',
        'vtrust': '0', 'dividends': '-0.00', 'take': '-2.500', 'childkey_take': '1E-20',
    }
    assert Decimal(row['stake']) == result.collections[0].collection.records[0].stake


def test_decimal_context_does_not_change_output_or_get_mutated(result):
    expected = diagnostic_json(result)
    with localcontext() as context:
        context.capitals = 0
        context.prec = 2
        assert diagnostic_json(result) == expected
        assert context.capitals == 0 and context.prec == 2


def test_redaction_and_no_invented_fields(result):
    report = build_diagnostic_report(result)
    text = diagnostic_json(result)
    assert report['schema_version'] == 1 and report['kind'] == 'diagnostic'
    assert report['redacted_fields'] == ['hotkey', 'identity']
    assert not nested_keys(report).intersection({
        'hotkey', 'identity', 'cookies', 'credentials', 'token', 'response_body',
        'score_current', 'chk_take', 'metrics_history', 'complete', 'success',
        'verified_snapshot', 'timestamp', 'snapshot_id', 'source_version',
    })
    for private in ('synthetic-private-hotkey', 'synthetic-private-identity', 'synthetic-cookie'):
        assert private not in text


def test_duplicates_counters_and_nullable_name_are_preserved(result):
    report = build_diagnostic_report(result)
    assert report['processed_subnets'] == [{'netuid': 2, 'name': None}]
    local = report['collections'][0]
    assert local['subnet']['name'] is None
    assert len(local['records']) == 2 and local['records'][0] == local['records'][1]
    assert local['unique_observations'] == 1 and local['pages_received'] == 1
    assert report['total_records_available'] == report['total_records_observed'] == 2
    assert report['subnet_list_completeness_guaranteed'] is False


def test_empty_result():
    source = MultiSubnetCollection((), (), (), 0, 0, 0, 0, 0,
                                   False, 'unknown', 'empty_subnet_list')
    report = json.loads(diagnostic_json(source))
    assert report['processed_subnets'] == report['collections'] == report['errors'] == []
    assert report['status'] == 'unknown' and report['reason'] == 'empty_subnet_list'


def test_multiple_subnets_keep_source_order_and_statuses(result):
    second_subnet = Subnet(0, 'synthetic-root')
    second = SubnetCollection(second_subnet, ValidatorCollection(
        (), 1, 0, 5, 0, 0, 'incomplete', 'offset_truncated_collection',
    ))
    source = replace(
        result, processed_subnets=(result.processed_subnets[0], second_subnet),
        collections=result.collections + (second,), subnets_reported=2,
        status='incomplete', reason='subnet_collections_incomplete', total_pages_received=2,
    )
    report = json.loads(diagnostic_json(source))
    assert [subnet['netuid'] for subnet in report['processed_subnets']] == [2, 0]
    assert [item['subnet']['netuid'] for item in report['collections']] == [2, 0]
    assert [item['status'] for item in report['collections']] == ['unknown', 'incomplete']
    assert report['status'] == 'incomplete'


def test_error_and_partial_observations_are_distinct_from_available_records(result):
    failed = Subnet(5, 'synthetic-failed')
    source = replace(
        result, processed_subnets=result.processed_subnets + (failed,),
        errors=(SubnetFailure(failed, 'http', 500, 2, 7),), subnets_reported=2,
        total_records_observed=9, total_pages_received=3,
        status='incomplete', reason='subnet_errors',
    )
    report = json.loads(diagnostic_json(source))
    assert report['total_records_available'] == 2
    assert report['total_records_observed'] == 9 and report['total_pages_received'] == 3
    assert report['errors'] == [{
        'subnet': {'netuid': 5, 'name': 'synthetic-failed'}, 'category': 'http',
        'status_code': 500, 'pages_received': 2, 'records_observed': 7,
    }]
    timeout = replace(source, errors=(SubnetFailure(failed, 'timeout', None, 0, 0),))
    assert build_diagnostic_report(timeout)['errors'][0]['status_code'] is None


def test_no_input_mutation_and_no_shared_report_containers(result):
    before = deepcopy(result)
    first = build_diagnostic_report(result)
    first['processed_subnets'][0]['name'] = 'changed'
    first['collections'][0]['records'][0]['stake'] = 'changed'
    first['redacted_fields'].append('changed')
    second = build_diagnostic_report(result)
    assert result == before
    assert second['processed_subnets'][0]['name'] is None
    assert second['collections'][0]['records'][0]['stake'] != 'changed'
    assert second['redacted_fields'] == ['hotkey', 'identity']


def test_counters_are_not_recomputed_or_certified(result):
    source = replace(result, total_records_available=99, total_records_observed=123)
    report = build_diagnostic_report(source)
    assert report['total_records_available'] == 99 and report['total_records_observed'] == 123
    assert report['status'] == source.status == 'unknown'


@pytest.mark.parametrize('bad', [None, {}, object(), 'synthetic-sensitive'])
def test_invalid_root_types_are_safe(bad):
    with pytest.raises(DiagnosticSerializationError) as error:
        diagnostic_json(bad)
    assert str(error.value) == 'result: invalid type'


@pytest.mark.parametrize('changes', [
    {'total_records_available': True}, {'total_records_observed': Decimal('2')},
    {'total_pages_received': object()}, {'subnet_list_completeness_guaranteed': 0},
    {'processed_subnets': object()}, {'collections': [object()]}, {'errors': [object()]},
    {'processed_subnets': [Subnet(2, object())]}, {'status': 'complete'},
    {'reason': RuntimeError('synthetic-sensitive')}, {'reason': 'synthetic-sensitive body'},
])
def test_invalid_report_fields_are_rejected_safely(changes, result):
    with pytest.raises(DiagnosticSerializationError) as error:
        diagnostic_json(replace(result, **changes))
    assert 'synthetic-sensitive' not in str(error.value)


@pytest.mark.parametrize('bad', [0.1, True, None, object(), Decimal('NaN'),
                               Decimal('sNaN'), Decimal('Infinity'), Decimal('-Infinity')])
def test_invalid_or_nonfinite_metric_is_safe(bad, result):
    item = result.collections[0]
    record = replace(item.collection.records[0], stake=bad)
    collection = replace(item.collection, records=(record,))
    source = replace(result, collections=(replace(item, collection=collection),))
    with pytest.raises(DiagnosticSerializationError) as error:
        diagnostic_json(source)
    assert str(error.value).startswith('collections[0].records[0].stake:')


def test_nonserializable_redacted_fields_are_not_traversed(result):
    item = result.collections[0]
    record = replace(item.collection.records[0], hotkey=object(), identity={'secret': object()})
    collection = replace(item.collection, records=(record,))
    source = replace(result, collections=(replace(item, collection=collection),))
    assert len(json.loads(diagnostic_json(source))['collections'][0]['records']) == 1


@pytest.mark.parametrize('category,status', [
    ('synthetic-sensitive-body', 500), ('http', object()), ('http', True),
])
def test_error_fields_do_not_export_arbitrary_text(category, status, result):
    source = replace(result, errors=(SubnetFailure(Subnet(5, None), category, status, 0, 0),))
    with pytest.raises(DiagnosticSerializationError) as error:
        diagnostic_json(source)
    assert 'synthetic-sensitive' not in str(error.value)


def test_unknown_local_reason_fails_closed(result):
    item = result.collections[0]
    collection = replace(item.collection, reason='synthetic exception body')
    with pytest.raises(DiagnosticSerializationError) as error:
        diagnostic_json(replace(result, collections=(replace(item, collection=collection),)))
    assert str(error.value) == 'collections[0].reason: unsupported code'
