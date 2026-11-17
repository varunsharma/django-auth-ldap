# CI test replay

Each supervised tox test environment uses the existing Django test command
with `ci_replay.ReplayDiscoverRunner`. The runner retains Django discovery and
database lifecycle and keeps each module's and each test class's fixtures
contiguous. The prior and anticipated phases select whole modules whenever a
test in that module is selected, so sibling tests in that module can run in the
same phase. Within modules and classes, tests are ordered by previous failure,
interrupted status, affected or historically risky status, and the remaining
suite.

New import or collection errors run after prior failures and interrupted tests,
but before ordinary affected or historically risky tests, so a collection
failure does not let unrelated test bodies run first.

Travis matrix jobs continue independently when another job fails; `fast_finish`
does not cancel their running siblings. This configuration now has one
Travis-generated job on Ubuntu Focal with Python 3.8.20 as its host runtime.
`before_install` uses pyenv pinned to commit
`03ffc48ef3cd33808899773df586f3cc7f74470a` to build Python 3.5.10, 3.6.15, and
3.7.17. The fetch is limited to two minutes, each interpreter build to ten
minutes, and installation of the repository-tested tox 3.28.0 release to five
minutes. Pinning tox preserves the legacy tox configuration contract; a
timeout fails setup instead of leaving the Travis job occupied until its
provider limit. It then passes
the complete `tox -l` environment list to
`ci_tox_matrix.py`. Its 40-minute overall deadline is shared across all phases,
and each tox child has a 30-minute deadline. The supervisor first runs all
previously failed or interrupted tests across test environments, up to five
concurrently. After that barrier passes, it runs the anticipated phase for
affected or historically risky tests across the environments. It then runs all
complete canonical suites, again up to five concurrently. Lint and documentation
start only after all three test phases pass. Each tox environment runs in a detached
checkout of the same commit with a separate tox work directory; the supervisor
passes the same absolute restored cache directory into each checkout. A failure
stops queued work and terminates/reaps active child process groups. SIGTERM
triggers the same cancellation and worktree cleanup. Travis itself has no
sibling jobs left to continue. The supervisor never cancels another build, PR,
branch, or commit.

The tox environment set retains Python 3.5 through 3.7 with Django 1.11, 2.1,
and 2.2, plus Django master, lint, and documentation environments.
The two Django-master lanes were previously marked allowed failures. They now
run as required environments; their first failure stops the current build.
This is an intentional CI behavior change.

Replay state in `.ci-replay-cache/` contains test identifiers, bounded risk
counts, and CI context, never test output. Its stable cache scope uses
repository, base ref, PR number, head ref, tox lane, and Python version. Each
unresolved test retains its originating commit SHA, PR and branch context, run,
job, lane, and runtime until a clean pass; skipped and expected-failure results
do not clear prior failure provenance. A base SHA or run attempt is recorded
when the provider exposes it. Overall run contexts and unresolved per-test
provenance are retained without count-based truncation. If the cache exceeds
the 8 MiB safety bound, checkpointing stops rather than discarding unresolved
provenance.

If a previously failed or interrupted test ID is absent from the newly
collected suite, the runner attempts to checkpoint history and aborts before
executing the suite. Restore the test or explicitly resolve the stale history
entry after review; an incomplete collection cannot produce a passing build.

For Travis pull requests, provenance records the PR source repository from
`TRAVIS_PULL_REQUEST_SLUG` and source head from `TRAVIS_PULL_REQUEST_SHA`
separately from `sha`, which is the checked-out commit tested by the job (often
a Travis-created merge commit). The base SHA is taken from the first side of
`TRAVIS_COMMIT_RANGE` only when both sides match full 40-character hexadecimal
SHAs separated by `...`; empty or malformed ranges leave `base_sha` blank.
Travis exposes only the `TRAVIS_JOB_RESTARTED` boolean, not a numeric retry
count, so `run_attempt` records `travis-initial` or `travis-restarted` when that
boolean is valid and `job_restarted` preserves the boolean itself. Build and job
IDs remain recorded independently. These fields identify the available
provider context without treating the tested merge SHA as the PR source SHA or
claiming an exact retry ordinal.

Travis restores cache archives for pull-request builds, but only saves cache
modifications from normal branch pushes. Therefore changes made during a PR
build are not guaranteed to persist into its next PR run. The cache expires
after 45 days and cache writes can fail; a cold, invalid, legacy, or unavailable
cache runs all discovered tests as the broadest anticipated set before the
canonical full suite. Skips and expected failures do not satisfy unresolved
previous failures or interruptions; the run stays failed until a clean pass.
Replay persistence is best effort, not durable history.
Concurrent runs on the same branch/PR cache scope can overwrite each other's
snapshots.
Invalid, oversized, inaccessible, or symlinked history is never trusted, and
checkpointing is disabled when the cache path is unsafe or unavailable.

Focused adapter checks: `python -m unittest discover -s ci_replay_tests`. The
replay adapter uses Python 2.7-compatible syntax. The supervisor uses Python
3.5+ standard-library APIs. Its focused tests verify five simultaneous
children, first-failure queue suppression, process-group cancellation, and
timeouts. I built the exact Python 3.5.10, 3.6.15, 3.7.17, and 3.8.20
interpreters in an Ubuntu 20.04 container using the pinned pyenv revision and
the packages listed in `.travis.yml`. This validates that provisioning is
feasible on Focal, but is not evidence from a Travis worker. The compiled job
graph and complete matrix still need validation on Travis before claiming
provider validation.

The pinned pyenv tree and interpreters are under the explicit Travis cache path
`$HOME/.cache/ci-pyenv-v2.8.15`; this can reduce setup time after a successful
normal branch push. It does not change the cold pull-request bound because
Travis does not persist cache modifications made by pull-request builds. The
configured cold upper bounds are 32 minutes for pyenv fetch/builds, 5 minutes
for tox installation, 5 minutes for focused adapter tests, and 40 minutes for
the supervised matrix: 82 minutes before Travis setup and apt installation.
Public Travis jobs have a 50-minute cap. Therefore these configured bounds do
not provide a cold-build completion guarantee within the provider limit, and
the current job cannot be described as meeting a whole-run budget with setup
headroom. A faster warm cache is not a substitute for cold-path validation.
Do not claim this rollout is provider-ready until the cold setup and full
matrix are measured within a smaller explicit end-to-end budget, or the CI
platform provides a supported pre-provisioned runtime image / longer job budget
while preserving same-build cancellation and all test environments.
