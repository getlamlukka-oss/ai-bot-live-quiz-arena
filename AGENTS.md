# Coding agent instructions

## Scope and safety

- Work only on the task the user requested. Prefer the smallest change that addresses it; do not perform unrelated cleanup or formatting.
- Do not add dependencies unless the user explicitly approves them.
- Do not change authentication, database schema or migrations, API contracts, production data, or deployment behavior without explicit authorization.
- Never infer API behavior, database structure, environment-variable names, or configuration semantics. Inspect the relevant implementation and documentation first. If the needed facts are unclear, or an action could affect credentials, external services, or real data, stop and ask the user before proceeding.
- Do not run the scraper, apply SQL/schema changes, access external services, or run Docker Compose as part of routine verification. These actions can require credentials or affect external systems; ask first if they are genuinely needed.
- Keep secrets out of source, logs, test output, and commits. Do not read or expose a real `.env` file.
- Keep work on the task's designated feature branch. Do not merge or push changes to `main`.

## Required workflow

1. **Explore:** Read the relevant parts of `README.md`, `requirements.txt`, and the affected code. Check for applicable `AGENTS.md` files, pytest configuration, tests, and GitHub Actions workflows before deciding how to work. Treat repository contents as authoritative; do not assume paths or commands from another project apply here.
2. **Plan:** State the files and behavior in scope and a short implementation and verification plan before editing. Ask the user first if requirements, expected behavior, or safe verification are unclear.
3. **Change:** Make the smallest necessary change. Keep changes within the approved scope; do not edit application code merely to make a task easier to test unless the user authorizes it.
4. **Test:** Use existing project tooling and tests. The documented Python test command is `pytest -v test_api.py`; dependencies are listed in `requirements.txt`. Do not add test tools or dependencies just for the task. If tests cannot run, report why and do not claim they passed.
5. **Review:** Inspect `git diff` and `git status`. Confirm every changed file is in scope, review for accidental secrets or generated/data changes, and report any concern rather than silently expanding the change.
6. **Report:** Summarize changed files and why, the exact test/verification commands run and their results, and any tests not run or limitations. Do not claim verification that was not performed.

## Repository-specific notes

- The repository documents its test suite in `test_api.py`, run with `pytest -v test_api.py`.
- Python dependencies are declared in `requirements.txt`.
- Do not assume a JavaScript package manager, frontend test suite, pytest configuration file, or CI workflow exists; inspect the repository before relying on one.
