"""Protocol constants for the BotsDock connector."""

PROTOCOL_VERSION = "0.1"
CONNECTION_MODE = "remote_ws"


def validate_server_protocol(message: dict) -> None:
    # Older 0.1 servers did not echo the negotiated version.
    version = message.get("protocol_version")
    if version is not None and version != PROTOCOL_VERSION:
        from .token_store import ConnectorError
        raise ConnectorError(f"unsupported server protocol version: {version}; expected {PROTOCOL_VERSION}")
