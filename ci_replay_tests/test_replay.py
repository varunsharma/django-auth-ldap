"""Focused replay adapter contract tests, separate from Django discovery."""
import io
import os
import shutil
import tempfile
import unittest
from django.test import SimpleTestCase, TestCase

import ci_replay
from ci_replay import (ReplayHistory, ReplayTextTestResult,
                       ReplayTextTestRunner)


class SampleTest(unittest.TestCase):
    def test_pass(self):
        pass

    def test_skip(self):
        self.skipTest('skip')


class ReplayHistoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix='ci-replay-test-')
        self.old_cache = os.environ.get('CI_REPLAY_CACHE_DIR')
        os.environ['CI_REPLAY_CACHE_DIR'] = self.directory
        self.history = ReplayHistory()

    def tearDown(self):
        if self.old_cache is None:
            os.environ.pop('CI_REPLAY_CACHE_DIR', None)
        else:
            os.environ['CI_REPLAY_CACHE_DIR'] = self.old_cache
        shutil.rmtree(self.directory)

    def _result(self):
        return ReplayTextTestResult(
            stream=io.StringIO(), descriptions=False, verbosity=0)

    def _reload_history(self):
        self.history = ReplayHistory(self.directory)

    def test_skip_does_not_clear_previous_failure(self):
        test = SampleTest('test_skip')
        self.history.state['failed'] = [test.id()]
        self.history.state['pending'] = [test.id()]
        self.history.save()
        self._reload_history()

        result = self._result()
        result.history = self.history
        result.startTest(test)
        result.addSkip(test, 'skip')

        self.assertEqual([test.id()], self.history.state['failed'])
        self.assertEqual([test.id()], self.history.state['pending'])
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(1, len(result.failures))

    def test_expected_failure_does_not_clear_previous_failure(self):
        test = SampleTest('test_pass')
        self.history.state['failed'] = [test.id()]
        self.history.state['pending'] = [test.id()]
        self.history.save()
        self._reload_history()

        result = self._result()
        result.history = self.history
        result.startTest(test)
        result.addExpectedFailure(
            test, (AssertionError, AssertionError('expected'), None))

        self.assertEqual([test.id()], self.history.state['failed'])
        self.assertEqual([test.id()], self.history.state['pending'])
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(1, len(result.failures))

    def test_skip_does_not_clear_interrupted_only_history(self):
        test = SampleTest('test_skip')
        self.history.state['pending'] = [test.id()]
        self.history.state['pending_contexts'][test.id()] = [
            {'run': 'interrupted-run'}]
        self.history.save()
        self._reload_history()

        result = self._result()
        result.history = self.history
        result.startTest(test)
        result.addSkip(test, 'still skipped')

        self.assertEqual([test.id()], self.history.state['pending'])
        self.assertEqual('interrupted-run',
                         self.history.state['pending_contexts'][test.id()][0]['run'])
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(1, len(result.failures))

    def test_expected_failure_does_not_clear_interrupted_only_history(self):
        test = SampleTest('test_pass')
        self.history.state['pending'] = [test.id()]
        self.history.state['pending_contexts'][test.id()] = [
            {'run': 'interrupted-run'}]
        self.history.save()
        self._reload_history()

        result = self._result()
        result.history = self.history
        result.startTest(test)
        result.addExpectedFailure(
            test, (AssertionError, AssertionError('expected'), None))

        self.assertEqual([test.id()], self.history.state['pending'])
        self.assertEqual('interrupted-run',
                         self.history.state['pending_contexts'][test.id()][0]['run'])
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(1, len(result.failures))

    def test_new_checkpoint_skip_is_not_reported_as_previous_unresolved(self):
        test = SampleTest('test_skip')
        self.history.state['pending'] = [test.id()]
        self.history.save()

        result = self._result()
        result.history = self.history
        result.startTest(test)
        result.addSkip(test, 'first observed skip')

        self.assertTrue(result.wasSuccessful())
        self.assertEqual([], result.failures)
        self.assertEqual([], self.history.state['pending'])

    def test_only_clean_success_clears_previous_failure(self):
        test = SampleTest('test_pass')
        self.history.state['failed'] = [test.id()]
        self.history.state['pending'] = [test.id()]
        self.history.save()

        result = self._result()
        result.history = self.history
        result.startTest(test)
        result.addSuccess(test)

        self.assertEqual([], self.history.state['failed'])
        self.assertEqual([], self.history.state['pending'])

    def test_call_success_after_teardown_error_does_not_clear_failure(self):
        test = SampleTest('test_pass')
        result = self._result()
        result.history = self.history
        result.startTest(test)
        result.addError(test, (RuntimeError, RuntimeError('teardown'), None))
        result.addSuccess(test)

        self.assertEqual([test.id()], self.history.state['failed'])

    def test_failed_then_pending_then_historically_risky_order(self):
        class OrderedTests(unittest.TestCase):
            def test_remaining(self):
                pass

            def test_risky(self):
                pass

            def test_pending(self):
                pass

            def test_failed(self):
                pass

        tests = [OrderedTests(name) for name in (
            'test_remaining', 'test_risky', 'test_pending', 'test_failed')]
        remaining, risky, pending, failed = [test.id() for test in tests]
        self.history.state['failed'] = [failed]
        self.history.state['pending'] = [pending]
        self.history.state['risk'] = {risky: 7}
        ordered = self.history.order(tests)

        self.assertEqual([failed, pending, risky, remaining],
                         [test.id() for test in ordered])

    def test_prior_phase_precedes_anticipated_phase_and_canonical(self):
        class CandidateCase(unittest.TestCase):
            __module__ = 'candidate_module'

            def test_risky(self):
                pass

            def test_same_class_fixture_group(self):
                pass

        class OrdinaryCase(unittest.TestCase):
            __module__ = 'ordinary_module'

            def test_ordinary(self):
                pass

        candidate = CandidateCase('test_risky')
        grouped = CandidateCase('test_same_class_fixture_group')
        ordinary = OrdinaryCase('test_ordinary')
        self.history.state['risk'][candidate.id()] = 1

        failed = OrdinaryCase('test_ordinary')
        self.history.state['failed'] = [failed.id()]
        self.history._changed_files = lambda: set()
        prior = self.history.order(
            [ordinary, grouped, candidate], phase='prior')
        self.assertEqual([ordinary.id()], [test.id() for test in prior])
        self.history.state['pending'] = []
        anticipated = self.history.order(
            [ordinary, grouped, candidate], phase='anticipated')
        canonical = self.history.order(
            [ordinary, grouped, candidate], phase='canonical')

        self.assertEqual(set([candidate.id(), grouped.id()]),
                         set(test.id() for test in anticipated))
        self.assertEqual(set([ordinary.id(), grouped.id(), candidate.id()]),
                         set(test.id() for test in canonical))

    def test_same_sha_phase_context_does_not_erase_changed_file_baseline(self):
        prior = dict(self.history.context)
        prior['sha'] = 'a' * 40
        current = dict(prior, sha='b' * 40)
        self.history.state['contexts'] = [prior, current]
        self.history.save()
        original_context = ci_replay._context
        ci_replay._context = lambda: current
        try:
            next_phase = ReplayHistory()
        finally:
            ci_replay._context = original_context

        self.assertEqual('a' * 40, next_phase.previous_sha)

    def test_order_aborts_before_tests_if_write_ahead_checkpoint_fails(self):
        test = SampleTest('test_pass')
        self.history.save = lambda: False

        with self.assertRaises(RuntimeError):
            self.history.order([test])

    def test_reordered_cases_keep_class_fixture_groups_contiguous(self):
        class FirstCase(unittest.TestCase):
            def test_risky(self):
                pass

            def test_other(self):
                pass

        class SecondCase(unittest.TestCase):
            def test_failed(self):
                pass

            def test_other(self):
                pass

        tests = [FirstCase('test_risky'), FirstCase('test_other'),
                 SecondCase('test_other'), SecondCase('test_failed')]
        risky, first_other, second_other, failed = [test.id() for test in tests]
        self.history.state['failed'] = [failed]
        self.history.state['risk'] = {risky: 4}

        ordered = self.history.order(tests)

        self.assertEqual([failed, second_other, risky, first_other],
                         [test.id() for test in ordered])

    def test_failed_simple_test_case_precedes_unfailed_test_case(self):
        class DatabaseCase(TestCase):
            def test_regular(self):
                pass

        class SimpleCase(SimpleTestCase):
            def test_failed(self):
                pass

        regular = DatabaseCase('test_regular')
        failed = SimpleCase('test_failed')
        self.history.state['failed'] = [failed.id()]

        ordered = self.history.order([regular, failed])

        self.assertEqual([failed.id(), regular.id()],
                         [test.id() for test in ordered])

    def test_collection_error_is_first_and_stops_discovered_tests(self):
        executed = []

        class OrdinaryTest(unittest.TestCase):
            def test_would_run(self):
                executed.append('ordinary')

        collection_error = list(
            unittest.TestLoader().loadTestsFromName(
                '_missing_ci_replay_test_module_'))[0]
        suite = unittest.TestSuite(self.history.order([
            OrdinaryTest('test_would_run'), collection_error]))
        runner = ReplayTextTestRunner(
            stream=io.StringIO(), descriptions=False, verbosity=0,
            replay_history=self.history)

        result = runner.run(suite)

        self.assertEqual([], executed)
        self.assertEqual(1, result.testsRun)
        self.assertIn(collection_error.id(), self.history.state['failed'])

    def test_collection_error_precedes_historically_risky_tests(self):
        risky = SampleTest('test_pass')
        collection_error = list(
            unittest.TestLoader().loadTestsFromName(
                '_missing_ci_replay_test_module_'))[0]
        self.history.state['risk'][risky.id()] = 7

        ordered = self.history.order([risky, collection_error])

        self.assertEqual([collection_error.id(), risky.id()],
                         [test.id() for test in ordered])

    def test_class_fixture_error_is_attached_to_replayable_tests(self):
        class FixtureFailure(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                raise RuntimeError('fixture failure')

            def test_first(self):
                pass

            def test_second(self):
                pass

        tests = list(unittest.defaultTestLoader.loadTestsFromTestCase(
            FixtureFailure))
        identifiers = [test.id() for test in tests]
        runner = ReplayTextTestRunner(
            stream=io.StringIO(), descriptions=False, verbosity=0,
            replay_history=self.history)

        result = runner.run(unittest.TestSuite(self.history.order(tests)))

        self.assertEqual(1, len(result.errors))
        self.assertEqual(set(identifiers), set(self.history.state['failed']))
        self.assertTrue(all(identifier in self.history.state['origins']
                            for identifier in identifiers))

    def test_module_fixture_error_is_attached_to_replayable_tests(self):
        class FirstModuleTest(unittest.TestCase):
            def test_first(self):
                pass

        class SecondModuleTest(unittest.TestCase):
            def test_second(self):
                pass

        tests = [FirstModuleTest('test_first'), SecondModuleTest('test_second')]
        identifiers = [test.id() for test in tests]
        self.history.order(tests)
        holder = unittest.suite._ErrorHolder(
            'setUpModule (%s)' % FirstModuleTest.__module__)

        self.history.error(holder)

        self.assertEqual(set(identifiers), set(self.history.state['failed']))

    def test_class_teardown_error_is_attached_to_replayable_tests(self):
        class FixtureFailure(unittest.TestCase):
            @classmethod
            def tearDownClass(cls):
                raise RuntimeError('teardown failure')

            def test_runs(self):
                pass

        test = FixtureFailure('test_runs')
        runner = ReplayTextTestRunner(
            stream=io.StringIO(), descriptions=False, verbosity=0,
            replay_history=self.history)

        result = runner.run(unittest.TestSuite(self.history.order([test])))

        self.assertEqual(1, len(result.errors))
        self.assertEqual([test.id()], self.history.state['failed'])

    def test_disappeared_collection_error_fails_closed(self):
        collection_error = list(
            unittest.TestLoader().loadTestsFromName(
                '_missing_ci_replay_test_module_'))[0]
        failed_runner = ReplayTextTestRunner(
            stream=io.StringIO(), descriptions=False, verbosity=0,
            replay_history=self.history)
        failed_runner.run(unittest.TestSuite(self.history.order([collection_error])))
        self.assertIn(collection_error.id(), self.history.state['failed'])

        class Recovered(unittest.TestCase):
            def test_passes(self):
                pass

        recovered = Recovered('test_passes')
        with self.assertRaises(RuntimeError) as raised:
            self.history.order([recovered])

        self.assertIn(collection_error.id(), str(raised.exception))
        self.assertIn(collection_error.id(), self.history.state['failed'])
        self.assertIn(collection_error.id(), self.history.state['collection_errors'])

    def test_missing_historical_test_fails_closed_and_keeps_its_history(self):
        missing_id = 'tests.tests.RemovedCase.test_previously_failed'
        self.history.state['failed'] = [missing_id]
        self.history.state['pending'] = [missing_id]
        self.history.save()

        with self.assertRaises(RuntimeError) as raised:
            self.history.order([SampleTest('test_pass')])

        self.assertIn(missing_id, str(raised.exception))
        persisted = ReplayHistory()
        self.assertEqual([missing_id], persisted.state['failed'])
        self.assertEqual([missing_id], persisted.state['pending'])

    def test_module_fixture_groups_stay_contiguous_when_reordered(self):
        class FirstModuleCase(unittest.TestCase):
            __module__ = 'synthetic_module_a'

            def test_failed(self):
                pass

        class SecondModuleCase(unittest.TestCase):
            __module__ = 'synthetic_module_a'

            def test_regular(self):
                pass

        class OtherModuleCase(unittest.TestCase):
            __module__ = 'synthetic_module_b'

            def test_failed(self):
                pass

        first = FirstModuleCase('test_failed')
        regular = SecondModuleCase('test_regular')
        other = OtherModuleCase('test_failed')
        self.history.state['failed'] = [first.id(), other.id()]

        ordered = self.history.order([first, regular, other])

        modules = [test.__class__.__module__ for test in ordered]
        self.assertEqual(['synthetic_module_a', 'synthetic_module_a',
                          'synthetic_module_b'], modules)

    def test_prior_phase_keeps_module_fixture_group_whole(self):
        Historical = type('Historical', (unittest.TestCase,), {
            '__module__': 'synthetic_module_a',
            'test_failed': lambda self: None,
        })
        SameModule = type('SameModule', (unittest.TestCase,), {
            '__module__': 'synthetic_module_a',
            'test_regular': lambda self: None,
        })
        OtherModule = type('OtherModule', (unittest.TestCase,), {
            '__module__': 'synthetic_module_b',
            'test_failed': lambda self: None,
        })
        failed = Historical('test_failed')
        regular = SameModule('test_regular')
        other_failed = OtherModule('test_failed')
        self.history.state['failed'] = [failed.id(), other_failed.id()]
        self.history._changed_files = lambda: set()

        prior = self.history.order(
            [regular, failed, other_failed], phase='prior')

        self.assertEqual([failed.id(), regular.id(), other_failed.id()],
                         [test.id() for test in prior])

    def test_repository_is_part_of_cache_scope(self):
        previous = os.environ.get('CI_REPLAY_REPOSITORY')
        try:
            os.environ['CI_REPLAY_REPOSITORY'] = 'owner/first'
            first = ReplayHistory()
            os.environ['CI_REPLAY_REPOSITORY'] = 'owner/second'
            second = ReplayHistory()
            self.assertNotEqual(first.path, second.path)
        finally:
            if previous is None:
                os.environ.pop('CI_REPLAY_REPOSITORY', None)
            else:
                os.environ['CI_REPLAY_REPOSITORY'] = previous

    def test_pull_request_number_and_head_ref_are_part_of_cache_scope(self):
        names = ('CI_REPLAY_REPOSITORY', 'CI_REPLAY_BASE_REF',
                 'CI_REPLAY_HEAD_REF', 'CI_REPLAY_PR')
        previous = dict((name, os.environ.get(name)) for name in names)
        try:
            os.environ['CI_REPLAY_REPOSITORY'] = 'owner/repository'
            os.environ['CI_REPLAY_BASE_REF'] = 'master'
            os.environ['CI_REPLAY_HEAD_REF'] = 'feature'
            os.environ['CI_REPLAY_PR'] = '41'
            first = ReplayHistory()
            os.environ['CI_REPLAY_PR'] = '42'
            second = ReplayHistory()
            self.assertNotEqual(first.path, second.path)
            os.environ['CI_REPLAY_PR'] = '41'
            os.environ['CI_REPLAY_HEAD_REF'] = 'other-feature'
            third = ReplayHistory()
            self.assertNotEqual(first.path, third.path)
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def test_failure_provenance_survives_skips_and_is_removed_after_clean_pass(self):
        test = SampleTest('test_pass')
        self.history.context['run'] = 'origin-run'
        self.history.context['job'] = 'origin-job'
        self.history.error(test)
        self.history.complete(test)

        second = ReplayHistory()
        skipped = self._result()
        skipped.history = second
        skipped.startTest(test)
        skipped.addSkip(test, 'still skipped')
        self.assertEqual('origin-run',
                         second.state['origins'][test.id()][0]['run'])

        third = ReplayHistory()
        passed = self._result()
        passed.history = third
        passed.startTest(test)
        passed.addSuccess(test)
        self.assertNotIn(test.id(), third.state['origins'])
        self.assertNotIn(test.id(), third.state['failed'])

    def test_unresolved_failure_provenance_is_not_capped_to_recent_runs(self):
        test = SampleTest('test_pass')
        for index in range(15):
            self.history.context['run'] = 'run-%d' % index
            self.history.context['job'] = 'job-%d' % index
            self.history.error(test)
        self.assertEqual(15, len(self.history.state['origins'][test.id()]))

    def test_run_contexts_are_not_truncated_when_reloading_history(self):
        self.history.state['contexts'] = [
            {'sha': 'sha-%d' % index, 'run': 'run-%d' % index}
            for index in range(15)]
        self.history.save()

        loaded = ReplayHistory()

        self.assertEqual(16, len(loaded.state['contexts']))
        self.assertEqual('sha-0', loaded.state['contexts'][0]['sha'])

    def test_invalid_existing_cache_fails_closed(self):
        self.history.save()
        invalid_states = [
            b'{invalid',
            b'{"version": 99, "failed": [], "pending": [], "risk": {}, '
            b'"contexts": []}',
            b' ' * (ci_replay.MAX_BYTES + 1),
        ]
        for invalid_state in invalid_states:
            with open(self.history.path, 'wb') as stream:
                stream.write(invalid_state)

            with self.assertRaises(ValueError):
                ReplayHistory(self.directory)

    def test_symlinked_cache_is_ignored_without_following_it(self):
        self.history.save()
        os.unlink(self.history.path)
        target = os.path.join(self.directory, 'outside.json')
        with open(target, 'w') as stream:
            stream.write('unchanged')
        os.symlink(target, self.history.path)

        isolated_history = ReplayHistory()
        self.assertTrue(isolated_history.checkpoint_disabled)
        with self.assertRaises(RuntimeError):
            isolated_history.order([SampleTest('test_pass')])

        with open(target) as stream:
            self.assertEqual('unchanged', stream.read())

    def test_runner_stops_after_first_failure_and_keeps_remaining_pending(self):
        executed = []

        class OrderedTests(unittest.TestCase):
            def test_first_fails(self):
                executed.append('first')
                self.fail('expected failure')

            def test_second_is_not_started(self):
                executed.append('second')

        suite = unittest.TestSuite([
            OrderedTests('test_first_fails'),
            OrderedTests('test_second_is_not_started'),
        ])
        tests = list(suite)
        first_id, second_id = tests[0].id(), tests[1].id()
        suite = unittest.TestSuite(self.history.order(tests))
        runner = ReplayTextTestRunner(
            stream=io.StringIO(), descriptions=False, verbosity=0,
            replay_history=self.history)

        result = runner.run(suite)

        self.assertEqual(['first'], executed)
        self.assertEqual(1, result.testsRun)
        self.assertEqual([first_id],
                         self.history.state['failed'])
        self.assertEqual([first_id, second_id],
                         self.history.state['pending'])


class TravisContextTests(unittest.TestCase):
    ENV_KEYS = (
        'CI_REPLAY_HEAD_REF', 'CI_REPLAY_BASE_REF', 'CI_REPLAY_BRANCH',
        'CI_REPLAY_PR', 'CI_REPLAY_REPOSITORY', 'CI_REPLAY_BASE_SHA',
        'TRAVIS_REPO_SLUG', 'TRAVIS_PULL_REQUEST_SLUG',
        'TRAVIS_PULL_REQUEST_SHA', 'TRAVIS_PULL_REQUEST_BRANCH',
        'TRAVIS_BRANCH', 'TRAVIS_PULL_REQUEST', 'TRAVIS_COMMIT_RANGE',
        'TRAVIS_JOB_RESTARTED', 'TRAVIS_BUILD_ID', 'TRAVIS_JOB_ID',
        'CI_REPLAY_CACHE_DIR', 'CI_REPLAY_LANE', 'TOX_ENV',
        'GITHUB_HEAD_REF', 'GITHUB_REF_NAME', 'GITHUB_REF',
        'GITHUB_BASE_REF', 'GITHUB_BASE_SHA', 'GITHUB_RUN_ID',
        'GITHUB_RUN_ATTEMPT', 'GITHUB_JOB', 'CI_COMMIT_REF_NAME',
        'BRANCH_NAME', 'GIT_BRANCH', 'CHANGE_ID', 'BUILD_TAG',
        'BUILD_NUMBER', 'CI_PIPELINE_ID', 'CI_MERGE_REQUEST_IID',
        'CI_MERGE_REQUEST_TARGET_BRANCH_NAME',
        'CI_MERGE_REQUEST_DIFF_BASE_SHA',
    )

    def setUp(self):
        self.old_env = dict((key, os.environ.get(key)) for key in self.ENV_KEYS)
        self.old_git = ci_replay._git
        self.directory = tempfile.mkdtemp(prefix='ci-replay-travis-test-')
        for key in self.ENV_KEYS:
            os.environ.pop(key, None)
        os.environ['CI_REPLAY_CACHE_DIR'] = self.directory
        os.environ['CI_REPLAY_LANE'] = 'py35-django111'
        ci_replay._git = lambda *args: (
            'f' * 40 if args == ('rev-parse', 'HEAD') else 'checked-out-branch')

    def tearDown(self):
        ci_replay._git = self.old_git
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.directory)

    def test_travis_pull_request_context_keeps_source_and_checkout_distinct(self):
        base_sha = 'a' * 40
        source_sha = 'b' * 40
        os.environ.update({
            'TRAVIS_REPO_SLUG': 'varunsharma/django-auth-ldap',
            'TRAVIS_PULL_REQUEST_SLUG': 'contributor/django-auth-ldap',
            'TRAVIS_PULL_REQUEST_SHA': source_sha,
            'TRAVIS_PULL_REQUEST_BRANCH': 'feature/replay',
            'TRAVIS_BRANCH': 'master',
            'TRAVIS_PULL_REQUEST': '17',
            'TRAVIS_COMMIT_RANGE': base_sha + '...' + source_sha,
            'TRAVIS_JOB_RESTARTED': 'false',
            'TRAVIS_BUILD_ID': 'build-8',
            'TRAVIS_JOB_ID': 'job-9',
        })

        context = ci_replay._context()

        self.assertEqual('varunsharma/django-auth-ldap', context['repository'])
        self.assertEqual('contributor/django-auth-ldap',
                         context['source_repository'])
        self.assertEqual(source_sha, context['head_sha'])
        self.assertEqual(base_sha, context['base_sha'])
        self.assertEqual('f' * 40, context['sha'])
        self.assertNotEqual(context['head_sha'], context['sha'])
        self.assertEqual('17', context['pr'])
        self.assertEqual('build-8', context['run'])
        self.assertEqual('job-9', context['job'])
        self.assertEqual('travis-initial', context['run_attempt'])
        self.assertEqual('false', context['job_restarted'])

    def test_invalid_travis_commit_range_is_not_used_as_base_sha(self):
        os.environ['TRAVIS_COMMIT_RANGE'] = 'main...feature'
        os.environ['TRAVIS_JOB_RESTARTED'] = 'true ' * 200

        context = ci_replay._context()

        self.assertEqual('', context['base_sha'])
        self.assertEqual('', context['job_restarted'])
        self.assertEqual('', context['run_attempt'])

    def test_travis_restart_boolean_is_bounded_and_distinguishes_attempt(self):
        os.environ['TRAVIS_JOB_RESTARTED'] = 'true'

        context = ci_replay._context()

        self.assertEqual('true', context['job_restarted'])
        self.assertEqual('travis-restarted', context['run_attempt'])

    def test_travis_failure_provenance_survives_next_run_context(self):
        source_sha = 'b' * 40
        os.environ.update({
            'TRAVIS_REPO_SLUG': 'varunsharma/django-auth-ldap',
            'TRAVIS_PULL_REQUEST_SLUG': 'contributor/django-auth-ldap',
            'TRAVIS_PULL_REQUEST_SHA': source_sha,
            'TRAVIS_PULL_REQUEST_BRANCH': 'feature/replay',
            'TRAVIS_BRANCH': 'master',
            'TRAVIS_PULL_REQUEST': '17',
            'TRAVIS_COMMIT_RANGE': 'a' * 40 + '...' + source_sha,
            'TRAVIS_JOB_RESTARTED': 'false',
            'TRAVIS_BUILD_ID': 'build-8',
            'TRAVIS_JOB_ID': 'job-9',
        })
        test = SampleTest('test_pass')
        first_run = ReplayHistory()
        first_run.error(test)

        os.environ['TRAVIS_JOB_RESTARTED'] = 'true'
        os.environ['TRAVIS_BUILD_ID'] = 'build-8'
        os.environ['TRAVIS_JOB_ID'] = 'job-10'
        next_run = ReplayHistory()

        origin = next_run.state['origins'][test.id()][0]
        self.assertEqual('contributor/django-auth-ldap',
                         origin['source_repository'])
        self.assertEqual(source_sha, origin['head_sha'])
        self.assertEqual('a' * 40, origin['base_sha'])
        self.assertEqual('f' * 40, origin['sha'])
        self.assertEqual('build-8', origin['run'])
        self.assertEqual('job-9', origin['job'])
        self.assertEqual('travis-initial', origin['run_attempt'])


if __name__ == '__main__':
    unittest.main()
