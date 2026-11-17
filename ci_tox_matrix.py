"""Run tox environments concurrently and stop this job on first failure.

The supervisor owns only its child process groups. It does not cancel other
Travis jobs, builds, pull requests, or commits.
"""
from __future__ import print_function

import argparse
import errno
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

MAX_JOBS = 5
GIT_TIMEOUT = 5
_STOP_REQUESTED = False


class SupervisorCancelled(Exception):
    pass


def _request_stop(signum, frame):
    global _STOP_REQUESTED
    _STOP_REQUESTED = True


def _check_stop():
    if _STOP_REQUESTED:
        raise SupervisorCancelled()


def _run_bounded(command, timeout=GIT_TIMEOUT):
    """Run a local command with a hard deadline and reap it on timeout."""
    process = subprocess.Popen(command, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    try:
        output, error = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise
    if process.returncode:
        raise subprocess.CalledProcessError(
            process.returncode, command, output=output, stderr=error)
    return output


def tox_command(environment, workdir_root='.tox-ci-matrix'):
    """Build an isolated tox invocation for one configured environment."""
    if not re.match(r'^[A-Za-z0-9_.-]+$', environment):
        raise ValueError('invalid tox environment name')
    workdir = os.path.abspath(os.path.join(workdir_root, environment))
    return ['tox', '--workdir', workdir, '-e', environment]


def create_worktrees(environments, repository, worktree_root):
    """Create one detached checkout per tox environment at the same HEAD."""
    repository = os.path.abspath(repository)
    worktree_root = os.path.abspath(worktree_root)
    if os.path.exists(worktree_root):
        raise ValueError('worktree root already exists: %s' % worktree_root)
    os.makedirs(worktree_root)
    worktrees = {}
    try:
        for environment in environments:
            _check_stop()
            if not re.match(r'^[A-Za-z0-9_.-]+$', environment):
                raise ValueError('invalid tox environment name')
            path = os.path.join(worktree_root, environment)
            _run_bounded([
                'git', '-C', repository, 'worktree', 'add', '--detach',
                path, 'HEAD'])
            worktrees[environment] = path
    except Exception:
        remove_worktrees(repository, worktrees)
        shutil.rmtree(worktree_root, ignore_errors=True)
        raise
    return worktrees


def remove_worktrees(repository, worktrees):
    """Remove only worktrees created by this supervisor invocation."""
    failures = []
    for path in worktrees.values():
        try:
            _run_bounded(['git', '-C', repository, 'worktree', 'remove',
                          '--force', path])
        except (OSError, subprocess.CalledProcessError,
                subprocess.TimeoutExpired) as exc:
            failures.append('%s (%s)' % (path, exc))
    if failures:
        raise RuntimeError('Unable to remove supervisor worktree(s): %s' %
                           '; '.join(failures))


def _signal_group(process, signum):
    try:
        os.killpg(process.pid, signum)
    except OSError as exc:
        if exc.errno != errno.ESRCH:
            raise


def _has_exited_without_reaping(process):
    """Observe exit while keeping the group leader PID reserved."""
    info = os.waitid(os.P_PID, process.pid,
                     os.WEXITED | os.WNOHANG | os.WNOWAIT)
    return info is not None


def _stop_all(processes, grace):
    for process in processes:
        _signal_group(process, signal.SIGTERM)

    deadline = time.monotonic() + max(0, grace)
    while time.monotonic() < deadline:
        if all(_has_exited_without_reaping(process) for process in processes):
            break
        time.sleep(.05)

    # Keep leaders unreaped until their groups receive KILL. This prevents a
    # completed leader PID from being recycled as an unrelated process-group ID.
    for process in processes:
        _signal_group(process, signal.SIGKILL)
    for process in processes:
        try:
            process.wait()
        except OSError:
            pass


def _poll_and_clean_group(process, grace):
    """Reap a completed leader only after stopping its remaining group."""
    if not _has_exited_without_reaping(process):
        return None
    _signal_group(process, signal.SIGTERM)
    if grace > 0:
        time.sleep(min(grace, .05))
    _signal_group(process, signal.SIGKILL)
    return process.wait()


def run_matrix(environments, command_factory, jobs=5, timeout=2400,
               overall_timeout=2700, terminate_grace=10, cwd_factory=None,
               environment=None, deadline=None):
    """Run up to *jobs* commands; never start queued work after a failure.

    Return zero only when every environment succeeds. A timeout returns 124;
    a child failure returns its exit status. Process groups are terminated and
    reaped before returning.
    """
    if (jobs < 1 or jobs > MAX_JOBS or timeout <= 0 or overall_timeout <= 0 or
            terminate_grace < 0):
        raise ValueError('jobs must be 1-%d; timeouts positive; grace nonnegative' %
                         MAX_JOBS)

    queue = list(environments)
    active = {}
    first_failure = None
    overall_deadline = (deadline if deadline is not None else
                        time.monotonic() + overall_timeout)
    try:
        while queue or active:
            if first_failure is not None or _STOP_REQUESTED:
                if first_failure is None:
                    first_failure = 128 + signal.SIGTERM
                break

            now = time.monotonic()
            if now >= overall_deadline:
                first_failure = 124
                break
            for process, (name, deadline) in list(active.items()):
                result = _poll_and_clean_group(process, terminate_grace)
                if result is not None:
                    del active[process]
                    if result:
                        first_failure = result
                        break
                elif now >= deadline:
                    first_failure = 124
                    break

            if first_failure is not None:
                break

            while (queue and len(active) < jobs and
                   time.monotonic() < overall_deadline and
                   not _STOP_REQUESTED):
                name = queue.pop(0)
                try:
                    command = command_factory(name)
                    now = time.monotonic()
                    for process, (_, deadline) in list(active.items()):
                        result = _poll_and_clean_group(
                            process, terminate_grace)
                        if result is not None:
                            del active[process]
                            if result:
                                first_failure = result
                                break
                        elif now >= deadline:
                            first_failure = 124
                            break
                    if first_failure is not None:
                        break
                    process = subprocess.Popen(
                        command, start_new_session=True,
                        cwd=cwd_factory(name) if cwd_factory else None,
                        env=environment)
                except Exception as exc:
                    print('Could not start tox environment %s: %s' % (name, exc),
                          file=sys.stderr)
                    first_failure = 127
                    break
                active[process] = (name, time.monotonic() + timeout)
                if _STOP_REQUESTED:
                    first_failure = 128 + signal.SIGTERM
                    break

            if first_failure is not None:
                break
            if queue or active:
                time.sleep(.05)
    except KeyboardInterrupt:
        first_failure = 130
    finally:
        if _STOP_REQUESTED and first_failure is None:
            first_failure = 128 + signal.SIGTERM
        if first_failure is not None:
            _stop_all(list(active), terminate_grace)

    return first_failure or 0


def run_build_phases(test_environments, quality_environments, jobs=MAX_JOBS,
                     timeout=2400, overall_timeout=2700,
                     terminate_grace=10, command_factory=None,
                     cwd_factory=None, environment=None):
    """Run all phases within one shared overall deadline."""
    command_factory = command_factory or tox_command
    base_environment = dict(os.environ if environment is None else environment)
    overall_deadline = time.monotonic() + overall_timeout
    phases = [('prior', test_environments),
              ('anticipated', test_environments),
              ('canonical', test_environments),
              ('canonical', quality_environments)]
    for phase, environments in phases:
        if not environments:
            continue
        remaining = overall_deadline - time.monotonic()
        if remaining <= 0:
            return 124
        phase_environment = dict(base_environment)
        phase_environment['CI_REPLAY_PHASE'] = phase
        result = run_matrix(
            environments, command_factory, jobs=jobs, timeout=timeout,
            overall_timeout=remaining, terminate_grace=terminate_grace,
            cwd_factory=cwd_factory, environment=phase_environment,
            deadline=overall_deadline)
        if result:
            return result
    return 0


def main(argv=None):
    global _STOP_REQUESTED
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jobs', type=int, default=MAX_JOBS)
    parser.add_argument('--timeout', type=float, default=2400)
    parser.add_argument('--overall-timeout', type=float, default=2700)
    parser.add_argument('--terminate-grace', type=float, default=10)
    parser.add_argument('environments', nargs='+')
    args = parser.parse_args(argv)
    if args.jobs < 1 or args.jobs > MAX_JOBS:
        parser.error('--jobs must be between 1 and %d' % MAX_JOBS)
    repository = os.path.abspath('.')
    _STOP_REQUESTED = False
    old_sigterm = signal.signal(signal.SIGTERM, _request_stop)
    worktree_root = None
    worktrees = {}
    try:
        worktree_root = tempfile.mkdtemp(prefix='.tox-ci-matrix-', dir=repository)
        os.rmdir(worktree_root)
        _check_stop()
        worktrees = create_worktrees(
            args.environments, repository, worktree_root)
        child_environment = os.environ.copy()
        child_environment['CI_REPLAY_CACHE_DIR'] = os.path.abspath(
            os.environ.get('CI_REPLAY_CACHE_DIR', '.ci-replay-cache'))
        os.makedirs(child_environment['CI_REPLAY_CACHE_DIR'], exist_ok=True)
        tests = [name for name in args.environments
                 if re.match(r'^py[0-9]', name)]
        quality = [name for name in args.environments if name not in tests]
        return run_build_phases(
            tests, quality, jobs=args.jobs,
            timeout=args.timeout, overall_timeout=args.overall_timeout,
            terminate_grace=args.terminate_grace,
            cwd_factory=worktrees.__getitem__, environment=child_environment)
    finally:
        try:
            remove_worktrees(repository, worktrees)
        except Exception:
            # Keep the directory as evidence if Git could not unregister a
            # worktree; deleting it would leave stale .git/worktrees entries.
            raise
        else:
            if worktree_root:
                shutil.rmtree(worktree_root, ignore_errors=True)
        finally:
            signal.signal(signal.SIGTERM, old_sigterm)


if __name__ == '__main__':
    sys.exit(main())
