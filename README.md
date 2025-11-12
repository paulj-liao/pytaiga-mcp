<p align="center">
<picture>
  <img src="https://taiga.io/media/images/favicon.width-44.png">
</picture>
</p>

# Taiga MCP Bridge


[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)

## Overview

The Taiga MCP Bridge is a powerful integration layer that connects [Taiga](https://taiga.io/) project management platform with the Model Context Protocol (MCP), enabling AI tools and workflows to interact seamlessly with Taiga's resources.

This bridge provides a comprehensive set of tools and resources for AI agents to:
- Create and manage projects, epics, user stories, tasks, and issues in Taiga
- Track sprints and milestones
- Assign and update work items
- Query detailed information about project artifacts
- Manage project members and permissions

By using the MCP standard, this bridge allows AI systems to maintain contextual awareness about project state and perform complex project management tasks programmatically.

## Features

### Comprehensive Resource Support

The bridge supports the following Taiga resources with complete CRUD operations:

- **Projects**: Create, update, and manage project settings and metadata
- **Epics**: Manage large features that span multiple sprints
- **User Stories**: Handle detailed requirements and acceptance criteria
- **Tasks**: Track smaller units of work within user stories
- **Issues**: Manage bugs, questions, and enhancement requests
- **Sprints (Milestones)**: Plan and track work in time-boxed intervals

### Enterprise-Grade Security

- **Session Management**: Automatic expiration, activity tracking, and cleanup
- **Rate Limiting**: Protection against brute force and DoS attacks
- **Input Validation**: Comprehensive validation to prevent injection attacks
- **Secure Logging**: Privacy-focused logging with credential hashing
- **Thread Safety**: Lock-based synchronization for concurrent operations

## Installation

This project uses [uv](https://github.com/astral-sh/uv) for fast, reliable Python package management.

### Prerequisites

- Python 3.10 or higher
- uv package manager

### Basic Installation

```bash
# Clone the repository
git clone https://github.com/your-org/pyTaigaMCP.git
cd pyTaigaMCP

# Install dependencies
./install.sh
```

### Development Installation

For development (includes testing and code quality tools):

```bash
./install.sh --dev
```

### Manual Installation

If you prefer to install manually:

```bash
# Production dependencies only
uv pip install -e .

# With development dependencies
uv pip install -e ".[dev]"
```

## Configuration

The bridge can be configured through environment variables or a `.env` file:

### Core Configuration

| Environment Variable | Description | Default |
| --- | --- | --- |
| `TAIGA_API_URL` | Base URL for the Taiga API | http://localhost:9000 |
| `TAIGA_TRANSPORT` | Transport mode (stdio or sse) | stdio |
| `LOG_LEVEL` | Logging level | INFO |

### Security Configuration

| Environment Variable | Description | Default |
| --- | --- | --- |
| `SESSION_EXPIRY` | Session expiration time in seconds | 28800 (8 hours) |
| `SESSION_CLEANUP_INTERVAL` | Session cleanup interval in seconds | 3600 (1 hour) |
| `RATE_LIMIT_ENABLED` | Enable/disable rate limiting | true |
| `RATE_LIMIT_LOGIN_REQUESTS` | Max login attempts | 5 |
| `RATE_LIMIT_LOGIN_WINDOW` | Login rate limit window in seconds | 300 (5 minutes) |
| `RATE_LIMIT_API_REQUESTS` | Max API requests | 100 |
| `RATE_LIMIT_API_WINDOW` | API rate limit window in seconds | 60 (1 minute) |
| `MAX_INPUT_LENGTH` | Maximum length for text input fields | 10000 |
| `MAX_NAME_LENGTH` | Maximum length for name fields | 255 |

Create a `.env` file in the project root to set these values:

```env
# Core Configuration
TAIGA_API_URL=https://api.taiga.io/api/v1/
TAIGA_TRANSPORT=stdio
LOG_LEVEL=INFO

# Security Configuration
SESSION_EXPIRY=28800
RATE_LIMIT_ENABLED=true
RATE_LIMIT_LOGIN_REQUESTS=5
RATE_LIMIT_API_REQUESTS=100
```

## Usage

### With stdio mode

Paste the following json in your Claude App's or Cursor's mcp settings section:

```json
{
    "mcpServers": {
        "taigaApi": {
            "command": "uv",
            "args": [
                "--directory",
                "<path to local pyTaigaMCP folder>",
                "run",
                "src/server.py"
            ],
            "env": {
                "TAIGA_TRANSPORT": "<stdio|sse>",                
                "TAIGA_API_URL": "<Taiga API Url (ex: http://localhost:9000)",
                "TAIGA_USERNAME": "<taiga username>",
                "TAIGA_PASSWORD": "<taiga password>"
            }
        }
}
```

### Running the Bridge

Start the MCP server with:

```bash
# Default stdio transport
./run.sh

# For SSE transport
./run.sh --sse
```

Or manually:

```bash
# For stdio transport (default)
uv run python src/server.py

# For SSE transport
uv run python src/server.py --sse
```

### Transport Modes

The server supports two transport modes:

1. **stdio (Standard Input/Output)** - Default mode for terminal-based clients
2. **SSE (Server-Sent Events)** - Web-based transport with server push capabilities

You can set the transport mode in several ways:
- Using the `--sse` flag with run.sh or server.py (default is stdio)
- Setting the `TAIGA_TRANSPORT` environment variable 
- Adding `TAIGA_TRANSPORT=sse` to your `.env` file

### Authentication Flow

This MCP bridge uses a session-based authentication model:

1. **Login**: Clients must first authenticate using the `login` tool:
   ```python
   session = client.call_tool("login", {
       "username": "your_taiga_username",
       "password": "your_taiga_password",
       "host": "https://api.taiga.io" # Optional
   })
   # Save the session_id from the response
   session_id = session["session_id"]
   ```

2. **Using Tools and Resources**: Include the `session_id` in every API call:
   ```python
   # For resources, include session_id in the URI
   projects = client.get_resource(f"taiga://projects?session_id={session_id}")
   
   # For project-specific resources
   epics = client.get_resource(f"taiga://projects/123/epics?session_id={session_id}")
   
   # For tools, include session_id as a parameter
   new_project = client.call_tool("create_project", {
       "session_id": session_id,
       "name": "New Project",
       "description": "Description"
   })
   ```

3. **Check Session Status**: You can check if your session is still valid:
   ```python
   status = client.call_tool("session_status", {"session_id": session_id})
   # Returns information about session validity and remaining time
   ```

4. **Logout**: When finished, you can logout to terminate the session:
   ```python
   client.call_tool("logout", {"session_id": session_id})
   ```

### Example: Complete Project Creation Workflow

Here's a complete example of creating a project with epics and user stories:

```python
from mcp.client import Client

# Initialize MCP client
client = Client()

# Authenticate and get session ID
auth_result = client.call_tool("login", {
    "username": "admin",
    "password": "password123",
    "host": "https://taiga.mycompany.com"
})
session_id = auth_result["session_id"]

# Create a new project
project = client.call_tool("create_project", {
    "session_id": session_id,
    "name": "My New Project",
    "description": "A test project created via MCP"
})
project_id = project["id"]

# Create an epic
epic = client.call_tool("create_epic", {
    "session_id": session_id,
    "project_id": project_id,
    "subject": "User Authentication",
    "description": "Implement user authentication features"
})
epic_id = epic["id"]

# Create a user story in the epic
story = client.call_tool("create_user_story", {
    "session_id": session_id,
    "project_id": project_id,
    "subject": "User Login",
    "description": "As a user, I want to log in with my credentials",
    "epic_id": epic_id
})

# Logout when done
client.call_tool("logout", {"session_id": session_id})
```

## Development

### Project Structure

```
pyTaigaMCP/
├── src/
│   ├── server.py          # MCP server implementation with tools
│   ├── taiga_client.py    # Taiga API client with all CRUD operations
│   ├── tools.py           # MCP tools definitions
│   └── config.py          # Configuration settings with Pydantic
├── tests/
│   ├── conftest.py        # Shared pytest fixtures
│   ├── unit/              # Unit tests
│   └── integration/       # Integration tests
├── pyproject.toml         # Project configuration and dependencies
├── install.sh             # Installation script
├── run.sh                 # Server execution script
└── README.md              # Project documentation
```

### Testing

Run tests with pytest:

```bash
# Run all tests
pytest

# Run only unit tests
pytest tests/unit/

# Run only integration tests
pytest tests/integration/

# Run tests with specific markers
pytest -m "auth"  # Authentication tests
pytest -m "core"  # Core functionality tests

# Run tests with coverage reporting
pytest --cov=src
```

### Debugging and Inspection

Use the included inspector tool for debugging:

```bash
# Default stdio transport
./inspect.sh

# For SSE transport
./inspect.sh --sse

# For development mode
./inspect.sh --dev
```

## Security

The Taiga MCP Bridge implements comprehensive security measures to protect your data and prevent abuse:

### Session Management

- **Automatic Expiration**: Sessions expire after a configurable period (default: 8 hours)
- **Activity Tracking**: Session timeout resets on each API call
- **Background Cleanup**: Expired sessions are automatically removed every hour
- **Thread-Safe**: All session operations use locks to prevent race conditions

### Rate Limiting

Protection against brute force and DoS attacks:

- **Login Rate Limiting**: Max 5 login attempts per 5 minutes (configurable)
- **API Rate Limiting**: Max 100 API requests per minute (configurable)
- **Per-User Limits**: Rate limits are tracked per username/session
- **Sliding Window**: Uses sliding window algorithm for accurate tracking

### Input Validation

All user inputs are validated to prevent injection attacks:

- **URL Validation**: SSRF protection with protocol and domain validation
- **Length Constraints**: Enforced maximum lengths for all text fields
- **Email Validation**: Format validation for email addresses
- **Type Checking**: Strong type validation for all parameters

### Secure Logging

Privacy-focused logging practices:

- **Credential Protection**: Usernames and emails are hashed (SHA-256) in logs
- **No Sensitive Data**: Passwords and tokens never appear in logs
- **Exception Safety**: Only exception types logged, not sensitive details

### Authentication Security

- **Token-Based**: Uses secure token authentication
- **Session Isolation**: Each session is completely isolated
- **Automatic Cleanup**: Invalid sessions are immediately removed

### Security Configuration

All security features can be tuned via environment variables. See the [Configuration](#configuration) section for details.

For a comprehensive security analysis and implementation details, see:
- [SECURITY_ANALYSIS.md](SECURITY_ANALYSIS.md) - Vulnerability analysis and recommendations
- [SECURITY_IMPROVEMENTS.md](SECURITY_IMPROVEMENTS.md) - Implementation details

## Error Handling

All API operations return standardized error responses in the following format:

```json
{
  "status": "error",
  "error_type": "ExceptionClassName",
  "message": "Detailed error message"
}
```

## Performance Considerations

The bridge is designed for efficiency and reliability:

1. **Lightweight Sessions**: Session metadata adds only ~100 bytes per session
2. **Background Cleanup**: Automatic cleanup prevents memory growth
3. **Rate Limiting**: Protects both client and server from overload
4. **Minimal Overhead**: Security validation adds <1ms per request
5. **Thread-Safe Operations**: Lock-based synchronization for data consistency

### Performance Impact

- **Session Expiration**: Negligible CPU overhead
- **Rate Limiting**: <0.1ms per request check
- **Input Validation**: <1ms per request
- **Background Cleanup**: Runs every hour with minimal CPU usage

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Install development dependencies (`./install.sh --dev`)
4. Make your changes
5. Run tests (`pytest`)
6. Commit your changes (`git commit -m 'Add some amazing feature'`)
7. Push to the branch (`git push origin feature/amazing-feature`)
8. Open a Pull Request

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Acknowledgments

- [Taiga](https://www.taiga.io/) for their excellent project management platform
- [Model Context Protocol (MCP)](https://github.com/mcp-foundation/specification) for the standardized AI communication framework
- All contributors who have helped shape this project
