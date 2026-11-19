# Upstream synchronization

This fork tracks the original project at `ayamedog/fgo-stat-exporter`, using its current default branch `ayamedog/desktop`.

## Branch roles

- `ayamedog/desktop`: upstream's branch name; the automation fetches it directly from the original repository.
- `php-raw-export`: this fork's PHP raw-response export development branch.
- `upstream-sync`: temporary review branch produced by the automation. It is proposed back into `php-raw-export` by a pull request.

## Automation

The **Upstream Sync** workflow runs weekly (Monday 03:15 UTC) and can also be started manually from GitHub Actions. It:

1. Checks out the latest `php-raw-export` branch.
2. Fetches the original repository's `ayamedog/desktop` branch.
3. Attempts a normal Git merge, preserving local changes. A merge conflict stops the workflow.
4. Verifies that the PHP raw-export regression test remains present.
5. Runs the same Ruff and pytest checks used by the project test workflow.
6. Only after all checks pass, updates `upstream-sync` and creates or updates a pull request targeting `php-raw-export`.

The workflow never merges a pull request and never publishes an EXE or APK. Review and merge the PR manually. If upstream changes overlap with PHP export code, resolve the conflict and rerun the workflow rather than discarding either side.

## GitHub permissions

The workflow uses the repository-scoped `GITHUB_TOKEN` with only `contents: write` and `pull-requests: write`. No personal access token or account credentials are required.
