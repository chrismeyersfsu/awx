import random
from typing import Callable, Dict, Any

def retry_post_with_recovery(
    post_function: Callable[[str, Dict[str, Any], Dict[str, str]], Any],
    url: str,
    data: Dict[str, Any],
    headers: Dict[str, str] = None,
    recovery_step: Callable[[], None] = None,
    retries: int = 3,
    delay_strategy: Callable[[int], None] = None,
) -> Any:
    """
    Sends a POST request using the provided post_function and retries after performing a recovery step if the request fails.

    Args:
        post_function (Callable): A function that performs the POST request. Should accept (url, data, headers) as arguments.
        url (str): The endpoint URL.
        data (Dict[str, Any]): The data to send in the POST request.
        headers (Dict[str, str], optional): Headers to include in the request. Defaults to None.
        recovery_step (Callable[[], None], optional): A function to perform a recovery step if the request fails.
        retries (int): The maximum number of retry attempts. Defaults to 3.
        delay_strategy (Callable[[int], None], optional): A function to handle the delay between retries. It receives the attempt number.

    Returns:
        Any: The response object if the request is successful.

    Raises:
        Exception: If all retry attempts fail.
    """
    attempt = 0

    while attempt < retries:
        try:
            # Attempt the POST request
            response = post_function(url, data, headers)

            # If the response is successful, return it
            return response
        except Exception as e:
            attempt += 1
            print(f"Attempt {attempt} failed: {e}")

            # Perform the recovery step if provided
            if recovery_step:
                print("Performing recovery step...")
                recovery_step()

            # Apply the delay strategy if provided
            if attempt < retries and delay_strategy:
                delay_strategy(attempt)

    # If all retries fail, raise the last exception
    raise Exception(f"All {retries} retry attempts failed.")