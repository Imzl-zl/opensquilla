"""scrub_text: secret masking + home-dir normalization for shareable artifacts."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from opensquilla.observability.redact import scrub_json, scrub_text

FAKE_KEY = "sk-FAKE1234567890abcdef"


@pytest.mark.parametrize("key", [
    "requiresApiKey", "REQUIRESAPIKEY", "requires_api_key", "requires-api-key",
    "apiKeyConfigured", "APIKEYConfigured", "apiKeyEnv", "ApiKeyEnv", "api_key_env",
    "hasToken", "isSecret",
    "tokenCount", "session_key", "monkey", "notasecret",
    "rEqUiReS_api_key", "hAs_Api_KEY", "iS_Password", "sUpPoRtS_PRIVATE_KEY",
])
def test_metadata_assignments_keep_complete_key_boundaries(key: str) -> None:
    text = f'{key}=true "{key}": false {key}: null'
    assert scrub_text(text) == text
    assert scrub_json({key: True}) == {key: True}


@pytest.mark.parametrize("key", [
    "apiKey", "API_KEY", "api-key", "Authorization", "accessToken",
    "PROVIDER_API_KEY", "providerApiKey", "clientSecret", "client-secret",
    "AWS_SECRET_ACCESS_KEY", "aws_secret_access_key", "awsSecretAccessKey",
    "sshPrivateKey", "providerSecretKey", "x-api-key", "providerEncryptKey",
    "channelEncodingAesKey", "proxy-authorization", "_token", "_api_key",
    "aPiKeY", "sEcReT", "API_kEy", "aWs_sEcReT_aCcEsS_kEy", "pRiVaTe_KeY",
])
def test_complete_secret_assignments_remain_redacted(key: str) -> None:
    assert scrub_text(f'{key}="synthetic credential"') == f'{key}="[redacted]"'
    assert scrub_json({key: "synthetic credential"}) == {key: "[redacted]"}


@pytest.mark.parametrize("key", [
    "X-AuthToken", "X-AccessToken", "refreshToken", "idToken", "bearerToken",
    "apiToken", "appToken", "clientSecret",
    "X-CSRFToken", "X-SecurityToken", "X-ProviderApiKey", "X-CustomPassword",
    "X-CustomSecret", "X-CustomPrivateKey", "Vendor.CustomToken", "定制_CustomToken",
    "X-CustomPrivate_Key", "X-CustomSecret_Access_Key",
])
@pytest.mark.parametrize("case", ["original", "lower", "upper", "swapcase"])
def test_compound_credentials_are_case_insensitive(key: str, case: str) -> None:
    key = key if case == "original" else getattr(key, case)()
    assert scrub_json({"headers": [{key: "synthetic-opaque-credential"}]}) == {
        "headers": [{key: "[redacted]"}],
    }
    assert scrub_text(f'{key}: "synthetic-opaque-credential"') == f'{key}: "[redacted]"'
    assert scrub_text(f'helper --{key}=synthetic-opaque-credential') == (
        f'helper --{key}=[redacted]'
    )


@pytest.mark.parametrize("separator", "!#$%&'*+^`|~")
@pytest.mark.parametrize("suffix", ["Password", "Token", "ApiKey"])
def test_http_header_punctuation_preserves_secret_boundaries(
    separator: str, suffix: str,
) -> None:
    header = f"X{separator}{suffix}"
    for key in (header, header.lower(), header.upper(), header.swapcase()):
        assert scrub_json({"headers": {key: "synthetic-header-credential"}}) == {
            "headers": {key: "[redacted]"},
        }
        text = f'{key}: "synthetic-header-credential"'
        expected = f'{key}: "[redacted]"'
        assert scrub_text(text) == expected
        assert scrub_text(expected) == expected


@pytest.mark.parametrize("separator", "!#$%&'*+^`|~")
@pytest.mark.parametrize("suffix", ["requiresApiKey", "hasToken", "apiKeyEnv", "tokenCount"])
def test_punctuation_namespaced_metadata_stays_readable(separator: str, suffix: str) -> None:
    field = f"Vendor{separator}{suffix}"
    for key in (field, field.lower(), field.upper(), field.swapcase()):
        assert scrub_json({key: True}) == {key: True}
        assert scrub_text(f"{key}=true") == f"{key}=true"


def test_unicode_namespace_with_punctuation_masks_secret_values() -> None:
    key = "定制+Password"
    assert scrub_json({key: "synthetic-credential"}) == {key: "[redacted]"}
    assert scrub_text(f"{key}=synthetic-credential") == f"{key}=[redacted]"


@pytest.mark.parametrize("separator", "!#$%&'*+^`|~")
@pytest.mark.parametrize("suffix", ["CSRFToken", "SecurityToken", "ProviderApiKey"])
def test_punctuation_keeps_compound_credential_namespace(
    separator: str, suffix: str,
) -> None:
    header = f"X{separator}{suffix}"
    for key in (header, header.lower(), header.upper(), header.swapcase()):
        assert scrub_json({"headers": {key: "synthetic-credential"}}) == {
            "headers": {key: "[redacted]"},
        }
        for text, expected in (
            (f"{key}: synthetic-credential", f"{key}: [redacted]"),
            (f'"{key}": "synthetic-credential"', f'"{key}": "[redacted]"'),
            (f"helper --{key}=synthetic-credential", f"helper --{key}=[redacted]"),
        ):
            assert scrub_text(text) == expected
            assert scrub_text(expected) == expected


@pytest.mark.parametrize("text, expected", [
    ("'api_key'='synthetic credential'", "'api_key'='[redacted]'"),
    ("{'X!csrftoken': 'synthetic credential'}", "{'X!csrftoken': '[redacted]'}"),
    ('"X\'providerapikey": "synthetic credential"', '"X\'providerapikey": "[redacted]"'),
    ("GET https://example.invalid/?phase=before&corpsecret=synthetic-credential",
     "GET https://example.invalid/?phase=before&corpsecret=[redacted]"),
    ("GET https://example.invalid/?phase=before&x!csrftoken=synthetic-credential",
     "GET https://example.invalid/?phase=before&x!csrftoken=[redacted]"),
    ("GET https://example.invalid/?requiresApiKey=true&phase=before",
     "GET https://example.invalid/?requiresApiKey=true&phase=before"),
])
def test_punctuation_namespaces_keep_quoting_and_query_boundaries(
    text: str, expected: str,
) -> None:
    assert scrub_text(text) == expected
    assert scrub_text(expected) == expected


@pytest.mark.parametrize("key", [
    "requiresAuthToken", "requires_auth_token", "requiresaccesstoken", "hasAccessToken",
    "authTokenCount", "accessTokenEnv", "refreshTokenConfigured", "idTokenRequired",
    "clientSecretEnv", "notasecret",
    "X.requiresApiKey", "Vendor.Key.hasToken", "X.securityTokenCount",
    "X.providerApiKeyEnv",
])
def test_compound_credential_metadata_keeps_case_insensitive_boundaries(key: str) -> None:
    for spelling in (key, key.lower(), key.upper(), key.swapcase()):
        payload = {"metadata": {spelling: [True, False, 3, 1.25, None]}}
        assert scrub_json(payload) == payload
        assert scrub_text(f"{spelling}=true") == f"{spelling}=true"


@pytest.mark.parametrize("key", [
    "X.Provider-Token", "Vendor.Key-Api-Key", "定制_api_key", "厂商.Password",
    "corpsecret", "CORPSECRET", "this_is_app_secret", "service_has_token",
    "island_token", "hash_token", "isLand_token", "hasH_token",
    "X.isLand_token", "X.hasH_token", "X.IsLandToken",
    "ISLand_tOKEn", "X.IsLAND_ToKEn",
])
def test_custom_secret_fields_and_aliases_are_masked(key: str) -> None:
    payload = {"headers": [{key: "synthetic-custom-credential"}]}
    expected = {"headers": [{key: "[redacted]"}]}
    assert scrub_json(payload) == expected
    assert scrub_text(f'{key}="synthetic-custom-credential"') == f'{key}="[redacted]"'
    assert scrub_json(expected) == expected


@pytest.mark.parametrize("key", ["X-notasecret", "X.notasecret", "X-NOTASECRET"])
def test_ambiguous_namespaced_secret_suffix_is_conservatively_masked(key: str) -> None:
    # A custom namespace can qualify arbitrary credentials. An unqualified
    # ordinary word remains benign, but its namespaced use is ambiguous.
    assert scrub_json({key: "synthetic-credential", "notasecret": True}) == {
        key: "[redacted]", "notasecret": True,
    }
    assert scrub_text(f"{key}=synthetic-credential") == f"{key}=[redacted]"


@pytest.mark.parametrize("key", [
    "api-key", "password", "token", "client-secret", "X.Provider-Token", "定制_api_key",
])
@pytest.mark.parametrize("prefix", ["-", "--"])
@pytest.mark.parametrize("value, redacted", [
    ("synthetic-cli-credential", "[redacted]"),
    ('"synthetic cli credential"', '"[redacted]"'),
])
def test_cli_credential_assignments_are_masked(
    key: str, prefix: str, value: str, redacted: str,
) -> None:
    text = f"helper {prefix}{key}={value} --attempts=2"
    expected = f"helper {prefix}{key}={redacted} --attempts=2"
    assert scrub_text(text) == expected
    assert scrub_text(expected) == expected


@pytest.mark.parametrize("key", [
    "requires-api-key", "apiKeyConfigured", "api-key-env", "notasecret", "token-count",
    "X.requiresApiKey", "Vendor.Key.apiKeyConfigured", "厂商.requires_api_key",
])
def test_cli_and_namespaced_metadata_stays_readable(key: str) -> None:
    text = f"helper --{key}=true {key}=false"
    assert scrub_text(text) == text
    assert scrub_json({key: True}) == {key: True}


def test_query_credential_alias_is_masked() -> None:
    text = "GET https://example.invalid/cgi-bin/gettoken?corpid=dummy&corpsecret=synthetic-query"
    expected = "GET https://example.invalid/cgi-bin/gettoken?corpid=dummy&corpsecret=[redacted]"
    assert scrub_text(text) == expected
    assert scrub_text(expected) == expected


@pytest.mark.parametrize("prefix", [".", "..", "$.provider."])
def test_dotted_path_assignments_keep_secret_and_metadata_boundaries(prefix: str) -> None:
    assert scrub_text(f"{prefix}api_key=synthetic-credential") == f"{prefix}api_key=[redacted]"
    metadata = f"{prefix}requiresApiKey=true {prefix}apiKeyConfigured=false"
    assert scrub_text(metadata) == metadata


# Synthetic bare tokens (no key=value structure around them), as they appear
# verbatim inside provider/channel error messages.
FAKE_OPENAI = "sk-FAKEabc123def456ghi789"
FAKE_OPENAI_PROJ = "sk-proj-FAKEabc123def456ghi789"
FAKE_OPENAI_ANT = "sk-ant-api03-FAKEabc123def456ghi789"
FAKE_TOKENRHYTHM = "sk_tr_FAKEabc123def456ghi789"
FAKE_SLACK_BOT = "xoxb-FAKE1234567890-abcdefghij"
FAKE_SLACK_WEBHOOK = "https://hooks.slack.com/services/T0FAKE123/B0FAKE456/FAKEabcdefFAKE"
# Assembled at runtime so the tracked source never contains a GitHub-token-
# shaped literal (the public-release hygiene scan flags those shapes).
FAKE_GITHUB_PAT = "ghp_" + "FAKEabc123def456ghi789jkl"
FAKE_GITHUB_FINE_PAT = "github_pat_" + "FAKE1234567890abcdef_FAKEmoretail"
FAKE_AWS_KEY_ID = "AKIAFAKE0123456789AB"
FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJGQUtFIjoidHJ1ZSJ9.FAKEsig1234567890"
FAKE_GOOGLE_KEY = "AIzaFAKE0123456789abcdefghijklmnopqrstu"

BARE_TOKENS = [
    FAKE_OPENAI,
    FAKE_OPENAI_PROJ,
    FAKE_OPENAI_ANT,
    FAKE_TOKENRHYTHM,
    FAKE_SLACK_BOT,
    FAKE_GITHUB_PAT,
    FAKE_GITHUB_FINE_PAT,
    FAKE_AWS_KEY_ID,
    FAKE_JWT,
    FAKE_GOOGLE_KEY,
]


def test_masks_secret_shaped_assignments() -> None:
    text = (
        f"api_key={FAKE_KEY}\n"
        f'"slack_token": "xoxb-FAKE-0000"\n'
        f"password = hunter2-fake\n"
        f"Authorization: Bearer {FAKE_KEY}\n"
    )
    scrubbed = scrub_text(text)
    assert FAKE_KEY not in scrubbed
    assert "xoxb-FAKE-0000" not in scrubbed
    assert "hunter2-fake" not in scrubbed
    assert scrubbed.count("[redacted]") >= 4


def test_normalizes_home_directory() -> None:
    home = str(Path.home())
    scrubbed = scrub_text(f"config loaded from {home}/.opensquilla/config.toml")
    assert home not in scrubbed
    assert "~/.opensquilla/config.toml" in scrubbed


def test_leaves_ordinary_text_alone() -> None:
    text = "2026-07-07 [ERROR] opensquilla.engine: turn_runner.failed session_key='agent:x'"
    assert scrub_text(text) == text


def test_masks_quoted_multiword_value_fully() -> None:
    scrubbed = scrub_text('password = "correct horse battery staple"')
    assert "correct horse battery staple" not in scrubbed
    assert "horse" not in scrubbed
    assert "staple" not in scrubbed
    assert "[redacted]" in scrubbed


TRICKY_INPUTS = [
    f"api_key={FAKE_KEY}",
    '"slack_token": "xoxb-FAKE-0000"',
    "password = hunter2-fake",
    f"Authorization: Bearer {FAKE_KEY}",
    'password = "correct horse battery staple"',
    "Authorization: Basic dXNlcjpwYXNzLWZha2U=",
    "retrying with header Bearer abc+def/gh== now",
    "session_key=abc",
    "password:\nRestarting the gateway",
    "api_key=[redacted]",
    'secret_key=abc123 private-key: xyz789 "secret_access_key": "AKIAFAKE999"',
    *(f"provider said: {token} was rejected" for token in BARE_TOKENS),
    f"error posting to {FAKE_SLACK_WEBHOOK} (404)",
    f'{{"error": {{"message": "Incorrect API key {FAKE_OPENAI} provided"}}}}',
    "no tokens here: skill risk-free eyJustSaying task-1234 AKIAplan",
]


def test_scrub_is_idempotent() -> None:
    for text in TRICKY_INPUTS:
        once = scrub_text(text)
        assert scrub_text(once) == once, f"double scrub diverged for {text!r}"


def test_masks_basic_auth_credential() -> None:
    scrubbed = scrub_text("Authorization: Basic dXNlcjpwYXNzLWZha2U=")
    assert "dXNlcjpwYXNzLWZha2U=" not in scrubbed
    assert "[redacted]" in scrubbed


def test_masks_base64_bearer_token_fully() -> None:
    scrubbed = scrub_text("retrying with header Bearer abc+def/gh== now")
    assert "abc+def/gh==" not in scrubbed
    assert "gh==" not in scrubbed
    assert "[redacted]" in scrubbed


def test_masks_additional_secret_key_variants() -> None:
    text = 'secret_key=abc123 private-key: xyz789 "secret_access_key": "AKIAFAKE999"'
    scrubbed = scrub_text(text)
    assert "abc123" not in scrubbed
    assert "xyz789" not in scrubbed
    assert "AKIAFAKE999" not in scrubbed


def test_session_key_stays_readable() -> None:
    scrubbed = scrub_text("resuming turn with session_key=abc")
    assert "session_key=abc" in scrubbed


def test_bare_label_does_not_mask_next_line() -> None:
    text = "password:\nRestarting the gateway"
    assert scrub_text(text) == text


def test_masks_bare_tokens_in_prose() -> None:
    for token in BARE_TOKENS:
        text = f"provider error: token {token} was rejected upstream"
        scrubbed = scrub_text(text)
        assert token not in scrubbed, f"bare token survived: {token!r}"
        assert "[redacted]" in scrubbed
        assert scrubbed.startswith("provider error: token ")
        assert scrubbed.endswith(" was rejected upstream")


def test_masks_bare_tokens_inside_json_blob() -> None:
    for token in BARE_TOKENS:
        text = f'{{"error": {{"message": "Incorrect API key {token} provided", "code": 401}}}}'
        scrubbed = scrub_text(text)
        assert token not in scrubbed, f"bare token survived in JSON: {token!r}"
        assert '"code": 401' in scrubbed
        assert "Incorrect API key" in scrubbed


def test_masks_openai_style_error_message() -> None:
    text = f"Incorrect API key {FAKE_OPENAI} provided. You can find your key at ..."
    scrubbed = scrub_text(text)
    assert FAKE_OPENAI not in scrubbed
    assert "Incorrect API key [redacted] provided" in scrubbed


def test_masks_slack_webhook_path() -> None:
    text = f"channel delivery failed: POST {FAKE_SLACK_WEBHOOK} returned 404"
    scrubbed = scrub_text(text)
    assert "T0FAKE123" not in scrubbed
    assert "FAKEabcdefFAKE" not in scrubbed
    assert "hooks.slack.com/services/" in scrubbed
    assert "returned 404" in scrubbed


def test_bare_token_prose_non_matches() -> None:
    # Ordinary words and short identifiers that resemble token prefixes must
    # never be masked: word-boundary anchors + length floors.
    for text in [
        "the skill loader retried the task",
        "this deployment is risk-free and reversible",
        "eyJustSaying this is fine",
        "sk-1 sk-short sk- and ghp_ alone",
        "xoxb- with no tail; AKIA alone; AKIAlowercase123456",
        "AIza too short to be a key",
        "eyJab.eyJcd.ef segments below the floor",
        "task-1234 completed in 20ms",
        "see https://hooks.slack.com/services for docs",
    ]:
        assert scrub_text(text) == text, f"ordinary prose was mangled: {text!r}"


def test_masks_bare_tokens_abutting_run_punctuation() -> None:
    # A branch whose tail class excludes -_ dies against the shared (?!RUN)
    # trailing guard when the key abuts punctuation — failing closed and
    # leaking the WHOLE key. Every branch must over-mask here instead.
    for suffix in ("-rotated", "_old"):
        text = f"old key {FAKE_TOKENRHYTHM}{suffix} was replaced"
        scrubbed = scrub_text(text)
        assert "FAKEabc123def456ghi789" not in scrubbed, suffix


def test_bare_token_masking_is_idempotent() -> None:
    for token in BARE_TOKENS:
        text = f"log line with {token} embedded"
        once = scrub_text(text)
        assert scrub_text(once) == once, f"double scrub diverged for {token!r}"


@pytest.mark.parametrize("text, expected", [
    ('message="api_key=synthetic-credential" status=ok',
     'message="api_key=[redacted]" status=ok'),
    ('{"message": "upstream password=synthetic-credential"}',
     '{"message": "upstream password=[redacted]"}'),
    ('message="prefix apiKey=\'synthetic credential\' suffix"',
     'message="prefix apiKey=\'[redacted]\' suffix"'),
    ("message=api_key=synthetic-credential", "message=api_key=[redacted]"),
    ("context: detail: api_key=synthetic-credential", "context: detail: api_key=[redacted]"),
    ('message="api_key=synthetic-one auth_token=synthetic-two"',
     'message="api_key=[redacted] auth_token=[redacted]"'),
])
def test_secret_assignments_inside_benign_values(text: str, expected: str) -> None:
    assert scrub_text(text) == expected
    assert scrub_text(expected) == expected


@pytest.mark.parametrize("text, expected", [
    (r'password="synthetic \"quoted\" value" status=ok', 'password="[redacted]" status=ok'),
    ('password="synthetic \'quoted\' value" status=ok', 'password="[redacted]" status=ok'),
    (r"password='synthetic \'quoted\' value' status=ok", "password='[redacted]' status=ok"),
    ('password="synthetic credential\r\nstatus=ok', 'password="[redacted]\r\nstatus=ok'),
    ("password:\r\nstatus=ok", "password:\r\nstatus=ok"),
    ('password="[redacted]suffix"', 'password="[redacted]"'),
])
def test_secret_value_quoting_and_line_boundaries(text: str, expected: str) -> None:
    assert scrub_text(text) == expected
    assert scrub_text(expected) == expected


def test_long_assignment_runs_and_nested_labels() -> None:
    # Large identifier runs and many benign labels must not trigger recursive
    # processing or retry the suffix match from every character in a run.
    ordinary = "a" * 100_000
    labels = "message=" * 2_000
    assert scrub_text(ordinary) == ordinary
    assert scrub_text(labels + "api_key=" + ordinary) == labels + "api_key=[redacted]"


@pytest.mark.parametrize("segment", ["a_", "a.", "定制_", "a-", "a!", "a'"])
def test_long_component_runs_keep_complete_assignment_boundaries(segment: str) -> None:
    prefix = segment * 20_000
    benign_key = prefix + "apiKeyConfigured"
    text = f"--{benign_key}=true"
    assert scrub_text(text) == text
    assert scrub_json({benign_key: True}) == {benign_key: True}

    secret_key = prefix + "api_key"
    assert scrub_text(f"--{secret_key}=synthetic-long-credential") == f"--{secret_key}=[redacted]"
    assert scrub_json({secret_key: "synthetic-long-credential"}) == {secret_key: "[redacted]"}


def test_scrub_json_copies_nested_values_and_retains_metadata_types() -> None:
    original = {
        "providers": [{
            "requiresApiKey": True,
            "apiKeyConfigured": False,
            "apiKeyEnv": "SYNTHETIC_API_KEY",
            "apiKeyEnvPool": ["SYNTHETIC_POOL_A", "SYNTHETIC_POOL_B"],
            "attempts": 2,
            "fraction": 0.25,
            "missing": None,
            "clientAPIKey": "synthetic-secret",
            "Authorization": {"nested": ["synthetic-credential"]},
            "password": 12345,
            "message": 'context="token=synthetic-credential"',
        }],
        "tuple": (False, 0, None, {"app-secret": ["synthetic-one", "synthetic-two"]}),
    }
    before = deepcopy(original)

    scrubbed = scrub_json(original)

    assert original == before
    assert scrubbed["providers"][0] == {
        "requiresApiKey": True,
        "apiKeyConfigured": False,
        "apiKeyEnv": "SYNTHETIC_API_KEY",
        "apiKeyEnvPool": ["SYNTHETIC_POOL_A", "SYNTHETIC_POOL_B"],
        "attempts": 2,
        "fraction": 0.25,
        "missing": None,
        "clientAPIKey": "[redacted]",
        "Authorization": "[redacted]",
        "password": "[redacted]",
        "message": 'context="token=[redacted]"',
    }
    assert scrubbed["tuple"] == [False, 0, None, {"app-secret": "[redacted]"}]
    assert scrub_json(scrubbed) == scrubbed


@pytest.mark.parametrize("value", [True, False, 17, 1.25, None])
@pytest.mark.parametrize("key", [
    "requiresApiKey", "requires_api_key", "apiKeyConfigured", "APIKEYConfigured", "ApiKeyEnv",
])
def test_scrub_json_keeps_metadata_scalar_types(key: str, value) -> None:
    result = scrub_json({"nested": [{key: value}]})["nested"][0][key]
    assert result == value
    assert type(result) is type(value)


@pytest.mark.parametrize("home", [
    PurePosixPath("/home/synthetic"), PureWindowsPath(r"Q:\synthetic-home"),
])
def test_scrub_json_normalizes_paths_before_serialization(home, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    file_path = home / "diagnostics" / "status.json"
    suffix = str(file_path)[len(str(home)):]
    assert scrub_json({"path": file_path, "message": f"loaded {file_path}"}) == {
        "path": "~" + suffix,
        "message": "loaded ~" + suffix,
    }


def test_scrub_json_scrubs_fallback_string_values() -> None:
    class DiagnosticValue:
        def __str__(self) -> str:
            return "password=synthetic-fallback"

    assert scrub_json({"value": DiagnosticValue()}) == {"value": "password=[redacted]"}
