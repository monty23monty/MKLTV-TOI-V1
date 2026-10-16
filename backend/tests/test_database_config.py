import pytest
from sqlalchemy.engine import make_url

from toi.database import database_url


@pytest.fixture(autouse=True)
def clean_database_environment(monkeypatch):
    for variable in ("TOI_DATABASE_URL", "TOI_POSTGRES_HOST", "TOI_POSTGRES_PORT", "TOI_POSTGRES_DB",
                     "TOI_POSTGRES_USER", "TOI_POSTGRES_PASSWORD"):
        monkeypatch.delenv(variable, raising=False)


@pytest.fixture
def postgres_environment(monkeypatch):
    settings = {"HOST": "db", "DB": "lightning_toi", "USER": "toi", "PASSWORD": "test-password"}
    for field, value in settings.items():
        monkeypatch.setenv(f"TOI_POSTGRES_{field}", value)
    return settings


def test_native_database_default_is_unchanged():
    assert database_url() == "sqlite:///./data/toi.db"


def test_explicit_database_url_overrides_incomplete_postgres_settings(monkeypatch):
    monkeypatch.setenv("TOI_DATABASE_URL", "sqlite:///./custom.db")
    monkeypatch.setenv("TOI_POSTGRES_HOST", "db")
    monkeypatch.setenv("TOI_POSTGRES_PORT", "invalid")
    assert database_url() == "sqlite:///./custom.db"


def test_postgres_credentials_round_trip_without_url_ambiguity(monkeypatch, postgres_environment):
    password = " @:/?#%$ with spaces žluťoučký "
    username = "operator@rink:one"
    monkeypatch.setenv("TOI_POSTGRES_USER", username)
    monkeypatch.setenv("TOI_POSTGRES_PASSWORD", password)
    url = make_url(database_url())
    assert url.drivername == "postgresql+psycopg"
    assert url.host == "db"
    assert url.port == 5432
    assert url.database == "lightning_toi"
    assert url.username == username
    assert url.password == password


def test_custom_postgres_port(monkeypatch, postgres_environment):
    monkeypatch.setenv("TOI_POSTGRES_PORT", "15432")
    assert make_url(database_url()).port == 15432


@pytest.mark.parametrize("field", ["HOST", "DB", "USER", "PASSWORD"])
@pytest.mark.parametrize("value", [None, "", "   "])
def test_incomplete_postgres_configuration_names_missing_field_without_secrets(
    monkeypatch, postgres_environment, field, value,
):
    if value is None:
        monkeypatch.delenv(f"TOI_POSTGRES_{field}")
    else:
        monkeypatch.setenv(f"TOI_POSTGRES_{field}", value)
    with pytest.raises(ValueError, match=f"TOI_POSTGRES_{field}") as error:
        database_url()
    assert "test-password" not in str(error.value)


@pytest.mark.parametrize("port", ["", "not-a-port", "0", "65536", "-1", "3.14"])
def test_invalid_postgres_port_has_a_clear_safe_error(monkeypatch, postgres_environment, port):
    monkeypatch.setenv("TOI_POSTGRES_PORT", port)
    with pytest.raises(ValueError, match="TOI_POSTGRES_PORT must be an integer between 1 and 65535") as error:
        database_url()
    assert "test-password" not in str(error.value)
