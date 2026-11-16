# CI test replay

Each Travis tox test job uses the existing Django test command with
`ci_replay.ReplayDiscoverRunner`. The runner retains Django discovery and
database lifecycle, keeps test class fixtures contiguous, then orders previous
failure, interrupted tests, affected or historically risky tests, and the
remaining complete suite. It stops its current Django test process at the first
failure; tox then marks that matrix job failed.

Travis runs tox environments as separate matrix jobs. The existing
`fast_finish: true` setting does not stop sibling jobs after one test job fails.
Travis automatic cancellation applies to older queued builds on the same branch
or pull request, not running sibling jobs in the same build. This adapter does
not call an external cancellation API. Repository settings and the Travis matrix
remain unchanged.

The existing matrix includes Python 3.5 through 3.7 with Django 1.11, 2.1, and
2.2, plus Django master jobs; lint and documentation jobs remain unchanged.

Replay state in `.ci-replay-cache/` contains test identifiers, bounded risk
counts, and limited run context, never test output. State is scoped by
repository, branch, tox lane, and Python version. Travis workspace/cache
retention is best effort; a cold cache still runs the complete suite. Invalid,
oversized, inaccessible, or symlinked history is ignored and checkpointing is
disabled when the cache path is unsafe or unavailable.

Focused adapter checks: `python -m unittest discover -s ci_replay_tests`. The
adapter uses Python 2.7-compatible syntax, and should be checked on the
repository's oldest Python 3.5 runtime when available.
