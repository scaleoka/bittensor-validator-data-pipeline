from io import BytesIO
import importlib.util
import json
from pathlib import Path
import socket
import sys
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from decimal import Decimal, InvalidOperation, localcontext

from taoswap_client import ClientError, CollectionError, TaoSwapClient, collect_validators


class FakeResponse:
    def __init__(self, payload=None, *, status=200, text=None, body=None,
                 chunks=None, headers=None):
        self.status_code = status
        text = json.dumps(payload) if text is None else text
        self.body = text.encode('utf-8') if body is None else body
        self.chunks = chunks
        self.headers = {} if headers is None else headers
        self.closed = False
        self.chunks_read = 0
        self.chunk_sizes = []

    @property
    def text(self):
        raise AssertionError('Response must be streamed, not loaded through text')

    @property
    def content(self):
        raise AssertionError('Response must be streamed, not loaded through content')

    def iter_content(self, chunk_size):
        self.chunk_sizes.append(chunk_size)
        chunks = self.chunks
        if chunks is None:
            chunks = (self.body[i:i + chunk_size]
                      for i in range(0, len(self.body), chunk_size))
        for chunk in chunks:
            self.chunks_read += 1
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def close(self):
        self.closed = True


class FakeSession(requests.Session):
    def __init__(self, *responses):
        super().__init__()
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        assert kwargs.get('stream') is True
        self.calls.append((url, kwargs))
        assert self.responses, 'Unexpected extra request'
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Real network operations are forbidden')
    monkeypatch.setattr(socket, 'socket', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(requests.Session, 'send', forbidden)


def row(uid):
    return {'uid': uid, 'hotkey': f'synthetic-{uid}', 'identity': None,
            'stake': 0, 'vtrust': 0, 'dividends': 0, 'take': 0, 'childkey_take': 0}


def page(number=1, *, total=1, size=1, reachable=None, pages=None, rows=None, **changes):
    reachable = total if reachable is None else reachable
    pages = max(1, (reachable + size - 1) // size) if pages is None else pages
    payload = {
        'netuid': 2, 'yields_as_of': None,
        'results': [row(number)] if rows is None else rows,
        'pagination': {'page': number, 'page_size': size, 'total_count': total,
                       'reachable_count': reachable, 'total_pages': pages,
                       'has_next': number < pages, 'has_previous': number > 1},
    }
    payload['pagination'].update(changes)
    return payload


def client_for(*responses):
    session = FakeSession(*responses)
    with patch('taoswap_client.requests.Session', return_value=session):
        client = TaoSwapClient()
    return client, session


def test_subnets_and_request_configuration():
    response = FakeResponse({'results': [{'id': 0, 'name': None}]})
    session = FakeSession(response)
    session.headers['Authorization'] = 'synthetic-secret'
    session.auth = ('synthetic', 'synthetic')
    session.cookies.set('synthetic', 'synthetic')
    with patch('taoswap_client.requests.Session', return_value=session):
        client = TaoSwapClient(connect_timeout=2, read_timeout=7)
    assert client.fetch_subnets()[0].netuid == 0
    assert session.calls == [('https://api.taoswap.org/subnets/', {
        'params': None, 'timeout': (2, 7), 'allow_redirects': False, 'stream': True,
    })]
    assert session.auth is None and not session.headers and not session.cookies
    assert session.trust_env is False
    assert session.adapters['https://'].max_retries.total == 0
    assert response.closed


@pytest.mark.parametrize('keyword', [False, True])
def test_caller_owned_session_is_rejected_without_mutation_or_close(keyword):
    caller = FakeSession()
    caller.headers.update(Authorization='synthetic-secret', Cookie='synthetic-cookie')
    caller.auth = Mock(side_effect=AssertionError('Caller auth must not run'))
    caller.cookies.set('synthetic', 'cookie')
    caller.params['token'] = 'synthetic-token'
    caller.proxies['https'] = 'https://synthetic-proxy.invalid'
    caller.hooks['response'].append(Mock())
    caller.cert = 'synthetic-client-cert.pem'
    caller.verify = False
    caller.mount('https://api.taoswap.org/', requests.adapters.HTTPAdapter(max_retries=4))
    caller.close = Mock()
    before = (
        dict(caller.headers), caller.auth, caller.cookies.copy(), dict(caller.params),
        dict(caller.proxies), list(caller.hooks['response']), dict(caller.adapters),
        caller.cert, caller.verify, caller.trust_env,
    )
    with pytest.raises(TypeError):
        if keyword:
            TaoSwapClient(session=caller)
        else:
            TaoSwapClient(caller)
    after = (
        dict(caller.headers), caller.auth, caller.cookies.copy(), dict(caller.params),
        dict(caller.proxies), list(caller.hooks['response']), dict(caller.adapters),
        caller.cert, caller.verify, caller.trust_env,
    )
    assert after == before
    caller.auth.assert_not_called()
    caller.close.assert_not_called()
    assert not caller.calls


class PreparedFakeSession(requests.Session):
    # Preserve the standard send path despite the fixture blocking base.send.
    # The test installs a fake adapter before any request; sockets stay blocked.
    send = requests.Session.send


class RecordingAdapter(requests.adapters.HTTPAdapter):
    def __init__(self):
        super().__init__(max_retries=0)
        self.calls = []
        self.responses = []

    def send(self, request, **kwargs):
        self.calls.append((request, kwargs))
        response = requests.Response()
        response.status_code = 200
        response.url = request.url
        response.request = request
        response.raw = BytesIO(b'{"results": [{"id": 0, "name": null}]}')
        response.close = Mock(wraps=response.close)
        self.responses.append(response)
        return response


def test_prepared_requests_do_not_inherit_credentials_hooks_or_transport_settings(monkeypatch):
    owned = PreparedFakeSession()
    auth = Mock(side_effect=AssertionError('Auth must not run'))
    hook = Mock(side_effect=AssertionError('Hook must not run'))
    prefixed_adapter = requests.adapters.HTTPAdapter(max_retries=4)
    prefixed_adapter.send = Mock(side_effect=AssertionError('Old adapter must not run'))
    owned.headers.update(Authorization='synthetic-secret', Cookie='synthetic-cookie',
                         **{'Proxy-Authorization': 'synthetic-proxy-secret'})
    owned.auth = auth
    owned.params['token'] = 'synthetic-token'
    owned.cookies.set('synthetic', 'cookie')
    owned.hooks['response'].append(hook)
    owned.proxies['https'] = 'https://synthetic-proxy.invalid'
    owned.cert, owned.verify, owned.stream = 'synthetic.pem', False, True
    owned.mount('https://api.taoswap.org/subnets/', prefixed_adapter)
    environment_auth = Mock(side_effect=AssertionError('netrc must not be read'))
    environment_proxies = Mock(side_effect=AssertionError('Environment must not be read'))
    monkeypatch.setattr(requests.sessions, 'get_netrc_auth', environment_auth)
    monkeypatch.setattr(requests.sessions, 'get_environ_proxies', environment_proxies)
    monkeypatch.setenv('HTTPS_PROXY', 'https://synthetic-environment-proxy.invalid')
    with patch('taoswap_client.requests.Session', return_value=owned):
        client = TaoSwapClient(connect_timeout=2, read_timeout=7)
    assert list(owned.adapters) == ['https://']
    assert owned.adapters['https://'].max_retries.total == 0
    # Replace the owned transport only in this offline test.
    adapter = RecordingAdapter()
    owned.mount('https://', adapter)
    for _ in range(2):
        owned.cookies.set('response-cookie', 'synthetic-cookie')
        assert client.fetch_subnets()[0].netuid == 0
    assert len(adapter.calls) == 2
    for request, settings in adapter.calls:
        assert request.method == 'GET'
        assert urlsplit(request.url).scheme == 'https'
        assert urlsplit(request.url).netloc == 'api.taoswap.org'
        assert urlsplit(request.url).path == '/subnets/'
        assert parse_qs(urlsplit(request.url).query) == {}
        assert not {'authorization', 'cookie', 'proxy-authorization'} & {
            name.lower() for name in request.headers
        }
        assert request.hooks['response'] == []
        assert settings['timeout'] == (2, 7)
        assert settings['proxies'] == {}
        assert settings['cert'] is None and settings['verify'] is True
        assert settings['stream'] is True
    for response in adapter.responses:
        assert response._content is False
        response.close.assert_called_once_with()
    assert owned.trust_env is False
    for forbidden in (auth, hook, prefixed_adapter.send, environment_auth, environment_proxies):
        forbidden.assert_not_called()
    client.close()


def test_client_closes_its_owned_session():
    client, owned = client_for()
    owned.close = Mock()
    client.close()
    owned.close.assert_called_once_with()


def test_context_manager_closes_session_on_normal_exit():
    client, owned = client_for()
    owned.close = Mock()
    with client as entered:
        assert entered is client
        owned.close.assert_not_called()
    owned.close.assert_called_once_with()
    assert not owned.calls


def test_context_manager_closes_session_without_suppressing_exception():
    client, owned = client_for()
    owned.close = Mock()
    failure = RuntimeError('synthetic failure')
    with pytest.raises(RuntimeError) as error:
        with client:
            raise failure
    assert error.value is failure
    owned.close.assert_called_once_with()
    assert not owned.calls


def test_validator_page_and_exact_decimal():
    text = json.dumps(page()).replace('"stake": 0', '"stake": 0.123456789012345678901')
    client, session = client_for(FakeResponse(text=text))
    result = client.fetch_validator_page(2, page=1, page_size=1)
    assert result.records[0].stake == Decimal('0.123456789012345678901')
    assert session.calls[0][0] == 'https://api.taoswap.org/subnets/2/validators/'
    assert session.calls[0][1]['params'] == {'page': 1, 'page_size': 1}


@pytest.mark.parametrize('status', [302, 403, 404, 429, 500])
def test_http_errors_without_retries_or_sensitive_details(status):
    response = FakeResponse(status=status, text='synthetic-sensitive-body')
    client, session = client_for(response)
    with pytest.raises(ClientError) as error:
        client.fetch_subnets()
    assert error.value.kind == 'http' and error.value.status_code == status
    assert 'synthetic' not in str(error.value)
    assert len(session.calls) == 1 and response.closed


@pytest.mark.parametrize('failure,kind', [
    (requests.Timeout('synthetic-secret'), 'timeout'),
    (requests.ConnectionError('synthetic-secret'), 'transport'),
])
def test_transport_errors(failure, kind):
    client, session = client_for(failure)
    with pytest.raises(ClientError) as error:
        client.fetch_subnets()
    assert error.value.kind == kind
    assert 'synthetic' not in str(error.value)
    assert error.value.__suppress_context__
    assert len(session.calls) == 1


@pytest.mark.parametrize('text', ['not-json', '{', '{"results": NaN}'])
def test_invalid_json(text):
    client, session = client_for(FakeResponse(text=text))
    with pytest.raises(ClientError) as error:
        client.fetch_subnets()
    assert error.value.kind == 'json'
    assert len(session.calls) == 1


def test_decimal_decode_failure_is_normalized_and_response_closed():
    invalid_text = json.dumps(page()).replace(
        '"stake": 0', '"stake": 1e999999999999999999999999999999', 1,
    )
    valid_text = json.dumps(page()).replace(
        '"stake": 0', '"stake": 0.123456789012345678901', 1,
    )
    invalid_response = FakeResponse(text=invalid_text)
    valid_response = FakeResponse(text=valid_text)
    client, session = client_for(invalid_response, valid_response)

    with localcontext() as context:
        context.traps[InvalidOperation] = True
        # The real Decimal callback raises before model validation can run.
        with pytest.raises(InvalidOperation):
            json.loads(invalid_text, parse_float=Decimal)
        with pytest.raises(ClientError) as error:
            client.fetch_validator_page(2, page_size=1)
        assert error.value.kind == 'json'
        assert error.value.status_code is None
        assert str(error.value) == 'API response is not valid JSON'
        assert error.value.__suppress_context__
        assert invalid_response.closed and len(session.calls) == 1

        result = client.fetch_validator_page(2, page_size=1)
        assert result.records[0].stake == Decimal('0.123456789012345678901')
        assert valid_response.closed and len(session.calls) == 2


def test_decimal_decode_failure_is_isolated_to_one_subnet():
    from taoswap_collection import collect_subnet_validators

    first_page = dict(page(total=2), netuid=0)
    invalid_text = json.dumps(dict(page(2, total=2), netuid=0)).replace(
        '"stake": 0', '"stake": 1e999999999999999999999999999999', 1,
    )
    responses = [
        FakeResponse({'results': [{'id': 0, 'name': None},
                                  {'id': 2, 'name': 'synthetic-two'}]}),
        FakeResponse(first_page),
        FakeResponse(text=invalid_text),
        FakeResponse(page()),
    ]
    client, session = client_for(*responses)
    with localcontext() as context:
        context.traps[InvalidOperation] = True
        result = collect_subnet_validators(client, page_size=1, max_pages=2)

    assert [item.subnet.netuid for item in result.collections] == [2]
    assert result.collections[0].collection.status == 'unknown'
    assert len(result.errors) == 1
    failure = result.errors[0]
    assert failure.subnet.netuid == 0 and failure.category == 'json'
    assert failure.status_code is None
    assert failure.pages_received == failure.records_observed == 1
    assert result.total_records_available == 1
    assert result.total_records_observed == result.total_pages_received == 2
    assert result.status == 'incomplete' and result.reason == 'subnet_errors'
    assert not result.subnet_list_completeness_guaranteed
    assert len(session.calls) == 4
    assert [kwargs['params']['page'] for _, kwargs in session.calls[1:]] == [1, 2, 1]
    assert all(response.closed for response in responses)


@pytest.mark.parametrize('field,value,kind', [
    ('netuid', 5, 'validation'), ('page', 2, 'pagination'),
    ('page_size', 2, 'pagination'),
])
def test_response_context_mismatch(field, value, kind):
    payload = page()
    target = payload if field == 'netuid' else payload['pagination']
    target[field] = value
    client, _ = client_for(FakeResponse(payload))
    with pytest.raises(ClientError) as error:
        client.fetch_validator_page(2, page_size=1)
    assert error.value.kind == kind


def test_subnet_schema_error():
    client, _ = client_for(FakeResponse({}))
    with pytest.raises(ClientError) as error:
        client.fetch_subnets()
    assert error.value.kind == 'validation'


def test_multiple_pages_accumulate_without_certifying_snapshot():
    client, session = client_for(*[FakeResponse(page(n, total=3)) for n in (1, 2, 3)])
    result = collect_validators(client, 2, page_size=1)
    assert [record.uid for record in result.records] == [1, 2, 3]
    assert result.pages_received == result.unique_observations == 3
    assert result.total_count == result.reachable_count == 3
    assert result.status == 'unknown'
    assert result.reason == 'offset_pagination_has_no_snapshot_guarantee'
    assert [kwargs['params']['page'] for _, kwargs in session.calls] == [1, 2, 3]


def test_short_last_page_accumulates_without_complete_status():
    client, session = client_for(
        FakeResponse(page(total=3, size=2, rows=[row(1), row(2)])),
        FakeResponse(page(2, total=3, size=2, rows=[row(3)])),
    )
    result = collect_validators(client, 2, page_size=2)
    assert len(result.records) == result.unique_observations == result.total_count == 3
    assert result.status == 'unknown' and len(session.calls) == 2


@pytest.mark.parametrize('payload', [page(), page(total=0, rows=[])])
def test_one_page_or_empty_collection_is_not_a_snapshot_guarantee(payload):
    client, session = client_for(FakeResponse(payload))
    result = collect_validators(client, 2, page_size=1)
    assert result.status == 'unknown' and result.pages_received == 1
    assert len(session.calls) == 1


def test_reachable_count_below_total_is_incomplete():
    client, session = client_for(FakeResponse(page(total=5, reachable=1, pages=1)))
    result = collect_validators(client, 2, page_size=1)
    assert result.status == 'incomplete'
    assert result.reason == 'offset_truncated_collection'
    assert result.reachable_count < result.total_count and len(session.calls) == 1


@pytest.mark.parametrize('rows', [[], [row(1)]])
def test_empty_or_short_intermediate_page(rows):
    client, session = client_for(FakeResponse(page(total=4, size=2, rows=rows)))
    result = collect_validators(client, 2, page_size=2)
    assert result.status == 'incomplete' and result.reason == 'short_intermediate_page'
    assert len(session.calls) == 1


@pytest.mark.parametrize('rows,reason', [
    ([row(1)], 'repeated_page'), ([row(1), row(3)], 'repeated_observation'),
    ([dict(row(1), hotkey='synthetic-new')], 'identity_association_changed'),
    ([dict(row(3), hotkey='synthetic-1')], 'identity_association_changed'),
])
def test_overlap_and_identity_ambiguity(rows, reason):
    size = len(rows)
    first = page(total=size * 2, size=size, rows=[row(i) for i in range(1, size + 1)])
    second = page(2, total=size * 2, size=size, rows=rows)
    client, session = client_for(FakeResponse(first), FakeResponse(second))
    result = collect_validators(client, 2, page_size=size)
    assert result.status == 'unknown' and result.reason == reason
    assert len(result.records) == size * 2 and len(session.calls) == 2


def test_duplicate_observations_within_page():
    client, _ = client_for(FakeResponse(page(total=2, size=2, rows=[row(1), row(1)])))
    result = collect_validators(client, 2, page_size=2)
    assert result.status == 'unknown' and result.reason == 'repeated_observation'
    assert len(result.records) == 2 and result.unique_observations == 1


@pytest.mark.parametrize('second', [
    page(2, total=4), page(2, total=3, reachable=2, pages=2),
])
def test_changed_counts_are_unknown(second):
    client, session = client_for(FakeResponse(page(total=3)), FakeResponse(second))
    result = collect_validators(client, 2, page_size=1)
    assert result.status == 'unknown' and result.reason == 'pagination_metadata_changed'
    assert result.pages_received == len(session.calls) == 2


def test_max_pages_is_enforced():
    client, session = client_for(FakeResponse(page(total=3)))
    result = collect_validators(client, 2, page_size=1, max_pages=1)
    assert result.status == 'incomplete' and result.reason == 'max_pages_reached'
    assert len(session.calls) == 1


def test_offset_guard_is_enforced_without_overshoot():
    responses = [FakeResponse(page(
        n, total=11000, size=500, rows=[row(i) for i in range((n - 1) * 500, n * 500)],
    )) for n in range(1, 22)]
    client, session = client_for(*responses)
    result = collect_validators(client, 2, page_size=500, max_pages=30)
    assert result.status == 'incomplete' and result.reason == 'offset_guard_reached'
    assert len(session.calls) == result.pages_received == 21
    assert all((args['params']['page'] - 1) * 500 <= 10000 for _, args in session.calls)


@pytest.mark.parametrize('changes', [{'has_next': False}, {'has_previous': True}])
def test_contradictory_navigation_stops(changes):
    client, session = client_for(FakeResponse(page(total=3, **changes)))
    result = collect_validators(client, 2, page_size=1)
    assert result.status == 'unknown' and result.reason == 'navigation_metadata_disagree'
    assert len(session.calls) == 1


def test_failure_on_third_page_is_not_a_partial_success():
    client, session = client_for(FakeResponse(page(total=3)), FakeResponse(page(2, total=3)),
                                 FakeResponse(status=500, text='synthetic-secret'))
    with pytest.raises(CollectionError) as error:
        collect_validators(client, 2, page_size=1)
    assert error.value.pages_received == error.value.records_received == 2
    assert error.value.status_code == 500 and len(session.calls) == 3
    assert 'synthetic' not in str(error.value)


@pytest.mark.parametrize('response,kind', [
    (FakeResponse({}), 'validation'),
    (FakeResponse(page(total=3)), 'pagination'),
])
def test_schema_or_repeated_page_number_failure_is_not_hidden(response, kind):
    client, session = client_for(FakeResponse(page(total=3)), response)
    with pytest.raises(CollectionError) as error:
        collect_validators(client, 2, page_size=1)
    assert error.value.kind == kind and error.value.pages_received == 1
    assert len(session.calls) == 2


@pytest.mark.parametrize('kwargs', [{'netuid': True}, {'page': 0}, {'page_size': 501},
                                    {'page': 22, 'page_size': 500}])
def test_bad_request_arguments_do_not_issue_get(kwargs):
    client, session = client_for()
    arguments = {'netuid': 2, **kwargs}
    with pytest.raises(ValueError):
        client.fetch_validator_page(**arguments)
    assert not session.calls


@pytest.mark.parametrize('limit', [0, -1, True, 1.5])
def test_invalid_max_pages(limit):
    client, session = client_for()
    with pytest.raises(ValueError):
        collect_validators(client, 2, max_pages=limit)
    assert not session.calls


def test_import_does_not_create_session_or_issue_requests(monkeypatch):
    forbidden = Mock(side_effect=AssertionError('Import must not create a session'))
    monkeypatch.setattr(requests, 'Session', forbidden)
    path = Path(__file__).resolve().parents[1] / 'taoswap_client.py'
    spec = importlib.util.spec_from_file_location('taoswap_client_import_test', path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    forbidden.assert_not_called()


@pytest.mark.parametrize('headers', [
    {}, {'Content-Length': '1'}, {'Content-Length': '999999999'},
    {'Content-Length': 'invalid'},
])
def test_streamed_response_exact_byte_limit(monkeypatch, headers):
    # UTF-8 bytes, not character count, determine the boundary.
    body = '{"results": [{"id": 0, "name": "synthetic-\u03bb"}]}'.encode('utf-8')
    assert len(body) > len(body.decode('utf-8'))
    monkeypatch.setattr('taoswap_client.MAX_RESPONSE_BYTES', len(body))
    response = FakeResponse(body=body, headers=headers)
    client, session = client_for(response)
    assert client.fetch_subnets()[0].netuid == 0
    assert response.closed and len(session.calls) == 1
    assert response.chunk_sizes == [64 * 1024]


@pytest.mark.parametrize('across_chunks', [False, True])
def test_oversized_response_stops_before_parsing(monkeypatch, across_chunks):
    body = b'{"results": []}'
    monkeypatch.setattr('taoswap_client.MAX_RESPONSE_BYTES', len(body))
    chunks = [body, b' ', b'unread'] if across_chunks else [body + b' ', b'unread']
    response = FakeResponse(chunks=chunks, headers={'Content-Length': str(len(body))})
    client, session = client_for(response)
    with patch('taoswap_client.json.loads') as decode:
        with pytest.raises(ClientError) as error:
            client.fetch_subnets()
        decode.assert_not_called()
    assert error.value.kind == 'json'
    assert error.value.status_code is None
    assert str(error.value) == 'API response exceeds size limit'
    assert response.closed and len(session.calls) == 1
    assert response.chunks_read == (2 if across_chunks else 1)


@pytest.mark.parametrize('failure,kind', [
    (requests.Timeout('synthetic-sensitive'), 'timeout'),
    (requests.ConnectionError('synthetic-sensitive'), 'transport'),
    (requests.exceptions.ChunkedEncodingError('synthetic-sensitive'), 'transport'),
    (requests.exceptions.ContentDecodingError('synthetic-sensitive'), 'transport'),
])
def test_partial_body_failure_is_normalized_and_closed(failure, kind):
    response = FakeResponse(chunks=[b'{"results":', failure, b'unread'])
    client, session = client_for(response)
    with patch('taoswap_client.json.loads') as decode:
        with pytest.raises(ClientError) as error:
            client.fetch_subnets()
        decode.assert_not_called()
    assert error.value.kind == kind
    assert error.value.__suppress_context__
    assert 'synthetic' not in str(error.value)
    assert response.closed and response.chunks_read == 2
    assert len(session.calls) == 1


def test_oversized_response_is_isolated_to_one_subnet(monkeypatch):
    from taoswap_collection import collect_subnet_validators

    monkeypatch.setattr('taoswap_client.MAX_RESPONSE_BYTES', 1024)
    responses = [
        FakeResponse({'results': [{'id': 0, 'name': None}, {'id': 2, 'name': None}]}),
        FakeResponse(dict(page(total=2), netuid=0)),
        FakeResponse(chunks=[b' ' * 1024, b' ', b'unread']),
        FakeResponse(page()),
    ]
    client, session = client_for(*responses)
    result = collect_subnet_validators(client, page_size=1)
    assert result.status == 'incomplete'
    assert result.reason == 'subnet_errors'
    assert not result.subnet_list_completeness_guaranteed
    assert len(result.collections) == 1
    assert result.collections[0].subnet.netuid == 2
    assert result.collections[0].collection.status == 'unknown'
    assert len(result.errors) == 1
    failure = result.errors[0]
    assert failure.subnet.netuid == 0 and failure.category == 'json'
    assert failure.pages_received == 1 and failure.records_observed == 1
    assert result.total_records_available == 1
    assert result.total_records_observed == 2
    assert result.total_pages_received == 2
    assert len(session.calls) == 4
    assert all(response.closed for response in responses)
    assert responses[2].chunks_read == 2
