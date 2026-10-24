"""Atomic diagnostic replacement in a trusted, existing local directory.

Final-component symlinks and Windows reparse points are rejected via lstat.
Ancestor links and concurrent path substitution are not secured against: the
directory and its ancestors must not be modified by an untrusted actor.
mkstemp uses restricted creation permissions on POSIX; Windows permissions
depend on the directory's ACL. No chmod or permission widening is performed.
Replacement does not preserve the old inode, mode or ACL. File fsync precedes
replacement, but the directory is not fsynced: atomic visibility is not a
promise of persistence through power loss on every filesystem.
"""

import json
import os
from pathlib import Path
import stat
import tempfile

from taoswap_collection import MultiSubnetCollection
from taoswap_diagnostics import DiagnosticSerializationError, diagnostic_json


FILENAME = 'taoswap-diagnostic.json'
MAX_JSON_BYTES = 10 * 1024 * 1024


class DiagnosticWriteError(Exception):
    """Only fixed categories are exposed, not paths, payloads or OS messages."""

    def __init__(self, category, *, primary_category=None):
        super().__init__(f'Diagnostic write failed: {category}')
        self.category = category
        self.primary_category = primary_category


def _reject_constant(_value):
    raise ValueError('Non-finite JSON constant')


def _serialize(result):
    try:
        text = diagnostic_json(result)
        data = text.encode('utf-8')
    except DiagnosticSerializationError:
        raise
    except Exception:
        raise DiagnosticSerializationError('diagnostic JSON: serialization failed') from None
    if len(data) > MAX_JSON_BYTES:
        raise DiagnosticWriteError('size_limit')
    try:
        json.loads(text, parse_constant=_reject_constant)
    except Exception:
        raise DiagnosticSerializationError('diagnostic JSON: invalid JSON') from None
    return data


def _is_link(info):
    reparse_flag = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, 'st_file_attributes', 0) & reparse_flag
    )


def _directory(output_dir):
    try:
        directory = Path(output_dir)
        info = directory.lstat()
        if _is_link(info) or not stat.S_ISDIR(info.st_mode):
            raise ValueError('Not an ordinary directory')
    except Exception:
        raise DiagnosticWriteError('invalid_directory') from None
    return directory


def _check_target(target):
    try:
        info = target.lstat()
    except FileNotFoundError:
        return
    except Exception:
        raise DiagnosticWriteError('forbidden_target') from None
    if _is_link(info) or not stat.S_ISREG(info.st_mode):
        raise DiagnosticWriteError('forbidden_target')


def _cleanup(fd, temporary, primary_category):
    failed = False
    if fd is not None:
        try:
            os.close(fd)
        except Exception:
            failed = True
    if temporary is not None:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        except Exception:
            failed = True
    if failed:
        raise DiagnosticWriteError('cleanup', primary_category=primary_category) from None


def write_diagnostic_report(result: MultiSubnetCollection, output_dir) -> Path:
    """Write only taoswap-diagnostic.json; never create directories.

    Serialization and the 10 MiB UTF-8 limit are checked before filesystem I/O.
    Before successful replace, failures preserve an existing regular target.
    Cleanup is attempted on every exit; cleanup failure reports both categories
    and can leave a temporary file requiring manual removal. No fallible file
    operations follow successful replace. Concurrent writers are last-writer-wins.
    """
    data = _serialize(result)
    directory = _directory(output_dir)
    target = directory / FILENAME
    _check_target(target)
    fd, temporary = None, None
    operation = 'temporary_write'
    try:
        fd, temporary = tempfile.mkstemp(
            prefix='.taoswap-diagnostic-', suffix='.tmp', dir=directory,
        )
        with os.fdopen(fd, 'wb') as stream:
            fd = None  # The stream now owns the descriptor, including on failure.
            if stream.write(data) != len(data):
                raise OSError('Short temporary write')
            stream.flush()
            os.fsync(stream.fileno())
        operation = 'atomic_replace'
        os.replace(temporary, target)
        temporary = None
    except Exception:
        raise DiagnosticWriteError(operation) from None
    finally:
        _cleanup(fd, temporary, operation)
    return target
