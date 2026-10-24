# Bittensor Validator Data Pipeline — TaoSwap Adapter

A Python project for validating validator data, collecting bounded API pages,
and producing deterministic, redacted diagnostic reports. The working offline
CLI demonstrates reporting with entirely synthetic data. This standalone
project contains the TaoSwap components and their offline tests.

## Project Status

- The TaoSwap HTTP client and collectors are available programmatically. They
  are not called by the offline demo; no live-data CLI is provided.
- The offline demo uses synthetic in-memory models only, makes no HTTP requests,
  and requires no Google credentials.
- No production publisher, deployment, or external data publication is implemented.
- Snapshot completeness is not guaranteed. Reading every advertised page or
  matching `total_count` does not establish a consistent snapshot.

## Architecture

| Module | Responsibility |
| --- | --- |
| `taoswap_models.py` | Dataclasses and pure validation of subnet/validator envelopes, request context, pagination fields, and finite Decimal metrics. |
| `taoswap_client.py` | Anonymous HTTPS GET client with an owned session, explicit timeouts, no redirects or automatic retries; `collect_validators()` implements bounded per-subnet pagination. |
| `taoswap_collection.py` | Sequential multi-subnet orchestration with subnet/page budgets, per-subnet failures, and aggregate diagnostic counters. |
| `taoswap_diagnostics.py` | Deterministic schema-v1 diagnostic JSON with explicit fields, Decimal strings, and hotkey/identity redaction. |
| `taoswap_file_writer.py` | Size-checked temporary-file writing and atomic replacement of a fixed diagnostic filename in an existing trusted directory. |
| `taoswap_demo.py` | Offline CLI and deterministic synthetic result builder; does not create an HTTP client. |

The demo constructs a `MultiSubnetCollection` in memory and passes it through
serialization and the atomic writer. It does not fetch data or run collectors.
The separately implemented API path is:

```text
TaoSwapClient -> validated models -> collect_validators (one subnet)
    -> collect_subnet_validators (multiple subnets) -> diagnostic result
```

## Quick Start: Offline Demo

Use **Python 3.10 or newer**. Open a terminal in the local directory containing
this README, then run the commands below. The intended repository name is
`bittensor-validator-data-pipeline`; no new Git repository or remote has been
created yet. Dependency installation needs a package index; running the demo does not.

`requirements.txt` contains only `requests`. `requirements-dev.txt` includes
that runtime dependency and `pytest`. No Google Sheets SDK or credentials
are required. These files are not version-pinned lockfiles.

### Windows PowerShell

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements-dev.txt
New-Item -ItemType Directory -Path .\demo-output
.\venv\Scripts\python.exe -m taoswap_demo --output-dir .\demo-output
```

### Linux / macOS

```bash
python3 -m venv venv
./venv/bin/python -m pip install -r requirements-dev.txt
mkdir demo-output
./venv/bin/python -m taoswap_demo --output-dir ./demo-output
```

Choose a new local directory for the directory-creation step. The CLI itself
requires an existing directory and never creates it. Quote paths containing
spaces. URLs, UNC paths, drive-relative paths, and control characters are rejected;
Windows absolute drive paths are accepted only on Windows.

Output: `demo-output/taoswap-diagnostic.json`. Running the demo again replaces
this file with identical bytes. Successful stdout identifies the offline
synthetic demo, output path, global status, and counters.

The demo includes subnet IDs 0, 2, and 5, a nullable name, high-precision Decimal
metrics, local `unknown` and `incomplete` results, and a synthetic timeout.
Its global status is intentionally `incomplete`: 3 available records and 7
observed records illustrate that failed traversals can report observations whose
rows are unavailable. Counters do not count globally distinct validators.

## Testing

With development requirements installed, run the full suite from the repository root:

```powershell
# Windows PowerShell
.\venv\Scripts\python.exe -m pytest -q
```

```bash
# Linux / macOS
./venv/bin/python -m pytest -q
```

Tests use synthetic payloads, prepared models, fake HTTP responses, and temporary
directories. Tests exercising HTTP, collection, diagnostics, writer, and demo
paths block external network operations with socket/request mocks; import tests
also guard against external initialization. Pure model tests require no I/O.
No live API or Google Sheets credentials are needed. Some symlink tests may skip
on Windows when the account lacks permission to create symlinks.

## Data Integrity and Safety

- **Decimal preservation:** the HTTP client parses JSON fractional numbers directly
  into `Decimal`; validation rejects booleans and non-finite values. Diagnostic
  metrics are strings preserving digits, scale, and signed zero. Conversion from
  an already rounded Python float cannot recover its original digits.
- **Bounded pagination:** caller budgets limit pages and subnets. The client permits
  page sizes from 1 to 500 and enforces the inspected contract's offset guard of
  10,000. There are no automatic retries. Budgets and per-request timeouts do not
  constitute an overall wall-clock deadline.
- **Explicit uncertainty:** collectors report `incomplete` for known shortfalls
  and `unknown` when completeness cannot be established. The accepted collectors
  do not emit `complete`. Offset pagination provides no snapshot-consistency proof,
  even when counts match and every advertised page has been read.
- **No silent deduplication:** repeated observations and changing UID/hotkey
  associations stop per-subnet traversal diagnostically; received rows remain in
  the returned diagnostic collection. Metadata changes also produce uncertainty.
  A failed traversal exposes progress counters, not its partial rows.
- **Redacted diagnostics:** exported records omit `hotkey` and `identity`.
  In-memory models retain them. Other fields, such as subnet names, are not
  guaranteed to be anonymous.
- **Atomic replacement:** the writer validates serialized UTF-8 JSON against a
  **10 MiB** size limit before filesystem I/O. It writes a temporary file in the
  output directory, flushes and fsyncs it, then uses `os.replace()` for the fixed
  `taoswap-diagnostic.json` target. Failures before replacement preserve an existing
  regular target. Cleanup failures are reported and can leave a temporary file.
- **Trusted directory required:** the directory must already exist. Final-component
  symlinks and Windows reparse points are rejected. Ancestor links and concurrent
  path substitution are not secured against; the directory and ancestors must be
  trusted. Replacement does not preserve the old file's mode or ACL. Concurrent
  writers are last-writer-wins. The directory is not fsynced, so atomic visibility
  does not guarantee power-loss durability.

## Offline CI

`.github/workflows/pytest.yml` defines **Offline pytest**, triggered by `push`
and `pull_request`. It uses `ubuntu-latest` and Python 3.12, installs
`requirements-dev.txt`, and runs `python -m pytest -q -rs -p no:cacheprovider`
with a 10-minute job timeout. It uses no GitHub Secrets and does not access
Google Sheets or the TaoSwap API. There is no schedule,
manual dispatch, artifact publication, or deployment. Token permissions are limited
to `contents: read`, and checkout does not persist credentials.

Dependency installation uses the network; **Offline** refers to the synthetic
test scenarios, not complete network isolation of the runner. Hosted CI has not
yet been verified by an actual run. The standalone project has not been published.

## Limitations

The offline CLI is a working, reproducible diagnostic demonstration. The TaoSwap
client and collectors are programmatic components tested with fakes. Production
API behavior, current live API availability, and schema stability have not been
verified for this standalone export. There is no production publisher or
Google Sheets export.

This project does not claim a production-ready pipeline, a complete export, or
a consistent cross-page/cross-subnet snapshot. TaoSwap metric names are preserved;
metric units and equivalence to metrics from other sources are not assumed.
Diagnostic JSON reports observations and uncertainty; it does not authorize
publishing an incomplete dataset to production.
