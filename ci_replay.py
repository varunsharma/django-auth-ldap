"""Best-effort unittest replay ordering for Django's single test job.

This module uses only Python 2.7 standard-library syntax. History stores test
identifiers and CI provenance, never test output or environment dumps.
"""
from __future__ import print_function

import hashlib
import json
import os
import platform
import re
import subprocess
import tempfile
import threading
import unittest
from django.test import SimpleTestCase, TestCase

MAX_BYTES = 8 * 1024 * 1024
MAX_TESTS = 25000


def _git(*args):
    try:
        process = subprocess.Popen(['git'] + list(args), stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        timer = threading.Timer(5, process.kill)
        timer.start()
        try:
            output, _ = process.communicate()
        finally:
            timer.cancel()
        if process.returncode:
            return None
        return output.decode('utf-8', 'replace').strip()
    except (OSError, AttributeError):
        return None


def _context():
    env = os.environ
    commit_range = env.get('TRAVIS_COMMIT_RANGE', '')
    range_match = re.match(r'^([0-9a-fA-F]{40})\.\.\.([0-9a-fA-F]{40})$',
                           commit_range)
    travis_base_sha = range_match.group(1).lower() if range_match else ''
    travis_restarted = env.get('TRAVIS_JOB_RESTARTED', '').lower()
    if travis_restarted not in ('true', 'false'):
        travis_restarted = ''
    head_ref = (env.get('CI_REPLAY_HEAD_REF') or
                env.get('TRAVIS_PULL_REQUEST_BRANCH') or
                env.get('GITHUB_HEAD_REF') or env.get('GITHUB_REF_NAME') or
                env.get('CI_COMMIT_REF_NAME') or env.get('TRAVIS_BRANCH') or '')
    base_ref = (env.get('CI_REPLAY_BASE_REF') or env.get('TRAVIS_BRANCH') or
                env.get('GITHUB_BASE_REF') or env.get('CI_MERGE_REQUEST_TARGET_BRANCH_NAME')
                or '')
    branch = (env.get('CI_REPLAY_BRANCH') or env.get('TRAVIS_PULL_REQUEST_BRANCH')
              or env.get('BRANCH_NAME') or env.get('GIT_BRANCH')
              or head_ref or base_ref
              or _git('rev-parse', '--abbrev-ref', 'HEAD') or '')
    ref = env.get('GITHUB_REF', '')
    pr = (env.get('CI_REPLAY_PR') or env.get('CHANGE_ID') or
          env.get('TRAVIS_PULL_REQUEST') or env.get('CI_MERGE_REQUEST_IID'))
    if not pr and ref.startswith('refs/pull/'):
        pr = ref.split('/')[2]
    values = {
        'repository': env.get('CI_REPLAY_REPOSITORY') or
                      env.get('TRAVIS_REPO_SLUG') or env.get('GITHUB_REPOSITORY')
                      or env.get('CI_PROJECT_PATH') or '',
        'source_repository': env.get('TRAVIS_PULL_REQUEST_SLUG') or '',
        'branch': branch,
        'head_ref': head_ref or branch,
        'base_ref': base_ref or branch,
        'base_sha': env.get('CI_REPLAY_BASE_SHA') or env.get('GITHUB_BASE_SHA')
                    or env.get('CI_MERGE_REQUEST_DIFF_BASE_SHA') or
                    travis_base_sha,
        'head_sha': env.get('TRAVIS_PULL_REQUEST_SHA') or '',
        'pr': pr or '',
        'sha': _git('rev-parse', 'HEAD') or '',
        'run': env.get('TRAVIS_BUILD_ID') or env.get('GITHUB_RUN_ID')
               or env.get('BUILD_TAG') or env.get('BUILD_NUMBER')
               or env.get('CI_PIPELINE_ID') or '',
        'run_attempt': env.get('GITHUB_RUN_ATTEMPT') or
                       env.get('CI_JOB_RETRY') or
                       ('travis-restarted' if travis_restarted == 'true' else
                        'travis-initial' if travis_restarted == 'false' else ''),
        'job_restarted': travis_restarted,
        'job': env.get('TRAVIS_JOB_ID') or env.get('GITHUB_JOB')
               or env.get('JOB_NAME')
               or env.get('CI_JOB_ID') or '',
        'lane': env.get('CI_REPLAY_LANE') or env.get('TOX_ENV') or 'default',
        'python': platform.python_version(),
    }
    return dict((key, str(value)[:400]) for key, value in values.items())


class ReplayHistory(object):
    def __init__(self, directory=None):
        self.context = _context()
        scope = [self.context[key] for key in
                 ('repository', 'base_ref', 'pr', 'head_ref', 'lane', 'python')]
        digest = hashlib.sha256(json.dumps(scope).encode('utf-8')).hexdigest()[:32]
        self.directory = os.path.abspath(directory or
                                         os.environ.get('CI_REPLAY_CACHE_DIR', '.ci-replay-cache'))
        self.path = os.path.join(self.directory, digest + '.json')
        self.state = {'version': 1, 'failed': [], 'pending': [],
                      'risk': {}, 'contexts': [], 'origins': {},
                      'pending_contexts': {}, 'collection_errors': []}
        self.checkpoint_disabled = False
        if os.path.islink(self.directory) or os.path.islink(self.path):
            self.checkpoint_disabled = True
        elif os.path.exists(self.path):
            # An existing but invalid checkpoint may contain the only record
            # of unresolved failures. Refuse to run a cold suite that would
            # silently skip those failures.
            self._load()
        self.unresolved_at_start = (set(self.state['failed']) |
                                    set(self.state['pending']))
        self.previous_sha = next(
            (item.get('sha', '') for item in reversed(self.state['contexts'])
             if item.get('sha') and item.get('sha') != self.context.get('sha')),
            '')
        self.state['contexts'] = self.state['contexts'] + [self.context]
        self.failed_this_run = set()
        self.passed_tests = set()

    def _valid_ids(self, value):
        return (isinstance(value, list) and len(value) <= MAX_TESTS and
                all(isinstance(item, type(u'')) and len(item) <= 2000
                    for item in value))

    def _valid_provenance(self, value):
        if not isinstance(value, dict) or len(value) > MAX_TESTS:
            return False
        for identifier, contexts in value.items():
            if (not isinstance(identifier, type(u'')) or len(identifier) > 2000 or
                    not isinstance(contexts, list)):
                return False
            for context in contexts:
                if (not isinstance(context, dict) or
                        not all(isinstance(key, type(u'')) and
                                isinstance(item, type(u'')) and len(item) <= 400
                                for key, item in context.items())):
                    return False
        return True

    def _record_context(self, target, identifier):
        contexts = target.setdefault(identifier, [])
        if not contexts or contexts[-1] != self.context:
            contexts.append(dict(self.context))

    def _record_failure(self, identifier):
        first_error = identifier not in self.failed_this_run
        self.failed_this_run.add(identifier)
        if identifier not in self.state['failed']:
            self.state['failed'].append(identifier)
        self._record_context(self.state['origins'], identifier)
        if first_error:
            self.state['risk'][identifier] = min(
                1000000, self.state['risk'].get(identifier, 0) + 1)

    def _load(self):
        try:
            if os.path.getsize(self.path) > MAX_BYTES:
                raise ValueError('oversized state')
            with open(self.path) as stream:
                value = json.load(stream)
            if (not isinstance(value, dict) or value.get('version') != 1 or
                    not self._valid_ids(value.get('failed')) or
                    not self._valid_ids(value.get('pending')) or
                    not isinstance(value.get('risk'), dict) or
                    len(value['risk']) > MAX_TESTS or
                    not self._valid_ids(list(value['risk'])) or
                    not all(isinstance(n, int) and 0 <= n <= 1000000
                            for n in value['risk'].values()) or
                    not isinstance(value.get('contexts'), list) or
                    not all(isinstance(item, dict) and
                            isinstance(item.get('sha', ''), type(u''))
                            for item in value['contexts']) or
                    not self._valid_provenance(value.get('origins', {})) or
                    not self._valid_provenance(value.get('pending_contexts', {})) or
                    not self._valid_ids(value.get('collection_errors', []))):
                raise ValueError('invalid schema')
            value.setdefault('origins', {})
            value.setdefault('pending_contexts', {})
            value.setdefault('collection_errors', [])
            self.state = value
        except (ValueError, TypeError, AttributeError, IOError, OSError) as exc:
            raise ValueError('Invalid replay history: ' + str(exc))

    def save(self):
        if self.checkpoint_disabled or os.path.islink(self.directory) or os.path.islink(self.path):
            self.checkpoint_disabled = True
            return False
        if (len(self.state['failed']) > MAX_TESTS or
                len(self.state['pending']) > MAX_TESTS or
                len(self.state['risk']) > MAX_TESTS):
            self.checkpoint_disabled = True
            return False
        try:
            if not os.path.isdir(self.directory):
                os.makedirs(self.directory)
            payload = json.dumps(self.state, sort_keys=True).encode('utf-8')
            if len(payload) > MAX_BYTES:
                self.checkpoint_disabled = True
                return False
            fd, temporary = tempfile.mkstemp(prefix='.checkpoint-', dir=self.directory)
        except (IOError, OSError, ValueError):
            self.checkpoint_disabled = True
            return False
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.rename(temporary, self.path)
        except (IOError, OSError, ValueError):
            self.checkpoint_disabled = True
            return False
        finally:
            if os.path.exists(temporary):
                try:
                    os.unlink(temporary)
                except (IOError, OSError):
                    pass
        return True

    def _changed_files(self):
        if not re.match(r'^[0-9a-f]{40}$', self.previous_sha):
            return None
        output = _git('diff', '--no-ext-diff', '--name-only', self.previous_sha,
                      'HEAD', '--')
        return None if output is None else set(output.splitlines())

    def order(self, tests, phase='canonical'):
        if phase not in ('prior', 'anticipated', 'canonical'):
            raise ValueError('unknown replay phase: %s' % phase)
        self.collected_ids = set(test.id() for test in tests)
        self.class_tests = {}
        self.module_tests = {}
        for test in tests:
            test_class = test.__class__
            class_name = getattr(test_class, '__qualname__', test_class.__name__)
            class_key = test_class.__module__ + '.' + class_name
            self.class_tests.setdefault(class_key, []).append(test.id())
            self.module_tests.setdefault(test_class.__module__, []).append(
                test.id())
        # Collection failures have no stable test ID or reliable module mapping.
        # Do not infer that a missing _FailedTest was repaired: the source may
        # instead have disappeared from discovery. Require explicit resolution.
        unresolved = sorted(
            (set(self.state['failed']) | set(self.state['pending'])) -
            self.collected_ids)
        if unresolved:
            # Keep the unresolved provenance checkpointed and refuse to report
            # a passing suite when a previously failing/interrupted test cannot
            # be collected in this revision.
            self.save()
            shown = ', '.join(identifier[:300] for identifier in unresolved[:5])
            if len(unresolved) > 5:
                shown += ', ...'
            raise RuntimeError(
                'Replay history has %d unresolved test ID(s) absent from '
                'collection: %s. Restore the test or explicitly resolve its '
                'history after review.' % (len(unresolved), shown))
        failed, pending = set(self.state['failed']), set(self.state['pending'])
        risk = self.state['risk']
        changed = self._changed_files()

        def priority(test):
            identifier = test.id()
            if identifier in failed:
                return (0, 0)
            if identifier in pending:
                return (1, 0)
            if test.__class__.__name__ == '_FailedTest':
                return (2, 0)
            if changed is None or changed:
                return (3, -risk.get(identifier, 0))
            return (4, -risk.get(identifier, 0))

        # Keep module fixtures and each TestCase's class fixtures contiguous.
        # Within a module, retain Django's TestCase -> SimpleTestCase -> other
        # suite order when replay priorities are equal.
        module_groups = []
        by_module = {}
        by_class = {}
        for test in tests:
            test_class = test.__class__
            module_name = test_class.__module__
            if module_name not in by_module:
                by_module[module_name] = []
                module_groups.append(by_module[module_name])
            class_key = (module_name, test_class)
            if class_key not in by_class:
                by_class[class_key] = []
                by_module[module_name].append(by_class[class_key])
            by_class[class_key].append(test)

        def group_priority(group):
            test_class = group[0].__class__
            if test_class.__name__ == '_FailedTest':
                # Import and collection failures must be surfaced before tests.
                rank = -1
            elif issubclass(test_class, TestCase):
                rank = 0
            elif issubclass(test_class, SimpleTestCase):
                rank = 1
            else:
                rank = 2
            return (min(priority(test) for test in group), rank)

        module_groups.sort(
            key=lambda module_group: min(group_priority(group)
                                         for group in module_group))
        tests = []
        for module_group in module_groups:
            module_priority = min(group_priority(group)
                                  for group in module_group)
            if phase == 'prior' and module_priority[0] >= (3, 0):
                continue
            if (phase == 'anticipated' and changed and
                    module_priority[0] >= (4, 0)):
                continue
            if (phase == 'anticipated' and not changed and
                    not any(risk.get(test.id(), 0)
                            for group in module_group for test in group) and
                    changed is not None):
                continue
            module_group.sort(key=group_priority)
            for class_group in module_group:
                class_group.sort(key=priority)
                tests.extend(class_group)
        pending_ids = []
        seen = set()
        for identifier in [test.id() for test in tests] + self.state['pending']:
            if identifier not in seen:
                pending_ids.append(identifier)
                seen.add(identifier)
        self.state['pending'] = pending_ids
        for identifier in pending_ids:
            self._record_context(self.state['pending_contexts'], identifier)
        if not self.save():
            raise RuntimeError(
                'Unable to save replay history before tests; refusing to run '
                'without a write-ahead checkpoint.')
        return tests

    def error(self, test):
        identifier = test.id()
        test_class_name = test.__class__.__name__
        if test_class_name == '_FailedTest':
            if identifier not in self.state['collection_errors']:
                self.state['collection_errors'].append(identifier)
            self._record_failure(identifier)
        elif test_class_name == '_ErrorHolder' and identifier.startswith(
                ('setUpClass (', 'tearDownClass (', 'setUpModule (',
                 'tearDownModule (')):
            class_name = identifier.split('(', 1)[1].rsplit(')', 1)[0]
            if identifier.startswith(('setUpClass (', 'tearDownClass (')):
                scoped_ids = self.class_tests.get(class_name, [])
            else:
                scoped_ids = self.module_tests.get(class_name, [])
            if scoped_ids:
                for scoped_id in scoped_ids:
                    self._record_failure(scoped_id)
            else:
                self._record_failure(identifier)
        else:
            self._record_failure(identifier)
        self.save()

    def complete(self, test, passed=False):
        identifier = test.id()
        if passed and identifier not in self.failed_this_run:
            self.passed_tests.add(identifier)
            self.state['pending'] = [item for item in self.state['pending']
                                     if item != identifier]
            self.state['pending_contexts'].pop(identifier, None)
            self.state['failed'] = [item for item in self.state['failed']
                                    if item != identifier]
            self.state['origins'].pop(identifier, None)
        self.save()

    def has_unresolved(self, test):
        identifier = test.id()
        return identifier in self.unresolved_at_start

    def complete_without_pass(self, test):
        """Clear only this run's write-ahead marker after a skip or xfail."""
        identifier = test.id()
        if identifier not in self.unresolved_at_start:
            self.state['pending'] = [item for item in self.state['pending']
                                     if item != identifier]
            self.state['pending_contexts'].pop(identifier, None)
        self.save()


class ReplayTextTestResult(unittest.TextTestResult):
    history = None

    def startTest(self, test):
        self._test_errors = set()
        unittest.TextTestResult.startTest(self, test)

    def addError(self, test, err):
        if not hasattr(self, '_test_errors'):
            self._test_errors = set()
        self._test_errors.add(test.id())
        self.history.error(test)
        self.history.complete(test)
        return unittest.TextTestResult.addError(self, test, err)

    def addFailure(self, test, err):
        if not hasattr(self, '_test_errors'):
            self._test_errors = set()
        self._test_errors.add(test.id())
        self.history.error(test)
        self.history.complete(test)
        return unittest.TextTestResult.addFailure(self, test, err)

    def addSuccess(self, test):
        passed = test.id() not in self._test_errors
        self.history.complete(test, passed=passed)
        return unittest.TextTestResult.addSuccess(self, test)

    def addSkip(self, test, reason):
        unresolved = self.history.has_unresolved(test)
        self.history.complete_without_pass(test)
        if unresolved:
            return self._add_unresolved_outcome_failure(test, 'skipped')
        return unittest.TextTestResult.addSkip(self, test, reason)

    def addExpectedFailure(self, test, err):
        unresolved = self.history.has_unresolved(test)
        self.history.complete_without_pass(test)
        if unresolved:
            return self._add_unresolved_outcome_failure(
                test, 'expected failure')
        return unittest.TextTestResult.addExpectedFailure(self, test, err)

    def _add_unresolved_outcome_failure(self, test, outcome):
        error = AssertionError(
            'test remains unresolved from a previous failure or interruption; '
            'current outcome was %s, so a clean pass is required' % outcome)
        return unittest.TextTestResult.addFailure(
            self, test, (AssertionError, error, None))

    def addUnexpectedSuccess(self, test):
        self.history.error(test)
        self.history.complete(test)
        return unittest.TextTestResult.addUnexpectedSuccess(self, test)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            if not hasattr(self, '_test_errors'):
                self._test_errors = set()
            self._test_errors.add(test.id())
            self.history.error(test)
            self.history.complete(test)
        add_subtest = getattr(unittest.TextTestResult, 'addSubTest', None)
        if add_subtest is None:
            if err is not None:
                return self.addFailure(test, err)
            return None
        return add_subtest(self, test, subtest, err)


class ReplayTextTestRunner(unittest.TextTestRunner):
    def __init__(self, *args, **kwargs):
        history = kwargs.pop('replay_history')
        kwargs['failfast'] = True
        kwargs['resultclass'] = type(
            'ReplayBoundTextTestResult', (ReplayTextTestResult,),
            {'history': history})
        unittest.TextTestRunner.__init__(self, *args, **kwargs)


from django.test.runner import DiscoverRunner


class ReplayDiscoverRunner(DiscoverRunner):
    """Django runner retaining the standard discovery and DB lifecycle."""

    def __init__(self, *args, **kwargs):
        if kwargs.get('parallel', 1) > 1:
            raise ValueError('Replay runner requires a single test process')
        DiscoverRunner.__init__(self, *args, **kwargs)

    def run_suite(self, suite, **kwargs):
        history = ReplayHistory()
        tests = []

        def flatten(items):
            for item in items:
                if isinstance(item, unittest.TestSuite):
                    flatten(item)
                else:
                    tests.append(item)

        flatten(suite)
        phase = os.environ.get('CI_REPLAY_PHASE', 'canonical')
        suite = unittest.TestSuite(history.order(tests, phase=phase))
        runner = ReplayTextTestRunner(
            verbosity=self.verbosity, replay_history=history)
        return runner.run(suite)
