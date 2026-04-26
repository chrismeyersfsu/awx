# Coverage Middleware

Records per-request Python coverage data keyed by integration test name. Each API request that carries an `X-Test-Name` header produces a separate `.coverage.*` file, which can later be combined and reported.

## Enabling

Add `CoverageMiddleware` as the first entry in `MIDDLEWARE` (so it wraps every other middleware):

```python
# awx/settings/development.py or equivalent
MIDDLEWARE = [
    'awx.coverage_middleware.CoverageMiddleware',
    # … existing middleware …
]
```

## Configuration

| Setting | Env var | Default |
|---|---|---|
| `TEST_COVERAGE_OUTPUT_DIR` | `TEST_COVERAGE_OUTPUT_DIR` | `/tmp/awx-coverage` |

Set the env var before starting the AWX process:

```bash
export TEST_COVERAGE_OUTPUT_DIR=/tmp/awx-coverage
```

## How It Works

Each request that includes the `X-Test-Name` header triggers coverage collection. The middleware:

1. Reads `X-Test-Name` from the request header.
2. Sanitizes the name (replaces `/` and spaces with `_`).
3. Creates a `coverage.Coverage` instance writing to `${TEST_COVERAGE_OUTPUT_DIR}/.coverage.${safe_name}.${PID}`.
4. Starts coverage, calls the next middleware/view, then stops and saves in a `finally` block.

Requests without `X-Test-Name` are passed through unchanged.

## Combining and Reporting

After test runs, combine all per-request files and generate a report:

```bash
# Combine all .coverage.* files in the output dir
coverage combine /tmp/awx-coverage

# Terminal report
coverage report

# HTML report
coverage html
# Open htmlcov/index.html in a browser
```
