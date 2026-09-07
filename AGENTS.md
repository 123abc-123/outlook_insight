# Repository working instructions

## Start of every task

1. Run `git status --short --branch` before changing files. Never discard another machine's uncommitted work.
2. When the working tree is clean and the task needs the latest shared state, run `git pull --ff-only origin main`.
3. Read `LATEST_CHANGES.md` first, then read the dated document linked from it.
4. Treat the dated handoff document and Git history as context, not as a replacement for inspecting the current code.

## End of a material change

1. Update `LATEST_CHANGES.md` with the date, behavior changes, important files, validation results, and the newest dated handoff document.
2. Create a dated `docs/MMDD_*.md` file when the change affects architecture, API contracts, prompts, evidence policy, or report behavior.
3. Run the relevant tests and record the actual result. Do not claim that a change is deployed or pushed until the remote commit has been verified.

