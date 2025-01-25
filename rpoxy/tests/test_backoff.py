
def test_exponential_backoff():
    """Unit tests for exponential_backoff."""
    logs = []

    def mock_sleep(duration: float):
        logs.append(duration)

    # Test 1: Base delay only
    exponential_backoff(1, base_delay=2, jitter=0, start_delay=0, sleep_fn=mock_sleep)
    assert logs[0] == 2, f"Expected 2, got {logs[0]}"

    # Test 2: Exponential growth
    logs.clear()
    exponential_backoff(3, base_delay=2, jitter=0, start_delay=0, sleep_fn=mock_sleep)
    assert logs[0] == 8, f"Expected 8, got {logs[0]}"

    # Test 3: Start delay
    logs.clear()
    exponential_backoff(2, base_delay=2, jitter=0, start_delay=1, sleep_fn=mock_sleep)
    assert logs[0] == 5, f"Expected 5, got {logs[0]}"

    # Test 4: Jitter effect
    logs.clear()
    exponential_backoff(2, base_delay=2, jitter=0.5, start_delay=0, sleep_fn=mock_sleep)
    assert 2 <= logs[0] <= 6, f"Expected jittered delay between 2 and 6, got {logs[0]}"

    # Test 5: Negative delay prevented
    logs.clear()
    exponential_backoff(1, base_delay=2, jitter=2, start_delay=-10, sleep_fn=mock_sleep)
    assert logs[0] >= 0, f"Expected non-negative delay, got {logs[0]}"

    # Test 6: Combination of base delay, exponential growth, start delay, and jitter
    logs.clear()
    exponential_backoff(3, base_delay=2, jitter=0.5, start_delay=1, sleep_fn=mock_sleep)
    expected_min = 15  # 8 + 1 - jitter range
    expected_max = 17  # 8 + 1 + jitter range
    assert expected_min <= logs[0] <= expected_max, f"Expected delay between {expected_min} and {expected_max}, got {logs[0]}"
