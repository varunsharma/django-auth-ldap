"""Focused replay adapter contract tests, separate from Django discovery."""
import io
import os
import shutil
import tempfile
import unittest
from django.test import SimpleTestCase, TestCase

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

    def test_skip_does_not_clear_previous_failure(self):
        test = SampleTest('test_skip')
        self.history.state['failed'] = [test.id()]
        self.history.state['pending'] = [test.id()]
        self.history.save()

        result = self._result()
        result.history = self.history
        result.startTest(test)
        result.addSkip(test, 'skip')

        self.assertEqual([test.id()], self.history.state['failed'])
        self.assertEqual([], self.history.state['pending'])

    def test_expected_failure_does_not_clear_previous_failure(self):
        test = SampleTest('test_pass')
        self.history.state['failed'] = [test.id()]
        self.history.state['pending'] = [test.id()]
        self.history.save()

        result = self._result()
        result.history = self.history
        result.startTest(test)
        result.addExpectedFailure(
            test, (AssertionError, AssertionError('expected'), None))

        self.assertEqual([test.id()], self.history.state['failed'])
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

    def test_invalid_cache_falls_back_to_cold_history(self):
        self.history.save()
        with open(self.history.path, 'w') as stream:
            stream.write('{invalid')

        cold_history = ReplayHistory()
        self.assertEqual([], cold_history.state['failed'])
        cold_history.order([SampleTest('test_pass')])
        with open(cold_history.path) as stream:
            self.assertEqual(1, __import__('json').load(stream)['version'])

    def test_symlinked_cache_is_ignored_without_following_it(self):
        self.history.save()
        os.unlink(self.history.path)
        target = os.path.join(self.directory, 'outside.json')
        with open(target, 'w') as stream:
            stream.write('unchanged')
        os.symlink(target, self.history.path)

        isolated_history = ReplayHistory()
        self.assertTrue(isolated_history.checkpoint_disabled)
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
        self.assertEqual([second_id],
                         self.history.state['pending'])


if __name__ == '__main__':
    unittest.main()
