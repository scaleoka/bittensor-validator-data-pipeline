from dataclasses import replace
from decimal import Decimal
import json
import os
from pathlib import Path
import socket
import stat
from types import SimpleNamespace

import pytest
import requests

import taoswap_file_writer as writer
from taoswap_client import ValidatorCollection
from taoswap_collection import MultiSubnetCollection, SubnetCollection
from taoswap_diagnostics import DiagnosticSerializationError, diagnostic_json
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
        2, 0, 'synthetic-private-hotkey', {'name': 'synthetic-private-identity'},
        Decimal('0.123456789012345678901'), Decimal(0), Decimal('-2.50'),
        Decimal(0), Decimal(0),
    )
    collection = ValidatorCollection((record,), 1, 1, 1, 1, 1, 'unknown',
                                      'offset_pagination_has_no_snapshot_guarantee')
    return MultiSubnetCollection(
        (subnet,), (SubnetCollection(subnet, collection),), (), 1, 0, 1, 1, 1,
        False, 'unknown', 'global_completeness_not_guaranteed',
    )


@pytest.fixture
def previous(tmp_path):
    target = tmp_path / writer.FILENAME
    target.write_bytes(b'{"previous": true}')
    return target


def assert_previous_and_no_temps(tmp_path, previous):
    assert previous.read_bytes() == b'{"previous": true}'
    assert set(tmp_path.iterdir()) == {previous}


def test_first_write_exact_utf8_decimal_redaction_and_no_temps(tmp_path, result):
    target = writer.write_diagnostic_report(result, tmp_path)
    assert target == tmp_path / 'taoswap-diagnostic.json'
    assert target.read_bytes() == diagnostic_json(result).encode('utf-8')
    report = json.loads(target.read_text(encoding='utf-8'))
    row = report['collections'][0]['records'][0]
    assert row['stake'] == '0.123456789012345678901' and row['take'] == '0'
    assert 'hotkey' not in row and 'identity' not in row
    assert 'synthetic-private' not in target.read_text(encoding='utf-8')
    assert set(tmp_path.iterdir()) == {target}


def test_replacement_and_determinism(tmp_path, previous, result):
    first = writer.write_diagnostic_report(result, tmp_path).read_bytes()
    assert writer.write_diagnostic_report(result, tmp_path).read_bytes() == first
    changed = replace(result, status='incomplete', reason='max_subnets_reached')
    assert writer.write_diagnostic_report(changed, tmp_path) == previous
    assert previous.read_bytes() == diagnostic_json(changed).encode('utf-8')
    assert set(tmp_path.iterdir()) == {previous}


def test_fsync_before_replace_and_descriptor_closed(tmp_path, result, monkeypatch):
    events, descriptors = [], []
    real_fsync, real_replace = writer.os.fsync, writer.os.replace

    def fsync(fd):
        events.append('fsync')
        descriptors.append(fd)
        real_fsync(fd)

    def replace_file(source, target):
        events.append('replace')
        assert Path(source).parent == tmp_path and Path(target).parent == tmp_path
        assert Path(source).read_bytes() == diagnostic_json(result).encode('utf-8')
        with pytest.raises(OSError):
            os.fstat(descriptors[0])
        real_replace(source, target)

    monkeypatch.setattr(writer.os, 'fsync', fsync)
    monkeypatch.setattr(writer.os, 'replace', replace_file)
    writer.write_diagnostic_report(result, tmp_path)
    assert events == ['fsync', 'replace']


def test_serialization_failure_precedes_filesystem(tmp_path, previous, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Filesystem must not be touched')
    with monkeypatch.context() as context:
        context.setattr(writer, '_directory', forbidden)
        context.setattr(writer, '_check_target', forbidden)
        context.setattr(writer.tempfile, 'mkstemp', forbidden)
        context.setattr(writer.os, 'replace', forbidden)
        with pytest.raises(DiagnosticSerializationError):
            writer.write_diagnostic_report(None, tmp_path)
    assert_previous_and_no_temps(tmp_path, previous)


@pytest.mark.parametrize('text', ['not-json', '{"value": NaN}'])
def test_invalid_serialized_json_before_filesystem(tmp_path, result, monkeypatch, text):
    monkeypatch.setattr(writer, 'diagnostic_json', lambda _: text)
    monkeypatch.setattr(writer, '_directory', lambda _: pytest.fail('Filesystem accessed'))
    with pytest.raises(DiagnosticSerializationError, match='invalid JSON'):
        writer.write_diagnostic_report(result, tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('kind', ['missing', 'file'])
def test_invalid_output_directory(tmp_path, result, kind):
    output = tmp_path / 'output'
    if kind == 'file':
        output.write_bytes(b'unchanged')
    with pytest.raises(writer.DiagnosticWriteError) as error:
        writer.write_diagnostic_report(result, output)
    assert error.value.category == 'invalid_directory'
    if kind == 'missing':
        assert not output.exists()
    else:
        assert output.read_bytes() == b'unchanged'


def test_target_directory_is_forbidden(tmp_path, result):
    target = tmp_path / writer.FILENAME
    target.mkdir()
    marker = target / 'marker'
    marker.write_bytes(b'unchanged')
    with pytest.raises(writer.DiagnosticWriteError) as error:
        writer.write_diagnostic_report(result, tmp_path)
    assert error.value.category == 'forbidden_target'
    assert marker.read_bytes() == b'unchanged'
    assert set(tmp_path.iterdir()) == {target}


def make_symlink_or_skip(link, target, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError):
        pytest.skip('Symlink creation is unavailable on this OS/account')


def test_target_symlink_is_rejected(tmp_path, result):
    output = tmp_path / 'reports'
    output.mkdir()
    original = tmp_path / 'original'
    original.write_bytes(b'unchanged')
    target = output / writer.FILENAME
    make_symlink_or_skip(target, original)
    with pytest.raises(writer.DiagnosticWriteError) as error:
        writer.write_diagnostic_report(result, output)
    assert error.value.category == 'forbidden_target'
    assert original.read_bytes() == b'unchanged' and target.is_symlink()


def test_output_directory_symlink_is_rejected(tmp_path, result):
    real = tmp_path / 'real'
    real.mkdir()
    link = tmp_path / 'link'
    make_symlink_or_skip(link, real, directory=True)
    with pytest.raises(writer.DiagnosticWriteError) as error:
        writer.write_diagnostic_report(result, link)
    assert error.value.category == 'invalid_directory'
    assert list(real.iterdir()) == []


@pytest.mark.parametrize('target_reparse', [False, True])
def test_windows_reparse_points_are_rejected(tmp_path, result, monkeypatch, target_reparse):
    inspected = tmp_path / writer.FILENAME if target_reparse else tmp_path
    real_lstat = Path.lstat

    def lstat(path, *args, **kwargs):
        if path == inspected:
            return SimpleNamespace(
                st_mode=stat.S_IFREG if target_reparse else stat.S_IFDIR,
                st_file_attributes=0x400,
            )
        return real_lstat(path, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(Path, 'lstat', lstat)
        with pytest.raises(writer.DiagnosticWriteError) as error:
            writer.write_diagnostic_report(result, tmp_path)
    assert error.value.category == ('forbidden_target' if target_reparse else 'invalid_directory')
    assert list(tmp_path.iterdir()) == []


def test_utf8_byte_limit_and_exact_boundary(tmp_path, previous, result, monkeypatch):
    assert writer.MAX_JSON_BYTES == 10 * 1024 * 1024
    text = '{"synthetic": "\u00e9"}'
    encoded = text.encode('utf-8')
    assert len(encoded) > len(text)
    monkeypatch.setattr(writer, 'diagnostic_json', lambda _: text)
    monkeypatch.setattr(writer, 'MAX_JSON_BYTES', len(encoded) - 1)
    with pytest.raises(writer.DiagnosticWriteError) as error:
        writer.write_diagnostic_report(result, tmp_path)
    assert error.value.category == 'size_limit'
    assert_previous_and_no_temps(tmp_path, previous)
    monkeypatch.setattr(writer, 'MAX_JSON_BYTES', len(encoded))
    assert writer.write_diagnostic_report(result, tmp_path).read_bytes() == encoded


class FaultyStream:
    def __init__(self, stream, stage):
        self.stream, self.stage = stream, stage

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stream.close()
        if self.stage == 'close':
            raise OSError('synthetic-sensitive OS message')

    def write(self, data):
        if self.stage in ('write', 'short'):
            written = self.stream.write(data[:3])
            if self.stage == 'short':
                return written
            raise OSError('synthetic-sensitive OS message')
        return self.stream.write(data)

    def flush(self):
        if self.stage == 'flush':
            raise OSError('synthetic-sensitive OS message')
        self.stream.flush()

    def fileno(self):
        return self.stream.fileno()


@pytest.mark.parametrize('stage', ['create', 'fdopen', 'write', 'short', 'flush', 'fsync', 'close', 'replace'])
def test_failures_preserve_previous_and_remove_temp(tmp_path, previous, result, monkeypatch, stage):
    def fail(*args, **kwargs):
        raise OSError('synthetic-sensitive OS message')
    real_fdopen = writer.os.fdopen
    descriptors = []

    def fdopen(fd, mode):
        descriptors.append(fd)
        if stage == 'fdopen':
            fail()
        return FaultyStream(real_fdopen(fd, mode), stage)

    if stage == 'create':
        monkeypatch.setattr(writer.tempfile, 'mkstemp', fail)
    else:
        monkeypatch.setattr(writer.os, 'fdopen', fdopen)
    if stage == 'fsync':
        monkeypatch.setattr(writer.os, 'fsync', fail)
    if stage == 'replace':
        monkeypatch.setattr(writer.os, 'replace', fail)
    with pytest.raises(writer.DiagnosticWriteError) as error:
        writer.write_diagnostic_report(result, tmp_path)
    assert error.value.category == ('atomic_replace' if stage == 'replace' else 'temporary_write')
    assert 'synthetic-sensitive' not in str(error.value)
    assert str(tmp_path) not in str(error.value) and error.value.__suppress_context__
    assert_previous_and_no_temps(tmp_path, previous)
    for fd in descriptors:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize('primary', ['fsync', 'replace'])
def test_cleanup_error_reports_both_categories(tmp_path, previous, result, monkeypatch, primary):
    def fail(*args, **kwargs):
        raise OSError('synthetic-sensitive OS message')
    with monkeypatch.context() as context:
        context.setattr(writer.os, primary, fail)
        context.setattr(writer.os, 'unlink', fail)
        with pytest.raises(writer.DiagnosticWriteError) as error:
            writer.write_diagnostic_report(result, tmp_path)
    assert error.value.category == 'cleanup'
    assert error.value.primary_category == ('atomic_replace' if primary == 'replace' else 'temporary_write')
    assert 'synthetic-sensitive' not in str(error.value)
    assert previous.read_bytes() == b'{"previous": true}'
    leftovers = [path for path in tmp_path.iterdir() if path != previous]
    assert len(leftovers) == 1 and leftovers[0].parent == tmp_path
    leftovers[0].unlink()  # Test-only cleanup, confined to this pytest tmp_path.
    assert_previous_and_no_temps(tmp_path, previous)
