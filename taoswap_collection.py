"""Sequential, bounded orchestration; no client creation, publishing or retries.

With the accepted client, GET attempts <= 1 + max_subnets * max_pages,
including the subnet-list request and any failed page attempt. The existing
collector applies the tighter offset guard. Fake/custom clients must obey the
same one-call/one-request contract for this bound to describe HTTP traffic.
The supplied client remains caller-owned and is not closed here.
"""

from dataclasses import dataclass
from typing import Literal

from taoswap_client import ClientError, CollectionError, ValidatorCollection, collect_validators
from taoswap_models import Subnet


class CollectionStartError(Exception):
    """No subnet traversal started; only a safe category and status are exposed."""

    def __init__(self, category, status_code=None):
        super().__init__(f'Cannot start subnet collection: {category}')
        self.category = category
        self.status_code = status_code


@dataclass(frozen=True)
class SubnetCollection:
    subnet: Subnet
    collection: ValidatorCollection


@dataclass(frozen=True)
class SubnetFailure:
    subnet: Subnet
    category: str
    status_code: int | None
    pages_received: int
    records_observed: int


@dataclass(frozen=True)
class MultiSubnetCollection:
    processed_subnets: tuple[Subnet, ...]
    collections: tuple[SubnetCollection, ...]
    errors: tuple[SubnetFailure, ...]
    subnets_reported: int
    subnets_skipped: int
    total_records_available: int
    total_records_observed: int
    total_pages_received: int
    subnet_list_completeness_guaranteed: bool
    status: Literal['incomplete', 'unknown']
    reason: str


def _positive_integer(value, name):
    if type(value) is not int or value < 1:
        raise ValueError(f'{name}: expected a positive integer')


def _safe_error(error):
    categories = {'http', 'timeout', 'transport', 'json', 'validation', 'pagination'}
    category = error.kind if isinstance(error.kind, str) and error.kind in categories else 'client_error'
    status = error.status_code
    if type(status) is not int or not 100 <= status <= 599:
        status = None
    return category, status


def _checked_subnets(subnets):
    # Dataclass constructors do not validate, so check prepared models too.
    if not isinstance(subnets, (list, tuple)):
        raise CollectionStartError('invalid_subnet_list')
    seen = set()
    for subnet in subnets:
        if not isinstance(subnet, Subnet) or type(subnet.netuid) is not int or subnet.netuid < 0:
            raise CollectionStartError('invalid_subnet_model')
        if subnet.name is not None and not isinstance(subnet.name, str):
            raise CollectionStartError('invalid_subnet_model')
        if subnet.netuid in seen:
            raise CollectionStartError('duplicate_netuid')
        seen.add(subnet.netuid)
    return tuple(sorted(subnets, key=lambda subnet: subnet.netuid))


def collect_subnet_validators(client, *, max_subnets=10, max_pages=10, page_size=100):
    """Collect diagnostics, never a certified global snapshot.

    Available records come only from returned collection results. Observed
    records additionally include counts reported by CollectionError; their
    partial rows are unavailable through the accepted collector interface.
    Counts are observations, not distinct global validators. Page counts cover
    successfully parsed pages, not failed GET attempts.
    Known ClientError failures are isolated; unexpected programming errors
    propagate rather than being disguised as ordinary API failures.
    """
    for name, value in (('max_subnets', max_subnets), ('max_pages', max_pages), ('page_size', page_size)):
        _positive_integer(value, name)
    if page_size > 500:
        raise ValueError('page_size: exceeds documented maximum')
    try:
        subnets = client.fetch_subnets()
    except ClientError as error:
        category, status = _safe_error(error)
        raise CollectionStartError(category, status) from None
    subnets = _checked_subnets(subnets)
    selected = subnets[:max_subnets]
    collections, errors = [], []
    for subnet in selected:
        try:
            collection = collect_validators(
                client, subnet.netuid, page_size=page_size, max_pages=max_pages,
            )
        except ClientError as error:
            category, status = _safe_error(error)
            pages = error.pages_received if isinstance(error, CollectionError) else 0
            observed = error.records_received if isinstance(error, CollectionError) else 0
            errors.append(SubnetFailure(subnet, category, status, pages, observed))
        else:
            collections.append(SubnetCollection(subnet, collection))

    available = sum(len(item.collection.records) for item in collections)
    observed = available + sum(error.records_observed for error in errors)
    pages = sum(item.collection.pages_received for item in collections)
    pages += sum(error.pages_received for error in errors)
    skipped = len(subnets) - len(selected)
    if errors:
        status, reason = 'incomplete', 'subnet_errors'
    elif skipped:
        status, reason = 'incomplete', 'max_subnets_reached'
    elif any(item.collection.status == 'incomplete' for item in collections):
        status, reason = 'incomplete', 'subnet_collections_incomplete'
    elif not subnets:
        status, reason = 'unknown', 'empty_subnet_list'
    elif not available:
        status, reason = 'unknown', 'no_validator_records'
    else:
        status, reason = 'unknown', 'global_completeness_not_guaranteed'
    return MultiSubnetCollection(
        processed_subnets=selected, collections=tuple(collections), errors=tuple(errors),
        subnets_reported=len(subnets), subnets_skipped=skipped,
        total_records_available=available, total_records_observed=observed,
        total_pages_received=pages, subnet_list_completeness_guaranteed=False,
        status=status, reason=reason,
    )
