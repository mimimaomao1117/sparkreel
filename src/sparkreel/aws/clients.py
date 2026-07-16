"""boto3 client factory with availability detection and graceful fallback.

The whole point of SparkReel's design: any capability can target a real AWS
service, but if credentials are missing or a call fails, the adapter falls back
to the local implementation and records a warning — the demo never breaks.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Dict, Optional


class AwsClients:
    """Lazily builds and caches boto3 clients for one region/profile."""

    def __init__(self, region: str = "us-east-1", profile: Optional[str] = None):
        self.region = region
        self.profile = profile
        self._session = None
        self._clients: Dict[str, object] = {}
        self._creds_checked: Optional[bool] = None

    def _session_or_none(self):
        if self._session is None:
            try:
                import boto3  # noqa
                self._session = boto3.Session(
                    region_name=self.region,
                    profile_name=self.profile or None,
                )
            except Exception:
                self._session = False  # sentinel: boto3 unavailable
        return self._session or None

    def credentials_available(self) -> bool:
        """True only if boto3 is importable AND resolvable credentials exist."""
        if self._creds_checked is not None:
            return self._creds_checked
        ok = False
        sess = self._session_or_none()
        if sess is not None:
            try:
                ok = sess.get_credentials() is not None
            except Exception:
                ok = False
        self._creds_checked = ok
        return ok

    def client(self, service: str):
        """Return a boto3 client, or None if unavailable."""
        if service in self._clients:
            return self._clients[service]
        sess = self._session_or_none()
        if sess is None:
            return None
        try:
            c = sess.client(service)
        except Exception:
            c = None
        self._clients[service] = c
        return c


@lru_cache(maxsize=8)
def _cached(region: str, profile: Optional[str]) -> AwsClients:
    return AwsClients(region=region, profile=profile)


def get_clients(region: str = "us-east-1", profile: Optional[str] = None) -> AwsClients:
    return _cached(region, profile)


def aws_status(region: str = "us-east-1") -> Dict[str, object]:
    """Human-readable snapshot of AWS availability for reports/CLI."""
    c = get_clients(region)
    creds = c.credentials_available()
    return {
        "boto3": c._session_or_none() is not None,
        "credentials": creds,
        "region": region,
        "mode": "cloud-ready" if creds else "local-fallback",
    }
