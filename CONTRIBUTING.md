# Contributing

Thank you for helping make local photo-library management safer and easier.

## Local setup

```bash
uv sync --extra dev
uv run pytest
```

Use a small, disposable test library. Do not add personal photos, videos, generated thumbnails, SQLite catalogues, model caches, or recovery folders to a commit.

## Safety requirements

- Scans and recommendations must be read-only.
- Any filesystem change must be opt-in, clearly described, and use `--apply`.
- Moves must be recoverable and logged with original and destination paths.
- Never silently overwrite a source file.
- Tests must cover file-selection and path-handling logic before an operation can move media.

## Pull requests

Keep each change focused. Explain how you tested it and call out any behaviour that can change a user's files or disk usage.
