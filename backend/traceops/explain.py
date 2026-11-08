"""Turning a report into readable text.

`build_summary` is deterministic and always available: it is built only from
fields in the report, so it can never state something the evidence lacks.

`explain_with_llm` is OPTIONAL. It sends the structured report to a LOCAL
Ollama model (free, nothing leaves your machine) and asks for a plain-English
explanation that cites evidence ids. TraceOps works fully without it.
"""

import json
from datetime import datetime, timezone

import httpx

from traceops.config import settings
from traceops.models import Explanation, InvestigationReport


class ExplanationUnavailable(Exception):
    pass


def _propagation_chain(report: InvestigationReport) -> str:
    """'inventory (HTTP 503) -> orders (HTTP 502) -> gateway (HTTP 502)'."""
    seen, steps = set(), []
    for hop in reversed(report.propagation):
        if hop.kind != "server" or hop.service in seen:
            continue
        seen.add(hop.service)
        status = f"HTTP {hop.http_status}" if hop.http_status else hop.status
        steps.append(f"{hop.service} ({status})")
    return " -> ".join(steps)


def build_summary(report: InvestigationReport) -> str:
    if not report.hypotheses and not report.symptoms:
        return "No abnormal behaviour was found in this time window."

    parts = []
    if report.hypotheses:
        top = report.hypotheses[0]
        parts.append(
            f"Most likely failure point: {top.component} "
            f"[{top.failure_type.replace('_', ' ')}], "
            f"{top.confidence_label} confidence ({top.confidence:.2f})."
        )
    if report.symptoms:
        observed = "; ".join(s.description for s in report.symptoms[:4])
        parts.append(f"Observed: {observed}.")
    chain = _propagation_chain(report)
    if chain:
        parts.append(f"In the representative trace the failure travelled {chain}.")
    if report.hypotheses:
        parts.append(f"Basis: {report.hypotheses[0].reasoning}")
    return " ".join(parts)


PROMPT = """You are helping an engineer understand a production incident.
Below is a structured investigation report produced by an automated tool from
real metrics, traces and logs.

Rules:
- Use ONLY facts in the report. Do not invent services, numbers or causes.
- Clearly separate "What was observed" (symptoms, evidence) from
  "Likely cause" (hypotheses) and say how confident the tool is.
- Cite evidence ids like [E3] for each claim.
- Mention the listed uncertainty notes that matter.
- Suggest 2-3 concrete next checks an engineer could run.
- Be concise: under 250 words, plain text with short headings.

REPORT:
{report}
"""


def explain_with_llm(report: InvestigationReport) -> Explanation:
    if settings.explain_provider != "ollama":
        raise ExplanationUnavailable(
            "No LLM is configured (EXPLAIN_PROVIDER=none). The deterministic "
            "summary above is built directly from the evidence."
        )
    compact = report.model_dump(mode="json")
    for item in compact["evidence"]:  # keep the prompt small
        item.get("data", {}).pop("samples", None)
    try:
        response = httpx.post(
            f"{settings.ollama_url}/api/generate",
            json={
                "model": settings.ollama_model,
                "prompt": PROMPT.format(report=json.dumps(compact, indent=1)),
                "stream": False,
                "options": {"temperature": 0.1},
            },
            timeout=180,
        )
        response.raise_for_status()
        text = response.json()["response"].strip()
    except (httpx.HTTPError, KeyError, ValueError) as error:
        raise ExplanationUnavailable(f"Ollama request failed: {error}") from error
    return Explanation(
        provider="ollama",
        model=settings.ollama_model,
        text=text,
        generated_at=datetime.now(timezone.utc),
    )
