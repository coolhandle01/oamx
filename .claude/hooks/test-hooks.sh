#!/usr/bin/env bash
#
# Behaviour tests for the .claude hooks.
#
#     .claude/hooks/test-hooks.sh
#
# Bash and jq, nothing else, matching the tool's own zero-dependency stance.
#
# These hooks fail open by design: every unexpected input exits 0 with no
# output so a tool call is never blocked. That makes silent breakage the
# expected failure mode -- a hook that stopped matching anything at all would
# look exactly like a hook with nothing to say. Hence tests that assert a
# skill *is* injected, not only that the wrong ones are not.

set -uo pipefail

here=$(cd "$(dirname "$0")" && pwd)
repo_root=$(cd "$here/../.." && pwd)
load_skill="$here/load-skill.sh"
session_start="$here/session-start.sh"

pass=0
fail=0

ok ()   { printf '  ok    %s\n' "$1"; pass=$((pass + 1)); }
bad ()  { printf '  FAIL  %s\n        %s\n' "$1" "$2"; fail=$((fail + 1)); }

check_eq () { # name expected actual
    if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "expected '$2', got '$3'"; fi
}

check_contains () { # name needle haystack
    case "$3" in
        *"$2"*) ok "$1" ;;
        *)      bad "$1" "expected to contain '$2', got: ${3:0:120}" ;;
    esac
}

# --- load-skill.sh ----------------------------------------------------------

# Session ids are namespaced per run. The hook keeps a once-per-session
# sentinel on disk, so reusing fixed ids would make a second run of this suite
# see every skill as already injected and report silence as failure.
run_token="test-$$-${RANDOM}"
trap 'rm -rf "${TMPDIR:-/tmp}/oamx-skills-$run_token"-*' EXIT

# Returns the injected skill name, or "" when the hook stayed silent.
skill_for () { # session_suffix file_path
    printf '{"session_id":"%s","tool_input":{"file_path":"%s"}}' "$run_token-$1" "$2" \
        | "$load_skill" 2>/dev/null \
        | jq -r '.hookSpecificOutput.additionalContext // ""' 2>/dev/null \
        | sed -n 's/^Auto-loading skill \([a-z0-9-]*\) .*/\1/p'
}

# Exit code for an arbitrary stdin payload.
rc_for () {
    printf '%s' "$1" | "$load_skill" >/dev/null 2>&1
    printf '%s' "$?"
}

echo "load-skill.sh"

check_eq "matches oamx/model.py"          oamx-model  "$(skill_for a "$repo_root/oamx/model.py")"
check_eq "matches oamx/reader.py"         oamx-reader "$(skill_for b "$repo_root/oamx/reader.py")"
check_eq "matches oamx/select.py"         oamx-select "$(skill_for c "$repo_root/oamx/select.py")"
check_eq "matches oamx/cli.py"            oamx-cli    "$(skill_for d "$repo_root/oamx/cli.py")"
check_eq "matches oamx/integrations.py"   oamx-cli    "$(skill_for e "$repo_root/oamx/integrations.py")"
check_eq "matches tests/test_oamx.py"     oamx-tests  "$(skill_for f "$repo_root/tests/test_oamx.py")"
check_eq "matches tests/fixtures.py"      oamx-tests  "$(skill_for g "$repo_root/tests/fixtures.py")"

# Once per skill per session: the first matching edit carries it, the rest stay quiet.
_first=$(skill_for dedupe "$repo_root/oamx/model.py")
check_eq "first edit in a session injects"  oamx-model "$_first"
check_eq "second edit in a session is quiet" ""        "$(skill_for dedupe "$repo_root/oamx/model.py")"
check_eq "a new session injects again"       oamx-model "$(skill_for dedupe-2 "$repo_root/oamx/model.py")"

# Containment. A session can hold several repositories, and these patterns are
# ordinary enough to match somebody else's files.
check_eq "ignores tests/ in another repo" "" "$(skill_for h /somewhere/else/tests/test_thing.py)"
check_eq "ignores an identical relative path elsewhere" "" \
    "$(skill_for i /other/project/oamx/model.py)"
check_eq "ignores a path that only prefixes the repo root" "" \
    "$(skill_for j "${repo_root}-evil/oamx/model.py")"

# Unmatched files in this repo.
check_eq "ignores README.md"     "" "$(skill_for k "$repo_root/README.md")"
check_eq "ignores pyproject.toml" "" "$(skill_for l "$repo_root/pyproject.toml")"

# Fails open. A PreToolUse hook exiting non-zero can block the edit.
check_eq "exit 0 on malformed json"  0 "$(rc_for 'not json at all')"
check_eq "exit 0 on empty stdin"     0 "$(rc_for '')"
check_eq "exit 0 on empty object"    0 "$(rc_for '{}')"
check_eq "exit 0 on missing session" 0 \
    "$(rc_for "{\"tool_input\":{\"file_path\":\"$repo_root/oamx/cli.py\"}}")"
check_eq "exit 0 on a match"         0 \
    "$(rc_for "{\"session_id\":\"$run_token-rc\",\"tool_input\":{\"file_path\":\"$repo_root/oamx/cli.py\"}}")"

# Every skill the hook can name has a file to load.
for skill in oamx-model oamx-reader oamx-select oamx-cli oamx-tests; do
    if [ -f "$repo_root/.claude/skills/$skill/SKILL.md" ]; then
        ok "$skill has a SKILL.md"
    else
        bad "$skill has a SKILL.md" "missing .claude/skills/$skill/SKILL.md"
    fi
done

# --- session-start.sh -------------------------------------------------------

echo
echo "session-start.sh"

# Run the hook inside a throwaway repository checked out at $1, so the
# assertions do not depend on whatever branch this suite happens to run from.
# Returns the hook's own exit status.
_in_repo_on () { # branch_name_or_DETACHED command...
    local tmp rc
    tmp=$(mktemp -d)
    (
        cd "$tmp" || exit 1
        git init -q -b main .
        git config user.email t@example.com
        git config user.name test
        mkdir -p .claude/hooks
        cp "$session_start" .claude/hooks/ 2>/dev/null
        chmod +x .claude/hooks/session-start.sh 2>/dev/null
        echo seed > seed.txt
        git add -A >/dev/null 2>&1
        git commit -qm seed >/dev/null 2>&1
        if [ "$1" = "DETACHED" ]; then
            git checkout -q --detach
        elif [ "$1" != "main" ]; then
            git checkout -q -b "$1"
        fi
        shift
        "$@"
    )
    rc=$?
    rm -rf "$tmp"
    return "$rc"
}

session_start_on () { _in_repo_on "$1" ./.claude/hooks/session-start.sh 2>/dev/null; }

_main_out=$(session_start_on main)
check_contains "warns when on main" "does not land directly on main" "$_main_out"
check_contains "names the branch"   "main"                           "$_main_out"

# Names the branch AND stays quiet. Checked together on purpose: "contains no
# warning" is trivially true of empty output, which is the assertion shape
# oamx-tests warns about.
_conventional=$(session_start_on fix/some-bug)
case "$_conventional" in
    *"does not land directly on main"*) bad "quiet on a conventional branch" "warned about main" ;;
    *"convention is"*)                  bad "quiet on a conventional branch" "nagged about naming" ;;
    *"fix/some-bug"*)                   ok  "quiet on a conventional branch" ;;
    *) bad "quiet on a conventional branch" "did not name the branch: ${_conventional:0:120}" ;;
esac

check_contains "nags an off-convention name" "convention is" "$(session_start_on wibble)"
check_contains "flags a harness-synthetic name" "harness-synthetic" \
    "$(session_start_on claude/auto-123)"
check_contains "flags a detached HEAD" "detached HEAD" "$(session_start_on DETACHED)"
check_contains "carries the test-first rule" "failing test first" "$_main_out"

# Valid JSON for the hook protocol, and never a hard failure.
if printf '%s' "$_main_out" | jq -e '.hookSpecificOutput.hookEventName == "SessionStart"' \
        >/dev/null 2>&1; then
    ok "emits SessionStart hook JSON"
else
    bad "emits SessionStart hook JSON" "got: ${_main_out:0:120}"
fi

session_start_on main >/dev/null 2>&1
_ss_rc=$?
check_eq "exit 0" 0 "$_ss_rc"

# --- settings.json ----------------------------------------------------------

echo
echo "settings.json"

settings="$repo_root/.claude/settings.json"
if jq -e . "$settings" >/dev/null 2>&1; then
    ok "is valid JSON"
else
    bad "is valid JSON" "jq could not parse $settings"
fi

for cmd in $(jq -r '.hooks[][].hooks[].command' "$settings" 2>/dev/null); do
    if [ -x "$repo_root/$cmd" ]; then
        ok "$cmd is wired and executable"
    else
        bad "$cmd is wired and executable" "not executable at $repo_root/$cmd"
    fi
done

# --- skills -----------------------------------------------------------------

echo
echo "skills"

for skill_md in "$repo_root"/.claude/skills/*/SKILL.md; do
    name=$(basename "$(dirname "$skill_md")")
    # Frontmatter has to open on line 1 and carry both keys, or the skill will
    # not register.
    if [ "$(head -1 "$skill_md")" = "---" ] \
        && grep -q '^name: ' "$skill_md" \
        && grep -q '^description: ' "$skill_md"; then
        ok "$name has well-formed frontmatter"
    else
        bad "$name has well-formed frontmatter" "missing --- / name: / description:"
    fi

    declared=$(sed -n 's/^name: //p' "$skill_md" | head -1)
    check_eq "$name frontmatter name matches its directory" "$name" "$declared"
done

# --- result -----------------------------------------------------------------

echo
if [ "$fail" -eq 0 ]; then
    echo "$pass passed"
    exit 0
fi
echo "$pass passed, $fail failed"
exit 1
