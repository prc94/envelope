#!/bin/sh
# Mark OpenSpec tasks complete: scripts/done.sh 1.3 3.1 ...
f=openspec/changes/add-envelope-core/tasks.md
for id in "$@"; do sed -i "s/^- \[ \] $id /- [x] $id /" "$f"; done
grep -c '^- \[x\]' "$f"
