"""Authenticated node client with DNS pinning and no HTTP redirects."""
import ipaddress
import json
import socket
from urllib.parse import urlsplit

import certifi
import requests
import urllib3


def _request(method, base_url, path, api_key, payload=None, timeout=8, raw=False):
    base = urlsplit(str(base_url).rstrip("/"))
    if base.scheme not in {"http", "https"} or not base.hostname or base.username or base.password or base.query or base.fragment:
        raise ValueError("Invalid node URL")
    hostname = base.hostname.encode("idna").decode("ascii")
    if "%" in hostname:
        raise ValueError("Scoped node addresses are not allowed")
    port = base.port or (443 if base.scheme == "https" else 80)
    resolved = {row[4][0] for row in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)}
    addresses = [ipaddress.ip_address(ip) for ip in resolved]
    if not addresses or any(not ip.is_global or ip.is_multicast or ip.is_reserved for ip in addresses):
        raise ValueError("Node must resolve only to public addresses")
    # Connect to the validated numeric address, not a second DNS lookup.
    address = sorted(resolved)[0]
    authority = f"[{hostname}]" if ":" in hostname else hostname
    if base.port:
        authority += f":{port}"
    target = (base.path.rstrip("/") + "/" + str(path).lstrip("/")) or "/"
    if any(ord(ch) < 32 for ch in target) or "#" in target:
        raise ValueError("Invalid node request path")
    headers = {"Host": authority, "Authorization": f"Bearer {str(api_key).strip()}"}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    options = {"port": port, "timeout": urllib3.Timeout(connect=timeout, read=timeout), "retries": False}
    if base.scheme == "https":
        pool = urllib3.HTTPSConnectionPool(address, server_hostname=hostname,
                 assert_hostname=hostname, cert_reqs="CERT_REQUIRED", ca_certs=certifi.where(), **options)
    else:
        pool = urllib3.HTTPConnectionPool(address, **options)
    try:
        response = pool.urlopen(method, target, headers=headers, body=body, redirect=False,
                                retries=False, assert_same_host=False)
        result = requests.Response()
        result.status_code = response.status
        result.headers.update(response.headers)
        result._content = response.data
        result.url = base_url + "/" + str(path).lstrip("/")
        if 300 <= response.status < 400:
            raise requests.HTTPError("Node redirects are not permitted", response=result)
        result.raise_for_status()
        return result if raw else decode_response(result)
    except urllib3.exceptions.HTTPError as exc:
        raise requests.ConnectionError("Node connection failed") from exc
    finally:
        pool.close()


def decode_response(response):
    ctype = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
    if ctype == "application/json" or ctype.endswith("+json") or response.text.lstrip().startswith(("{", "[")):
        try:
            return response.json()
        except ValueError:
            pass
    return response.text


def get(base_url, path, api_key, *, timeout=6):
    return _request("GET", base_url, path, api_key, timeout=timeout)


def post(base_url, path, api_key, payload=None, *, timeout=8):
    return _request("POST", base_url, path, api_key, payload=payload or {}, timeout=timeout)


def delete(base_url, path, api_key, payload=None, *, timeout=8):
    return _request("DELETE", base_url, path, api_key, payload=payload, timeout=timeout)


def get_response(base_url, path, api_key, *, timeout=25):
    return _request("GET", base_url, path, api_key, timeout=timeout, raw=True)


def post_response(base_url, path, api_key, payload, *, timeout=35):
    return _request("POST", base_url, path, api_key, payload=payload, timeout=timeout, raw=True)
