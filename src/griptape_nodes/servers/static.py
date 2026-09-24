"""Contract with the static file server the host process runs for the workspace.

The engine serves nothing itself. The host (griptape-nodes-app) serves the workspace and passes
its URL on AppInitializationComplete. Shipped app versions import STATIC_SERVER_URL from here.
"""

import os

# Where the host's server listens by default. Only used to build URLs when no host reports its own.
STATIC_SERVER_HOST = os.getenv("STATIC_SERVER_HOST", "localhost")
STATIC_SERVER_PORT = int(os.getenv("STATIC_SERVER_PORT", "8124"))
# URL path the workspace is served under.
STATIC_SERVER_URL = os.getenv("STATIC_SERVER_URL", "/workspace")
# Set by the orchestrator on a worker subprocess's environment: the URL of the static server
# already serving this workspace. A worker adopts it so asset URLs it produces outlive it.
ORCHESTRATOR_STATIC_SERVER_BASE_URL_ENV = "GTN_ORCHESTRATOR_STATIC_SERVER_BASE_URL"
