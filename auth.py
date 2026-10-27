"""
Autenticação do gestor (RF04) com senha criptografada (RNF04).

Senhas: PBKDF2-HMAC-SHA256 com salt aleatório (biblioteca padrão do Python).
Sessão: token aleatório enviado em cookie HttpOnly; o banco guarda só o hash
SHA-256 do token, então um vazamento da tabela não permite entrar no painel.
Força bruta: depois de várias senhas erradas em pouco tempo (por IP ou por
e-mail), o login fica bloqueado por alguns minutos.
"""

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request

from database import agora_iso, conexao

ITERACOES = 200_000
COOKIE_SESSAO = "sessao"
DURACAO_SESSAO = timedelta(hours=12)

# Por IP o limite é baixo; por e-mail é mais alto, para que alguém tentando
# senhas no e-mail do gestor não consiga bloquear o gestor com facilidade.
MAX_TENTATIVAS_IP = 8
MAX_TENTATIVAS_EMAIL = 25
JANELA_TENTATIVAS = timedelta(minutes=15)


def gerar_hash_senha(senha: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", senha.encode(), salt, ITERACOES)
    return f"pbkdf2_sha256${ITERACOES}${salt.hex()}${digest.hex()}"


def verificar_senha(senha: str, senha_hash: str) -> bool:
    try:
        _, iteracoes, salt_hex, digest_hex = senha_hash.split("$")
        digest = hashlib.pbkdf2_hmac("sha256", senha.encode(), bytes.fromhex(salt_hex), int(iteracoes))
    except ValueError:
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


# Usado quando o e-mail não existe, para a resposta demorar o mesmo tempo
# e não revelar quais e-mails estão cadastrados.
_HASH_FICTICIO = gerar_hash_senha(secrets.token_hex(16))


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _momento(delta: timedelta = timedelta(0)) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat(timespec="seconds")


# --- Limite de tentativas de login ---

def _chaves_tentativa(email: str, ip: str):
    return [(f"ip:{ip}", MAX_TENTATIVAS_IP), (f"email:{email.strip().lower()}", MAX_TENTATIVAS_EMAIL)]


def login_bloqueado(email: str, ip: str) -> bool:
    desde = _momento(-JANELA_TENTATIVAS)
    with conexao() as conn:
        for chave, limite in _chaves_tentativa(email, ip):
            total = conn.execute(
                "SELECT COUNT(*) AS total FROM tentativas_login WHERE chave = %s AND momento >= %s",
                (chave, desde),
            ).fetchone()["total"]
            if total >= limite:
                return True
    return False


def registrar_falha_login(email: str, ip: str):
    with conexao() as conn:
        conn.execute("DELETE FROM tentativas_login WHERE momento < %s", (_momento(-JANELA_TENTATIVAS),))
        conn.executemany(
            "INSERT INTO tentativas_login (chave, momento) VALUES (%s, %s)",
            [(chave, agora_iso()) for chave, _ in _chaves_tentativa(email, ip)],
        )


def ip_do_cliente(request: Request) -> str:
    # No Vercel, o IP real do visitante vem no primeiro item do X-Forwarded-For
    encaminhado = request.headers.get("x-forwarded-for", "")
    if encaminhado:
        return encaminhado.split(",")[0].strip()
    return request.client.host if request.client else "desconhecido"


# --- Login e sessão ---

def autenticar(email: str, senha: str):
    with conexao() as conn:
        usuario = conn.execute(
            "SELECT id, nome, email, senha_hash FROM usuarios WHERE email = %s", (email.strip().lower(),)
        ).fetchone()
    if usuario is None:
        verificar_senha(senha, _HASH_FICTICIO)
        return None
    if not verificar_senha(senha, usuario["senha_hash"]):
        return None
    return usuario


def criar_sessao(usuario_id: int) -> str:
    token = secrets.token_urlsafe(32)
    with conexao() as conn:
        conn.execute("DELETE FROM sessoes WHERE expira_em < %s", (_momento(),))
        conn.execute(
            "INSERT INTO sessoes (token, usuario_id, expira_em) VALUES (%s, %s, %s)",
            (_hash_token(token), usuario_id, _momento(DURACAO_SESSAO)),
        )
    return token


def encerrar_sessao(token: str):
    with conexao() as conn:
        conn.execute("DELETE FROM sessoes WHERE token = %s", (_hash_token(token),))


def usuario_atual(request: Request) -> dict:
    """Dependência do FastAPI: bloqueia a rota se não houver sessão válida."""
    token = request.cookies.get(COOKIE_SESSAO)
    if token:
        with conexao() as conn:
            linha = conn.execute(
                "SELECT u.id, u.nome, u.email, s.expira_em FROM sessoes s "
                "JOIN usuarios u ON u.id = s.usuario_id WHERE s.token = %s",
                (_hash_token(token),),
            ).fetchone()
        if linha and linha["expira_em"] > _momento():
            return {"id": linha["id"], "nome": linha["nome"], "email": linha["email"]}
    raise HTTPException(status_code=401, detail="Não autenticado")


def definir_gestor(email: str, senha: str, nome: str = "Gestor") -> str:
    """Cria o gestor ou troca a senha dele (usado pelo gerenciar.py). Encerra as sessões antigas."""
    email = email.strip().lower()
    senha_hash = gerar_hash_senha(senha)
    with conexao() as conn:
        existente = conn.execute("SELECT id FROM usuarios WHERE email = %s", (email,)).fetchone()
        if existente:
            conn.execute("UPDATE usuarios SET senha_hash = %s WHERE id = %s", (senha_hash, existente["id"]))
            conn.execute("DELETE FROM sessoes WHERE usuario_id = %s", (existente["id"],))
            return "senha atualizada"
        conn.execute(
            "INSERT INTO usuarios (nome, email, senha_hash, criado_em) VALUES (%s, %s, %s, %s)",
            (nome, email, senha_hash, agora_iso()),
        )
        return "gestor criado"
