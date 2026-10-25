"""Bounded TaoSwap reads; no integration with the legacy runtime or writer.

The earlier inspected contract specifies page_size <= 500 and offset <= 10000.
Its exact snapshot is not bundled; current live API compatibility is unverified.
Collector stability checks are defensive policy, not additional server guarantees.
(netuid, uid, hotkey) identifies an observation, not a proven immutable entity.
Repeated observations or changed UID/hotkey associations make coverage unknown;
all received rows are retained, without silently deduplicating them.
Even matching counts cannot certify a snapshot across offset-paginated reads,
so this collector never emits complete without a future consistency contract.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
import math
from typing import Literal

import requests
from requests.adapters import HTTPAdapter

from taoswap_models import (
    ValidationError, ValidatorSnapshot, parse_subnets, parse_validator_page,
)


BASE_URL = 'https://api.taoswap.org'
MAX_OFFSET = 10000
# Local safety policy, not an API guarantee or a bound on total process memory.
# Count decoded body bytes; Content-Length may describe compressed data or lie.
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
RESPONSE_CHUNK_BYTES = 64 * 1024


class ClientError(Exception):
    """Safe failure details; no response body or underlying transport message."""

    def __init__(self, message, *, kind, status_code=None):
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code


class CollectionError(ClientError):
    """A failed traversal is not returned as a successful partial collection."""

    def __init__(self, error, pages_received, records_received):
        super().__init__(
            f'Collection failed after {pages_received} pages ({error.kind})',
            kind=error.kind, status_code=error.status_code,
        )
        self.pages_received = pages_received
        self.records_received = records_received


@dataclass(frozen=True)
class ValidatorCollection:
    """Diagnostic data only. Counters are from the last received page."""

    records: tuple[ValidatorSnapshot, ...]
    pages_received: int
    unique_observations: int
    total_count: int
    reachable_count: int
    total_pages: int
    status: Literal['complete', 'incomplete', 'unknown']
    reason: str


def _integer(value, name, minimum=0, maximum=None):
    if type(value) is not int or value < minimum or (
        maximum is not None and value > maximum
    ):
        raise ValueError(f'{name}: invalid integer argument')
    return value


def _invalid_constant(_value):
    raise ValueError('Non-finite JSON constant')


class TaoSwapClient:
    """Anonymous GET client with an exclusively owned requests.Session.

    Caller-owned sessions are not accepted: their auth, hooks and transport
    cannot be safely reused by resetting individual request arguments.
    Offline tests may patch the Session factory with a trusted fake; arbitrary
    custom Session/adapter implementations are outside the security contract.
    """

    def __init__(self, *, connect_timeout=3.05, read_timeout=10):
        for value in (connect_timeout, read_timeout):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError('Timeouts must be finite positive numbers')
        self.timeout = (connect_timeout, read_timeout)
        self._session = requests.Session()
        self._session.trust_env = False
        self._session.auth = None
        self._session.headers.clear()
        self._session.params.clear()
        self._session.proxies.clear()
        self._session.hooks = {'response': []}
        self._session.cert = None
        self._session.verify = True
        self._session.stream = False
        for adapter in self._session.adapters.values():
            adapter.close()
        self._session.adapters.clear()
        self._session.mount('https://', HTTPAdapter(max_retries=0))

    def close(self):
        self._session.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def _get(self, path, params=None):
        self._session.cookies.clear()
        response = None
        try:
            response = self._session.get(
                BASE_URL + path, params=params, timeout=self.timeout,
                allow_redirects=False, stream=True,
            )
            if not 200 <= response.status_code < 300:
                raise ClientError(
                    f'API HTTP status {response.status_code}', kind='http',
                    status_code=response.status_code,
                )
            body = bytearray()
            for chunk in response.iter_content(chunk_size=RESPONSE_CHUNK_BYTES):
                if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise ClientError('API response exceeds size limit', kind='json')
                body.extend(chunk)
            try:
                return json.loads(
                    body, parse_float=Decimal, parse_constant=_invalid_constant,
                )
            except (ValueError, InvalidOperation):
                raise ClientError('API response is not valid JSON', kind='json') from None
        except requests.Timeout:
            raise ClientError('API request timed out', kind='timeout') from None
        except requests.RequestException:
            raise ClientError('API transport failure', kind='transport') from None
        finally:
            if response is not None:
                response.close()

    def fetch_subnets(self):
        payload = self._get('/subnets/')
        try:
            return parse_subnets(payload)
        except ValidationError as error:
            raise ClientError(f'Invalid API response: {error}', kind='validation') from None

    def fetch_validator_page(self, netuid, *, page=1, page_size=100):
        _integer(netuid, 'netuid')
        _integer(page, 'page', 1)
        _integer(page_size, 'page_size', 1, 500)
        if (page - 1) * page_size > MAX_OFFSET:
            raise ValueError('Requested page exceeds documented offset guard')
        payload = self._get(
            f'/subnets/{netuid}/validators/', {'page': page, 'page_size': page_size},
        )
        try:
            result = parse_validator_page(payload, netuid)
        except ValidationError as error:
            raise ClientError(f'Invalid API response: {error}', kind='validation') from None
        if result.page != page or result.page_size != page_size:
            raise ClientError('Response page does not match request', kind='pagination')
        return result


def collect_validators(client, netuid, *, page_size=100, max_pages=10):
    """Collect consecutive pages within both limits; never certify a snapshot.

    Metadata changes, overlap and ambiguous identity changes stop with unknown.
    Short intermediate pages stop diagnostically instead of assuming progress.
    Transport/schema errors raise CollectionError with successful-page counts.
    Navigation checks below are conservative traversal policy, not model rules.
    """
    _integer(netuid, 'netuid')
    _integer(page_size, 'page_size', 1, 500)
    _integer(max_pages, 'max_pages', 1)
    records, seen, signatures = [], set(), set()
    uid_hotkeys, hotkey_uids = {}, {}
    baseline = None
    pages_received = 0

    def finish(current, status, reason):
        return ValidatorCollection(
            tuple(records), pages_received, len(seen), current.total_count,
            current.reachable_count, current.total_pages, status, reason,
        )

    for page in range(1, max_pages + 1):
        try:
            current = client.fetch_validator_page(netuid, page=page, page_size=page_size)
        except ClientError as error:
            raise CollectionError(error, pages_received, len(records)) from error
        pages_received += 1
        records.extend(current.records)
        keys = [(row.netuid, row.uid, row.hotkey) for row in current.records]
        signature = tuple(sorted(keys))
        repeated_page = bool(keys) and signature in signatures
        repeated_record = len(set(keys)) != len(keys) or bool(seen.intersection(keys))
        ambiguous_identity = False
        # Detect changed associations both across pages and within this page.
        for _, uid, hotkey in keys:
            if (uid in uid_hotkeys and uid_hotkeys[uid] != hotkey) or (
                hotkey in hotkey_uids and hotkey_uids[hotkey] != uid
            ):
                ambiguous_identity = True
            uid_hotkeys[uid], hotkey_uids[hotkey] = hotkey, uid
        seen.update(keys)
        signatures.add(signature)
        metadata = (
            current.total_count, current.reachable_count, current.total_pages,
            current.yields_as_of,
        )
        if baseline is not None and metadata != baseline:
            return finish(current, 'unknown', 'pagination_metadata_changed')
        baseline = metadata
        if repeated_page or repeated_record or ambiguous_identity:
            reason = 'repeated_page' if repeated_page else (
                'repeated_observation' if repeated_record else 'identity_association_changed'
            )
            return finish(current, 'unknown', reason)
        if len(seen) > current.total_count:
            return finish(current, 'unknown', 'more_observations_than_total')
        if current.has_previous != (page > 1) or current.has_next != (page < current.total_pages):
            return finish(current, 'unknown', 'navigation_metadata_disagree')
        if not current.has_next:
            if current.reachable_count < current.total_count:
                return finish(current, 'incomplete', 'offset_truncated_collection')
            if len(seen) < current.total_count:
                return finish(current, 'incomplete', 'fewer_observations_than_total')
            return finish(current, 'unknown', 'offset_pagination_has_no_snapshot_guarantee')
        if len(current.records) < page_size:
            status = 'incomplete' if len(seen) < current.total_count else 'unknown'
            return finish(current, status, 'short_intermediate_page')
        if page * page_size > MAX_OFFSET:
            status = 'incomplete' if len(seen) < current.total_count else 'unknown'
            return finish(current, status, 'offset_guard_reached')
        if page == max_pages:
            status = 'incomplete' if len(seen) < current.total_count else 'unknown'
            return finish(current, status, 'max_pages_reached')
