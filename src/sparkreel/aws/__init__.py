"""AWS adapter layer.

Every cloud capability is wrapped so the pipeline degrades gracefully to a local
implementation when credentials or services are unavailable. See `clients.py`.
"""
from .clients import AwsClients, aws_status  # noqa: F401
