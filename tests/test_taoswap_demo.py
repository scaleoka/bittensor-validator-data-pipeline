import argparse
import importlib.util
import json
import os
from pathlib import Path, PureWindowsPath
import runpy
import socket
import sys
from unittest.mock import Mock
from decimal import Decimal

import pytest
import requests

import taoswap_demo as demo
import taoswap_file_writer as writer
from taoswap_diagnostics import DiagnosticSerializationError, diagnostic_json


@pytest.fixture(autouse=True)
def prohibit_network_and_clients(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Real network operations and HTTP clients are forbidden')
    monkeypatch.setattr(socket, 'socket', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(requests.Session, 'send', forbidden)
    monkeypatch.setattr(requests, 'Session', forbidden)


def test_import_has_no_cli_or_filesystem_side_effects(tmp_path, monkeypatch):
    forbidden = Mock(side_effect=AssertionError('Import must not execute the demo'))
    monkeypatch.setattr(writer, 'write_diagnostic_report', forbidden)
    monkeypatch.setattr(writer.tempfile, 'mkstemp', forbidden)
    monkeypatch.setattr(argparse.ArgumentParser, 'parse_args', forbidden)
    monkeypatch.setattr(sys, 'exit', forbidden)
    monkeypatch.setattr(sys, 'argv', ['taoswap_demo', '--output-dir', str(tmp_path)])
    path = Path(__file__).resolve().parents[1] / 'taoswap_demo.py'
    spec = importlib.util.spec_from_file_location('taoswap_demo_import_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module.build_demo_result) and callable(module.main)
    forbidden.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_synthetic_result_is_deterministic_and_fresh():
    first, second = demo.build_demo_result(), demo.build_demo_result()
    assert first == second
    assert diagnostic_json(first) == diagnostic_json(second)
    second.collections[0].collection.records[0].identity['name'] = 'changed-copy'
    assert first.collections[0].collection.records[0].identity['name'] == 'synthetic-root-validator'


def test_demo_shape_precision_statuses_and_counters():
    result = demo.build_demo_result()
    assert [subnet.netuid for subnet in result.processed_subnets] == [0, 2, 5]
    assert result.processed_subnets[1].name is None
    assert [item.collection.status for item in result.collections] == ['unknown', 'incomplete']
    assert result.errors[0].category == 'timeout' and result.errors[0].subnet.netuid == 5
    assert result.status == 'incomplete' and result.reason == 'subnet_errors'
    assert not result.subnet_list_completeness_guaranteed
    assert result.total_records_available == 3
    assert result.total_records_observed == 7 and result.total_pages_received == 4
    assert result.total_records_observed == result.total_records_available + result.errors[0].records_observed
    record = result.collections[0].collection.records[1]
    assert record.stake == Decimal('9876543210.1234567890123456789')
    for item in result.collections:
        for row in item.collection.records:
            assert row.hotkey.startswith('synthetic-demo-')
            assert all(isinstance(getattr(row, name), Decimal) for name in (
                'stake', 'vtrust', 'dividends', 'take', 'childkey_take',
            ))


def test_cli_writes_exact_json_and_repeats_bytes(tmp_path, capsys):
    args = ['--output-dir', str(tmp_path)]
    assert demo.main(args) == 0
    output = tmp_path / writer.FILENAME
    first = output.read_bytes()
    assert first == diagnostic_json(demo.build_demo_result()).encode('utf-8')
    captured = capsys.readouterr()
    assert captured.err == ''
    assert captured.out == (
        f'Offline synthetic demo: file={output}; status=incomplete; '
        'processed_subnets=3; available_records=3; observed_records=7\n'
    )
    assert demo.main(args) == 0
    assert output.read_bytes() == first
    assert set(tmp_path.iterdir()) == {output}


def test_module_entry_point(tmp_path, monkeypatch, capsys):
    # Exercise the same run_module entry mechanism as python -m, in-process,
    # so all network guards remain active.
    monkeypatch.delitem(sys.modules, 'taoswap_demo', raising=False)
    monkeypatch.setattr(sys, 'argv', ['taoswap_demo', '--output-dir', str(tmp_path)])
    with pytest.raises(SystemExit) as error:
        runpy.run_module('taoswap_demo', run_name='__main__')
    assert error.value.code == 0
    assert (tmp_path / writer.FILENAME).read_bytes() == diagnostic_json(demo.build_demo_result()).encode('utf-8')
    assert 'Offline synthetic demo' in capsys.readouterr().out


def test_redaction_in_file_and_console(tmp_path, capsys):
    assert demo.main(['--output-dir', str(tmp_path)]) == 0
    text = (tmp_path / writer.FILENAME).read_text(encoding='utf-8')
    report = json.loads(text)
    assert report['redacted_fields'] == ['hotkey', 'identity']
    for item in report['collections']:
        for row in item['records']:
            assert 'hotkey' not in row and 'identity' not in row
    assert 'synthetic-demo-' not in text and 'synthetic-root-validator' not in text
    captured = capsys.readouterr()
    assert 'hotkey' not in captured.out and 'identity' not in captured.out
    assert captured.err == ''


@pytest.mark.parametrize('args', [[], ['--unknown', 'synthetic-secret'], ['--output-dir']])
def test_missing_or_invalid_arguments_are_safe(tmp_path, capsys, args):
    assert demo.main(args) == 2
    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err == 'Offline synthetic demo: invalid arguments; use --help.\n'
    assert 'synthetic-secret' not in captured.err and 'Traceback' not in captured.err
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('value', [
    'https://example.invalid/reports', 'http:/example.invalid', 'file:///tmp/reports',
    's3://synthetic/reports', '//synthetic-server/share', '\\\\synthetic-server\\share',
    'C:relative', '', 'synthetic\x00path', 'synthetic\npath',
])
def test_urls_and_unsafe_path_forms_are_rejected_before_writer(tmp_path, capsys, monkeypatch, value):
    forbidden = Mock(side_effect=AssertionError('Writer must not be called'))
    monkeypatch.setattr(demo, 'write_diagnostic_report', forbidden)
    assert demo.main(['--output-dir', value]) == 2
    forbidden.assert_not_called()
    captured = capsys.readouterr()
    assert captured.out == '' and 'invalid arguments' in captured.err
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('windows', [False, True], ids=['posix', 'windows'])
@pytest.mark.parametrize('prefix', [r'\\', '//', '\\/', '/\\'])
def test_unc_prefixes_are_rejected_before_path_or_writer(tmp_path, capsys, monkeypatch, windows, prefix):
    value = prefix + 'synthetic-server/share'
    # PureWindowsPath applies Windows rules on every host without accessing a share.
    assert PureWindowsPath(value).drive.startswith('\\\\')
    with pytest.raises(argparse.ArgumentTypeError):
        demo._check_local_directory(value, windows=windows)

    forbidden = Mock(side_effect=AssertionError('UNC paths must not reach Path or writer'))
    monkeypatch.setattr(demo, 'Path', forbidden)
    monkeypatch.setattr(demo, 'write_diagnostic_report', forbidden)
    assert demo.main(['--output-dir', value]) == 2
    forbidden.assert_not_called()
    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err == 'Offline synthetic demo: invalid arguments; use --help.\n'
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('windows', [False, True], ids=['posix', 'windows'])
@pytest.mark.parametrize('value,posix_ok,windows_ok', [
    ('reports', True, True), ('demo output', True, True),
    ('./synthetic reports/данные 演示', True, True), ('../local reports', True, True),
    ('/tmp/данные с пробелами', True, True),
    ('C:/demo output/данные', False, True), (r'D:\demo output\演示', False, True),
    ('C:/', False, True), ('C:\\', False, True),
    ('C:relative', False, False), ('C:', False, False),
    ('https://example.invalid/reports', False, False),
    ('https:/example.invalid/reports', False, False),
    ('file:///tmp/reports', False, False), ('s3:synthetic', False, False),
    ('//synthetic-server/share', False, False),
    (r'\\synthetic-server\share', False, False), (r'\\?\C:\reports', False, False),
    ('', False, False), ('synthetic\x00path', False, False),
    ('synthetic\tpath', False, False), ('synthetic\npath', False, False),
    ('synthetic\x7fpath', False, False), ('synthetic\x85path', False, False),
])
def test_local_path_platform_semantics(windows, value, posix_ok, windows_ok):
    # Pure spelling checks: neither a Windows drive nor a real directory is used.
    allowed = windows_ok if windows else posix_ok
    if allowed:
        assert demo._check_local_directory(value, windows=windows) is None
    else:
        with pytest.raises(argparse.ArgumentTypeError):
            demo._check_local_directory(value, windows=windows)


@pytest.mark.parametrize('value', ['C:/synthetic reports', r'C:\synthetic reports'])
def test_drive_path_conversion_uses_native_platform(value):
    if os.name == 'nt':
        assert demo._local_directory(value) == Path(value)
    else:
        with pytest.raises(argparse.ArgumentTypeError):
            demo._local_directory(value)


def test_cli_accepts_existing_local_directory_with_spaces_and_unicode(tmp_path, capsys):
    output = tmp_path / 'synthetic данные 演示'
    output.mkdir()
    assert demo.main(['--output-dir', str(output)]) == 0
    assert (output / writer.FILENAME).read_bytes() == diagnostic_json(demo.build_demo_result()).encode('utf-8')
    assert set(tmp_path.iterdir()) == {output}
    assert set(output.iterdir()) == {output / writer.FILENAME}
    captured = capsys.readouterr()
    assert str(output / writer.FILENAME) in captured.out and captured.err == ''


@pytest.mark.parametrize('kind', ['missing', 'file'])
def test_invalid_directory_does_not_create_or_modify_files(tmp_path, capsys, kind):
    output = tmp_path / 'output'
    if kind == 'file':
        output.write_bytes(b'unchanged')
    assert demo.main(['--output-dir', str(output)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err == 'Offline synthetic demo: file publication failed.\n'
    assert str(output) not in captured.err and 'Traceback' not in captured.err
    if kind == 'missing':
        assert list(tmp_path.iterdir()) == []
    else:
        assert set(tmp_path.iterdir()) == {output} and output.read_bytes() == b'unchanged'


@pytest.mark.parametrize('failure,expected', [
    (DiagnosticSerializationError('synthetic-secret payload'), 'diagnostic serialization failed'),
    (writer.DiagnosticWriteError('synthetic-secret OS message'), 'file publication failed'),
])
def test_expected_failures_have_safe_stderr(tmp_path, capsys, monkeypatch, failure, expected):
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(demo, 'write_diagnostic_report', fail)
    assert demo.main(['--output-dir', str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == '' and expected in captured.err
    assert 'synthetic-secret' not in captured.err and 'Traceback' not in captured.err
    assert list(tmp_path.iterdir()) == []


def test_real_serializer_failure_precedes_file_creation(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(demo, 'build_demo_result', lambda: None)
    assert demo.main(['--output-dir', str(tmp_path)]) == 1
    assert 'diagnostic serialization failed' in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_unexpected_programming_error_is_not_hidden(tmp_path, capsys, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError('synthetic programming error')
    monkeypatch.setattr(demo, 'write_diagnostic_report', fail)
    with pytest.raises(RuntimeError):
        demo.main(['--output-dir', str(tmp_path)])
    captured = capsys.readouterr()
    assert captured.out == captured.err == ''
    assert list(tmp_path.iterdir()) == []
