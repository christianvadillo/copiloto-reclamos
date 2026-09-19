"""`.env` se carga solo, sin pisar el entorno."""

import os

from copiloto.cli import _load_dotenv


def test_dotenv_loads_without_overriding(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("# comentario\nCOPILOTO_X_NUEVA=uno\nCOPILOTO_X_EXISTE=archivo\nCOPILOTO_X_VACIA=\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COPILOTO_X_EXISTE", "entorno")
    monkeypatch.delenv("COPILOTO_X_NUEVA", raising=False)
    monkeypatch.delenv("COPILOTO_X_VACIA", raising=False)
    _load_dotenv()
    assert os.environ["COPILOTO_X_NUEVA"] == "uno"
    assert os.environ["COPILOTO_X_EXISTE"] == "entorno"
    assert "COPILOTO_X_VACIA" not in os.environ
    monkeypatch.delenv("COPILOTO_X_NUEVA")
