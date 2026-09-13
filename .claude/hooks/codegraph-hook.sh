#!/bin/sh
# CodeGraph prompt hook with an A/B switch for evaluating it.
#
#   claude                     -> arm "on":  inject CodeGraph context
#   CODEGRAPH_OFF=1 claude     -> arm "off": inject nothing
#
# Every prompt records "<session_id> <arm>" in .codegraph/ab-arms.log so
# scripts/codegraph-eval.py can tell an "off" session from an "on" session
# whose prompts simply never matched. Never fails the prompt.
input=$(cat)
dir="${CLAUDE_PROJECT_DIR:-.}"
arm=on
[ "$CODEGRAPH_OFF" = 1 ] && arm=off

sid=$(printf '%s' "$input" | python3 -c 'import sys, json; print(json.load(sys.stdin).get("session_id", ""))' 2>/dev/null)
[ -d "$dir/.codegraph" ] && [ -n "$sid" ] && echo "$sid $arm" >> "$dir/.codegraph/ab-arms.log"

[ "$arm" = on ] && printf '%s' "$input" | codegraph prompt-hook 2>/dev/null
exit 0
