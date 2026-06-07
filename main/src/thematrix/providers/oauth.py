from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
from secrets import token_urlsafe
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from thematrix.providers.models import provider_catalog
from thematrix.schemas import AuthMode, FileChangeConsent, PrivacyMode

OPENROUTER_PROVIDER_ID = "openrouter"
OPENROUTER_AUTH_URL = "https://openrouter.ai/auth"
OPENROUTER_TOKEN_URL = "https://openrouter.ai/api/v1/auth/keys"


class OAuthProviderError(RuntimeError):
    """Raised when a provider OAuth setup flow cannot continue."""


@dataclass(frozen=True)
class OAuthPendingSetup:
    provider_id: str
    selected_model: str
    base_url: str
    privacy_mode: PrivacyMode
    file_change_consent: FileChangeConsent
    guarded_shell_enabled: bool
    test_provider: bool
    code_verifier: str
    callback_state: str


@dataclass(frozen=True)
class OAuthStart:
    flow_id: str
    code_verifier: str
    authorization_url: str


def automated_oauth_provider_ids() -> set[str]:
    return {OPENROUTER_PROVIDER_ID}


def oauth_is_automated(provider_id: str) -> bool:
    return provider_id in automated_oauth_provider_ids()


def create_oauth_flow_id() -> str:
    return token_urlsafe(18)


def start_openrouter_oauth(callback_url: str, flow_id: str | None = None) -> OAuthStart:
    code_verifier = token_urlsafe(48)
    flow_id = flow_id or create_oauth_flow_id()
    code_challenge = _s256_code_challenge(code_verifier)
    authorization_url = (
        f"{OPENROUTER_AUTH_URL}?"
        + urlencode(
            {
                "callback_url": callback_url,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
    )
    return OAuthStart(
        flow_id=flow_id,
        code_verifier=code_verifier,
        authorization_url=authorization_url,
    )


def build_openrouter_oauth_setup(
    form: dict[str, str],
    callback_url_for_flow: Callable[[str, str], str],
) -> tuple[OAuthStart, OAuthPendingSetup]:
    profile = next(
        (item for item in provider_catalog() if item.provider_id == OPENROUTER_PROVIDER_ID),
        None,
    )
    if profile is None or form.get("provider_id") != OPENROUTER_PROVIDER_ID:
        raise OAuthProviderError("Choose OpenRouter to use this sign-in flow.")
    try:
        privacy_mode = PrivacyMode(form.get("privacy_mode", PrivacyMode.ASK_EACH_TIME.value))
        file_consent = FileChangeConsent(
            form.get("file_change_consent", FileChangeConsent.ASK_EACH_TIME.value)
        )
    except ValueError as exc:
        raise OAuthProviderError("Choose valid safety settings.") from exc
    flow_id = create_oauth_flow_id()
    callback_state = token_urlsafe(24)
    start = start_openrouter_oauth(
        callback_url_for_flow(flow_id, callback_state),
        flow_id=flow_id,
    )
    selected_model = form.get("model", "").strip() or (
        profile.suggested_models[0] if profile.suggested_models else "default"
    )
    pending = OAuthPendingSetup(
        provider_id=OPENROUTER_PROVIDER_ID,
        selected_model=selected_model,
        base_url=form.get("base_url", "").strip() or profile.default_base_url,
        privacy_mode=privacy_mode,
        file_change_consent=file_consent,
        guarded_shell_enabled=form.get("guarded_shell_enabled") == "on",
        test_provider=form.get("test_provider") == "on",
        code_verifier=start.code_verifier,
        callback_state=callback_state,
    )
    return start, pending


def setup_form_from_oauth(pending: OAuthPendingSetup, api_key: str) -> dict[str, str]:
    form = {
        "provider_id": pending.provider_id,
        "model": pending.selected_model,
        "auth_mode": AuthMode.OAUTH.value,
        "oauth_api_key": api_key,
        "base_url": pending.base_url,
        "privacy_mode": pending.privacy_mode.value,
        "file_change_consent": pending.file_change_consent.value,
    }
    if pending.guarded_shell_enabled:
        form["guarded_shell_enabled"] = "on"
    if pending.test_provider:
        form["test_provider"] = "on"
    return form


def exchange_openrouter_code(code: str, code_verifier: str) -> str:
    body = json.dumps(
        {
            "code": code,
            "code_verifier": code_verifier,
            "code_challenge_method": "S256",
        }
    ).encode("utf-8")
    request = Request(
        OPENROUTER_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except OSError as exc:
        raise OAuthProviderError(f"OpenRouter token exchange failed: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OAuthProviderError("OpenRouter returned invalid JSON during sign-in.") from exc
    key = payload.get("key")
    if not isinstance(key, str) or not key.strip():
        raise OAuthProviderError("OpenRouter did not return an API key.")
    return key.strip()


def _s256_code_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
