"""Users, roles and passwords for the Automation Hub server.

Roles
    admin     everything: users, catalog, stored files, inventories, backups, run history,
              workflows, and approving any approval step
    approver  build and launch workflows, and approve or reject approval steps
              in runs launched by someone else
    user      build, test and launch workflows (the "workflow user")

Passwords are stored as PBKDF2-SHA256 hashes. Session tokens are random and only
their SHA-256 hash is stored, so a copy of the database can't be used to log in.
"""
import base64
import hashlib
import hmac
import secrets
import threading
import time

ROLES = {
    "admin": {"label": "Admin", "perms": {"read", "workflows", "approve", "catalog", "users", "backup", "history"}},
    "approver": {"label": "Approver", "perms": {"read", "workflows", "approve"}},
    "user": {"label": "Workflow user", "perms": {"read", "workflows"}},
}
ITERATIONS = 600_000
SESSION_SECONDS = 7 * 24 * 3600
MIN_PASSWORD = 8


def perms(role):
    return ROLES.get(role, {}).get("perms", set())


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(ITERATIONS, base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def verify_password(password, stored):
    try:
        algo, iters, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        test = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iters))
        return hmac.compare_digest(test, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


def check_password_rules(password):
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        return f"Passwords need at least {MIN_PASSWORD} characters"
    return None


def new_token():
    return secrets.token_urlsafe(32)


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


# Same cost for unknown users as for real ones, so response time doesn't reveal which usernames exist.
DUMMY_HASH = hash_password(secrets.token_urlsafe(12))


class LoginThrottle:
    """After 5 failed logins for a username or client within 5 minutes, refuse attempts for a while."""

    def __init__(self, limit=5, window=300, lockout=60):
        self.limit, self.window, self.lockout = limit, window, lockout
        self.fails = {}
        self.lock = threading.Lock()

    def _recent(self, key, t):
        return [x for x in self.fails.get(key, []) if t - x < self.window]

    def blocked(self, *keys):
        t = time.time()
        with self.lock:
            for k in keys:
                recent = self._recent(k, t)
                if len(recent) >= self.limit and t - recent[-1] < self.lockout:
                    return int(self.lockout - (t - recent[-1])) + 1
        return 0

    def failed(self, *keys):
        t = time.time()
        with self.lock:
            for k in keys:
                self.fails[k] = self._recent(k, t) + [t]

    def succeeded(self, *keys):
        with self.lock:
            for k in keys:
                self.fails.pop(k, None)
