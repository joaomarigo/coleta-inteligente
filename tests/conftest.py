"""
Os testes usam um banco SQLite temporário: não precisam de internet e nunca
tocam no banco online (Neon). As variáveis são definidas antes de importar o
sistema, pois database.py lê os arquivos .env na importação (e não sobrescreve
o que já estiver no ambiente).
"""

import os
import sys
import tempfile

import pytest

PASTA_TESTES = tempfile.mkdtemp(prefix="coleta-testes-")
ARQUIVO_BANCO = os.path.join(PASTA_TESTES, "teste.db")

os.environ["DATABASE_URL"] = "sqlite:///" + ARQUIVO_BANCO.replace("\\", "/")
os.environ["COLETA_ADMIN_EMAIL"] = "gestor@teste.local"
os.environ["COLETA_ADMIN_SENHA"] = "senha-de-teste-123"
os.environ.pop("COLETA_TOKEN_DISPOSITIVO", None)
os.environ.pop("VERCEL", None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

EMAIL = os.environ["COLETA_ADMIN_EMAIL"]
SENHA = os.environ["COLETA_ADMIN_SENHA"]


@pytest.fixture
def cliente():
    """Banco novo a cada teste, com as lixeiras iniciais e o gestor de teste."""
    if os.path.exists(ARQUIVO_BANCO):
        os.remove(ARQUIVO_BANCO)
    main._banco_pronto = False
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def logado(cliente):
    resposta = cliente.post("/api/login", json={"email": EMAIL, "senha": SENHA})
    assert resposta.status_code == 200
    return cliente
