# Supported platforms

Tamias is a local, single-user, loopback proxy. That scope is what makes the
platform matrix small: it only needs to run on the machine that runs your agent.

## Python

- Python **3.11 or newer**.
- CI (`.github/workflows/ci.yml`) runs the full test suite on CPython 3.11,
  3.12, 3.13, and 3.14, and builds and smoke-installs the wheel.

## Operating systems

| OS | Status | Notes |
| --- | --- | --- |
| Linux | Supported | Primary CI target. |
| macOS | Supported | Same code path as Linux. |
| Windows | Supported with caveats | See below. |
| WSL2 | Supported | Treat as Linux; loopback binding works as normal. |

The package is pure Python (`py3-none-any` wheel), so there is nothing to
compile.

## Windows caveats

- **Database permissions are not enforced.** Tamias tightens the log database
  to owner-only with `os.chmod(0o600)`, which is a **no-op on Windows**. To keep
  the database private on Windows, place it on a directory whose NTFS ACLs
  restrict other users, or use an encrypted volume. This is recorded in
  `docs/SECURITY-AUDIT-2026-10-10.md`.
- **Loopback binding requires no administrator rights.** Binding to a
  non-loopback host requires `--auth-token` on every platform.
- Paths are handled with `pathlib`; no POSIX-only path logic is used.

## Networking

- The proxy binds `127.0.0.1` by default. Exposing it wider requires an explicit
  `--host` **and** `--auth-token`; `serve` refuses a non-loopback bind without a
  token.
- TLS is **not** provided by tamias (see the "does not do" section of the
  README). If a client insists on an `https://` base URL, terminate TLS in front
  of tamias (for example with a local reverse proxy).
