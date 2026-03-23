#!/bin/bash
# Auto-lint Python files changed in the working tree
files=$(git diff --name-only --diff-filter=d HEAD 2>/dev/null | grep '\.py$')
if [ -n "$files" ]; then
  ruff check --fix $files 2>/dev/null
  ruff format $files 2>/dev/null
fi
