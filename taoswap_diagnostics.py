"""Pure schema-v1 diagnostic serialization, with no I/O or snapshot claims.

Only explicitly listed fields are exported. Hotkey and identity are never read.
Reason/category codes are limited to those emitted by the accepted collectors;
new codes require review rather than exporting arbitrary exception text.
Type checks protect serialization, not collection completeness or consistency.
Counters, statuses, array order and duplicate observations are not recomputed.
"""

from decimal import Decimal, localcontext
import json

from taoswap_client import ValidatorCollection
from taoswap_collection import MultiSubnetCollection, SubnetCollection, SubnetFailure
from taoswap_models import Subnet, ValidatorSnapshot


GLOBAL_REASONS = frozenset({
    'subnet_errors', 'max_subnets_reached', 'subnet_collections_incomplete',
    'empty_subnet_list', 'no_validator_records', 'global_completeness_not_guaranteed',
})
LOCAL_REASONS = frozenset({
    'pagination_metadata_changed', 'repeated_page', 'repeated_observation',
    'identity_association_changed', 'more_observations_than_total',
    'navigation_metadata_disagree', 'offset_truncated_collection',
    'fewer_observations_than_total', 'offset_pagination_has_no_snapshot_guarantee',
    'short_intermediate_page', 'offset_guard_reached', 'max_pages_reached',
})
ERROR_CATEGORIES = frozenset({
    'http', 'timeout', 'transport', 'json', 'validation', 'pagination', 'client_error',
})
METRICS = ('stake', 'vtrust', 'dividends', 'take', 'childkey_take')


class DiagnosticSerializationError(ValueError):
    """Safe field-path errors, without offending values or object reprs."""


def _typed(value, expected, path):
    if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
        raise DiagnosticSerializationError(f'{path}: invalid type')
    return value


def _nullable(value, expected, path):
    return None if value is None else _typed(value, expected, path)


def _sequence(value, path):
    return _typed(value, (list, tuple), path)


def _code(value, choices, path):
    _typed(value, str, path)
    if value not in choices:
        raise DiagnosticSerializationError(f'{path}: unsupported code')
    return value


def _counters(obj, names, path):
    return {name: _typed(getattr(obj, name), int, f'{path}.{name}') for name in names}


def _subnet(subnet, path):
    _typed(subnet, Subnet, path)
    return {
        'netuid': _typed(subnet.netuid, int, f'{path}.netuid'),
        'name': _nullable(subnet.name, str, f'{path}.name'),
    }


def _record(record, path):
    _typed(record, ValidatorSnapshot, path)
    result = _counters(record, ('netuid', 'uid'), path)
    for name in METRICS:
        value = _typed(getattr(record, name), Decimal, f'{path}.{name}')
        if not value.is_finite():
            raise DiagnosticSerializationError(f'{path}.{name}: non-finite Decimal')
        with localcontext() as context:
            context.capitals = 1
            result[name] = str(value)
    return result


def _collection(item, path):
    _typed(item, SubnetCollection, path)
    collection = _typed(item.collection, ValidatorCollection, f'{path}.collection')
    rows = _sequence(collection.records, f'{path}.records')
    return {
        'subnet': _subnet(item.subnet, f'{path}.subnet'),
        'status': _code(collection.status, {'complete', 'incomplete', 'unknown'}, f'{path}.status'),
        'reason': _code(collection.reason, LOCAL_REASONS, f'{path}.reason'),
        **_counters(collection, (
            'pages_received', 'unique_observations', 'total_count',
            'reachable_count', 'total_pages',
        ), path),
        'records': [_record(row, f'{path}.records[{index}]') for index, row in enumerate(rows)],
    }


def _error(error, path):
    _typed(error, SubnetFailure, path)
    return {
        'subnet': _subnet(error.subnet, f'{path}.subnet'),
        'category': _code(error.category, ERROR_CATEGORIES, f'{path}.category'),
        'status_code': _nullable(error.status_code, int, f'{path}.status_code'),
        **_counters(error, ('pages_received', 'records_observed'), path),
    }


def build_diagnostic_report(result: MultiSubnetCollection) -> dict:
    """Build a fresh JSON-compatible report; redacted fields are not traversed.

    Finite Decimal metrics use str(Decimal) with fixed exponent capitalization,
    preserving digits, scale and signed zero without float conversion.
    This function does not validate pagination or certify the supplied claims.
    """
    _typed(result, MultiSubnetCollection, 'result')
    subnets = _sequence(result.processed_subnets, 'processed_subnets')
    collections = _sequence(result.collections, 'collections')
    errors = _sequence(result.errors, 'errors')
    return {
        'schema_version': 1,
        'kind': 'diagnostic',
        'redacted_fields': ['hotkey', 'identity'],
        'status': _code(result.status, {'incomplete', 'unknown'}, 'status'),
        'reason': _code(result.reason, GLOBAL_REASONS, 'reason'),
        'subnet_list_completeness_guaranteed': _typed(
            result.subnet_list_completeness_guaranteed, bool,
            'subnet_list_completeness_guaranteed',
        ),
        **_counters(result, (
            'subnets_reported', 'subnets_skipped', 'total_records_available',
            'total_records_observed', 'total_pages_received',
        ), 'result'),
        'processed_subnets': [_subnet(subnet, f'processed_subnets[{index}]')
                              for index, subnet in enumerate(subnets)],
        'collections': [_collection(item, f'collections[{index}]')
                        for index, item in enumerate(collections)],
        'errors': [_error(error, f'errors[{index}]') for index, error in enumerate(errors)],
    }


def diagnostic_json(result: MultiSubnetCollection) -> str:
    """Return deterministic ASCII JSON, preserving all array ordering."""
    return json.dumps(
        build_diagnostic_report(result), sort_keys=True, ensure_ascii=True,
        separators=(',', ':'), allow_nan=False,
    )
