"""
Banco de dados do Coleta Inteligente (RF03 - leituras com data e hora).

Tabelas:
    usuarios          -> gestores que acessam o painel (RF04)
    sessoes           -> sessões de login ativas (guarda só o hash do token)
    tentativas_login  -> tentativas de login com erro, para bloquear força bruta
    lixeiras          -> cadastro das lixeiras (RF05)
    leituras          -> histórico de leituras de cada lixeira (RF03 / RF08)
    configuracoes     -> parâmetros ajustáveis pelo gestor (RN02)

O banco é escolhido pela variável DATABASE_URL:
    postgresql://...          -> PostgreSQL (Neon), usado no Vercel e no modo online
    sqlite:///coleta_local.db -> arquivo SQLite local, usado no modo offline e nos testes

No Vercel, a DATABASE_URL é criada pela integração com o Neon. Para rodar no
computador, `vercel env pull` grava essa variável no arquivo .env.local.
"""

import atexit
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from dotenv import load_dotenv

PASTA_PROJETO = os.path.dirname(os.path.abspath(__file__))

# Variáveis já definidas no ambiente têm prioridade sobre os arquivos .env
load_dotenv(os.path.join(PASTA_PROJETO, ".env.local"))
load_dotenv(os.path.join(PASTA_PROJETO, ".env"))

LIMITE_ALERTA_PADRAO = 80.0  # RN02

# Lixeiras iniciais: lixeira-01 é a única com sensor ESP32 real hoje; as demais
# são simuladas pra demonstrar o conceito com múltiplos pontos.
# Coordenadas em Indaiatuba/SP (OpenStreetMap).
LIXEIRAS_INICIAIS = [
    ("lixeira-01", "Praça Prudente de Moraes (sensor real)", "Praça Prudente de Moraes, Centro, Indaiatuba",
     -23.0880, -47.2164, 100.0, "real"),
    ("lixeira-02", "Praça Rui Barbosa (simulada)", "Praça Rui Barbosa, Cidade Nova I, Indaiatuba",
     -23.0888, -47.2126, 100.0, "simulada"),
    ("lixeira-03", "Parque Ecológico (simulada)", "Parque Ecológico Prefeito Dr. Clain Ferrari, Indaiatuba",
     -23.1043, -47.2281, 100.0, "simulada"),
    ("lixeira-04", "Rodoviária (simulada)", "Rodoviária de Indaiatuba, Rua Embira, Jardim Oliveira Camargo",
     -23.1099, -47.2095, 100.0, "simulada"),
    ("lixeira-05", "Av. Eng. Fábio Roberto Barnabé (simulada)", "Av. Eng. Fábio Roberto Barnabé, Jardim Hubert, Indaiatuba",
     -23.1134, -47.2327, 100.0, "simulada"),
]

# A estrutura é a mesma nos dois bancos; muda só o tipo das colunas de número e de ID.
_TIPOS = {
    "postgres": {"id": "INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY", "real": "DOUBLE PRECISION"},
    "sqlite": {"id": "INTEGER PRIMARY KEY AUTOINCREMENT", "real": "REAL"},
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usuarios (
    id          {id},
    nome        TEXT NOT NULL,
    email       TEXT NOT NULL UNIQUE,
    senha_hash  TEXT NOT NULL,
    criado_em   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessoes (
    token       TEXT PRIMARY KEY,
    usuario_id  INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    expira_em   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tentativas_login (
    id       {id},
    chave    TEXT NOT NULL,
    momento  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tentativas_chave ON tentativas_login (chave, momento);

CREATE TABLE IF NOT EXISTS lixeiras (
    dispositivo_id  TEXT PRIMARY KEY,
    nome            TEXT NOT NULL,
    endereco        TEXT NOT NULL DEFAULT '',
    lat             {real} NOT NULL,
    lng             {real} NOT NULL,
    altura_cm       {real} NOT NULL DEFAULT 100,
    tipo            TEXT NOT NULL DEFAULT 'real' CHECK (tipo IN ('real', 'simulada')),
    criado_em       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leituras (
    id              {id},
    dispositivo_id  TEXT NOT NULL REFERENCES lixeiras(dispositivo_id) ON DELETE CASCADE ON UPDATE CASCADE,
    distancia_cm    {real},
    nivel_ocupacao  {real} NOT NULL,
    registrado_em   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_leituras_lixeira_data ON leituras (dispositivo_id, registrado_em);
CREATE INDEX IF NOT EXISTS idx_leituras_data ON leituras (registrado_em);

CREATE TABLE IF NOT EXISTS configuracoes (
    chave  TEXT PRIMARY KEY,
    valor  TEXT NOT NULL
);
"""


class BancoIndisponivel(RuntimeError):
    """Banco não configurado ou fora do ar (vira resposta 503 na API)."""


def agora_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def url_do_banco() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise BancoIndisponivel(
            "DATABASE_URL não definida. Rode `vercel env pull` (modo online) "
            "ou use DATABASE_URL=sqlite:///coleta_local.db (modo offline)."
        )
    return url


def tipo_do_banco() -> str:
    return "sqlite" if url_do_banco().startswith("sqlite:") else "postgres"


def caminho_sqlite(url: str) -> str:
    caminho = url[len("sqlite:///"):] if url.startswith("sqlite:///") else url[len("sqlite:"):]
    return caminho if os.path.isabs(caminho) else os.path.join(PASTA_PROJETO, caminho)


# --- Conexão ---
#
# As funções do sistema escrevem SQL com o marcador %s (padrão do psycopg).
# A classe abaixo dá a mesma interface para os dois bancos e converte o
# marcador para "?" quando o banco é SQLite.

class Conexao:
    def __init__(self, conn, tipo: str):
        self._conn = conn
        self.tipo = tipo

    def _sql(self, sql: str) -> str:
        return sql.replace("%s", "?") if self.tipo == "sqlite" else sql

    def execute(self, sql: str, parametros=()):
        return self._conn.execute(self._sql(sql), parametros)

    def executemany(self, sql: str, lista_parametros):
        if self.tipo == "sqlite":
            return self._conn.executemany(self._sql(sql), lista_parametros)
        with self._conn.cursor() as cur:
            cur.executemany(sql, lista_parametros)


def _linha_como_dict(cursor, linha):
    return {coluna[0]: valor for coluna, valor in zip(cursor.description, linha)}


_pool = None
_pool_trava = threading.Lock()


def _pool_postgres(url: str):
    """Pool de conexões reaproveitado entre requisições da mesma instância.
    Abrir uma conexão nova com o Neon custa um handshake TLS a cada consulta."""
    global _pool
    if _pool is None:
        with _pool_trava:
            if _pool is None:
                from psycopg.rows import dict_row
                from psycopg_pool import ConnectionPool

                _pool = ConnectionPool(
                    url,
                    min_size=1,
                    max_size=5,
                    max_idle=120,
                    timeout=10,
                    # o pooler do Neon não aceita prepared statements
                    kwargs={"row_factory": dict_row, "prepare_threshold": None, "connect_timeout": 8},
                    check=ConnectionPool.check_connection,
                    open=True,
                )
    return _pool


@contextmanager
def conexao():
    """Abre uma transação: faz commit no fim do bloco `with` ou rollback se der erro."""
    url = url_do_banco()
    if url.startswith("sqlite:"):
        conn = sqlite3.connect(caminho_sqlite(url), timeout=10)
        conn.row_factory = _linha_como_dict
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield Conexao(conn, "sqlite")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return

    import psycopg
    from psycopg_pool import PoolTimeout

    try:
        with _pool_postgres(url).connection() as conn:
            yield Conexao(conn, "postgres")
    except (psycopg.OperationalError, PoolTimeout) as erro:
        # Detalhes ficam no log do servidor; o cliente recebe só "banco indisponível"
        print(f"[banco] falha de conexão: {erro}")
        raise BancoIndisponivel("Não foi possível conectar ao PostgreSQL") from erro


def fechar_conexoes():
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


atexit.register(fechar_conexoes)


def criar_estrutura(conn: Conexao):
    script = _SCHEMA.format(**_TIPOS[conn.tipo])
    if conn.tipo == "sqlite":
        conn._conn.executescript(script)
    else:
        conn.execute(script)


def inicializar_banco(admin_email: str, admin_senha_hash):
    """Cria as tabelas e os dados iniciais. Sem senha de admin, o gestor não é criado."""
    with conexao() as conn:
        criar_estrutura(conn)

        conn.execute(
            "INSERT INTO configuracoes (chave, valor) VALUES ('limite_alerta', %s) ON CONFLICT (chave) DO NOTHING",
            (str(LIMITE_ALERTA_PADRAO),),
        )
        conn.execute(
            "INSERT INTO configuracoes (chave, valor) VALUES ('ultima_simulacao', '') ON CONFLICT (chave) DO NOTHING"
        )

        if conn.execute("SELECT COUNT(*) AS total FROM lixeiras").fetchone()["total"] == 0:
            conn.executemany(
                "INSERT INTO lixeiras (dispositivo_id, nome, endereco, lat, lng, altura_cm, tipo, criado_em) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (dispositivo_id) DO NOTHING",
                [(*l, agora_iso()) for l in LIXEIRAS_INICIAIS],
            )

        if admin_senha_hash and conn.execute("SELECT COUNT(*) AS total FROM usuarios").fetchone()["total"] == 0:
            conn.execute(
                "INSERT INTO usuarios (nome, email, senha_hash, criado_em) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (email) DO NOTHING",
                ("Gestor", admin_email.strip().lower(), admin_senha_hash, agora_iso()),
            )
            return True  # admin criado agora
    return False


def obter_limite_alerta(conn: Conexao = None) -> float:
    if conn is None:
        with conexao() as nova:
            return obter_limite_alerta(nova)
    linha = conn.execute("SELECT valor FROM configuracoes WHERE chave = 'limite_alerta'").fetchone()
    return float(linha["valor"]) if linha else LIMITE_ALERTA_PADRAO


def definir_limite_alerta(valor: float):
    with conexao() as conn:
        conn.execute(
            "INSERT INTO configuracoes (chave, valor) VALUES ('limite_alerta', %s) "
            "ON CONFLICT (chave) DO UPDATE SET valor = excluded.valor",
            (str(valor),),
        )
