#!/usr/bin/env bash
#
# Claude Code PreToolUse hook for Write|Edit.
#
# Maps the file being edited onto an oamx contributor skill and injects that
# skill's SKILL.md into the model context via
# hookSpecificOutput.additionalContext, so the conventions land before the
# edit rather than during review.
#
# One skill per file, no stacking: oamx is five modules and a test suite, and
# a rule that spans two of them is cross-referenced in prose rather than
# injected twice.
#
# Wired via .claude/settings.json. This hook must never block a tool call —
# missing jq, an unexpected stdin shape, or an absent skill file all exit 0
# silently.

set -uo pipefail
trap 'exit 0' ERR

command -v jq >/dev/null 2>&1 || exit 0

input=$(cat)
file_path=$(echo "$input" | jq -r '.tool_input.file_path // ""')
session_id=$(echo "$input" | jq -r '.session_id // ""')

[ -z "$file_path" ] && exit 0
[ -z "$session_id" ] && exit 0

repo_root=$(cd "$(dirname "$0")/../.." && pwd)

# Containment guard, then match on the repo-relative path.
#
# A Claude Code session can hold more than one repository at once, and the
# patterns below are ordinary enough ("tests/", "oamx/cli.py") to match a file
# in an unrelated project. Matching absolute paths with a leading */ wildcard
# would inject oamx's conventions into somebody else's test edits, which is
# worse than not firing at all. Only files under this repo are ours.
case "$file_path" in
    "$repo_root"/*) rel="${file_path#"$repo_root"/}" ;;
    *)              exit 0 ;;
esac

skill=""
case "$rel" in
    oamx/model.py)   skill=oamx-model  ;;
    oamx/reader.py)  skill=oamx-reader ;;
    oamx/select.py)  skill=oamx-select ;;
    oamx/cli.py|oamx/integrations.py|oamx/__main__.py)
                     skill=oamx-cli    ;;
    tests/*)         skill=oamx-tests  ;;
esac

[ -z "$skill" ] && exit 0

# Session-scoped sentinel: a skill is injected on its first matching edit and
# stays quiet for the rest of the session.
state_dir="${TMPDIR:-/tmp}/oamx-skills-$session_id"
mkdir -p "$state_dir"
sentinel="$state_dir/$skill"
[ -f "$sentinel" ] && exit 0

skill_path="$repo_root/.claude/skills/$skill/SKILL.md"
[ -f "$skill_path" ] || exit 0

touch "$sentinel"
body=$(cat "$skill_path")
header="Auto-loading skill $skill (matched on file path; first edit this session)."

jq -n --arg content "$header

$body" '
    {
        hookSpecificOutput: {
            hookEventName: "PreToolUse",
            additionalContext: $content
        }
    }
'

exit 0
