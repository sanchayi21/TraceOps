"""Turning Alertmanager webhooks into incidents.

One failure usually trips several alerts: when the database dies, gateway,
orders AND inventory all show high error rates. Opening three incidents for
one problem would be noise, so alerts that arrive while an incident is open
(within CORRELATION_WINDOW) are merged into it. That's the first, simplest
form of correlation TraceOps does: correlation by time.
"""

from datetime import datetime, timedelta

from traceops.models import AlertRecord, Incident

CORRELATION_WINDOW = timedelta(minutes=30)
LOOKBACK_BEFORE_ALERT = timedelta(seconds=90)  # alerts fire after `for:` delays
SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}


def parse_alerts(payload: dict) -> list[AlertRecord]:
    alerts = []
    for raw in payload.get("alerts", []):
        labels = raw.get("labels", {})
        ends_at = raw.get("endsAt")
        alerts.append(AlertRecord(
            fingerprint=raw.get("fingerprint") or str(sorted(labels.items())),
            alertname=labels.get("alertname", "unknown"),
            service=labels.get("job") or labels.get("service"),
            severity=labels.get("severity", "warning"),
            summary=raw.get("annotations", {}).get("summary", ""),
            status=raw.get("status", "firing"),
            starts_at=raw["startsAt"],
            # Alertmanager uses year 0001 for "not ended yet".
            ends_at=None if not ends_at or ends_at.startswith("0001") else ends_at,
        ))
    return alerts


def merge_alerts(existing: list[AlertRecord], incoming: list[AlertRecord]):
    """Returns (merged list, whether any alert started firing that wasn't before)."""
    by_fp = {alert.fingerprint: alert for alert in existing}
    newly_firing = False
    for alert in incoming:
        previous = by_fp.get(alert.fingerprint)
        if alert.status == "firing" and (previous is None or previous.status != "firing"):
            newly_firing = True
        by_fp[alert.fingerprint] = alert
    return list(by_fp.values()), newly_firing


def describe(alerts: list[AlertRecord]) -> tuple[str, str, list[str]]:
    """(title, severity, services) for a set of alerts."""
    names = sorted({alert.alertname for alert in alerts})
    services = sorted({alert.service for alert in alerts if alert.service})
    severity = max(
        (alert.severity for alert in alerts),
        key=lambda s: SEVERITY_ORDER.get(s, 0),
        default="warning",
    )
    title = f"{', '.join(names)} on {', '.join(services) or 'unknown service'}"
    return title, severity, services


def window_start_for(alerts: list[AlertRecord]) -> datetime:
    return min(alert.starts_at for alert in alerts) - LOOKBACK_BEFORE_ALERT


def handle_webhook(payload: dict, store, now: datetime) -> tuple[Incident | None, bool]:
    """Create or update an incident. Returns (incident, should_investigate)."""
    incoming = parse_alerts(payload)
    if not incoming:
        return None, False

    incident = store.find_open_alert_incident(since=now - CORRELATION_WINDOW)
    if incident is None:
        firing = [alert for alert in incoming if alert.status == "firing"]
        if not firing:
            return None, False  # a "resolved" for something we never opened
        title, severity, services = describe(firing)
        incident = store.create(
            title=title, severity=severity, source="alertmanager",
            services=services, window_start=window_start_for(firing),
            window_end=now, alerts=incoming,
        )
        return incident, True

    merged, newly_firing = merge_alerts(incident.alerts, incoming)
    title, severity, services = describe(merged)
    fields = dict(
        alerts=merged, title=title, severity=severity,
        services=sorted(set(services) | set(incident.services)),
        window_start=min(incident.window_start, window_start_for(merged)),
    )
    if all(alert.status == "resolved" for alert in merged):
        fields.update(status="resolved", resolved_at=now)
    return store.update(incident.id, **fields), newly_firing
