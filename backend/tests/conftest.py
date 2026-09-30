import os

# API integration tests run without external infrastructure. Worker behavior is
# covered separately with an isolated database and mocked providers.
os.environ.setdefault("ENQUEUE_DISTRIBUTIONS", "false")
