"""Arena challenge runner: isolated container execution service.

Deployed on a separate host. The CTFd platform never has Docker access; it
talks to this service over HTTPS with HMAC-signed requests.
"""

__version__ = "0.1.0"
