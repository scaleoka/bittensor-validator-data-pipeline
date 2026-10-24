"""Reproducible offline demo; creates no clients and performs no work on import.

All observations and failures below are constructed examples, not API data.
The caller must provide an existing, trusted local output directory. URLs,
UNC and drive-relative paths are not accepted; writer path protections apply.
"""

import argparse
from decimal import Decimal
import ntpath
import os
from pathlib import Path
import re
import sys
import unicodedata

from taoswap_client import ValidatorCollection
from taoswap_collection import MultiSubnetCollection, SubnetCollection, SubnetFailure
from taoswap_diagnostics import DiagnosticSerializationError
from taoswap_file_writer import DiagnosticWriteError, write_diagnostic_report
from taoswap_models import Subnet, ValidatorSnapshot


def _demo_record(netuid, uid, stake, identity):
    return ValidatorSnapshot(
        netuid=netuid, uid=uid, hotkey=f'synthetic-demo-{netuid}-{uid}',
        identity=identity, stake=Decimal(stake), vtrust=Decimal('0.75'),
        dividends=Decimal('0.00000000000000000001'), take=Decimal('0'),
        childkey_take=Decimal('0.1250'),
    )


def build_demo_result() -> MultiSubnetCollection:
    """Return fresh synthetic models, without time, randomness or side effects."""
    root = Subnet(0, 'synthetic-root')
    partial = Subnet(2, None)
    failed = Subnet(5, 'synthetic-unavailable')
    root_records = (
        _demo_record(0, 0, '0.123456789012345678901', {'name': 'synthetic-root-validator'}),
        _demo_record(0, 1, '9876543210.1234567890123456789', None),
    )
    partial_records = (_demo_record(2, 0, '0', None),)
    collections = (
        SubnetCollection(root, ValidatorCollection(
            root_records, 1, 2, 2, 2, 1, 'unknown',
            'offset_pagination_has_no_snapshot_guarantee',
        )),
        SubnetCollection(partial, ValidatorCollection(
            partial_records, 1, 1, 3, 1, 1, 'incomplete', 'offset_truncated_collection',
        )),
    )
    return MultiSubnetCollection(
        processed_subnets=(root, partial, failed), collections=collections,
        errors=(SubnetFailure(failed, 'timeout', None, 2, 4),),
        subnets_reported=3, subnets_skipped=0,
        total_records_available=3, total_records_observed=7, total_pages_received=4,
        subnet_list_completeness_guaranteed=False,
        status='incomplete', reason='subnet_errors',
    )


class DemoArgumentError(ValueError):
    """Invalid CLI arguments; no user-supplied values in the message."""


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise DemoArgumentError('Invalid demo arguments')


def _check_local_directory(value, *, windows):
    """Check path spelling without interpreting a foreign drive as a filename."""
    uri_scheme = re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', value)
    absolute_drive = windows and re.match(r'^[A-Za-z]:[\\/]', value)
    # Windows treats both slash forms as separators, including mixed prefixes.
    unc_prefix = ntpath.normcase(value).startswith('\\\\')
    if (not value or any(unicodedata.category(character) == 'Cc' for character in value)
            or '://' in value or unc_prefix
            or (uri_scheme and not absolute_drive)):
        raise argparse.ArgumentTypeError('An explicit local path is required')


def _local_directory(value):
    _check_local_directory(value, windows=os.name == 'nt')
    return Path(value)


def parse_args(argv=None):
    parser = _Parser(prog='taoswap_demo', description='Offline synthetic TaoSwap diagnostic demo.')
    parser.add_argument('--output-dir', required=True, type=_local_directory,
                        help='Existing trusted local directory for the diagnostic JSON.')
    return parser.parse_args(argv)


def main(argv=None) -> int:
    try:
        args = parse_args(argv)
    except DemoArgumentError:
        print('Offline synthetic demo: invalid arguments; use --help.', file=sys.stderr)
        return 2
    try:
        result = build_demo_result()
        output = write_diagnostic_report(result, args.output_dir)
    except DiagnosticSerializationError:
        print('Offline synthetic demo: diagnostic serialization failed.', file=sys.stderr)
        return 1
    except DiagnosticWriteError:
        print('Offline synthetic demo: file publication failed.', file=sys.stderr)
        return 1
    print(
        f'Offline synthetic demo: file={output}; status={result.status}; '
        f'processed_subnets={len(result.processed_subnets)}; '
        f'available_records={result.total_records_available}; '
        f'observed_records={result.total_records_observed}'
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())
