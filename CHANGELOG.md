# Changelog

## v2.1.1

- Download assignment attachments together with the assignment into its code folder, reusing local originals without overwriting student work.
- Keep legacy attachment folders untouched and give every assignment its own indexed attachment copies.
- Recover orphaned download markers automatically using an OS-held mutex that is released even when the process is terminated.
- Keep concurrent downloads from separate processes protected; never remove the persistent guard file.
- Record unexpected per-file failures without blocking later queue items or retries.
- Add regression coverage for empty legacy markers, failed files, live contention, abrupt process exit and queue restart after an index error.

## v2.1.0

- Add the provided Canvas account-settings screenshot to the setup wizard and documentation.
- Explain the exact “创建新访问许可证” entry point and retain placeholders for screenshots not yet provided.
- Include the image and README in the portable download; serve only the explicitly allowed image path.
- Keep the assignment dashboard, selected downloads, account setup, semester initialization, configurable folder rules and no-overwrite protections.
- Verify 34 Python tests, 5 frontend logic tests, clean-package setup flow and credential exclusion locally on Windows.

Browser visual testing remains pending because the browser automation connection was unavailable. The CI workflow checks offline behavior on Windows and Linux; macOS has not been tested on a physical machine.
