# server_fastmcp.py
import logging
import logging.config
import uuid
import time
import threading
import os
from collections import defaultdict
from typing import Optional, List, Dict, Any, Tuple
from urllib.parse import urlparse
from dataclasses import dataclass
from datetime import datetime, timedelta

from mcp.server.fastmcp import FastMCP
from pytaigaclient.exceptions import TaigaException

# Assuming taiga_client.py is in the same directory or accessible via PYTHONPATH
from src.taiga_client import TaigaClientWrapper

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],  # Log to stderr by default
)
logger = logging.getLogger(__name__)
# Quiet down pytaigaclient library logging if needed
logging.getLogger("pytaigaclient").setLevel(logging.WARNING)

# --- Security Configuration ---
SESSION_EXPIRY = int(os.getenv("SESSION_EXPIRY", "28800"))  # 8 hours default
SESSION_CLEANUP_INTERVAL = int(os.getenv("SESSION_CLEANUP_INTERVAL", "3600"))  # 1 hour
RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "true").lower() == "true"
RATE_LIMIT_LOGIN_REQUESTS = int(os.getenv("RATE_LIMIT_LOGIN_REQUESTS", "5"))  # 5 login attempts
RATE_LIMIT_LOGIN_WINDOW = int(os.getenv("RATE_LIMIT_LOGIN_WINDOW", "300"))  # per 5 minutes
RATE_LIMIT_API_REQUESTS = int(os.getenv("RATE_LIMIT_API_REQUESTS", "100"))  # 100 API calls
RATE_LIMIT_API_WINDOW = int(os.getenv("RATE_LIMIT_API_WINDOW", "60"))  # per minute
MAX_INPUT_LENGTH = int(os.getenv("MAX_INPUT_LENGTH", "10000"))
MAX_NAME_LENGTH = int(os.getenv("MAX_NAME_LENGTH", "255"))


# --- Data Structures ---
@dataclass
class SessionData:
    """Session data with expiration tracking"""

    client: TaigaClientWrapper
    created_at: float
    last_activity: float
    username: str

    def is_expired(self) -> bool:
        """Check if session has expired"""
        return time.time() - self.last_activity > SESSION_EXPIRY

    def update_activity(self):
        """Update last activity timestamp"""
        self.last_activity = time.time()


# --- Rate Limiting ---
class RateLimiter:
    """Thread-safe rate limiter"""

    def __init__(self, max_requests: int, window: int):
        self.max_requests = max_requests
        self.window = window
        self.requests: Dict[str, List[float]] = defaultdict(list)
        self.lock = threading.Lock()

    def is_allowed(self, identifier: str) -> bool:
        """Check if request is allowed for identifier"""
        with self.lock:
            now = time.time()
            # Remove old requests outside the window
            self.requests[identifier] = [
                req_time for req_time in self.requests[identifier] if now - req_time < self.window
            ]

            if len(self.requests[identifier]) >= self.max_requests:
                return False

            self.requests[identifier].append(now)
            return True

    def reset(self, identifier: str):
        """Reset rate limit for identifier"""
        with self.lock:
            self.requests.pop(identifier, None)


# Initialize rate limiters
login_rate_limiter = RateLimiter(RATE_LIMIT_LOGIN_REQUESTS, RATE_LIMIT_LOGIN_WINDOW)
api_rate_limiter = RateLimiter(RATE_LIMIT_API_REQUESTS, RATE_LIMIT_API_WINDOW)


# --- Input Validation ---
def validate_string_input(
    value: str,
    field_name: str,
    max_length: int = MAX_INPUT_LENGTH,
    min_length: int = 1,
    allow_empty: bool = False,
) -> str:
    """Validate string input with length constraints"""
    if value is None:
        if allow_empty:
            return ""
        raise ValueError(f"{field_name} cannot be None")

    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")

    value = value.strip()

    if not allow_empty and len(value) < min_length:
        raise ValueError(f"{field_name} cannot be empty")

    if len(value) > max_length:
        raise ValueError(f"{field_name} too long (max {max_length} characters)")

    return value


def validate_host_url(host: str) -> str:
    """Validate and sanitize host URL"""
    host = validate_string_input(host, "Host URL", max_length=500)

    try:
        parsed = urlparse(host)
    except Exception as e:
        raise ValueError(f"Invalid host URL: {e}")

    # Ensure scheme is present
    if not parsed.scheme:
        raise ValueError("Host URL must include protocol (http:// or https://)")

    # Ensure it's HTTP or HTTPS
    if parsed.scheme not in ["http", "https"]:
        raise ValueError("Host URL must use HTTP or HTTPS protocol")

    # Warn on HTTP for non-localhost
    if (
        parsed.scheme == "http"
        and not parsed.netloc.startswith("localhost")
        and not parsed.netloc.startswith("127.0.0.1")
    ):
        logger.warning(
            f"Using unencrypted HTTP connection to {parsed.netloc} - this is insecure for production!"
        )

    # Ensure netloc exists
    if not parsed.netloc:
        raise ValueError("Host URL must include a valid domain or IP address")

    return host


def validate_project_name(name: str) -> str:
    """Validate project name"""
    return validate_string_input(name, "Project name", max_length=MAX_NAME_LENGTH)


def validate_email(email: str) -> str:
    """Basic email validation"""
    email = validate_string_input(email, "Email", max_length=254)

    if "@" not in email or "." not in email.split("@")[1]:
        raise ValueError("Invalid email format")

    return email


def hash_for_logging(value: str) -> str:
    """Hash a value for safe logging"""
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()[:8]


# --- Manual Session Management ---
# Store active sessions: session_id -> SessionData instance
active_sessions: Dict[str, SessionData] = {}
session_lock = threading.Lock()

# --- MCP Server Definition ---
# No lifespan needed for this approach
mcp = FastMCP("Taiga Bridge (Session ID)", dependencies=["pytaigaclient"])


# --- Session Cleanup ---
def cleanup_expired_sessions():
    """Periodically cleanup expired sessions"""
    while True:
        try:
            time.sleep(SESSION_CLEANUP_INTERVAL)
            with session_lock:
                expired_sessions = [
                    sid
                    for sid, session_data in active_sessions.items()
                    if session_data.is_expired()
                ]

                for sid in expired_sessions:
                    logger.info(f"Cleaning up expired session: {sid[:8]}...")
                    active_sessions.pop(sid, None)

                if expired_sessions:
                    logger.info(f"Cleaned up {len(expired_sessions)} expired session(s)")

        except Exception as e:
            logger.error(f"Error in session cleanup: {e}", exc_info=True)


# Start cleanup thread
cleanup_thread = threading.Thread(target=cleanup_expired_sessions, daemon=True)
cleanup_thread.start()
logger.info(
    f"Session cleanup thread started (interval: {SESSION_CLEANUP_INTERVAL}s, expiry: {SESSION_EXPIRY}s)"
)

# --- Helper Function for Session Validation ---


def _get_authenticated_client(session_id: str) -> TaigaClientWrapper:
    """
    Retrieves the authenticated TaigaClientWrapper for a given session ID.
    Raises PermissionError if the session is invalid or not found.
    """
    with session_lock:
        session_data = active_sessions.get(session_id)

        # Check if session exists
        if not session_data:
            logger.warning(f"Session not found: {session_id[:8]}")
            raise PermissionError("Invalid session ID. Please login again.")

        # Check if session is expired
        if session_data.is_expired():
            logger.warning(f"Session expired: {session_id[:8]}")
            active_sessions.pop(session_id, None)
            raise PermissionError("Session expired. Please login again.")

        # Check if client is still authenticated
        if not session_data.client.is_authenticated:
            logger.warning(f"Client not authenticated for session: {session_id[:8]}")
            active_sessions.pop(session_id, None)
            raise PermissionError("Session invalid. Please login again.")

        # Update last activity
        session_data.update_activity()

        logger.debug(f"Retrieved valid client for session: {session_id[:8]}")
        return session_data.client


# --- MCP Tools ---


@mcp.tool(
    "login",
    description="Logs into a Taiga instance using username/password and returns a session_id for subsequent authenticated calls.",
)
def login(host: str, username: str, password: str) -> Dict[str, str]:
    """
    Handles Taiga login and creates a session.

    Args:
        host: The URL of the Taiga instance (e.g., 'https://tree.taiga.io').
        username: The Taiga username.
        password: The Taiga password.

    Returns:
        A dictionary containing the session_id upon successful login.
        Example: {"session_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"}
    """
    # Validate inputs
    try:
        host = validate_host_url(host)
        username = validate_string_input(username, "Username", max_length=150)
        password = validate_string_input(password, "Password", max_length=128)
    except ValueError as e:
        logger.warning(f"Login validation failed: {e}")
        raise e

    # Use hashed username for logging
    username_hash = hash_for_logging(username)
    logger.info(f"Login attempt for user hash {username_hash} on host '{host}'")

    # Rate limiting
    if RATE_LIMIT_ENABLED:
        rate_limit_key = f"login:{username_hash}"
        if not login_rate_limiter.is_allowed(rate_limit_key):
            logger.warning(f"Rate limit exceeded for login attempts: {username_hash}")
            raise PermissionError(
                f"Too many login attempts. Please try again in {RATE_LIMIT_LOGIN_WINDOW} seconds."
            )

    try:
        wrapper = TaigaClientWrapper(host=host)
        login_successful = wrapper.login(username=username, password=password)

        if login_successful:
            # Generate a unique session ID
            new_session_id = str(uuid.uuid4())

            # Create session data with timestamp
            now = time.time()
            session_data = SessionData(
                client=wrapper, created_at=now, last_activity=now, username=username
            )

            # Store the session
            with session_lock:
                active_sessions[new_session_id] = session_data

            logger.info(
                f"Login successful for user hash {username_hash}. Session: {new_session_id[:8]}"
            )

            # Reset rate limiter on successful login
            if RATE_LIMIT_ENABLED:
                login_rate_limiter.reset(rate_limit_key)

            # Return the session ID to the client
            return {"session_id": new_session_id, "expires_in": SESSION_EXPIRY}
        else:
            # Should not happen if login raises exception on failure, but handle defensively
            logger.error(
                f"Login attempt for user hash {username_hash} returned False unexpectedly."
            )
            raise RuntimeError("Login failed for an unknown reason.")

    except (ValueError, TaigaException) as e:
        logger.warning(f"Login failed for user hash {username_hash}: {type(e).__name__}")
        # Re-raise the exception - FastMCP will turn it into an error response
        raise e
    except Exception as e:
        logger.error(f"Unexpected error during login for user hash {username_hash}", exc_info=True)
        raise RuntimeError("An unexpected server error occurred during login")


# server_fastmcp.py

# ... (keep existing imports, logging setup, active_sessions, FastMCP instance) ...
# ... (keep _get_authenticated_client helper function) ...
# ... (keep login tool function) ...


# --- Project Tools ---


@mcp.tool(
    "list_projects",
    description="Lists projects accessible to the user associated with the provided session_id.",
)
def list_projects(session_id: str) -> List[Dict[str, Any]]:
    """Lists projects accessible by the authenticated user."""
    logger.info(f"Executing list_projects for session {session_id[:8]}...")
    taiga_client_wrapper = _get_authenticated_client(session_id)  # Use wrapper variable name
    try:
        # Use pytaigaclient syntax: client.resource.method()
        projects = taiga_client_wrapper.api.projects.list()
        # Remove .to_dict() as pytaigaclient should return dicts
        # result = [p.to_dict() for p in projects]
        logger.info(
            f"list_projects successful for session {session_id[:8]}, found {len(projects)} projects."
        )
        return projects  # Return directly
    except TaigaException as e:
        logger.error(f"Taiga API error listing projects: {e}", exc_info=False)
        raise e
    except Exception as e:
        logger.error(f"Unexpected error listing projects: {e}", exc_info=True)
        raise RuntimeError(f"Server error listing projects: {e}")


@mcp.tool(
    "list_all_projects",
    description="Lists all projects visible to the user (requires admin privileges for full list). Uses the provided session_id.",
)
def list_all_projects(session_id: str) -> List[Dict[str, Any]]:
    """Lists all projects visible to the authenticated user (scope depends on permissions)."""
    logger.info(f"Executing list_all_projects for session {session_id[:8]}...")
    # pytaigaclient's list() likely behaves similarly to python-taiga's
    return list_projects(session_id)  # Keep delegation


@mcp.tool(
    "get_project", description="Gets detailed information about a specific project by its ID."
)
def get_project(session_id: str, project_id: int) -> Dict[str, Any]:
    """Retrieves project details by ID."""
    logger.info(f"Executing get_project ID {project_id} for session {session_id[:8]}...")
    taiga_client_wrapper = _get_authenticated_client(session_id)  # Use wrapper variable name
    try:
        # Use pytaigaclient syntax: client.resource.get(project_id)
        project = taiga_client_wrapper.api.projects.get(project_id)
        # return project.to_dict() # Remove .to_dict()
        return project  # Return directly
    except TaigaException as e:
        logger.error(f"Taiga API error getting project {project_id}: {e}", exc_info=False)
        raise e
    except Exception as e:
        logger.error(f"Unexpected error getting project {project_id}: {e}", exc_info=True)
        raise RuntimeError(f"Server error getting project: {e}")


@mcp.tool(
    "get_project_by_slug",
    description="Gets detailed information about a specific project by its slug.",
)
def get_project_by_slug(session_id: str, slug: str) -> Dict[str, Any]:
    """Retrieves project details by slug."""
    logger.info(f"Executing get_project_by_slug '{slug}' for session {session_id[:8]}...")
    taiga_client_wrapper = _get_authenticated_client(session_id)  # Use wrapper variable name
    try:
        # Use pytaigaclient syntax: client.resource.get(slug=...)
        project = taiga_client_wrapper.api.projects.get(slug=slug)
        # return project.to_dict() # Remove .to_dict()
        return project  # Return directly
    except TaigaException as e:
        logger.error(f"Taiga API error getting project by slug '{slug}': {e}", exc_info=False)
        raise e
    except Exception as e:
        logger.error(f"Unexpected error getting project by slug '{slug}': {e}", exc_info=True)
        raise RuntimeError(f"Server error getting project by slug: {e}")


@mcp.tool("create_project", description="Creates a new project.")
def create_project(session_id: str, name: str, description: str, **kwargs) -> Dict[str, Any]:
    """Creates a new project. Requires name and description. Optional args (e.g., is_private) via kwargs."""
    # Validate inputs
    name = validate_project_name(name)
    description = validate_string_input(description, "Description", max_length=MAX_INPUT_LENGTH)

    logger.info(f"Executing create_project for session {session_id[:8]}")
    taiga_client_wrapper = _get_authenticated_client(session_id)

    try:
        # Use pytaigaclient syntax: client.projects.create(name=..., description=..., **kwargs)
        new_project = taiga_client_wrapper.api.projects.create(
            name=name, description=description, **kwargs
        )
        logger.info(f"Project created successfully (ID: {new_project.get('id', 'N/A')}).")
        return new_project  # Return the created project dict
    except TaigaException as e:
        logger.error(f"Taiga API error creating project: {type(e).__name__}", exc_info=False)
        raise e
    except Exception as e:
        logger.error(f"Unexpected error creating project", exc_info=True)
        raise RuntimeError("Server error creating project")


@mcp.tool("update_project", description="Updates details of an existing project.")
def update_project(session_id: str, project_id: int, **kwargs) -> Dict[str, Any]:
    """Updates a project. Pass fields to update as keyword arguments (e.g., name='New Name', description='New Desc')."""
    logger.info(
        f"Executing update_project ID {project_id} for session {session_id[:8]} with data: {kwargs}"
    )
    taiga_client_wrapper = _get_authenticated_client(session_id)  # Use wrapper variable name
    try:
        # Use pytaigaclient update pattern: client.resource.update(id=..., data=...)
        if not kwargs:
            logger.info(f"No fields provided for update on project {project_id}")
            # Return current state if no updates provided
            return taiga_client_wrapper.api.projects.get(project_id=project_id)

        # First fetch the project to get its current version
        current_project = taiga_client_wrapper.api.projects.get(project_id=project_id)
        version = current_project.get("version")
        if not version:
            raise ValueError(f"Could not determine version for project {project_id}")

        # The project update method requires project_id, version, and project_data
        updated_project = taiga_client_wrapper.api.projects.update(
            project_id=project_id, version=version, project_data=kwargs
        )
        logger.info(f"Project {project_id} update request sent.")
        # Return the result from the update call
        return updated_project
    except TaigaException as e:
        logger.error(f"Taiga API error updating project {project_id}: {e}", exc_info=False)
        raise e
    except Exception as e:
        logger.error(f"Unexpected error updating project {project_id}: {e}", exc_info=True)
        raise RuntimeError(f"Server error updating project: {e}")


@mcp.tool("delete_project", description="Deletes a project by its ID. This is irreversible.")
def delete_project(session_id: str, project_id: int) -> Dict[str, Any]:
    """Deletes a project by ID."""
    logger.warning(f"Executing delete_project ID {project_id} for session {session_id[:8]}...")
    taiga_client_wrapper = _get_authenticated_client(session_id)  # Use wrapper variable name
    try:
        # Use pytaigaclient syntax: client.resource.delete(id=...)
        taiga_client_wrapper.api.projects.delete(id=project_id)
        logger.info(f"Project {project_id} deleted successfully.")
        return {"status": "deleted", "project_id": project_id}
    except TaigaException as e:
        logger.error(f"Taiga API error deleting project {project_id}: {e}", exc_info=False)
        raise e
    except Exception as e:
        logger.error(f"Unexpected error deleting project {project_id}: {e}", exc_info=True)
        raise RuntimeError(f"Server error deleting project: {e}")


# @mcp.tool("get_project_roles", description="Lists the available roles within a specific project.")
# def get_project_roles(session_id: str, project_id: int) -> List[Dict[str, Any]]:
#     """Retrieves the list of roles for a project. (REMOVED - Not directly supported by pytaigaclient)"""
#     logger.warning(f"get_project_roles called, but not supported by pytaigaclient. Project: {project_id}")
#     raise NotImplementedError("Listing project-specific roles is not currently supported by the pytaigaclient wrapper.")

# --- User Story Tools ---


@mcp.tool(
    "list_user_stories",
    description="Lists user stories within a specific project, optionally filtered.",
)
def list_user_stories(session_id: str, project_id: int, **filters) -> List[Dict[str, Any]]:
    """Lists user stories for a project. Optional filters like 'milestone', 'status', 'assigned_to' can be passed as keyword arguments."""
    logger.info(
        f"Executing list_user_stories for project {project_id}, session {session_id[:8]}, filters: {filters}"
    )
    taiga_client_wrapper = _get_authenticated_client(session_id)  # Use wrapper variable name
    try:
        # Use pytaigaclient syntax: client.resource.list(project=..., **filters)
        stories = taiga_client_wrapper.api.user_stories.list(project=project_id, **filters)
        # return [s.to_dict() for s in stories] # Remove .to_dict()
        return stories  # Return directly
    except TaigaException as e:
        logger.error(
            f"Taiga API error listing user stories for project {project_id}: {e}", exc_info=False
        )
        raise e
    except Exception as e:
        logger.error(
            f"Unexpected error listing user stories for project {project_id}: {e}", exc_info=True
        )
        raise RuntimeError(f"Server error listing user stories: {e}")


@mcp.tool("create_user_story", description="Creates a new user story within a project.")
def create_user_story(session_id: str, project_id: int, subject: str, **kwargs) -> Dict[str, Any]:
    """Creates a user story. Requires project_id and subject. Optional fields (description, milestone_id, status_id, assigned_to_id, etc.) via kwargs."""
    logger.info(
        f"Executing create_user_story '{subject}' in project {project_id}, session {session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(session_id)  # Use wrapper variable name
    if not subject:
        raise ValueError("User story subject cannot be empty.")
    try:
        # Use pytaigaclient syntax: client.resource.create(project=..., subject=..., **kwargs)
        story = taiga_client_wrapper.api.user_stories.create(
            project=project_id, subject=subject, **kwargs
        )
        logger.info(
            f"User story '{subject}' created successfully (ID: {story.get('id', 'N/A')})."
        )  # Use .get() for safety
        # return story.to_dict() # Remove .to_dict()
        return story  # Return directly
    except TaigaException as e:
        logger.error(f"Taiga API error creating user story '{subject}': {e}", exc_info=False)
        raise e
    except Exception as e:
        logger.error(f"Unexpected error creating user story '{subject}': {e}", exc_info=True)
        raise RuntimeError(f"Server error creating user story: {e}")


@mcp.tool(
    "get_user_story", description="Gets detailed information about a specific user story by its ID."
)
def get_user_story(session_id: str, user_story_id: int) -> Dict[str, Any]:
    """Retrieves user story details by ID."""
    logger.info(f"Executing get_user_story ID {user_story_id} for session {session_id[:8]}...")
    taiga_client_wrapper = _get_authenticated_client(session_id)  # Use wrapper variable name
    try:
        # User stories expects user_story_id as a positional argument
        story = taiga_client_wrapper.api.user_stories.get(user_story_id)
        # return story.to_dict() # Remove .to_dict()
        return story  # Return directly
    except TaigaException as e:
        logger.error(f"Taiga API error getting user story {user_story_id}: {e}", exc_info=False)
        raise e
    except Exception as e:
        logger.error(f"Unexpected error getting user story {user_story_id}: {e}", exc_info=True)
        raise RuntimeError(f"Server error getting user story: {e}")


# @mcp.tool("get_user_story_by_ref", description="Gets detailed information about a specific user story by its reference number within a project.")
# def get_user_story_by_ref(session_id: str, project_id: int, ref: int) -> Dict[str, Any]:
#     """Retrieves user story details by project ID and reference number. (REMOVED - Not directly supported by pytaigaclient)"""
#     logger.warning(f"get_user_story_by_ref called, but not supported by pytaigaclient. Project: {project_id}, Ref: {ref}")
#     raise NotImplementedError("Getting user stories by reference number is not currently supported by the pytaigaclient wrapper.")


@mcp.tool("update_user_story", description="Updates details of an existing user story.")
def update_user_story(session_id: str, user_story_id: int, **kwargs) -> Dict[str, Any]:
    """Updates a user story. Pass fields to update as keyword arguments (e.g., subject, description, status_id, assigned_to)."""
    logger.info(
        f"Executing update_user_story ID {user_story_id} for session {session_id[:8]} with data: {kwargs}"
    )
    taiga_client_wrapper = _get_authenticated_client(session_id)  # Use wrapper variable name
    try:
        # Use pytaigaclient update pattern: client.resource.edit for partial updates
        if not kwargs:
            logger.info(f"No fields provided for update on user story {user_story_id}")
            return taiga_client_wrapper.api.user_stories.get(user_story_id)

        # Get current user story data to retrieve version
        current_story = taiga_client_wrapper.api.user_stories.get(user_story_id)
        version = current_story.get("version")
        if not version:
            raise ValueError(f"Could not determine version for user story {user_story_id}")

        # Use edit method for partial updates with keyword arguments
        updated_story = taiga_client_wrapper.api.user_stories.edit(
            user_story_id=user_story_id, version=version, **kwargs
        )
        logger.info(f"User story {user_story_id} update request sent.")
        return updated_story
    except TaigaException as e:
        logger.error(f"Taiga API error updating user story {user_story_id}: {e}", exc_info=False)
        raise e
    except Exception as e:
        logger.error(f"Unexpected error updating user story {user_story_id}: {e}", exc_info=True)
        raise RuntimeError(f"Server error updating user story: {e}")


@mcp.tool("delete_user_story", description="Deletes a user story by its ID.")
def delete_user_story(session_id: str, user_story_id: int) -> Dict[str, Any]:
    """Deletes a user story by ID."""
    logger.warning(
        f"Executing delete_user_story ID {user_story_id} for session {session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(session_id)  # Use wrapper variable name
    try:
        # Use pytaigaclient syntax: client.resource.delete(id=...)
        taiga_client_wrapper.api.user_stories.delete(id=user_story_id)
        logger.info(f"User story {user_story_id} deleted successfully.")
        return {"status": "deleted", "user_story_id": user_story_id}
    except TaigaException as e:
        logger.error(f"Taiga API error deleting user story {user_story_id}: {e}", exc_info=False)
        raise e
    except Exception as e:
        logger.error(f"Unexpected error deleting user story {user_story_id}: {e}", exc_info=True)
        raise RuntimeError(f"Server error deleting user story: {e}")


@mcp.tool(
    "assign_user_story_to_user", description="Assigns a specific user story to a specific user."
)
def assign_user_story_to_user(session_id: str, user_story_id: int, user_id: int) -> Dict[str, Any]:
    """Assigns a user story to a user."""
    logger.info(
        f"Executing assign_user_story_to_user: US {user_story_id} -> User {user_id}, session {session_id[:8]}..."
    )
    # Delegate to update_user_story, assuming 'assigned_to' key works
    return update_user_story(session_id, user_story_id, assigned_to=user_id)


@mcp.tool(
    "unassign_user_story_from_user",
    description="Unassigns a specific user story (sets assigned user to null).",
)
def unassign_user_story_from_user(session_id: str, user_story_id: int) -> Dict[str, Any]:
    """Unassigns a user story."""
    logger.info(
        f"Executing unassign_user_story_from_user: US {user_story_id}, session {session_id[:8]}..."
    )
    # Delegate to update_user_story with assigned_to=None
    return update_user_story(session_id, user_story_id, assigned_to=None)


@mcp.tool(
    "get_user_story_statuses",
    description="Lists the available statuses for user stories within a specific project.",
)
def get_user_story_statuses(session_id: str, project_id: int) -> List[Dict[str, Any]]:
    """Retrieves the list of user story statuses for a project."""
    logger.info(
        f"Executing get_user_story_statuses for project {project_id}, session {session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(session_id)  # Use wrapper variable name
    try:
        # Use pytaigaclient syntax: client.resource.list(project_id=...)
        # Update resource name: user_story_statuses -> userstory_statuses
        statuses = taiga_client_wrapper.api.userstory_statuses.list(project_id=project_id)
        # return [s.to_dict() for s in statuses] # Remove .to_dict()
        return statuses  # Return directly
    except TaigaException as e:
        logger.error(
            f"Taiga API error getting user story statuses for project {project_id}: {e}",
            exc_info=False,
        )
        raise e
    except Exception as e:
        logger.error(
            f"Unexpected error getting user story statuses for project {project_id}: {e}",
            exc_info=True,
        )
        raise RuntimeError(f"Server error getting user story statuses: {e}")


# --- Task Tools ---


@mcp.tool("list_tasks", description="Lists tasks within a specific project, optionally filtered.")
def list_tasks(session_id: str, project_id: int, **filters) -> List[Dict[str, Any]]:
    """Lists tasks for a project. Optional filters like 'milestone', 'status', 'user_story', 'assigned_to' can be passed as keyword arguments."""
    logger.info(
        f"Executing list_tasks for project {project_id}, session {session_id[:8]}, filters: {filters}"
    )
    taiga_client_wrapper = _get_authenticated_client(session_id)  # Use wrapper variable name
    try:
        # Use pytaigaclient syntax: client.resource.list(project=..., **filters)
        tasks = taiga_client_wrapper.api.tasks.list(project=project_id, **filters)
        # return [t.to_dict() for t in tasks] # Remove .to_dict()
        return tasks  # Return directly
    except TaigaException as e:
        logger.error(f"Taiga API error listing tasks for project {project_id}: {e}", exc_info=False)
        raise e
    except Exception as e:
        logger.error(f"Unexpected error listing tasks for project {project_id}: {e}", exc_info=True)
        raise RuntimeError(f"Server error listing tasks: {e}")


@mcp.tool("create_task", description="Creates a new task within a project.")
def create_task(session_id: str, project_id: int, subject: str, **kwargs) -> Dict[str, Any]:
    """Creates a task. Requires project_id and subject. Optional fields (description, milestone_id, status_id, user_story_id, assigned_to_id, etc.) via kwargs."""
    logger.info(
        f"Executing create_task '{subject}' in project {project_id}, session {session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(session_id)  # Use wrapper variable name
    if not subject:
        raise ValueError("Task subject cannot be empty.")
    try:
        # Use pytaigaclient syntax: client.resource.create(project=..., subject=..., **kwargs)
        task = taiga_client_wrapper.api.tasks.create(project=project_id, subject=subject, **kwargs)
        logger.info(f"Task '{subject}' created successfully (ID: {task.get('id', 'N/A')}).")
        # return task.to_dict() # Remove .to_dict()
        return task  # Return directly
    except TaigaException as e:
        logger.error(f"Taiga API error creating task '{subject}': {e}", exc_info=False)
        raise e
    except Exception as e:
        logger.error(f"Unexpected error creating task '{subject}': {e}", exc_info=True)
        raise RuntimeError(f"Server error creating task: {e}")


@mcp.tool("get_task", description="Gets detailed information about a specific task by its ID.")
def get_task(session_id: str, task_id: int) -> Dict[str, Any]:
    """Retrieves task details by ID."""
    logger.info(f"Executing get_task ID {task_id} for session {session_id[:8]}...")
    taiga_client_wrapper = _get_authenticated_client(session_id)  # Use wrapper variable name
    try:
        # Tasks expects task_id as a positional argument
        task = taiga_client_wrapper.api.tasks.get(task_id)
        # return task.to_dict() # Remove .to_dict()
        return task  # Return directly
    except TaigaException as e:
        logger.error(f"Taiga API error getting task {task_id}: {e}", exc_info=False)
        raise e
    except Exception as e:
        logger.error(f"Unexpected error getting task {task_id}: {e}", exc_info=True)
        raise RuntimeError(f"Server error getting task: {e}")


# @mcp.tool("get_task_by_ref", description="Gets detailed information about a specific task by its reference number within a project.")
# def get_task_by_ref(session_id: str, project_id: int, ref: int) -> Dict[str, Any]:
#     """Retrieves task details by project ID and reference number. (REMOVED - Not directly supported by pytaigaclient)"""
#     logger.warning(f"get_task_by_ref called, but not supported by pytaigaclient. Project: {project_id}, Ref: {ref}")
#     raise NotImplementedError("Getting tasks by reference number is not currently supported by the pytaigaclient wrapper.")


@mcp.tool("update_task", description="Updates details of an existing task.")
def update_task(session_id: str, task_id: int, **kwargs) -> Dict[str, Any]:
    """Updates a task. Pass fields to update as keyword arguments (e.g., subject, description, status_id, assigned_to)."""
    logger.info(
        f"Executing update_task ID {task_id} for session {session_id[:8]} with data: {kwargs}"
    )
    taiga_client_wrapper = _get_authenticated_client(session_id)  # Use wrapper variable name
    try:
        # Use pytaigaclient edit pattern for partial updates
        if not kwargs:
            logger.info(f"No fields provided for update on task {task_id}")
            return taiga_client_wrapper.api.tasks.get(task_id)

        # Get current task data to retrieve version
        current_task = taiga_client_wrapper.api.tasks.get(task_id)
        version = current_task.get("version")
        if not version:
            raise ValueError(f"Could not determine version for task {task_id}")

        # Use edit method for partial updates with keyword arguments
        updated_task = taiga_client_wrapper.api.tasks.edit(
            task_id=task_id, version=version, **kwargs
        )
        logger.info(f"Task {task_id} update request sent.")
        return updated_task
    except TaigaException as e:
        logger.error(f"Taiga API error updating task {task_id}: {e}", exc_info=False)
        raise e
    except Exception as e:
        logger.error(f"Unexpected error updating task {task_id}: {e}", exc_info=True)
        raise RuntimeError(f"Server error updating task: {e}")


@mcp.tool("delete_task", description="Deletes a task by its ID.")
def delete_task(session_id: str, task_id: int) -> Dict[str, Any]:
    """Deletes a task by ID."""
    logger.warning(f"Executing delete_task ID {task_id} for session {session_id[:8]}...")
    taiga_client_wrapper = _get_authenticated_client(session_id)  # Use wrapper variable name
    try:
        # Use pytaigaclient syntax: client.resource.delete(id=...)
        taiga_client_wrapper.api.tasks.delete(id=task_id)
        logger.info(f"Task {task_id} deleted successfully.")
        return {"status": "deleted", "task_id": task_id}
    except TaigaException as e:
        logger.error(f"Taiga API error deleting task {task_id}: {e}", exc_info=False)
        raise e
    except Exception as e:
        logger.error(f"Unexpected error deleting task {task_id}: {e}", exc_info=True)
        raise RuntimeError(f"Server error deleting task: {e}")


@mcp.tool("assign_task_to_user", description="Assigns a specific task to a specific user.")
def assign_task_to_user(session_id: str, task_id: int, user_id: int) -> Dict[str, Any]:
    """Assigns a task to a user."""
    logger.info(
        f"Executing assign_task_to_user: Task {task_id} -> User {user_id}, session {session_id[:8]}..."
    )
    # Delegate to update_task
    return update_task(session_id, task_id, assigned_to=user_id)


@mcp.tool(
    "unassign_task_from_user", description="Unassigns a specific task (sets assigned user to null)."
)
def unassign_task_from_user(session_id: str, task_id: int) -> Dict[str, Any]:
    """Unassigns a task."""
    logger.info(f"Executing unassign_task_from_user: Task {task_id}, session {session_id[:8]}...")
    # Delegate to update_task
    return update_task(session_id, task_id, assigned_to=None)


# @mcp.tool("get_task_statuses", description="Lists the available statuses for tasks within a specific project.")
# def get_task_statuses(session_id: str, project_id: int) -> List[Dict[str, Any]]:
#     """Retrieves the list of task statuses for a project. (REMOVED - Not directly supported by pytaigaclient)"""
#     logger.warning(f"get_task_statuses called, but not supported by pytaigaclient. Project: {project_id}")
#     raise NotImplementedError("Listing task statuses is not currently supported by the pytaigaclient wrapper.")

# --- Issue Tools ---


@mcp.tool("list_issues", description="Lists issues within a specific project, optionally filtered.")
def list_issues(session_id: str, project_id: int, **filters) -> List[Dict[str, Any]]:
    """Lists issues for a project. Optional filters like 'milestone', 'status', 'priority', 'severity', 'type', 'assigned_to' can be passed as kwargs."""
    logger.info(
        f"Executing list_issues for project {project_id}, session {session_id[:8]}, filters: {filters}"
    )
    taiga_client_wrapper = _get_authenticated_client(session_id)  # Use wrapper variable name
    try:
        # Use pytaigaclient syntax: client.resource.list(project_id=..., **filters)
        issues = taiga_client_wrapper.api.issues.list(project_id=project_id, **filters)
        # return [i.to_dict() for i in issues] # Remove .to_dict()
        return issues  # Return directly
    except TaigaException as e:
        logger.error(
            f"Taiga API error listing issues for project {project_id}: {e}", exc_info=False
        )
        raise e
    except Exception as e:
        logger.error(
            f"Unexpected error listing issues for project {project_id}: {e}", exc_info=True
        )
        raise RuntimeError(f"Server error listing issues: {e}")


@mcp.tool("create_issue", description="Creates a new issue within a project.")
def create_issue(
    session_id: str,
    project_id: int,
    subject: str,
    priority_id: int,
    status_id: int,
    severity_id: int,
    type_id: int,
    **kwargs,
) -> Dict[str, Any]:
    """Creates an issue. Requires project_id, subject, priority_id, status_id, severity_id, type_id. Optional fields (description, assigned_to_id, etc.) via kwargs."""
    logger.info(
        f"Executing create_issue '{subject}' in project {project_id}, session {session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(session_id)  # Use wrapper variable name
    if not subject:
        raise ValueError("Issue subject cannot be empty.")
    try:
        # Use pytaigaclient syntax: client.resource.create(...)
        # Assuming pytaigaclient expects _id suffix for relational fields in create, but 'project' for project itself
        issue = taiga_client_wrapper.api.issues.create(
            project=project_id,  # Changed project_id to project
            subject=subject,
            priority_id=priority_id,  # Assume _id suffix
            status_id=status_id,  # Assume _id suffix
            type_id=type_id,  # Assume _id suffix
            severity_id=severity_id,  # Assume _id suffix
            **kwargs,
        )
        logger.info(f"Issue '{subject}' created successfully (ID: {issue.get('id', 'N/A')}).")
        # return issue.to_dict() # Remove .to_dict()
        return issue  # Return directly
    except TaigaException as e:
        logger.error(f"Taiga API error creating issue '{subject}': {e}", exc_info=False)
        raise e
    except Exception as e:
        logger.error(f"Unexpected error creating issue '{subject}': {e}", exc_info=True)
        raise RuntimeError(f"Server error creating issue: {e}")


@mcp.tool("get_issue", description="Gets detailed information about a specific issue by its ID.")
def get_issue(session_id: str, issue_id: int) -> Dict[str, Any]:
    """Retrieves issue details by ID."""
    logger.info(f"Executing get_issue ID {issue_id} for session {session_id[:8]}...")
    taiga_client_wrapper = _get_authenticated_client(session_id)  # Use wrapper variable name
    try:
        # Issues expects issue_id as a positional argument
        issue = taiga_client_wrapper.api.issues.get(issue_id)
        # return issue.to_dict() # Remove .to_dict()
        return issue  # Return directly
    except TaigaException as e:
        logger.error(f"Taiga API error getting issue {issue_id}: {e}", exc_info=False)
        raise e
    except Exception as e:
        logger.error(f"Unexpected error getting issue {issue_id}: {e}", exc_info=True)
        raise RuntimeError(f"Server error getting issue: {e}")


# @mcp.tool("get_issue_by_ref", description="Gets detailed information about a specific issue by its reference number within a project.")
# def get_issue_by_ref(session_id: str, project_id: int, ref: int) -> Dict[str, Any]:
#     """Retrieves issue details by project ID and reference number. (REMOVED - Not directly supported by pytaigaclient)"""
#     logger.warning(f"get_issue_by_ref called, but not supported by pytaigaclient. Project: {project_id}, Ref: {ref}")
#     raise NotImplementedError("Getting issues by reference number is not currently supported by the pytaigaclient wrapper.")


@mcp.tool("update_issue", description="Updates details of an existing issue.")
def update_issue(session_id: str, issue_id: int, **kwargs) -> Dict[str, Any]:
    """Updates an issue. Pass fields to update as keyword arguments (e.g., subject, description, status_id, assigned_to)."""
    logger.info(
        f"Executing update_issue ID {issue_id} for session {session_id[:8]} with data: {kwargs}"
    )
    taiga_client_wrapper = _get_authenticated_client(session_id)  # Use wrapper variable name
    try:
        # Use pytaigaclient edit pattern for partial updates
        if not kwargs:
            logger.info(f"No fields provided for update on issue {issue_id}")
            return taiga_client_wrapper.api.issues.get(issue_id)

        # Get current issue data to retrieve version
        current_issue = taiga_client_wrapper.api.issues.get(issue_id)
        version = current_issue.get("version")
        if not version:
            raise ValueError(f"Could not determine version for issue {issue_id}")

        # Use edit method for partial updates with keyword arguments
        updated_issue = taiga_client_wrapper.api.issues.edit(
            issue_id=issue_id, version=version, **kwargs
        )
        logger.info(f"Issue {issue_id} update request sent.")
        return updated_issue
    except TaigaException as e:
        logger.error(f"Taiga API error updating issue {issue_id}: {e}", exc_info=False)
        raise e
    except Exception as e:
        logger.error(f"Unexpected error updating issue {issue_id}: {e}", exc_info=True)
        raise RuntimeError(f"Server error updating issue: {e}")


@mcp.tool("delete_issue", description="Deletes an issue by its ID.")
def delete_issue(session_id: str, issue_id: int) -> Dict[str, Any]:
    """Deletes an issue by ID."""
    logger.warning(f"Executing delete_issue ID {issue_id} for session {session_id[:8]}...")
    taiga_client_wrapper = _get_authenticated_client(session_id)  # Use wrapper variable name
    try:
        # Use pytaigaclient syntax: client.resource.delete(id=...)
        taiga_client_wrapper.api.issues.delete(id=issue_id)
        logger.info(f"Issue {issue_id} deleted successfully.")
        return {"status": "deleted", "issue_id": issue_id}
    except TaigaException as e:
        logger.error(f"Taiga API error deleting issue {issue_id}: {e}", exc_info=False)
        raise e
    except Exception as e:
        logger.error(f"Unexpected error deleting issue {issue_id}: {e}", exc_info=True)
        raise RuntimeError(f"Server error deleting issue: {e}")


@mcp.tool("assign_issue_to_user", description="Assigns a specific issue to a specific user.")
def assign_issue_to_user(session_id: str, issue_id: int, user_id: int) -> Dict[str, Any]:
    """Assigns an issue to a user."""
    logger.info(
        f"Executing assign_issue_to_user: Issue {issue_id} -> User {user_id}, session {session_id[:8]}..."
    )
    # Delegate to update_issue
    return update_issue(session_id, issue_id, assigned_to=user_id)


@mcp.tool(
    "unassign_issue_from_user",
    description="Unassigns a specific issue (sets assigned user to null).",
)
def unassign_issue_from_user(session_id: str, issue_id: int) -> Dict[str, Any]:
    """Unassigns an issue."""
    logger.info(
        f"Executing unassign_issue_from_user: Issue {issue_id}, session {session_id[:8]}..."
    )
    # Delegate to update_issue
    return update_issue(session_id, issue_id, assigned_to=None)


@mcp.tool(
    "get_issue_statuses",
    description="Lists the available statuses for issues within a specific project.",
)
def get_issue_statuses(session_id: str, project_id: int) -> List[Dict[str, Any]]:
    """Retrieves the list of issue statuses for a project."""
    logger.info(
        f"Executing get_issue_statuses for project {project_id}, session {session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(session_id)  # Use wrapper variable name
    try:
        # Use pytaigaclient syntax: client.resource.list(project_id=...)
        statuses = taiga_client_wrapper.api.issue_statuses.list(project_id=project_id)
        # return [s.to_dict() for s in statuses] # Remove .to_dict()
        return statuses  # Return directly
    except TaigaException as e:
        logger.error(
            f"Taiga API error getting issue statuses for project {project_id}: {e}", exc_info=False
        )
        raise e
    except Exception as e:
        logger.error(
            f"Unexpected error getting issue statuses for project {project_id}: {e}", exc_info=True
        )
        raise RuntimeError(f"Server error getting issue statuses: {e}")


@mcp.tool(
    "get_issue_priorities",
    description="Lists the available priorities for issues within a specific project.",
)
def get_issue_priorities(session_id: str, project_id: int) -> List[Dict[str, Any]]:
    """Retrieves the list of issue priorities for a project."""
    logger.info(
        f"Executing get_issue_priorities for project {project_id}, session {session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(session_id)  # Use wrapper variable name
    try:
        # Use pytaigaclient syntax: client.resource.list(project_id=...)
        # Update resource name: priorities -> issue_priorities
        priorities = taiga_client_wrapper.api.issue_priorities.list(project_id=project_id)
        # return [p.to_dict() for p in priorities] # Remove .to_dict()
        return priorities  # Return directly
    except TaigaException as e:
        logger.error(
            f"Taiga API error getting issue priorities for project {project_id}: {e}",
            exc_info=False,
        )
        raise e
    except Exception as e:
        logger.error(
            f"Unexpected error getting issue priorities for project {project_id}: {e}",
            exc_info=True,
        )
        raise RuntimeError(f"Server error getting issue priorities: {e}")


@mcp.tool(
    "get_issue_severities",
    description="Lists the available severities for issues within a specific project.",
)
def get_issue_severities(session_id: str, project_id: int) -> List[Dict[str, Any]]:
    """Retrieves the list of issue severities for a project."""
    logger.info(
        f"Executing get_issue_severities for project {project_id}, session {session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(session_id)  # Use wrapper variable name
    try:
        # Use pytaigaclient syntax: client.resource.list(project_id=...)
        # Update resource name: severities -> issue_severities
        severities = taiga_client_wrapper.api.issue_severities.list(project_id=project_id)
        # return [s.to_dict() for s in severities] # Remove .to_dict()
        return severities  # Return directly
    except TaigaException as e:
        logger.error(
            f"Taiga API error getting issue severities for project {project_id}: {e}",
            exc_info=False,
        )
        raise e
    except Exception as e:
        logger.error(
            f"Unexpected error getting issue severities for project {project_id}: {e}",
            exc_info=True,
        )
        raise RuntimeError(f"Server error getting issue severities: {e}")


@mcp.tool(
    "get_issue_types", description="Lists the available types for issues within a specific project."
)
def get_issue_types(session_id: str, project_id: int) -> List[Dict[str, Any]]:
    """Retrieves the list of issue types for a project."""
    logger.info(f"Executing get_issue_types for project {project_id}, session {session_id[:8]}...")
    taiga_client_wrapper = _get_authenticated_client(session_id)  # Use wrapper variable name
    try:
        # Use pytaigaclient syntax: client.resource.list(project_id=...)
        types = taiga_client_wrapper.api.issue_types.list(project_id=project_id)
        # return [t.to_dict() for t in types] # Remove .to_dict()
        return types  # Return directly
    except TaigaException as e:
        logger.error(
            f"Taiga API error getting issue types for project {project_id}: {e}", exc_info=False
        )
        raise e
    except Exception as e:
        logger.error(
            f"Unexpected error getting issue types for project {project_id}: {e}", exc_info=True
        )
        raise RuntimeError(f"Server error getting issue types: {e}")


# --- Epic Tools ---


@mcp.tool("list_epics", description="Lists epics within a specific project, optionally filtered.")
def list_epics(session_id: str, project_id: int, **filters) -> List[Dict[str, Any]]:
    """Lists epics for a project. Optional filters like 'status', 'assigned_to' can be passed as keyword arguments."""
    logger.info(
        f"Executing list_epics for project {project_id}, session {session_id[:8]}, filters: {filters}"
    )
    taiga_client_wrapper = _get_authenticated_client(session_id)  # Use wrapper variable name
    try:
        # Use pytaigaclient syntax: client.resource.list(project_id=..., **filters)
        epics = taiga_client_wrapper.api.epics.list(project_id=project_id, **filters)
        # return [e.to_dict() for e in epics] # Remove .to_dict()
        return epics  # Return directly
    except TaigaException as e:
        logger.error(f"Taiga API error listing epics for project {project_id}: {e}", exc_info=False)
        raise e
    except Exception as e:
        logger.error(f"Unexpected error listing epics for project {project_id}: {e}", exc_info=True)
        raise RuntimeError(f"Server error listing epics: {e}")


@mcp.tool("create_epic", description="Creates a new epic within a project.")
def create_epic(session_id: str, project_id: int, subject: str, **kwargs) -> Dict[str, Any]:
    """Creates an epic. Requires project_id and subject. Optional fields (description, status_id, assigned_to_id, color, etc.) via kwargs."""
    logger.info(
        f"Executing create_epic '{subject}' in project {project_id}, session {session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(session_id)  # Use wrapper variable name
    if not subject:
        raise ValueError("Epic subject cannot be empty.")
    try:
        # Use pytaigaclient syntax: client.resource.create(project=..., subject=..., **kwargs)
        epic = taiga_client_wrapper.api.epics.create(project=project_id, subject=subject, **kwargs)
        logger.info(f"Epic '{subject}' created successfully (ID: {epic.get('id', 'N/A')}).")
        # return epic.to_dict() # Remove .to_dict()
        return epic  # Return directly
    except TaigaException as e:
        logger.error(f"Taiga API error creating epic '{subject}': {e}", exc_info=False)
        raise e
    except Exception as e:
        logger.error(f"Unexpected error creating epic '{subject}': {e}", exc_info=True)
        raise RuntimeError(f"Server error creating epic: {e}")


@mcp.tool("get_epic", description="Gets detailed information about a specific epic by its ID.")
def get_epic(session_id: str, epic_id: int) -> Dict[str, Any]:
    """Retrieves epic details by ID."""
    logger.info(f"Executing get_epic ID {epic_id} for session {session_id[:8]}...")
    taiga_client_wrapper = _get_authenticated_client(session_id)  # Use wrapper variable name
    try:
        # Epics expects epic_id as a positional argument
        epic = taiga_client_wrapper.api.epics.get(epic_id)
        # return epic.to_dict() # Remove .to_dict()
        return epic  # Return directly
    except TaigaException as e:
        logger.error(f"Taiga API error getting epic {epic_id}: {e}", exc_info=False)
        raise e
    except Exception as e:
        logger.error(f"Unexpected error getting epic {epic_id}: {e}", exc_info=True)
        raise RuntimeError(f"Server error getting epic: {e}")


# @mcp.tool("get_epic_by_ref", description="Gets detailed information about a specific epic by its reference number within a project.")
# def get_epic_by_ref(session_id: str, project_id: int, ref: int) -> Dict[str, Any]:
#     """Retrieves epic details by project ID and reference number. (REMOVED - Not directly supported by pytaigaclient)"""
#     logger.warning(f"get_epic_by_ref called, but not supported by pytaigaclient. Project: {project_id}, Ref: {ref}")
#     raise NotImplementedError("Getting epics by reference number is not currently supported by the pytaigaclient wrapper.")


@mcp.tool("update_epic", description="Updates details of an existing epic.")
def update_epic(session_id: str, epic_id: int, **kwargs) -> Dict[str, Any]:
    """Updates an epic. Pass fields to update as keyword arguments (e.g., subject, description, status_id, assigned_to, color)."""
    logger.info(
        f"Executing update_epic ID {epic_id} for session {session_id[:8]} with data: {kwargs}"
    )
    taiga_client_wrapper = _get_authenticated_client(session_id)  # Use wrapper variable name
    try:
        # Use pytaigaclient edit pattern for partial updates
        if not kwargs:
            logger.info(f"No fields provided for update on epic {epic_id}")
            return taiga_client_wrapper.api.epics.get(epic_id)

        # Get current epic data to retrieve version
        current_epic = taiga_client_wrapper.api.epics.get(epic_id)
        version = current_epic.get("version")
        if not version:
            raise ValueError(f"Could not determine version for epic {epic_id}")

        # Use edit method for partial updates with keyword arguments
        updated_epic = taiga_client_wrapper.api.epics.edit(
            epic_id=epic_id, version=version, **kwargs
        )
        logger.info(f"Epic {epic_id} update request sent.")
        return updated_epic
    except TaigaException as e:
        logger.error(f"Taiga API error updating epic {epic_id}: {e}", exc_info=False)
        raise e
    except Exception as e:
        logger.error(f"Unexpected error updating epic {epic_id}: {e}", exc_info=True)
        raise RuntimeError(f"Server error updating epic: {e}")


@mcp.tool("delete_epic", description="Deletes an epic by its ID.")
def delete_epic(session_id: str, epic_id: int) -> Dict[str, Any]:
    """Deletes an epic by ID."""
    logger.warning(f"Executing delete_epic ID {epic_id} for session {session_id[:8]}...")
    taiga_client_wrapper = _get_authenticated_client(session_id)  # Use wrapper variable name
    try:
        # Use pytaigaclient syntax: client.resource.delete(id=...)
        taiga_client_wrapper.api.epics.delete(id=epic_id)
        logger.info(f"Epic {epic_id} deleted successfully.")
        return {"status": "deleted", "epic_id": epic_id}
    except TaigaException as e:
        logger.error(f"Taiga API error deleting epic {epic_id}: {e}", exc_info=False)
        raise e
    except Exception as e:
        logger.error(f"Unexpected error deleting epic {epic_id}: {e}", exc_info=True)
        raise RuntimeError(f"Server error deleting epic: {e}")


@mcp.tool("assign_epic_to_user", description="Assigns a specific epic to a specific user.")
def assign_epic_to_user(session_id: str, epic_id: int, user_id: int) -> Dict[str, Any]:
    """Assigns an epic to a user."""
    logger.info(
        f"Executing assign_epic_to_user: Epic {epic_id} -> User {user_id}, session {session_id[:8]}..."
    )
    # Delegate to update_epic
    return update_epic(session_id, epic_id, assigned_to=user_id)


@mcp.tool(
    "unassign_epic_from_user", description="Unassigns a specific epic (sets assigned user to null)."
)
def unassign_epic_from_user(session_id: str, epic_id: int) -> Dict[str, Any]:
    """Unassigns an epic."""
    logger.info(f"Executing unassign_epic_from_user: Epic {epic_id}, session {session_id[:8]}...")
    # Delegate to update_epic
    return update_epic(session_id, epic_id, assigned_to=None)


# @mcp.tool("get_epic_statuses", description="Lists the available statuses for epics within a specific project.")
# def get_epic_statuses(session_id: str, project_id: int) -> List[Dict[str, Any]]:
#     """Retrieves the list of epic statuses for a project. (REMOVED - Resource not found in pytaigaclient)"""
#     logger.warning(f"get_epic_statuses called, but epic_statuses resource not found in pytaigaclient. Project: {project_id}")
#     raise NotImplementedError("Listing epic statuses is not currently supported by the pytaigaclient wrapper.")

# --- Milestone (Sprint) Tools ---


@mcp.tool("list_milestones", description="Lists milestones (sprints) within a specific project.")
def list_milestones(session_id: str, project_id: int) -> List[Dict[str, Any]]:
    """Lists milestones for a project."""
    logger.info(f"Executing list_milestones for project {project_id}, session {session_id[:8]}...")
    taiga_client_wrapper = _get_authenticated_client(session_id)  # Use wrapper variable name
    try:
        # Use pytaigaclient syntax: client.resource.list(project_id=...)
        milestones = taiga_client_wrapper.api.milestones.list(project_id=project_id)
        # return [m.to_dict() for m in milestones] # Remove .to_dict()
        return milestones  # Return directly
    except TaigaException as e:
        logger.error(
            f"Taiga API error listing milestones for project {project_id}: {e}", exc_info=False
        )
        raise e
    except Exception as e:
        logger.error(
            f"Unexpected error listing milestones for project {project_id}: {e}", exc_info=True
        )
        raise RuntimeError(f"Server error listing milestones: {e}")


@mcp.tool("create_milestone", description="Creates a new milestone (sprint) within a project.")
def create_milestone(
    session_id: str, project_id: int, name: str, estimated_start: str, estimated_finish: str
) -> Dict[str, Any]:
    """Creates a milestone. Requires project_id, name, estimated_start (YYYY-MM-DD), and estimated_finish (YYYY-MM-DD)."""
    logger.info(
        f"Executing create_milestone '{name}' in project {project_id}, session {session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(session_id)  # Use wrapper variable name
    if not all([name, estimated_start, estimated_finish]):
        raise ValueError("Milestone requires name, estimated_start, and estimated_finish.")
    try:
        # Use pytaigaclient syntax: client.resource.create(...)
        milestone = taiga_client_wrapper.api.milestones.create(
            project=project_id,  # Changed project_id to project
            name=name,
            estimated_start=estimated_start,
            estimated_finish=estimated_finish,
        )
        logger.info(f"Milestone '{name}' created successfully (ID: {milestone.get('id', 'N/A')}).")
        # return milestone.to_dict() # Remove .to_dict()
        return milestone  # Return directly
    except TaigaException as e:
        logger.error(f"Taiga API error creating milestone '{name}': {e}", exc_info=False)
        raise e
    except Exception as e:
        logger.error(f"Unexpected error creating milestone '{name}': {e}", exc_info=True)
        raise RuntimeError(f"Server error creating milestone: {e}")


@mcp.tool(
    "get_milestone", description="Gets detailed information about a specific milestone by its ID."
)
def get_milestone(session_id: str, milestone_id: int) -> Dict[str, Any]:
    """Retrieves milestone details by ID."""
    logger.info(f"Executing get_milestone ID {milestone_id} for session {session_id[:8]}...")
    taiga_client_wrapper = _get_authenticated_client(session_id)  # Use wrapper variable name
    try:
        # Milestones expects milestone_id as a positional argument
        milestone = taiga_client_wrapper.api.milestones.get(milestone_id)
        # return milestone.to_dict() # Remove .to_dict()
        return milestone  # Return directly
    except TaigaException as e:
        logger.error(f"Taiga API error getting milestone {milestone_id}: {e}", exc_info=False)
        raise e
    except Exception as e:
        logger.error(f"Unexpected error getting milestone {milestone_id}: {e}", exc_info=True)
        raise RuntimeError(f"Server error getting milestone: {e}")


@mcp.tool("update_milestone", description="Updates details of an existing milestone.")
def update_milestone(session_id: str, milestone_id: int, **kwargs) -> Dict[str, Any]:
    """Updates a milestone. Pass fields to update as kwargs (e.g., name, estimated_start, estimated_finish)."""
    logger.info(
        f"Executing update_milestone ID {milestone_id} for session {session_id[:8]} with data: {kwargs}"
    )
    taiga_client_wrapper = _get_authenticated_client(session_id)  # Use wrapper variable name
    try:
        # Use pytaigaclient edit pattern for partial updates
        if not kwargs:
            logger.info(f"No fields provided for update on milestone {milestone_id}")
            return taiga_client_wrapper.api.milestones.get(milestone_id)

        # Get current milestone data to retrieve version
        current_milestone = taiga_client_wrapper.api.milestones.get(milestone_id)
        version = current_milestone.get("version")
        if not version:
            raise ValueError(f"Could not determine version for milestone {milestone_id}")

        # Use edit method for partial updates with keyword arguments
        updated_milestone = taiga_client_wrapper.api.milestones.edit(
            milestone_id=milestone_id, version=version, **kwargs
        )
        logger.info(f"Milestone {milestone_id} update request sent.")
        return updated_milestone
    except TaigaException as e:
        logger.error(f"Taiga API error updating milestone {milestone_id}: {e}", exc_info=False)
        raise e
    except Exception as e:
        logger.error(f"Unexpected error updating milestone {milestone_id}: {e}", exc_info=True)
        raise RuntimeError(f"Server error updating milestone: {e}")


@mcp.tool("delete_milestone", description="Deletes a milestone by its ID.")
def delete_milestone(session_id: str, milestone_id: int) -> Dict[str, Any]:
    """Deletes a milestone by ID."""
    logger.warning(f"Executing delete_milestone ID {milestone_id} for session {session_id[:8]}...")
    taiga_client_wrapper = _get_authenticated_client(session_id)  # Use wrapper variable name
    try:
        # Use pytaigaclient syntax: client.resource.delete(id=...)
        taiga_client_wrapper.api.milestones.delete(id=milestone_id)
        logger.info(f"Milestone {milestone_id} deleted successfully.")
        return {"status": "deleted", "milestone_id": milestone_id}
    except TaigaException as e:
        logger.error(f"Taiga API error deleting milestone {milestone_id}: {e}", exc_info=False)
        raise e
    except Exception as e:
        logger.error(f"Unexpected error deleting milestone {milestone_id}: {e}", exc_info=True)
        raise RuntimeError(f"Server error deleting milestone: {e}")


# @mcp.tool("get_milestone_stats", description="Gets statistics (total points, completed points, etc.) for a specific milestone.")
# def get_milestone_stats(session_id: str, milestone_id: int) -> Dict[str, Any]:
#     """Retrieves statistics for a milestone. (REMOVED - Not directly supported by pytaigaclient)"""
#     logger.warning(f"get_milestone_stats called, but not supported by pytaigaclient. Milestone: {milestone_id}")
#     raise NotImplementedError("Getting milestone statistics is not currently supported by the pytaigaclient wrapper.")

# --- User Management Tools ---


@mcp.tool("get_project_members", description="Lists members of a specific project.")
def get_project_members(session_id: str, project_id: int) -> List[Dict[str, Any]]:
    """Retrieves the list of members for a project."""
    logger.info(
        f"Executing get_project_members for project {project_id}, session {session_id[:8]}..."
    )
    taiga_client_wrapper = _get_authenticated_client(session_id)  # Use wrapper variable name
    try:
        # Use pytaigaclient memberships resource list method
        members = taiga_client_wrapper.api.memberships.list(project_id=project_id)
        # return [m.to_dict() for m in members] # Remove .to_dict()
        return members  # Return directly
    except TaigaException as e:
        logger.error(
            f"Taiga API error getting members for project {project_id}: {e}", exc_info=False
        )
        raise e
    except Exception as e:
        logger.error(
            f"Unexpected error getting members for project {project_id}: {e}", exc_info=True
        )
        raise RuntimeError(f"Server error getting project members: {e}")


@mcp.tool(
    "invite_project_user", description="Invites a user to a project by email with a specific role."
)
def invite_project_user(
    session_id: str, project_id: int, email: str, role_id: int
) -> Dict[str, Any]:
    """Invites a user via email to join the project with the specified role ID."""
    # Validate email
    email = validate_email(email)
    email_hash = hash_for_logging(email)

    logger.info(
        f"Executing invite_project_user (email hash: {email_hash}) to project {project_id}, session {session_id[:8]}"
    )
    taiga_client_wrapper = _get_authenticated_client(session_id)

    try:
        # Use pytaigaclient memberships resource invite method
        invitation_result = taiga_client_wrapper.api.memberships.invite(
            project=project_id, email=email, role_id=role_id
        )
        logger.info(f"Invitation request sent for project {project_id}.")
        # Return the result from the invite call (might be dict or status)
        return (
            invitation_result
            if isinstance(invitation_result, dict)
            else {"status": "invited", "email": email, "details": invitation_result}
        )
    except TaigaException as e:
        logger.error(
            f"Taiga API error inviting user to project {project_id}: {type(e).__name__}",
            exc_info=False,
        )
        raise e
    except Exception as e:
        logger.error(f"Unexpected error inviting user to project {project_id}", exc_info=True)
        raise RuntimeError("Server error inviting user")


# --- Wiki Tools ---


@mcp.tool("list_wiki_pages", description="Lists wiki pages within a specific project.")
def list_wiki_pages(session_id: str, project_id: int) -> List[Dict[str, Any]]:
    """Lists wiki pages for a project."""
    logger.info(f"Executing list_wiki_pages for project {project_id}, session {session_id[:8]}...")
    taiga_client_wrapper = _get_authenticated_client(session_id)  # Use wrapper variable name
    try:
        # Use pytaigaclient syntax: client.wiki.list(project_id=...)
        pages = taiga_client_wrapper.api.wiki.list(project_id=project_id)
        # return [p.to_dict() for p in pages] # Remove .to_dict()
        return pages  # Return directly
    except TaigaException as e:
        logger.error(
            f"Taiga API error listing wiki pages for project {project_id}: {e}", exc_info=False
        )
        raise e
    except Exception as e:
        logger.error(
            f"Unexpected error listing wiki pages for project {project_id}: {e}", exc_info=True
        )
        raise RuntimeError(f"Server error listing wiki pages: {e}")


@mcp.tool("get_wiki_page", description="Gets a specific wiki page by its ID.")
def get_wiki_page(session_id: str, wiki_page_id: int) -> Dict[str, Any]:
    """Retrieves wiki page details by ID."""
    logger.info(f"Executing get_wiki_page ID {wiki_page_id} for session {session_id[:8]}...")
    taiga_client_wrapper = _get_authenticated_client(session_id)  # Use wrapper variable name
    try:
        # Wiki expects wiki_page_id as a positional argument
        page = taiga_client_wrapper.api.wiki.get(wiki_page_id)
        # return page.to_dict() # Remove .to_dict()
        return page  # Return directly
    except TaigaException as e:
        logger.error(f"Taiga API error getting wiki page {wiki_page_id}: {e}", exc_info=False)
        raise e
    except Exception as e:
        logger.error(f"Unexpected error getting wiki page {wiki_page_id}: {e}", exc_info=True)
        raise RuntimeError(f"Server error getting wiki page: {e}")


# @mcp.tool("get_wiki_page_by_slug", description="Gets a specific wiki page by its slug within a project.")
# def get_wiki_page_by_slug(session_id: str, project_id: int, slug: str) -> Dict[str, Any]:
#     """Retrieves wiki page details by project ID and slug. (REMOVED - Not directly supported by pytaigaclient)"""
#     logger.warning(f"get_wiki_page_by_slug called, but not supported by pytaigaclient. Project: {project_id}, Slug: {slug}")
#     raise NotImplementedError("Getting wiki pages by slug is not currently supported by the pytaigaclient wrapper.")

# --- Session Management Tools ---


@mcp.tool("logout", description="Invalidates the current session_id.")
def logout(session_id: str) -> Dict[str, Any]:
    """Logs out the current session, invalidating the session_id."""
    logger.info(f"Executing logout for session {session_id[:8]}...")

    with session_lock:
        session_data = active_sessions.pop(session_id, None)

    if session_data:
        logger.info(f"Session {session_id[:8]} logged out successfully.")
        # No specific API logout call needed usually for token-based auth
        return {"status": "logged_out", "session_id": session_id}
    else:
        logger.warning(f"Attempted to log out non-existent session: {session_id[:8]}")
        return {"status": "session_not_found", "session_id": session_id}


@mcp.tool(
    "session_status", description="Checks if the provided session_id is currently active and valid."
)
def session_status(session_id: str) -> Dict[str, Any]:
    """Checks the validity of the current session_id."""
    logger.debug(f"Executing session_status check for session {session_id[:8]}...")

    with session_lock:
        session_data = active_sessions.get(session_id)

    if not session_data:
        logger.debug(f"Session {session_id[:8]} not found.")
        return {"status": "inactive", "reason": "not_found", "session_id": session_id}

    # Check if session is expired
    if session_data.is_expired():
        logger.warning(f"Session {session_id[:8]} is expired.")
        with session_lock:
            active_sessions.pop(session_id, None)
        return {"status": "inactive", "reason": "expired", "session_id": session_id}

    # Check if client is still authenticated
    if not session_data.client.is_authenticated:
        logger.warning(f"Session {session_id[:8]} client not authenticated.")
        with session_lock:
            active_sessions.pop(session_id, None)
        return {"status": "inactive", "reason": "not_authenticated", "session_id": session_id}

    try:
        # Use pytaigaclient users.me() call
        me = session_data.client.api.users.me()
        username = me.get("username", "Unknown")

        # Calculate time remaining
        time_remaining = SESSION_EXPIRY - (time.time() - session_data.last_activity)

        logger.debug(f"Session {session_id[:8]} is active.")
        return {
            "status": "active",
            "session_id": session_id,
            "username": username,
            "time_remaining": int(time_remaining),
            "created_at": datetime.fromtimestamp(session_data.created_at).isoformat(),
        }
    except TaigaException:
        logger.warning(f"Session {session_id[:8]} found but token invalid (API check failed).")
        with session_lock:
            active_sessions.pop(session_id, None)
        return {"status": "inactive", "reason": "token_invalid", "session_id": session_id}
    except Exception as e:
        logger.error(
            f"Unexpected error during session status check for {session_id[:8]}", exc_info=True
        )
        return {"status": "error", "reason": "check_failed", "session_id": session_id}


# --- Run the server ---
if __name__ == "__main__":
    mcp.run()
