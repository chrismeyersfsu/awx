import atexit
import os

import coverage
from django.conf import settings

os.environ["COVERAGE_CORE"] = "sysmon"

_cov = None
_data_file = None


def _save_coverage():
    global _cov
    if _cov is not None:
        _cov.stop()
        _cov.save()
        _cov = None


def _get_coverage():
    global _cov, _data_file
    if _cov is not None and not os.path.exists(_data_file):
        _cov.stop()
        _cov = None
    if _cov is None:
        output_dir = getattr(settings, 'TEST_COVERAGE_OUTPUT_DIR', '/tmp/awx-coverage')
        os.makedirs(output_dir, exist_ok=True)
        _data_file = os.path.join(output_dir, f'.coverage.{os.getpid()}')
        _cov = coverage.Coverage(data_file=_data_file)
        _cov.erase()
        _cov.start()
        _cov.save()  # create file immediately so deletion is detectable
        atexit.register(_save_coverage)
    return _cov


class CoverageMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        test_name = request.META.get('HTTP_X_TEST_NAME')
        if not test_name:
            return self.get_response(request)

        cov = _get_coverage()
        cov.switch_context(test_name)
        try:
            response = self.get_response(request)
        finally:
            cov.switch_context('')
        return response
