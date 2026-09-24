import json
from pathlib import Path

import pytest

from imageright_mcp.config import REDACTED, ConfigError, load_config, redact_url, resolve_profile


def test_defaults_when_nothing_is_set() -> None:
    config = load_config({})
    assert config.irVersion == "24.x"
    assert config.profile.profile == "24.2"
    assert config.writeMode == "dry-run"
    assert config.surfacePreference == ["rest-v2", "rest-v1", "soap"]
    assert config.restBaseUrl is None
    assert set(config.sources.values()) == {"default"}


def test_env_overrides_file_overrides_default(tmp_path: Path) -> None:
    file = tmp_path / "ir.json"
    file.write_text(
        json.dumps({"irVersion": "7.2", "restBaseUrl": "https://file.example", "writeMode": "deny"})
    )
    config = load_config(
        {
            "IMAGERIGHT_CONFIG_FILE": str(file),
            "IMAGERIGHT_VERSION": "25.1.340",
            "IMAGERIGHT_DRY_RUN": "true",
            "IMAGERIGHT_SURFACE_PREFERENCE": "rest-v1, soap",
        }
    )
    assert config.irVersion == "25.1.340"
    assert config.profile.profile == "25.1"
    assert config.restBaseUrl == "https://file.example"
    assert config.writeMode == "deny"
    assert config.dryRun is True
    assert config.surfacePreference == ["rest-v1", "soap"]
    assert config.sources["irVersion"] == "env"
    assert config.sources["restBaseUrl"] == "file"
    assert config.sources["authMode"] == "default"
    assert config.configFile == str(file)


def test_password_is_redacted_everywhere() -> None:
    secret = "hunter2-very-secret"
    config = load_config(
        {
            "IMAGERIGHT_PASSWORD": secret,
            "IMAGERIGHT_USERNAME": "svc",
            "IMAGERIGHT_REST_BASE_URL": f"https://svc:{secret}@ir.example:8443/ImageRight",
        }
    )
    view = config.redacted()
    assert view["password"] == REDACTED
    assert view["username"] == "svc"
    assert view["restBaseUrl"] == f"https://svc:{REDACTED}@ir.example:8443/ImageRight"
    assert config.sources["password"] == "env"
    assert secret not in json.dumps(view)
    assert secret not in repr(config)
    assert secret not in str(config)


def test_unset_password_is_null() -> None:
    assert load_config({}).redacted()["password"] is None


def test_config_file_rejects_secrets(tmp_path: Path) -> None:
    file = tmp_path / "ir.json"
    file.write_text(json.dumps({"password": "nope"}))
    with pytest.raises(ConfigError, match="must not contain secrets"):
        load_config({"IMAGERIGHT_CONFIG_FILE": str(file)})


def test_config_file_rejects_unknown_keys(tmp_path: Path) -> None:
    file = tmp_path / "ir.json"
    file.write_text(json.dumps({"irVersoin": "24.x"}))
    with pytest.raises(ConfigError, match="unknown keys"):
        load_config({"IMAGERIGHT_CONFIG_FILE": str(file)})


def test_invalid_enum_value_raises() -> None:
    with pytest.raises(ConfigError):
        load_config({"IMAGERIGHT_WRITE_MODE": "yolo"})


def test_invalid_bool_raises() -> None:
    with pytest.raises(ConfigError, match="DRY_RUN"):
        load_config({"IMAGERIGHT_DRY_RUN": "maybe"})


@pytest.mark.parametrize(
    ("version", "profile", "approximated"),
    [
        ("24.x", "24.2", False),
        ("24.2", "24.2", False),
        ("24.2.115", "24.2", False),
        ("25.x", "25.1", False),
        ("25.1.340", "25.1", False),
        ("7.2", "7.2", False),
        ("7.2.1181", "7.2", False),
        ("25.3", "25.1", True),
        ("24.4", "24.2", True),
        ("24.1", "24.2", True),
        ("7.1", None, False),
        ("23.1", None, False),
        ("latest", None, False),
    ],
)
def test_resolve_profile(version: str, profile: str | None, approximated: bool) -> None:
    resolution = resolve_profile(version)
    assert resolution.profile == profile
    assert resolution.approximated is approximated


def test_redact_url_leaves_plain_urls_alone() -> None:
    assert redact_url("https://ir.example/api") == "https://ir.example/api"
    assert redact_url(None) is None


def test_transport_and_auth_settings(tmp_path: Path) -> None:
    file = tmp_path / "ir.json"
    file.write_text(
        json.dumps(
            {
                "extraHeaders": {"X-Tenant-Id": "CORP_TENANT"},
                "secretEnv": {"password": "CORP_PW"},
                "samlTokenCommand": ["get-token", "--quiet"],
                "fileRoots": ["/srv/scans"],
                "timeoutSeconds": 12.5,
            }
        )
    )
    config = load_config(
        {
            "IMAGERIGHT_CONFIG_FILE": str(file),
            "CORP_TENANT": "tenant-1",
            "CORP_PW": "corp-secret-pw",
            "IMAGERIGHT_MAX_RETRIES": "1",
            "IMAGERIGHT_REQUIRE_CONFIRM": "false",
        }
    )
    assert config.password == "corp-secret-pw"
    assert config.extraHeaderValues == {"X-Tenant-Id": "tenant-1"}
    assert config.maxRetries == 1
    assert config.requireConfirm is False
    assert config.timeoutSeconds == 12.5
    assert config.samlTokenCommand == ["get-token", "--quiet"]
    view = json.dumps(config.redacted())
    assert "corp-secret-pw" not in view
    assert "tenant-1" not in view
    assert config.redacted()["extraHeaders"] == {"X-Tenant-Id": {"env": "CORP_TENANT", "set": True}}
    assert set(config.secret_values()) == {"corp-secret-pw", "tenant-1"}


def test_defaults_for_new_settings() -> None:
    config = load_config({})
    assert config.requireConfirm is True
    assert config.maxRetries == 2
    assert config.verifyTls is True
    assert config.redacted()["jwt"] is None


@pytest.mark.parametrize("key", ["jwt", "jwtPrivateKey", "samlToken"])
def test_config_file_rejects_every_secret(tmp_path: Path, key: str) -> None:
    file = tmp_path / "ir.json"
    file.write_text(json.dumps({key: "nope"}))
    with pytest.raises(ConfigError, match="must not contain secrets"):
        load_config({"IMAGERIGHT_CONFIG_FILE": str(file)})


def test_secret_env_only_names_known_secrets() -> None:
    with pytest.raises(ConfigError, match="secretEnv"):
        load_config({"IMAGERIGHT_SECRET_ENV": "apiKey=X"})


@pytest.mark.parametrize(
    ("var", "value"),
    [("IMAGERIGHT_EXTRA_HEADERS", "X-Tenant"), ("IMAGERIGHT_MAX_RETRIES", "lots")],
)
def test_malformed_env_values(var: str, value: str) -> None:
    with pytest.raises(ConfigError):
        load_config({var: value})


def test_strip_userinfo() -> None:
    from imageright_mcp.config import strip_userinfo

    assert strip_userinfo("https://u:p@h.example:8443/IR") == "https://h.example:8443/IR"
    assert strip_userinfo("https://h.example/IR") == "https://h.example/IR"
