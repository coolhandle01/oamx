#!/usr/bin/env bash
#
# Claude Code SessionStart hook.
#
# CLAUDE.md already carries the standing rules. This hook exists for the one
# thing a static file cannot report: where the working tree actually is right
# now. "Do not work on main" is only actionable if you know you are on main,
# and by the time an edit reveals it you have already made it.
#
# Wired via .claude/settings.json. Never fails a session start; covered by
# .claude/hooks/test-hooks.sh.

set -uo pipefail
trap 'exit 0' ERR

repo_root=$(cd "$(dirname "$0")/../.." && pwd)
cd "$repo_root" 2>/dev/null || exit 0

branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null) || exit 0
[ -z "$branch" ] && exit 0

msg="oamx: on branch '$branch'."

case "$branch" in
    main)
        msg="$msg Work does not land directly on main - cut a <type>/<short-description> branch before your first edit (see CLAUDE.md)."
        ;;
    HEAD)
        msg="oamx: detached HEAD. Check out a branch before editing, or the commits will be unreachable."
        ;;
    claude/*)
        msg="$msg That is a harness-synthetic name; the convention is <type>/<short-description>. If a real PR branch already exists for this work, switch to it and surface the mismatch before making changes."
        ;;
    feat/*|fix/*|docs/*|chore/*|refactor/*|test/*|perf/*|ci/*)
        ;;
    *)
        msg="$msg The convention is <type>/<short-description> (feat/, fix/, docs/, chore/, refactor/, test/, perf/, ci/)."
        ;;
esac

msg="$msg Fixing a bug? Commit the failing test first, then the fix."

if command -v jq >/dev/null 2>&1; then
    jq -n --arg content "$msg" \
        '{hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $content}}'
else
    printf '%s\n' "$msg"
fi

exit 0
