import os
from unittest.mock import MagicMock, call, patch

import pytest

import awx.coverage_middleware as cm_module
from awx.coverage_middleware import CoverageMiddleware


def _request(test_name=None):
    req = MagicMock()
    req.META = {'HTTP_X_TEST_NAME': test_name} if test_name else {}
    return req


@pytest.fixture(autouse=True)
def reset_globals():
    """Reset module-level singletons between tests."""
    orig_cov = cm_module._cov
    orig_data_file = cm_module._data_file
    yield
    cm_module._cov = orig_cov
    cm_module._data_file = orig_data_file


@pytest.fixture()
def output_dir(tmp_path):
    return str(tmp_path)


class TestCoverageMiddleware:
    def test_no_header_skips_coverage(self, output_dir):
        get_response = MagicMock(return_value=MagicMock())
        mw = CoverageMiddleware(get_response)

        with patch('awx.coverage_middleware.coverage') as mock_cov_module:
            mw(_request())

        mock_cov_module.Coverage.assert_not_called()
        get_response.assert_called_once()

    def test_coverage_file_uses_pid(self, output_dir):
        get_response = MagicMock(return_value=MagicMock())
        mw = CoverageMiddleware(get_response)
        fake_cov = MagicMock()
        pid = 12345

        with patch('awx.coverage_middleware.coverage') as mock_cov_module, patch('awx.coverage_middleware.os.getpid', return_value=pid), patch(
            'awx.coverage_middleware.settings'
        ) as mock_settings, patch('awx.coverage_middleware.os.makedirs'), patch('awx.coverage_middleware.os.path.exists', return_value=True):
            mock_settings.TEST_COVERAGE_OUTPUT_DIR = output_dir
            mock_cov_module.Coverage.return_value = fake_cov

            mw(_request('my_test'))

        expected = os.path.join(output_dir, f'.coverage.{pid}')
        mock_cov_module.Coverage.assert_called_once_with(data_file=expected)
        fake_cov.start.assert_called_once()

    def test_coverage_initialized_once_across_requests(self, output_dir):
        get_response = MagicMock(return_value=MagicMock())
        mw = CoverageMiddleware(get_response)
        fake_cov = MagicMock()

        with patch('awx.coverage_middleware.coverage') as mock_cov_module, patch('awx.coverage_middleware.os.getpid', return_value=1), patch(
            'awx.coverage_middleware.settings'
        ) as mock_settings, patch('awx.coverage_middleware.os.makedirs'), patch('awx.coverage_middleware.os.path.exists', return_value=True):
            mock_settings.TEST_COVERAGE_OUTPUT_DIR = output_dir
            mock_cov_module.Coverage.return_value = fake_cov

            mw(_request('test_a'))
            mw(_request('test_b'))
            mw(_request('test_c'))

        mock_cov_module.Coverage.assert_called_once()
        fake_cov.start.assert_called_once()

    def test_switch_context_called_per_request(self, output_dir):
        get_response = MagicMock(return_value=MagicMock())
        mw = CoverageMiddleware(get_response)
        fake_cov = MagicMock()

        with patch('awx.coverage_middleware.coverage') as mock_cov_module, patch('awx.coverage_middleware.os.getpid', return_value=1), patch(
            'awx.coverage_middleware.settings'
        ) as mock_settings, patch('awx.coverage_middleware.os.makedirs'), patch('awx.coverage_middleware.os.path.exists', return_value=True):
            mock_settings.TEST_COVERAGE_OUTPUT_DIR = output_dir
            mock_cov_module.Coverage.return_value = fake_cov

            mw(_request('test_a'))
            mw(_request('test_b'))

        assert fake_cov.switch_context.call_args_list == [call('test_a'), call(''), call('test_b'), call('')]

    def test_context_reset_even_if_view_raises(self, output_dir):
        get_response = MagicMock(side_effect=RuntimeError('boom'))
        mw = CoverageMiddleware(get_response)
        fake_cov = MagicMock()

        with patch('awx.coverage_middleware.coverage') as mock_cov_module, patch('awx.coverage_middleware.os.getpid', return_value=1), patch(
            'awx.coverage_middleware.settings'
        ) as mock_settings, patch('awx.coverage_middleware.os.makedirs'), patch('awx.coverage_middleware.os.path.exists', return_value=True):
            mock_settings.TEST_COVERAGE_OUTPUT_DIR = output_dir
            mock_cov_module.Coverage.return_value = fake_cov

            with pytest.raises(RuntimeError):
                mw(_request('failing_test'))

        fake_cov.switch_context.assert_called_with('')

    def test_file_deleted_triggers_reinit(self, output_dir):
        """If .coverage.<pid> is deleted, _get_coverage reinitializes."""
        get_response = MagicMock(return_value=MagicMock())
        mw = CoverageMiddleware(get_response)
        fake_cov = MagicMock()

        # First call: _cov is None, no exists check done.
        # Second call: _cov is not None, exists returns False → reinit.
        with patch('awx.coverage_middleware.coverage') as mock_cov_module, patch('awx.coverage_middleware.os.getpid', return_value=1), patch(
            'awx.coverage_middleware.settings'
        ) as mock_settings, patch('awx.coverage_middleware.os.makedirs'), patch('awx.coverage_middleware.os.path.exists', return_value=False):
            mock_settings.TEST_COVERAGE_OUTPUT_DIR = output_dir
            mock_cov_module.Coverage.return_value = fake_cov

            mw(_request('test_a'))  # _cov is None → init, no exists check
            mw(_request('test_b'))  # _cov is not None, exists=False → reinit

        assert mock_cov_module.Coverage.call_count == 2
        assert fake_cov.start.call_count == 2

    def test_save_called_on_init_to_create_file(self, output_dir):
        """save() called immediately after start so file exists for deletion detection."""
        get_response = MagicMock(return_value=MagicMock())
        mw = CoverageMiddleware(get_response)
        fake_cov = MagicMock()

        with patch('awx.coverage_middleware.coverage') as mock_cov_module, patch('awx.coverage_middleware.os.getpid', return_value=1), patch(
            'awx.coverage_middleware.settings'
        ) as mock_settings, patch('awx.coverage_middleware.os.makedirs'), patch('awx.coverage_middleware.os.path.exists', return_value=True):
            mock_settings.TEST_COVERAGE_OUTPUT_DIR = output_dir
            mock_cov_module.Coverage.return_value = fake_cov

            mw(_request('test_a'))

        start_idx = next(i for i, c in enumerate(fake_cov.mock_calls) if c == call.start())
        save_idx = next(i for i, c in enumerate(fake_cov.mock_calls) if c == call.save())
        assert save_idx > start_idx, "save() must be called after start() during init"
