"""All configuration comes from environment variables.

Defaults suit running TraceOps on your machine (outside Docker) while the
rest of the stack runs in Docker. compose.yaml overrides them for containers.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")


def _service_urls(raw: str) -> dict[str, str]:
    pairs = (item.split("=", 1) for item in raw.split(",") if "=" in item)
    return {name.strip(): url.strip() for name, url in pairs}


@dataclass(frozen=True)
class Settings:
    db_host: str = os.getenv("TRACEOPS_DB_HOST", "127.0.0.1")
    db_port: int = int(os.getenv("TRACEOPS_DB_PORT", "5433"))
    db_name: str = os.getenv("TRACEOPS_DB_NAME", "traceops")
    db_user: str = os.getenv("TRACEOPS_DB_USER", os.getenv("POSTGRES_USER", ""))
    db_password: str = os.getenv(
        "TRACEOPS_DB_PASSWORD", os.getenv("POSTGRES_PASSWORD", "")
    )

    prometheus_url: str = os.getenv("PROMETHEUS_URL", "http://127.0.0.1:9090")
    tempo_url: str = os.getenv("TEMPO_URL", "http://127.0.0.1:3200")
    loki_url: str = os.getenv("LOKI_URL", "http://127.0.0.1:3100")

    demo_service_urls: tuple = tuple(
        _service_urls(
            os.getenv(
                "DEMO_SERVICE_URLS",
                "gateway=http://127.0.0.1:8003,orders=http://127.0.0.1:8002,"
                "inventory=http://127.0.0.1:8001",
            )
        ).items()
    )

    explain_provider: str = os.getenv("EXPLAIN_PROVIDER", "none")
    ollama_url: str = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "llama3.2")

    @property
    def services(self) -> dict[str, str]:
        return dict(self.demo_service_urls)


settings = Settings()
