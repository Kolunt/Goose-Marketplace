from ozon_seller_mcp.env_file import load_project_dotenv


def test_dotenv_fills_empty_goose_placeholder(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("WB_API_TOKEN=from-file\n", encoding="utf-8")
    monkeypatch.setenv("OZON_SELLER_ENV_FILE", str(env_file))
    monkeypatch.setenv("WB_API_TOKEN", "")
    load_project_dotenv()
    assert __import__("os").environ["WB_API_TOKEN"] == "from-file"


def test_dotenv_does_not_override_nonempty(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("WB_API_TOKEN=from-file\n", encoding="utf-8")
    monkeypatch.setenv("OZON_SELLER_ENV_FILE", str(env_file))
    monkeypatch.setenv("WB_API_TOKEN", "from-goose")
    load_project_dotenv()
    assert __import__("os").environ["WB_API_TOKEN"] == "from-goose"
