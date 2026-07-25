## Summary

<!-- What does this PR do, and why? Link the issue it closes: "Closes #NN". -->

## Test plan

<!--
How you verified the change. Paste the relevant output from the "Before you
commit" stack in CONTRIBUTING.md - ruff, mypy, pylint, pytest with the coverage
flags, and .claude/hooks/test-hooks.sh. A bug fix needs its regression test
committed failing first; see the test-first discipline in CONTRIBUTING.md.
-->

## Out of scope

<!-- What this PR deliberately does NOT do, and where that work is tracked (an issue number, or "none"). -->

---

- [ ] Branch is `<type>/<short-description>` cut from current `main`; commits follow Conventional Commits (`cz check` gates this).
- [ ] `git diff origin/main --stat` shows only the intended changes (minimal-diff rule).
- [ ] New or changed behaviour is covered by tests; a bug fix has a regression test that was committed failing first.
- [ ] No session URLs (`https://claude.ai/code/session_...`) in this description or in any commit message - they link to a private conversation and a public repo is forever. The plain `https://claude.ai/code` attribution link is fine.
