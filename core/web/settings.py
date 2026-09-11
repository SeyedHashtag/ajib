from dataclasses import dataclass, field
import os
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    origin: str = "http://127.0.0.1:8080"
    secure_cookies: bool = False
    writes_enabled: bool = False
    pilot_users: frozenset[str] = field(default_factory=frozenset)
    public_portal: bool = False

    @classmethod
    def from_env(cls):
        origin = os.getenv("AJIB_WEB_ORIGIN", "http://127.0.0.1:8080").rstrip("/")
        url = urlsplit(origin)
        if url.scheme not in {"http", "https"} or not url.netloc or url.path or url.query or url.fragment or url.username:
            raise ValueError("AJIB_WEB_ORIGIN must be an HTTP(S) origin")
        if url.scheme != "https" and url.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("A public web origin requires HTTPS")
        return cls(origin=origin, secure_cookies=url.scheme == "https",
                   writes_enabled=os.getenv("AJIB_WEB_WRITES_ENABLED") == "1",
                   pilot_users=frozenset(os.getenv("AJIB_WEB_PILOT_USERS", "").replace(" ", "").split(",")) - {""},
                   public_portal=os.getenv("AJIB_WEB_PUBLIC_PORTAL") == "1")
