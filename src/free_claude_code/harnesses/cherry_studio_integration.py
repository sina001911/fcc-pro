"""Cherry Studio Desktop integration: config, detection, gateway probing."""

import json
import os
import re
import sys
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from free_claude_code.application.model_catalog import ModelCatalog
from free_claude_code.config.server_urls import same_proxy_url
from free_claude_code.core.json_types import JsonObject
from free_claude_code.harnesses.cherry_studio_config import (
    CHERRY_CUSTOM_PROVIDERS_KEY,
    CHERRY_PROVIDER_ID,
    build_cherry_provider,
    same_provider,
)
from free_claude_code.harnesses.config_file import atomic_write_text, decode_json

CONFIG_DIRNAME = "cherry-studio"
CONFIG_FILENAME = "config.json"
GATEWAY_CONFIG_KEY = "gateway"
SETTINGS_KEY = "settings"
GATEWAY_ENABLED_KEY = "enable"
GATEWAY_PORT_KEY = "port"
GATEWAY_AUTH_TYPE_KEY = "authType"
GATEWAY_AUTH_TOKEN_KEY = "authToken"
DEFAULT_GATEWAY_PORT = 23333
DEFAULT_GATEWAY_HOST = "127.0.0.1"
MODELS_ENDPOINT = "/models"
CHERRY_STUDIO_HOME_ENV = "CHERRY_STUDIO_HOME"
CHERRY_PROCESS_NAMES = ("cherry-studio.exe", "Cherry Studio.exe", "cherry-studio")
CHERRY_BINARY_NAMES = ("cherry-studio.exe", "Cherry Studio.exe")


class CherryConfigError(ValueError):
    """Cherry Studio configuration is missing, invalid, or incomplete."""


def config_root() -> Path:
    value = os.environ.get(CHERRY_STUDIO_HOME_ENV, "").strip()
    if value == "~":
        return Path.home()
    if value.startswith(("~/", "~\\")):
        return (Path.home() / value[2:]).resolve()
    if value:
        return Path(value).resolve()
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / CONFIG_DIRNAME
    elif sys.platform == "darwin":
        return Path.home() / "Library/Application Support" / CONFIG_DIRNAME
    else:
        return Path.home() / ".config" / CONFIG_DIRNAME


def config_path() -> Path:
    return config_root() / CONFIG_FILENAME


def gateway_settings_path() -> Path:
    return config_root() / CONFIG_FILENAME


def _read_object(path: Path) -> JsonObject:
    try:
        source = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    document = decode_json(source)
    if not isinstance(document, dict):
        raise CherryConfigError("Cherry Studio settings must be an object")
    return cast(JsonObject, document)


def _settings(document: JsonObject) -> JsonObject:
    settings = document.get(SETTINGS_KEY)
    if settings is None:
        return {}
    if not isinstance(settings, dict):
        raise CherryConfigError("Cherry Studio settings must be an object")
    return cast(JsonObject, settings)


def _gateway(settings: JsonObject) -> JsonObject:
    gateway = settings.get(GATEWAY_CONFIG_KEY)
    if gateway is None:
        return {}
    if not isinstance(gateway, dict):
        raise CherryConfigError("Cherry Studio gateway settings must be an object")
    return cast(JsonObject, gateway)


def gateway_settings(path: Path) -> JsonObject:
    """Return the detected gateway settings without exposing the auth token."""
    gateway = _gateway(_settings(_read_object(path)))
    return {
        "enabled": bool(gateway.get(GATEWAY_ENABLED_KEY, False)),
        "port": gateway.get(GATEWAY_PORT_KEY, DEFAULT_GATEWAY_PORT),
        "auth_type": gateway.get(GATEWAY_AUTH_TYPE_KEY, "none"),
    }


def _providers(settings: JsonObject) -> list[JsonObject]:
    providers = settings.get(CHERRY_CUSTOM_PROVIDERS_KEY, [])
    if not isinstance(providers, list):
        raise CherryConfigError("Cherry Studio customProviders must be an array")
    return cast(list[JsonObject], providers)


def configure_gateway(path: Path, *, host: str, port: int, auth_token: str) -> bool:
    """Set Cherry Studio's API Gateway settings to point at FCC's gateway."""
    path = path.resolve()
    parsed = urlsplit(f"http://{host}:{port}")
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise CherryConfigError("Cherry Studio gateway requires a loopback host")
    if not isinstance(port, int) or not (1 <= port <= 65535):
        raise CherryConfigError("Cherry Studio gateway port must be 1-65535")
    document = _read_object(path)
    settings = _settings(document)
    if SETTINGS_KEY not in document:
        document[SETTINGS_KEY] = settings
    gateway = _gateway(settings)
    if GATEWAY_CONFIG_KEY not in settings:
        settings[GATEWAY_CONFIG_KEY] = gateway
    before = json.dumps(gateway, sort_keys=True)
    gateway[GATEWAY_ENABLED_KEY] = True
    gateway[GATEWAY_PORT_KEY] = port
    gateway[GATEWAY_AUTH_TYPE_KEY] = "apiKey"
    gateway["host"] = host
    after = json.dumps(gateway, sort_keys=True)
    if before == after:
        return False
    _atomic_write(path, document)
    return True


def _find_provider(providers: list[JsonObject]) -> JsonObject | None:
    for provider in providers:
        if isinstance(provider, Mapping) and provider.get("id") == CHERRY_PROVIDER_ID:
            return cast(JsonObject, provider)
    return None


def has_provider(path: Path) -> bool:
    return _find_provider(_providers(_settings(_read_object(path)))) is not None


def _values(proxy_root_url: str, auth_token: str) -> JsonObject:
    parsed = urlsplit(proxy_root_url)
    if (
        not auth_token
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise CherryConfigError("Invalid Cherry Studio gateway")
    if parsed.scheme not in ("http", "https"):
        raise CherryConfigError("Cherry Studio requires a loopback HTTP or HTTPS gateway")
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise CherryConfigError("Cherry Studio requires a loopback HTTP or HTTPS gateway")
    return {"proxy_root_url": proxy_root_url.rstrip("/"), "auth_token": auth_token}


def _paths() -> JsonObject:
    return {"cherry_config": str(config_path()), "gateway_settings": str(gateway_settings_path())}


def status(
    path: Path,
    proxy_root_url: str,
    auth_token: str,
    catalog: ModelCatalog,
    *,
    provider_progress_timeout: float,
) -> JsonObject:
    path = path.resolve()
    document = _read_object(path)
    settings = _settings(document)
    providers = _providers(settings)
    existing = _find_provider(providers)
    values = _values(proxy_root_url, auth_token)
    candidate = build_cherry_provider(
        catalog.models,
        proxy_root_url=values["proxy_root_url"],
        credential_ref=values["auth_token"],
    )
    connected = bool(
        existing is not None
        and same_provider(existing, candidate)
    )
    return {"connected": connected, "paths": _paths()}


def configure(
    path: Path,
    proxy_root_url: str,
    auth_token: str,
    catalog: ModelCatalog,
    *,
    provider_progress_timeout: float,
    only_existing: bool = False,
) -> JsonObject:
    path = path.resolve()
    values = _values(proxy_root_url, auth_token)
    if not values["auth_token"].strip():
        raise CherryConfigError(
            "Set an FCC proxy authentication token before connecting Cherry Studio."
        )
    document = _read_object(path)
    settings = _settings(document)
    if SETTINGS_KEY not in document:
        document[SETTINGS_KEY] = settings
    providers = _providers(settings)
    existing = _find_provider(providers)
    if only_existing and existing is None:
        return {"connected": False, "changed": False, "paths": _paths()}
    candidate = build_cherry_provider(
        catalog.models,
        proxy_root_url=values["proxy_root_url"],
        credential_ref=values["auth_token"],
    )
    if existing is not None:
        if same_provider(existing, candidate):
            return status(path, proxy_root_url, auth_token, catalog, provider_progress_timeout=provider_progress_timeout) | {"changed": False}
        for index, provider in enumerate(providers):
            if isinstance(provider, Mapping) and provider.get("id") == CHERRY_PROVIDER_ID:
                providers[index] = cast(JsonObject, candidate)
                break
    else:
        providers.append(cast(JsonObject, candidate))
    settings[CHERRY_CUSTOM_PROVIDERS_KEY] = providers
    changed = _atomic_write(path, document)
    return status(path, proxy_root_url, auth_token, catalog, provider_progress_timeout=provider_progress_timeout) | {"changed": changed}


def disconnect(path: Path) -> JsonObject:
    path = path.resolve()
    document = _read_object(path)
    settings = _settings(document)
    providers = _providers(settings)
    before = len(providers)
    retained = [p for p in providers if not (isinstance(p, Mapping) and p.get("id") == CHERRY_PROVIDER_ID)]
    if len(retained) != before:
        settings[CHERRY_CUSTOM_PROVIDERS_KEY] = retained
        _atomic_write(path, document)
    return {"connected": False, "paths": _paths()}


def refresh_connected(
    path: Path,
    proxy_root_url: str,
    auth_token: str,
    catalog: ModelCatalog,
    *,
    provider_progress_timeout: float,
) -> bool:
    result = configure(
        path,
        proxy_root_url,
        auth_token,
        catalog,
        provider_progress_timeout=provider_progress_timeout,
        only_existing=True,
    )
    return result["changed"] is True


def _atomic_write(path: Path, document: JsonObject) -> bool:
    before = path.read_text(encoding="utf-8-sig") if path.exists() else None
    text = json.dumps(document, indent=2, allow_nan=False) + "\n"
    if before == text:
        return False
    atomic_write_text(path, text)
    return True


# ---------------------------------------------------------------------------
# Desktop app detection (Windows-first; conservative elsewhere)
# ---------------------------------------------------------------------------

def _candidate_install_dirs() -> list[Path]:
    home = Path.home()
    roots: list[Path] = []
    for env in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "APPDATA"):
        value = os.environ.get(env)
        if value:
            roots.append(Path(value))
    roots.append(home)
    candidates = []
    seen: set[str] = set()
    for root in roots:
        for sub in (CONFIG_DIRNAME, "programs", "Cherry Studio", "cherry-studio"):
            path = root / sub
            key = str(path.resolve())
            if key not in seen:
                seen.add(key)
                candidates.append(path)
    return candidates


def _uninstall_entries() -> list[str]:
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:
        return []
    entries: list[str] = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall") as root:
                i = 0
                while True:
                    try:
                        name = winreg.EnumKey(root, i)
                    except OSError:
                        break
                    i += 1
                    if "cherry" in name.lower():
                        entries.append(name)
        except OSError:
            continue
    return entries


def _process_running() -> bool:
    names = CHERRY_PROCESS_NAMES
    if sys.platform == "win32":
        try:
            import subprocess

            output = subprocess.run(
                ["tasklist"], capture_output=True, text=True, check=False
            ).stdout
        except OSError:
            return False
        return any(name.lower() in output.lower() for name in names)
    try:
        import subprocess

        output = subprocess.run(
            ["ps", "-A", "-o", "comm="], capture_output=True, text=True, check=False
        ).stdout
    except OSError:
        return False
    return any(name.lower() in output.lower() for name in names)


def detect_install() -> JsonObject:
    """Detect whether Cherry Studio is installed and whether it is running."""
    config_dir = config_root()
    config_file = config_path()
    installed = config_file.is_file()
    locations: list[str] = []
    for candidate in _candidate_install_dirs():
        if candidate.exists():
            locations.append(str(candidate))
    uninstall = _uninstall_entries()
    running = _process_running()
    return {
        "installed": installed or bool(locations) or bool(uninstall),
        "config_dir": str(config_dir),
        "config_path": str(config_file),
        "install_locations": locations,
        "uninstall_entries": uninstall,
        "running": running,
        "version": None,
    }


def launch_client(binary: str | None = None) -> bool:
    """Best-effort launch of Cherry Studio; returns True if a process started."""
    if sys.platform != "win32":
        return False
    import subprocess

    candidates = [binary] if binary else []
    for candidate_dir in _candidate_install_dirs():
        for name in CHERRY_BINARY_NAMES:
            candidates.append(str(candidate_dir / name))
    for candidate in candidates:
        if not candidate:
            continue
        try:
            subprocess.Popen([candidate], close_fds=True)
        except OSError:
            continue
        return True
    return False


# ---------------------------------------------------------------------------
# API Gateway probing (anonymous; never reads or forwards the gateway token)
# ---------------------------------------------------------------------------

def probe_gateway(host: str, port: int, *, timeout: float) -> JsonObject:
    """Probe Cherry Studio's OpenAI-compatible gateway without authentication."""
    import httpx

    base = f"http://{host}:{port}"
    try:
        with httpx.Client(timeout=timeout, trust_env_url=False) as client:
            response = client.get(f"{base}{MODELS_ENDPOINT}")
    except Exception as exc:  # noqa: BLE001 - network/HTTP errors are reported
        return {"reachable": False, "models": [], "message": str(exc)}
    if response.status_code in (401, 403):
        return {
            "reachable": True,
            "models": [],
            "message": "Gateway is password-protected. Configure it in Cherry Studio.",
        }
    if response.status_code >= 400:
        return {
            "reachable": False,
            "models": [],
            "message": f"Gateway returned HTTP {response.status_code}.",
        }
    try:
        payload = response.json()
    except ValueError:
        return {"reachable": True, "models": [], "message": "Gateway returned non-JSON models."}
    models = _extract_models(payload)
    return {"reachable": True, "models": models, "message": None}


def _extract_models(payload: object) -> list[JsonObject]:
    if isinstance(payload, Mapping):
        data = payload.get("data")
        if isinstance(data, list):
            models: list[JsonObject] = []
            for entry in data:
                if isinstance(entry, Mapping) and isinstance(entry.get("id"), str):
                    models.append(
                        {
                            "id": str(entry["id"]),
                            "name": str(entry.get("name") or entry["id"]),
                        }
                    )
            return models
        if isinstance(payload.get("models"), list):
            models = []
            for entry in payload["models"]:
                if isinstance(entry, Mapping) and isinstance(entry.get("id"), str):
                    models.append(
                        {
                            "id": str(entry["id"]),
                            "name": str(entry.get("name") or entry["id"]),
                        }
                    )
            return models
    return []
