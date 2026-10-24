"""Pure models for the TaoSwap envelopes inspected in Patch 02B.

The selected row fields are required; name, identity and yields_as_of are
nullable. The five metrics are non-null JSON numbers in the published schema.
Unknown fields are ignored. Identity contents, hotkey syntax, metric units,
ranges and equivalence to old MinersUnion fields are intentionally unspecified.
No mechanism is selected and no freshness or collection completeness is inferred.
Dataclasses hold parsed data; validation is performed by the parse functions.
"""

from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import re


class ValidationError(ValueError):
    """Invalid input; messages contain field paths, never payload values."""


@dataclass(frozen=True)
class Subnet:
    netuid: int
    name: str | None


@dataclass(frozen=True)
class ValidatorSnapshot:
    netuid: int
    uid: int
    hotkey: str = field(repr=False)
    identity: dict | None = field(repr=False)
    stake: Decimal
    vtrust: Decimal
    dividends: Decimal
    take: Decimal
    childkey_take: Decimal


@dataclass(frozen=True)
class ValidatorPage:
    netuid: int
    records: tuple[ValidatorSnapshot, ...]
    page: int
    page_size: int
    total_count: int
    reachable_count: int
    total_pages: int
    has_next: bool
    has_previous: bool
    yields_as_of: str | None


def to_decimal(value, *, allow_string=False, path='value'):
    """Accept finite numbers; strings require explicit contract permission.

    Pass Decimal values produced by json.loads(..., parse_float=Decimal) to
    preserve JSON decimal digits exactly. Existing floats use Decimal(str(x)):
    this avoids extra binary expansion but cannot restore already lost digits.
    No rounding, unit conversion or sign/range constraints are applied.
    """
    if type(value) in (int, float) or isinstance(value, Decimal):
        text = str(value)
    elif allow_string and isinstance(value, str) and re.fullmatch(
        r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?', value
    ):
        text = value
    else:
        raise ValidationError(f'{path}: expected a finite number')
    try:
        result = Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValidationError(f'{path}: invalid decimal number') from None
    if not result.is_finite():
        raise ValidationError(f'{path}: expected a finite number')
    return result


def _object(value, path):
    if not isinstance(value, dict):
        raise ValidationError(f'{path}: expected an object')
    return value


def _array(value, path):
    if not isinstance(value, list):
        raise ValidationError(f'{path}: expected a list')
    return value


def _required(obj, key, path):
    if key not in obj:
        raise ValidationError(f'{path}.{key}: required field is missing')
    return obj[key]


def _integer(value, path, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValidationError(f'{path}: expected an integer >= {minimum}')
    return value


def _nullable_string(value, path):
    if value is not None and not isinstance(value, str):
        raise ValidationError(f'{path}: expected a string or null')
    return value


def parse_subnets(payload):
    """Parse results in source order; id is the API's integer netuid."""
    payload = _object(payload, 'subnets')
    rows = _array(_required(payload, 'results', 'subnets'), 'subnets.results')
    result = []
    seen = set()
    for index, row in enumerate(rows):
        path = f'subnets.results[{index}]'
        row = _object(row, path)
        netuid = _integer(_required(row, 'id', path), f'{path}.id')
        if netuid in seen:
            raise ValidationError(f'{path}.id: duplicate subnet ID')
        seen.add(netuid)
        name = _nullable_string(_required(row, 'name', path), f'{path}.name')
        result.append(Subnet(netuid=netuid, name=name))
    return tuple(result)


def _snapshot(row, netuid, path):
    row = _object(row, path)
    uid = _integer(_required(row, 'uid', path), f'{path}.uid')
    hotkey = _required(row, 'hotkey', path)
    if not isinstance(hotkey, str):
        raise ValidationError(f'{path}.hotkey: expected a string')
    identity = _required(row, 'identity', path)
    if identity is not None:
        _object(identity, f'{path}.identity')
    metrics = {
        key: to_decimal(_required(row, key, path), path=f'{path}.{key}')
        for key in ('stake', 'vtrust', 'dividends', 'take', 'childkey_take')
    }
    return ValidatorSnapshot(netuid, uid, hotkey, deepcopy(identity), **metrics)


def parse_validator_page(payload, requested_netuid):
    """Validate one page, including a truncated but internally consistent set.

    OpenAPI defines reachable_count as min(total_count, total_pages * page_size).
    It does not define exact page lengths, formulas for the navigation flags,
    or the empty-set page convention. Preserve those values without deriving
    them. A short or empty page is not proof of collection completeness.
    Parsing does not certify completeness or authorize fetching another page,
    including a page outside the documented offset guard.
    """
    requested_netuid = _integer(requested_netuid, 'requested_netuid')
    payload = _object(payload, 'validators')
    netuid = _integer(_required(payload, 'netuid', 'validators'), 'validators.netuid')
    if netuid != requested_netuid:
        raise ValidationError('validators.netuid: does not match request context')
    rows = _array(_required(payload, 'results', 'validators'), 'validators.results')
    yields_as_of = _nullable_string(
        _required(payload, 'yields_as_of', 'validators'), 'validators.yields_as_of'
    )
    path = 'validators.pagination'
    pagination = _object(_required(payload, 'pagination', 'validators'), path)
    numbers = {
        key: _integer(_required(pagination, key, path), f'{path}.{key}', minimum)
        for key, minimum in (
            ('page', 1), ('page_size', 1), ('total_count', 0),
            ('reachable_count', 0), ('total_pages', 0),
        )
    }
    flags = {}
    for key in ('has_next', 'has_previous'):
        value = _required(pagination, key, path)
        if type(value) is not bool:
            raise ValidationError(f'{path}.{key}: expected a boolean')
        flags[key] = value
    size = numbers['page_size']
    if size > 500:
        raise ValidationError(f'{path}.page_size: exceeds documented maximum')
    total, reachable, pages = (
        numbers['total_count'], numbers['reachable_count'], numbers['total_pages']
    )
    if reachable != min(total, pages * size):
        raise ValidationError(f'{path}.reachable_count: inconsistent with counts and page_size')
    if len(rows) > min(size, reachable):
        raise ValidationError('validators.results: exceeds page_size or reachable_count')
    records = tuple(
        _snapshot(row, netuid, f'validators.results[{index}]')
        for index, row in enumerate(rows)
    )
    return ValidatorPage(
        netuid=netuid, records=records, yields_as_of=yields_as_of, **numbers, **flags
    )
