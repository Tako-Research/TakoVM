## What & why

<!-- What does this PR change, and what problem does it solve? Link related issues. -->

## How was this tested?

<!-- Commands run, new/updated tests, manual verification against a live server, etc. -->

## Checklist

- [ ] `pre-commit run --all-files` passes (ruff lint/format + pyright)
- [ ] `TAKO_VM_SECURITY_MODE=permissive pytest tests/` passes locally
- [ ] Docs updated if behavior changed (`docs/`, README)
- [ ] PR title uses conventional-commit style (`feat:`, `fix:`, `docs:`, `chore:`) — it becomes the squash commit message
- [ ] New failure paths log and surface errors verbosely (no silent swallowing)
