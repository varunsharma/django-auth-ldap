"""Regression tests for same-job tox matrix failure containment."""
from __future__ import print_function

import os
import configparser
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from ci_tox_matrix import (
    _run_bounded, create_worktrees, remove_worktrees, run_build_phases,
    run_matrix, tox_command,
)
import ci_tox_matrix


class ToxMatrixSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix='tox-matrix-')

    def tearDown(self):
        shutil.rmtree(self.directory)

    def _command(self, env_name):
        marker = os.path.join(self.directory, env_name)
        source = (
            'import os,sys,time\n'
            'open(sys.argv[1], "w").close()\n'
            'deadline=time.time()+6\n'
            'while time.time() < deadline:\n'
            '  if len(os.listdir(sys.argv[2])) >= 5: break\n'
            '  time.sleep(.02)\n'
            'if sys.argv[3] == "fail": sys.exit(17)\n'
            'while True: time.sleep(1)\n'
        )
        return [sys.executable, '-c', source, marker, self.directory,
                'fail' if env_name == 'env0' else 'wait']

    def test_tox_config_keeps_source_distribution_installation_enabled(self):
        configuration = configparser.ConfigParser()
        configuration.read(os.path.join(os.path.dirname(__file__), '..',
                                        'tox.ini'))

        self.assertFalse(configuration.getboolean(
            'tox', 'skipsdist', fallback=False))
        self.assertNotIn('python-ldap', configuration.get('testenv', 'deps'))
        self.assertIn('CI_REPLAY_CACHE_DIR',
                      configuration.get('testenv', 'setenv'))
        self.assertIn('CI_REPLAY_PHASE',
                      configuration.get('testenv', 'setenv'))

        self.assertEqual('python3.5', configuration.get(
            'testenv:ci-replay-tests', 'basepython'))
        self.assertEqual('true', configuration.get(
            'testenv:ci-replay-tests', 'skip_install'))
        self.assertEqual(
            "{envpython} -m unittest discover -f -s ci_replay_tests -p 'test_*.py'",
            configuration.get('testenv:ci-replay-tests', 'commands'))

    def test_replay_regressions_run_with_timeout_before_tox_matrix(self):
        with open(os.path.join(os.path.dirname(__file__), '..',
                               '.travis.yml')) as stream:
            travis_config = stream.read()

        replay_stage = 'timeout --kill-after=30s 300s tox -e ci-replay-tests'
        matrix_stage = 'python ci_tox_matrix.py --timeout 1800'
        self.assertIn(replay_stage, travis_config)
        self.assertIn(matrix_stage, travis_config)
        self.assertLess(travis_config.index(replay_stage),
                        travis_config.index(matrix_stage))
        self.assertIn('set -e\n', travis_config)

    def test_travis_bootstrap_commands_have_explicit_deadlines(self):
        with open(os.path.join(os.path.dirname(__file__), '..', '.travis.yml')) as stream:
            travis_config = stream.read()

        self.assertIn('timeout --kill-after=30s 120s git -C', travis_config)
        for version in ('3.5.10', '3.6.15', '3.7.17'):
            self.assertIn(
                'timeout --kill-after=30s 600s "$PYENV_ROOT/bin/pyenv" '
                'install --skip-existing %s' % version,
                travis_config)
        self.assertIn(
            'timeout --kill-after=30s 300s pip install tox==3.28.0',
            travis_config)
        self.assertIn('    - $HOME/.cache/ci-pyenv-v2.8.15', travis_config)

    def test_supervisor_temporary_worktree_roots_are_ignored(self):
        repository = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        path = '.tox-ci-matrix-run123/py35-django111'
        result = subprocess.call(
            ['git', '-C', repository, 'check-ignore', '--no-index', path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertEqual(0, result)

    def test_worktrees_isolate_build_artifacts_and_use_the_same_revision(self):
        repository = os.path.join(self.directory, 'repository')
        worktree_root = os.path.join(self.directory, 'worktrees')
        os.mkdir(repository)
        subprocess.check_call(['git', 'init', repository],
                              stdout=subprocess.DEVNULL)
        with open(os.path.join(repository, 'source.txt'), 'w') as source:
            source.write('same revision\n')
        subprocess.check_call(['git', '-C', repository, 'add', 'source.txt'])
        subprocess.check_call(
            ['git', '-C', repository, '-c', 'user.name=Test', '-c',
             'user.email=test@example.invalid', 'commit', '-m', 'fixture'],
            stdout=subprocess.DEVNULL)

        worktrees = create_worktrees(
            ['py35-django111', 'py36-django111'], repository, worktree_root)
        try:
            first = worktrees['py35-django111']
            second = worktrees['py36-django111']
            self.assertEqual(
                subprocess.check_output(
                    ['git', '-C', repository, 'rev-parse', 'HEAD']).strip(),
                subprocess.check_output(
                    ['git', '-C', first, 'rev-parse', 'HEAD']).strip())
            self.assertEqual(
                subprocess.check_output(
                    ['git', '-C', repository, 'rev-parse', 'HEAD']).strip(),
                subprocess.check_output(
                    ['git', '-C', second, 'rev-parse', 'HEAD']).strip())
            with open(os.path.join(first, 'build-artifact'), 'w') as artifact:
                artifact.write('first')
            self.assertFalse(os.path.exists(
                os.path.join(second, 'build-artifact')))
        finally:
            remove_worktrees(repository, worktrees)

        self.assertFalse(os.path.exists(first))

    def test_tox_environments_use_distinct_work_directories(self):
        first = tox_command('py35-django111', self.directory)
        second = tox_command('py36-django111', self.directory)

        self.assertEqual(['tox', '--workdir', os.path.abspath(os.path.join(
            self.directory, 'py35-django111')), '-e', 'py35-django111'], first)
        self.assertNotEqual(first[2], second[2])

    def test_local_git_helper_kills_and_reaps_on_deadline(self):
        start = time.time()
        with self.assertRaises(subprocess.TimeoutExpired):
            _run_bounded([sys.executable, '-c', 'import time; time.sleep(10)'],
                         timeout=.1)

        self.assertLess(time.time() - start, 3)

    def test_command_runs_from_its_environment_checkout(self):
        checkout = os.path.join(self.directory, 'checkout')
        os.mkdir(checkout)
        marker = os.path.join(self.directory, 'cwd-and-cache')
        source = (
            'import os,sys\n'
            'open(sys.argv[1], "w").write(os.getcwd() + "\\n" + '
            'os.environ["CI_REPLAY_CACHE_DIR"])\n'
        )
        result = run_matrix(
            ['py35-django111'],
            lambda name: [sys.executable, '-c', source, marker],
            jobs=1, timeout=3, overall_timeout=5,
            cwd_factory=lambda name: checkout,
            environment={'CI_REPLAY_CACHE_DIR': '/shared/replay-cache'})

        self.assertEqual(0, result)
        with open(marker) as stream:
            self.assertEqual(
                checkout + '\n/shared/replay-cache', stream.read())

    def test_build_phases_barrier_tests_before_canonical_and_quality(self):
        log = os.path.join(self.directory, 'phase-log')
        source = (
            'import os,sys,time\n'
            'with open(sys.argv[1], "a") as stream:\n'
            ' stream.write(os.environ["CI_REPLAY_PHASE"] + ":" + '
            'sys.argv[2] + "\\n")\n'
            'if os.environ["CI_REPLAY_PHASE"] == "prior":\n'
            ' deadline=time.monotonic()+2\n'
            ' while time.monotonic()<deadline:\n'
            '  with open(sys.argv[1]) as stream: count=len(stream.readlines())\n'
            '  if count >= 3: break\n'
            '  time.sleep(.01)\n'
            'if sys.argv[2] == "py36" and '
            'os.environ["CI_REPLAY_PHASE"] == "prior": sys.exit(17)\n'
        )
        result = run_build_phases(
            ['py35', 'py36', 'py37'], ['flake8', 'docs'], jobs=3,
            timeout=3, overall_timeout=5, terminate_grace=.1,
            command_factory=lambda name: [sys.executable, '-c', source,
                                          log, name], environment={})

        self.assertEqual(17, result)
        with open(log) as stream:
            entries = stream.read().splitlines()
        self.assertEqual(set(['prior:py35', 'prior:py36', 'prior:py37']),
                         set(entries))
        self.assertFalse(any(':flake8' in entry or ':docs' in entry or
                             entry.startswith('canonical:')
                             for entry in entries))

    def test_build_phases_finish_replay_barrier_before_quality(self):
        log = os.path.join(self.directory, 'phase-log')
        source = (
            'import os,sys\n'
            'with open(sys.argv[1], "a") as stream:\n'
            ' stream.write(os.environ["CI_REPLAY_PHASE"] + ":" + '
            'sys.argv[2] + "\\n")\n'
        )
        result = run_build_phases(
            ['py35', 'py36'], ['flake8'], jobs=2, timeout=3,
            overall_timeout=5, command_factory=lambda name: [
                sys.executable, '-c', source, log, name], environment={})

        self.assertEqual(0, result)
        with open(log) as stream:
            phases = [entry.split(':', 1)[0]
                      for entry in stream.read().splitlines()]
        self.assertEqual(['prior', 'prior', 'anticipated', 'anticipated',
                          'canonical', 'canonical', 'canonical'],
                         phases)

    def test_build_phases_share_one_overall_deadline(self):
        log = os.path.join(self.directory, 'phase-log')
        source = (
            'import os,sys,time\n'
            'with open(sys.argv[1], "a") as stream:\n'
            ' stream.write(os.environ["CI_REPLAY_PHASE"] + "\\n")\n'
            'time.sleep(.5)\n'
        )
        start = time.time()
        result = run_build_phases(
            ['py35'], ['flake8'], jobs=1, timeout=1, overall_timeout=.2,
            terminate_grace=.05, command_factory=lambda name: [
                sys.executable, '-c', source, log], environment={})

        self.assertEqual(124, result)
        self.assertLess(time.time() - start, 1)
        with open(log) as stream:
            phases = stream.read().splitlines()
        self.assertEqual(['prior'], phases)

    def test_detached_worktree_can_write_to_restored_shared_cache(self):
        repository = os.path.join(self.directory, 'repository')
        worktree_root = os.path.join(self.directory, 'worktrees')
        shared_cache = os.path.join(repository, '.ci-replay-cache')
        os.mkdir(repository)
        subprocess.check_call(['git', 'init', repository],
                              stdout=subprocess.DEVNULL)
        with open(os.path.join(repository, 'source.txt'), 'w') as stream:
            stream.write('fixture\n')
        subprocess.check_call(['git', '-C', repository, 'add', 'source.txt'])
        subprocess.check_call(
            ['git', '-C', repository, '-c', 'user.name=Test', '-c',
             'user.email=test@example.invalid', 'commit', '-m', 'fixture'],
            stdout=subprocess.DEVNULL)
        os.mkdir(shared_cache)
        worktrees = create_worktrees(['py35'], repository, worktree_root)
        try:
            output = os.path.join(shared_cache, 'child-write')
            source = (
                'import os,sys\n'
                'path=os.path.join(os.environ["CI_REPLAY_CACHE_DIR"], '
                '"child-write")\n'
                'open(path, "w").write(os.getcwd())\n'
            )
            result = run_matrix(
                ['py35'], lambda name: [sys.executable, '-c', source],
                jobs=1, timeout=3, overall_timeout=5,
                cwd_factory=worktrees.__getitem__, environment={
                    'CI_REPLAY_CACHE_DIR': shared_cache})

            self.assertEqual(0, result)
            with open(output) as stream:
                self.assertEqual(worktrees['py35'], stream.read())
        finally:
            remove_worktrees(repository, worktrees)

    def test_sigterm_kills_and_reaps_child_process_group_and_cleans_worktrees(self):
        repository = os.path.join(self.directory, 'repository')
        binary_directory = os.path.join(self.directory, 'bin')
        os.mkdir(repository)
        os.mkdir(binary_directory)
        subprocess.check_call(['git', 'init', repository],
                              stdout=subprocess.DEVNULL)
        with open(os.path.join(repository, 'source.txt'), 'w') as stream:
            stream.write('fixture\n')
        subprocess.check_call(['git', '-C', repository, 'add', 'source.txt'])
        subprocess.check_call(
            ['git', '-C', repository, '-c', 'user.name=Test', '-c',
             'user.email=test@example.invalid', 'commit', '-m', 'fixture'],
            stdout=subprocess.DEVNULL)

        started = os.path.join(self.directory, 'started')
        marker = os.path.join(self.directory, 'orphan-marker')
        tox = os.path.join(binary_directory, 'tox')
        tox_source = (
            '#!%s\n' % sys.executable +
            'import os,signal,subprocess,sys,time\n'
            'marker=%r\n' % marker +
            'started=%r\n' % started +
            'child="import signal,time; '
            'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
            'time.sleep(1.5); open(%r, \'w\').close()"\n' % marker +
            'subprocess.Popen([sys.executable, "-c", child])\n'
            'open(started, "w").close()\n'
            'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
            'while True: time.sleep(1)\n'
        )
        with open(tox, 'w') as stream:
            stream.write(tox_source)
        os.chmod(tox, 0o755)
        supervisor_script = os.path.abspath(os.path.join(
            os.path.dirname(__file__), '..', 'ci_tox_matrix.py'))
        env = os.environ.copy()
        env['PATH'] = binary_directory + os.pathsep + env.get('PATH', '')
        env['CI_REPLAY_CACHE_DIR'] = os.path.join(
            repository, '.ci-replay-cache')
        process = subprocess.Popen(
            [sys.executable, supervisor_script, '--timeout', '10',
             '--overall-timeout', '15', '--terminate-grace', '.1', 'py35'],
            cwd=repository, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        deadline = time.time() + 5
        while time.time() < deadline and not os.path.exists(started):
            if process.poll() is not None:
                break
            time.sleep(.02)
        self.assertTrue(os.path.exists(started))
        process.send_signal(ci_tox_matrix.signal.SIGTERM)
        output, error = process.communicate(timeout=5)
        time.sleep(1.7)

        self.assertNotEqual(0, process.returncode, (output, error))
        self.assertFalse(os.path.exists(marker))
        worktree_listing = subprocess.check_output(
            ['git', '-C', repository, 'worktree', 'list', '--porcelain'])
        self.assertNotIn('.tox-ci-matrix-', worktree_listing.decode('utf-8'))

    def test_rejects_environment_names_that_could_escape_work_directory(self):
        with self.assertRaises(ValueError):
            tox_command('../outside', self.directory)

    def test_command_factory_error_cancels_already_running_processes(self):
        marker = os.path.join(self.directory, 'running')

        def command(env_name):
            if env_name == 'bad':
                deadline = time.time() + 2
                while time.time() < deadline and not os.path.exists(marker):
                    time.sleep(.01)
                raise ValueError('invalid command')
            return [sys.executable, '-c',
                    'import sys,time; open(sys.argv[1], "w").close(); '
                    'time.sleep(30)', marker]

        started = time.time()
        result = run_matrix(['slow', 'bad'], command, jobs=2, timeout=10,
                            terminate_grace=.2)

        self.assertEqual(127, result)
        self.assertLess(time.time() - started, 3)
        self.assertTrue(os.path.exists(marker))

    def test_failure_observed_before_queued_environment_starts(self):
        state = {'failure_finished': False}
        started = []

        class FakeProcess(object):
            def __init__(self, name):
                self.name = name
                self.pid = 100 + len(started)

            def poll(self):
                if self.name == 'failure':
                    return 17 if state['failure_finished'] else None
                return 0

            def wait(self):
                return self.poll()

        original_popen = ci_tox_matrix.subprocess.Popen
        original_sleep = ci_tox_matrix.time.sleep
        original_signal_group = ci_tox_matrix._signal_group
        original_waitid_check = ci_tox_matrix._has_exited_without_reaping

        def fake_popen(command, **kwargs):
            started.append(command[0])
            return FakeProcess(command[0])

        def finish_failure(_seconds):
            state['failure_finished'] = True

        try:
            ci_tox_matrix.subprocess.Popen = fake_popen
            ci_tox_matrix.time.sleep = finish_failure
            ci_tox_matrix._signal_group = lambda process, signum: None
            ci_tox_matrix._has_exited_without_reaping = (
                lambda process: process.poll() is not None)
            result = run_matrix(
                ['failure', 'success', 'queued'],
                lambda name: [name], jobs=2, timeout=10,
                terminate_grace=0)
        finally:
            ci_tox_matrix.subprocess.Popen = original_popen
            ci_tox_matrix.time.sleep = original_sleep
            ci_tox_matrix._signal_group = original_signal_group
            ci_tox_matrix._has_exited_without_reaping = original_waitid_check

        self.assertEqual(17, result)
        self.assertEqual(['failure', 'success'], started)

    def test_failure_during_command_preparation_blocks_queued_environment(self):
        state = {'failure_finished': False, 'success_polled': False}
        started = []

        class FakeProcess(object):
            def __init__(self, name):
                self.name = name
                self.pid = 100 + len(started)

            def poll(self):
                if self.name == 'failure':
                    return 17 if state['failure_finished'] else None
                if self.name == 'success':
                    if not state['success_polled']:
                        state['success_polled'] = True
                        return 0
                    return 0
                return None

            def wait(self):
                return self.poll()

        original_popen = ci_tox_matrix.subprocess.Popen
        original_signal_group = ci_tox_matrix._signal_group
        original_waitid_check = ci_tox_matrix._has_exited_without_reaping

        def fake_popen(command, **kwargs):
            started.append(command[0])
            return FakeProcess(command[0])

        def command(name):
            if name == 'queued':
                state['failure_finished'] = True
            return [name]

        try:
            ci_tox_matrix.subprocess.Popen = fake_popen
            ci_tox_matrix._signal_group = lambda process, signum: None
            ci_tox_matrix._has_exited_without_reaping = (
                lambda process: process.poll() is not None)
            result = run_matrix(
                ['failure', 'success', 'queued'], command, jobs=2,
                timeout=10, terminate_grace=0)
        finally:
            ci_tox_matrix.subprocess.Popen = original_popen
            ci_tox_matrix._signal_group = original_signal_group
            ci_tox_matrix._has_exited_without_reaping = original_waitid_check

        self.assertEqual(17, result)
        self.assertEqual(['failure', 'success'], started)

    def test_runs_five_environments_concurrently_then_cancels_siblings(self):
        environments = ['env%d' % index for index in range(8)]
        started = time.time()

        result = run_matrix(environments, self._command, jobs=5,
                            timeout=20, terminate_grace=.2)

        self.assertEqual(17, result)
        self.assertLess(time.time() - started, 8)
        self.assertEqual(set(environments[:5]), set(os.listdir(self.directory)))

    def test_rejects_concurrency_above_five(self):
        with self.assertRaises(ValueError):
            run_matrix([], lambda name: [], jobs=6)

    def test_timeout_cancels_running_environment_and_returns_nonzero(self):
        source_dir = self.directory

        def command(env_name):
            marker = os.path.join(source_dir, env_name)
            return [sys.executable, '-c',
                    'import sys,time; open(sys.argv[1], "w").close(); '
                    'time.sleep(30)', marker]

        started = time.time()
        result = run_matrix(['slow'], command, jobs=1, timeout=.2,
                            terminate_grace=.2)

        self.assertNotEqual(0, result)
        self.assertLess(time.time() - started, 3)
        self.assertTrue(os.path.exists(os.path.join(self.directory, 'slow')))

    def test_failure_cancels_descendants_in_same_process_group(self):
        marker = os.path.join(self.directory, 'orphan-wrote')
        source = (
            'import subprocess,sys,time\n'
            'subprocess.Popen([sys.executable, "-c", '
            '"import sys,time; time.sleep(.8); open(sys.argv[1], \\\"w\\\").close()", '
            'sys.argv[1]])\n'
            'time.sleep(.1)\n'
            'sys.exit(19)\n'
        )

        result = run_matrix(
            ['failing'], lambda name: [sys.executable, '-c', source, marker],
            jobs=1, timeout=5, terminate_grace=.2)
        time.sleep(1)

        self.assertEqual(19, result)
        self.assertFalse(os.path.exists(marker))

    def test_failure_cancels_descendants_after_successful_leader_exits(self):
        fail_marker = os.path.join(self.directory, 'trigger-failure')
        orphan_marker = os.path.join(self.directory, 'successful-orphan-wrote')
        first_source = (
            'import subprocess,sys\n'
            'child=("import os,sys,time\\n"'
            '"marker,trigger=sys.argv[1:]\\n"'
            '"while not os.path.exists(trigger): time.sleep(.01)\\n"'
            '"time.sleep(.5)\\n"'
            '"open(marker, \'w\').close()")\n'
            'subprocess.Popen([sys.executable, "-c", child, '
            'sys.argv[1], sys.argv[2]])\n'
        )

        def command(environment):
            if environment == 'successful-leader':
                return [sys.executable, '-c', first_source,
                        orphan_marker, fail_marker]
            return [sys.executable, '-c',
                    'import os,sys,time; open(sys.argv[1], "w").close(); '
                    'time.sleep(.1); sys.exit(19)', fail_marker]

        result = run_matrix(
            ['successful-leader', 'later-failure'], command, jobs=1,
            timeout=5, terminate_grace=.2)
        time.sleep(.7)

        self.assertEqual(19, result)
        self.assertFalse(os.path.exists(orphan_marker))

    def test_successful_phase_does_not_leave_helpers_for_later_phase(self):
        fail_marker = os.path.join(self.directory, 'next-phase-failed')
        orphan_marker = os.path.join(self.directory, 'cross-phase-orphan')
        source = (
            'import os,subprocess,sys,time\n'
            'if os.environ.get("CI_REPLAY_PHASE") == "prior":\n'
            ' child=("import os,sys,time\\n"'
            '"marker,trigger=sys.argv[1:]\\n"'
            '"while not os.path.exists(trigger): time.sleep(.01)\\n"'
            '"time.sleep(.5)\\n"'
            '"open(marker, \'w\').close()")\n'
            ' subprocess.Popen([sys.executable, "-c", child, '
            'sys.argv[1], sys.argv[2]])\n'
            'else:\n'
            ' open(sys.argv[2], "w").close()\n'
            ' time.sleep(.1)\n'
            ' sys.exit(19)\n'
        )

        def command(environment):
            return [sys.executable, '-c', source, orphan_marker, fail_marker]

        result = run_build_phases(
            ['py35'], [], jobs=1, timeout=5, overall_timeout=5,
            terminate_grace=.2, command_factory=command)
        time.sleep(.7)

        self.assertEqual(19, result)
        self.assertFalse(os.path.exists(orphan_marker))

    def test_overall_deadline_prevents_queued_environments_from_starting(self):
        def command(env_name):
            marker = os.path.join(self.directory, env_name)
            return [sys.executable, '-c',
                    'import sys,time; open(sys.argv[1], "w").close(); '
                    'time.sleep(30)', marker]

        result = run_matrix(['first', 'queued'], command, jobs=1, timeout=20,
                            overall_timeout=.2, terminate_grace=.2)

        self.assertEqual(124, result)
        self.assertEqual(['first'], os.listdir(self.directory))

    def test_timeout_kills_child_that_ignores_termination(self):
        source = (
            'import signal,sys,time\n'
            'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
            'open(sys.argv[1], "w").close()\n'
            'while True: time.sleep(1)\n'
        )
        marker = os.path.join(self.directory, 'ignoring')
        started = time.time()

        result = run_matrix(
            ['ignoring'], lambda name: [sys.executable, '-c', source, marker],
            jobs=1, timeout=.2, terminate_grace=.2)

        self.assertEqual(124, result)
        self.assertLess(time.time() - started, 3)
        self.assertTrue(os.path.exists(marker))


if __name__ == '__main__':
    unittest.main()
