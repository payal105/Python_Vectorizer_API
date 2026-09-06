"""Authentication, throttling, SSRF policy and the introspection endpoints."""

from __future__ import annotations

import base64

import pytest

from app.config import Settings
from app.main import create_app
from fastapi.testclient import TestClient

from tests.conftest import AUTH, DEMO_ID, DEMO_SECRET, png_bytes

ENDPOINT = "/api/v1/vectorize"


def files(data: bytes):
    return {"image": ("a.png", data, "image/png")}


# --- Authentication ----------------------------------------------------------


def test_credentials_are_required(client, sample_png):
    response = client.post(ENDPOINT, files=files(sample_png))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == 2000
    assert response.headers["www-authenticate"].startswith("Basic")


def test_wrong_secret_is_rejected(client, sample_png):
    response = client.post(ENDPOINT, files=files(sample_png), auth=(DEMO_ID, "nope"))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == 2001


def test_unknown_key_id_is_rejected(client, sample_png):
    response = client.post(ENDPOINT, files=files(sample_png), auth=("ghost", "nope"))
    assert response.status_code == 401


def test_x_api_key_header_works(client, sample_png):
    response = client.post(
        ENDPOINT,
        files=files(sample_png),
        headers={"X-Api-Key": f"{DEMO_ID}:{DEMO_SECRET}"},
    )
    assert response.status_code == 200


def test_bearer_token_works(client, sample_png):
    response = client.post(
        ENDPOINT,
        files=files(sample_png),
        headers={"Authorization": f"Bearer {DEMO_ID}:{DEMO_SECRET}"},
    )
    assert response.status_code == 200


def test_malformed_basic_header_is_rejected(client, sample_png):
    response = client.post(
        ENDPOINT, files=files(sample_png), headers={"Authorization": "Basic !!!not-b64"}
    )
    assert response.status_code == 401


def test_auth_can_be_turned_off(sample_png):
    settings = Settings(require_auth=False, rate_limit_per_minute=0)
    with TestClient(create_app(settings)) as anonymous:
        response = anonymous.post(ENDPOINT, files=files(sample_png))
        assert response.status_code == 200


def test_authorize_button_is_hidden_when_auth_is_off(sample_png):
    settings = Settings(require_auth=False, rate_limit_per_minute=0)
    with TestClient(create_app(settings)) as anonymous:
        schema = anonymous.get("/openapi.json").json()
    assert "securitySchemes" not in schema.get("components", {})
    assert "security" not in schema["paths"][ENDPOINT]["post"]


def test_authorize_button_is_shown_when_auth_is_on(client):
    schema = client.get("/openapi.json").json()
    assert "HTTPBasic" in schema["components"]["securitySchemes"]
    assert schema["paths"][ENDPOINT]["post"]["security"] == [{"HTTPBasic": []}]


# --- Throttling --------------------------------------------------------------


def test_rate_limit_returns_429_with_retry_after(sample_png):
    settings = Settings(
        require_auth=True,
        api_keys=f"{DEMO_ID}:{DEMO_SECRET}",
        rate_limit_per_minute=2,
    )
    with TestClient(create_app(settings)) as limited:
        codes = [
            limited.post(ENDPOINT, files=files(sample_png), auth=AUTH).status_code
            for _ in range(3)
        ]
    assert codes[:2] == [200, 200]
    assert codes[2] == 429


def test_oversized_body_is_refused_before_reading(sample_png):
    # The sample PNG is ~1.3 KB, so a 1 KB ceiling rejects it on Content-Length
    # alone, before any of the body is read.
    settings = Settings(
        require_auth=False, rate_limit_per_minute=0, max_upload_bytes=1024
    )
    with TestClient(create_app(settings)) as tiny:
        response = tiny.post(ENDPOINT, files=files(sample_png))
    assert response.status_code == 413
    assert response.json()["error"]["code"] == 1005


# --- Credit exhaustion -------------------------------------------------------


def test_insufficient_credits_returns_402(sample_png):
    settings = Settings(
        require_auth=True,
        api_keys=f"{DEMO_ID}:{DEMO_SECRET}",
        rate_limit_per_minute=0,
        default_credit_balance=1.0,
    )
    with TestClient(create_app(settings)) as broke:
        assert broke.post(ENDPOINT, files=files(sample_png), auth=AUTH).status_code == 200
        second = broke.post(ENDPOINT, files=files(sample_png), auth=AUTH)
    assert second.status_code == 402
    assert second.json()["error"]["code"] == 2002


def test_free_test_mode_still_works_without_credits(sample_png):
    settings = Settings(
        require_auth=True,
        api_keys=f"{DEMO_ID}:{DEMO_SECRET}",
        rate_limit_per_minute=0,
        default_credit_balance=0.0,
    )
    with TestClient(create_app(settings)) as broke:
        response = broke.post(
            ENDPOINT, files=files(sample_png), data={"mode": "test"}, auth=AUTH
        )
    assert response.status_code == 200


# --- Remote fetch policy -----------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/x.png",
        "http://localhost/x.png",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://10.0.0.5/logo.png",
    ],
)
def test_private_addresses_are_blocked(client, url):
    response = client.post(ENDPOINT, data={"image.url": url}, auth=AUTH)
    assert response.status_code in (403, 400)
    assert response.json()["error"]["code"] in (1008, 1009)


def test_non_http_schemes_are_blocked(client):
    response = client.post(
        ENDPOINT, data={"image.url": "file:///etc/passwd"}, auth=AUTH
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == 1009


def test_url_fetch_can_be_disabled(sample_png):
    settings = Settings(require_auth=False, rate_limit_per_minute=0, allow_url_fetch=False)
    with TestClient(create_app(settings)) as no_fetch:
        response = no_fetch.post(
            ENDPOINT, data={"image.url": "https://example.com/a.png"}
        )
    assert response.status_code == 403


# --- Introspection -----------------------------------------------------------


def test_health(client):
    payload = client.get("/api/v1/health").json()
    assert payload["status"] == "ok"


def test_ready_reports_worker_capacity(client):
    payload = client.get("/api/v1/ready").json()
    assert payload["engine"] == "vtracer"
    assert payload["workers"]["capacity"] >= 1


def test_formats_lists_inputs_and_outputs(client):
    payload = client.get("/api/v1/formats").json()
    assert "PNG" in payload["input"]
    assert set(payload["output"]["vector"]) == {"svg", "pdf", "eps"}
    assert payload["modes"]["test"]["credits"] == 0.0


def test_parameters_documents_every_field(client):
    payload = client.get("/api/v1/parameters").json()
    assert "output.file_format" in payload["parameters"]
    assert "processing.max_colors" in payload["parameters"]
    assert "image.url" in payload["image_inputs"]


def test_account_reports_balance(client, sample_png):
    client.post(ENDPOINT, files=files(sample_png), auth=AUTH)
    payload = client.get("/api/v1/account", auth=AUTH).json()
    assert payload["key_id"] == DEMO_ID
    assert payload["requests"] == 1
    assert payload["credits"]["balance"] < payload["credits"]["balance"] + 1


def test_openapi_documents_the_dotted_form_fields(client):
    schema = client.get("/openapi.json").json()
    body = schema["paths"]["/api/v1/vectorize"]["post"]["requestBody"]
    properties = body["content"]["multipart/form-data"]["schema"]["properties"]
    assert "image" in properties
    assert "output.file_format" in properties
    assert "processing.max_colors" in properties


def test_optional_image_fields_default_to_empty_for_swagger(client):
    """Swagger UI submits its "string" placeholder for any string field that
    has no default, which would look like a second image being supplied."""
    schema = client.get("/openapi.json").json()
    properties = schema["paths"]["/api/v1/vectorize"]["post"]["requestBody"][
        "content"
    ]["multipart/form-data"]["schema"]["properties"]
    assert properties["image.base64"]["default"] == ""
    assert properties["image.url"]["default"] == ""
    # Every other field needs a default for the same reason.
    undefaulted = [
        name
        for name, spec in properties.items()
        if name != "image" and "default" not in spec
    ]
    assert undefaulted == []


def test_every_form_field_renders_in_swagger(client):
    """Swagger UI shows "Could not render Parameters" if a field's default
    does not match its declared type -- an array field with a string default,
    say. Every field must have one concrete type and a matching default."""
    schema = client.get("/openapi.json").json()
    properties = schema["paths"]["/api/v1/vectorize"]["post"]["requestBody"][
        "content"
    ]["multipart/form-data"]["schema"]["properties"]

    broken = []
    for name, spec in properties.items():
        if name == "image":
            continue
        if "anyOf" in spec or spec.get("type") is None:
            broken.append((name, "no single concrete type"))
            continue
        default = spec.get("default")
        expected = {
            "string": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
            "array": list,
        }.get(spec["type"])
        if expected and not isinstance(default, expected):
            broken.append((name, f"{spec['type']} field, default {default!r}"))
    assert broken == []


def test_blank_image_fields_are_treated_as_absent(client, sample_png):
    response = client.post(
        ENDPOINT,
        files=files(sample_png),
        data={"image.base64": "", "image.url": ""},
        auth=AUTH,
    )
    assert response.status_code == 200


def test_errors_share_one_envelope(client):
    response = client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    error = response.json()["error"]
    assert set(error) >= {"status", "code", "message"}
