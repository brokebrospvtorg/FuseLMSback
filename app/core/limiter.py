from slowapi import Limiter
from slowapi.util import get_remote_address

# Applied to auth endpoints (login, verify-token, submit-activation, password
# reset request) to block brute-force attempts.
limiter = Limiter(key_func=get_remote_address)
