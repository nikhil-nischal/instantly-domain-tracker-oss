"""User registry — multi-user support for Instantly Domain Tracker.

Manages users (each with their own Instantly API key and SQLite database)
via a JSON-based registry at data/users.json.
"""

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Paths
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
USERS_JSON_PATH = _PROJECT_ROOT / "data" / "users.json"
USERS_DATA_DIR = _PROJECT_ROOT / "data" / "users"


@dataclass
class User:
    """A tracked user with their own Instantly API key and database."""

    id: str  # slug: "default", "client_acme"
    name: str
    instantly_api_key: str
    is_admin: bool = False
    active: bool = True
    created_at: str = ""  # ISO timestamp

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()

    @property
    def db_path(self) -> Path:
        """Path to this user's SQLite database."""
        return USERS_DATA_DIR / self.id / "tracker.db"

    @property
    def data_dir(self) -> Path:
        """Path to this user's data directory."""
        return USERS_DATA_DIR / self.id


class UserRegistry:
    """Persistent registry of users backed by data/users.json."""

    def __init__(self):
        self.users: dict[str, User] = {}
        self.load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load(self):
        """Load users from users.json."""
        if USERS_JSON_PATH.exists():
            data = USERS_JSON_PATH.read_text()
            if data.strip():
                parsed = __import__("json").loads(data)
                self.users = {
                    k: User(**v) for k, v in parsed.get("users", {}).items()
                }
        # Ensure default user exists
        if "default" not in self.users:
            from .config import settings
            default_user = User(
                id="default",
                name="Default",
                instantly_api_key=settings.INSTANTLY_API_KEY or "",
                is_admin=True,
                active=True,
            )
            self.users["default"] = default_user
            self.save()

    def save(self):
        """Save users to users.json."""
        USERS_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {"users": {k: asdict(v) for k, v in self.users.items()}}
        USERS_JSON_PATH.write_text(__import__("json").dumps(data, indent=2))

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def add_user(self, user: User) -> None:
        """Add or update a user in the registry."""
        self.users[user.id] = user
        self.save()
        # Create data directory
        user.data_dir.mkdir(parents=True, exist_ok=True)

    def remove_user(self, slug: str) -> bool:
        """Remove a user from the registry. Returns True if removed."""
        if slug == "default":
            return False  # Cannot remove default user
        if slug in self.users:
            del self.users[slug]
            self.save()
            return True
        return False

    def get_user(self, slug: str) -> Optional[User]:
        """Get a user by slug/id."""
        return self.users.get(slug)

    def list_users(self) -> list[User]:
        """Return all users."""
        return list(self.users.values())

    def list_active_users(self) -> list[User]:
        """Return only active users."""
        return [u for u in self.users.values() if u.active]

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def get_user_db_path(self, slug: str) -> Path:
        """Get the database path for a user slug."""
        user = self.get_user(slug)
        if user:
            return user.db_path
        # Fallback to default location for backward compat
        return USERS_DATA_DIR / slug / "tracker.db"

    def get_user_api_key(self, slug: str) -> Optional[str]:
        """Get the API key for a user slug."""
        user = self.get_user(slug)
        if user:
            return user.instantly_api_key
        return None
