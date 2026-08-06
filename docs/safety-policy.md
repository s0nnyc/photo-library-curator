# Safety policy

## Default behaviour

Every scan is read-only. The scanner stores metadata and hashes in its own catalogue database; it never changes the source files.

## No permanent deletion

Duplicate detection never deletes a source file. Exact-duplicate cleanup is
explicitly opt-in through `--apply`; it moves additional copies to a recoverable
sibling folder and records each move in an undo log. Visual similarity cleanup
requires a selection in the local review page.

## Review decisions

The legacy visual-review server stores group-level labels and notes in the
project's SQLite catalogue only. Those decisions cannot change a source file.
The newer AI-cleanup page performs a recoverable move only when the person
using the page submits an explicit cleanup approval.

## Cleanup plans

The cleanup-plan generator is report-only. It may recommend a keeper from a
confirmed **Keep one** group, but it cannot operate on files.

## AI-assisted cleanup

The normal workflow uses only high-confidence visual matches and exact content
duplicates. It automatically chooses the keeper with the highest resolution,
then largest file size. Before every browser-approved recovery move, it
validates the file size and hash and writes an undo log after every move.

## Reversible organisation

Organisation starts with a dry run. `--apply` records each approved move with
original and destination paths in a sibling undo log. It never overwrites an
existing destination.

## Active copy operations

The source library must be stable before a content-hash or metadata scan. We will not scan while an `rsync` operation is still writing to it.
