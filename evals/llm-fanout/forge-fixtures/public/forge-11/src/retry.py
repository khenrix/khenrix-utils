"""Public synthetic stable-write-identity fusion case, deliberately flawed."""
def retry_write(send, payload):
    for attempt in range(4):
        try:
            return send(str(attempt), payload)
        except TimeoutError:
            if attempt == 3:
                raise
