import ipaddress
import socket
from urllib.parse import urlparse, urlunparse


class UnsafeWebhook(ValueError):
    pass


def resolve_public_webhook(value: str) -> set[str]:
    """Validate a webhook target and return every resolved public IP address."""
    url = urlparse(value)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.fragment
        or url.port not in (None, 443)
    ):
        raise UnsafeWebhook("webhook must be a public HTTPS URL on port 443")

    try:
        literal = ipaddress.ip_address(url.hostname)
        addresses = {str(literal)}
    except ValueError:
        try:
            answers = socket.getaddrinfo(url.hostname, url.port or 443, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise UnsafeWebhook("webhook host could not be resolved") from exc
        addresses = {answer[4][0] for answer in answers}

    if not addresses:
        raise UnsafeWebhook("webhook host could not be resolved")
    if any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise UnsafeWebhook("webhook host must resolve only to public addresses")
    return addresses


def pinned_webhook_targets(value: str) -> list[tuple[str, str, str]]:
    """Return (IP URL, Host header, TLS SNI name) targets after one DNS lookup."""
    parsed = urlparse(value)
    addresses = sorted(resolve_public_webhook(value))
    host = parsed.hostname or ""
    host_header = host if parsed.port in (None, 443) else f"{host}:{parsed.port}"
    targets = []
    for address in addresses:
        literal = f"[{address}]" if ipaddress.ip_address(address).version == 6 else address
        netloc = literal if parsed.port in (None, 443) else f"{literal}:{parsed.port}"
        targets.append((urlunparse((parsed.scheme, netloc, parsed.path or "/",
                                    parsed.params, parsed.query, "")), host_header, host))
    return targets
