import os

# Must be set before the service modules are imported.
os.environ["TELEMETRY_EXPORT"] = "false"  # don't try to reach Tempo/Loki
os.environ["CHAOS_ENABLED"] = "true"
os.environ.setdefault("POSTGRES_DB", "test")
os.environ.setdefault("POSTGRES_USER", "test")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
