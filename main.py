"""
Coleta Inteligente (PPI - UNIMAX)
Backend que recebe leituras do ESP32, guarda o histórico no banco (Neon ou SQLite),
simula lixeiras adicionais (pra demonstrar o conceito com múltiplos pontos) e
serve a página pública e o painel do gestor (login, dashboard, detalhes,
alertas, configurações e mapa).

Publicado no Vercel. Para rodar no computador, veja o README.md:
    python gerenciar.py iniciar            (online, banco Neon)
    python gerenciar.py iniciar --offline  (sem internet, banco SQLite local)

Variáveis de ambiente principais (lista completa no .env.example):
    DATABASE_URL              banco (postgresql://... ou sqlite:///arquivo.db)
    COLETA_ADMIN_EMAIL        e-mail do gestor inicial (padrão: admin@coleta.local)
    COLETA_ADMIN_SENHA        senha do gestor inicial (sem ela o gestor não é criado)
    COLETA_TOKEN_DISPOSITIVO  se definido, o ESP32 precisa enviar esse token
"""

import asyncio
import hmac
import math
import os
import random
from datetime import datetime, timedelta, timezone
from typing import List, Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

import auth
from database import (
    BancoIndisponivel,
    agora_iso,
    conexao,
    definir_limite_alerta,
    inicializar_banco,
    obter_limite_alerta,
    tipo_do_banco,
)

PASTA_STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

DEPOSITO = {"nome": "Central de Triagem", "lat": -23.0990, "lng": -47.2200}  # Indaiatuba/SP

# RN01 - faixas de status por cores (padrão semáforo)
LIMITE_VAZIA = 50.0   # até 50% = vazia (verde)
LIMITE_CHEIA = 80.0   # 80% ou mais = cheia (vermelho); entre os dois = parcial (amarelo)

# RN03 - sensor sem leituras por mais de 10 minutos é considerado offline
OFFLINE_APOS = timedelta(minutes=10)

INTERVALO_SIMULACAO_S = 15
DIAS_RETENCAO_HISTORICO = 7
MAX_PONTOS_HISTORICO = 300


# --- Regras de negócio ---

def status_por_nivel(nivel: float) -> str:
    if nivel >= LIMITE_CHEIA:
        return "cheia"
    if nivel > LIMITE_VAZIA:
        return "parcial"
    return "vazia"


def sensor_online(atualizado_em: Optional[str]) -> bool:
    if not atualizado_em:
        return False
    return datetime.now(timezone.utc) - datetime.fromisoformat(atualizado_em) <= OFFLINE_APOS


def calcular_nivel(distancia_cm: float, altura_cm: float) -> float:
    """RF01 - converte a distância medida pelo HC-SR04 (do sensor até o lixo)
    em percentual de preenchimento, usando a altura útil cadastrada da lixeira."""
    if altura_cm <= 0:
        return 0.0
    return round(max(0.0, min(100.0, (altura_cm - distancia_cm) / altura_cm * 100)), 2)


SQL_LIXEIRAS_COM_ULTIMA_LEITURA = """
    SELECT l.*, u.nivel_ocupacao, u.distancia_cm, u.registrado_em AS atualizado_em
    FROM lixeiras l
    LEFT JOIN leituras u ON u.id = (
        SELECT id FROM leituras WHERE dispositivo_id = l.dispositivo_id
        ORDER BY registrado_em DESC, id DESC LIMIT 1
    )
    ORDER BY l.dispositivo_id
"""


def listar_lixeiras_com_status(conn=None) -> List[dict]:
    """Junta o cadastro de cada lixeira com a última leitura e calcula status.
    Aceita uma conexão já aberta para a rota fazer todas as consultas nela."""
    simular_se_necessario()
    if conn is None:
        with conexao() as nova:
            linhas = nova.execute(SQL_LIXEIRAS_COM_ULTIMA_LEITURA).fetchall()
    else:
        linhas = conn.execute(SQL_LIXEIRAS_COM_ULTIMA_LEITURA).fetchall()

    resultado = []
    for linha in linhas:
        item = dict(linha)
        item["nivel_ocupacao"] = item["nivel_ocupacao"] or 0
        item["status"] = status_por_nivel(item["nivel_ocupacao"]) if item["atualizado_em"] else "sem_dados"
        item["online"] = sensor_online(item["atualizado_em"])
        resultado.append(item)
    return resultado


def buscar_lixeira(dispositivo_id: str) -> dict:
    for lixeira in listar_lixeiras_com_status():
        if lixeira["dispositivo_id"] == dispositivo_id:
            return lixeira
    raise HTTPException(status_code=404, detail="Lixeira não encontrada")


# --- Rota (prévia de evolução futura - RS03) ---

def distancia_haversine_km(a: dict, b: dict) -> float:
    raio_terra_km = 6371.0
    lat1, lng1 = math.radians(a["lat"]), math.radians(a["lng"])
    lat2, lng2 = math.radians(b["lat"]), math.radians(b["lng"])
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return raio_terra_km * 2 * math.asin(math.sqrt(h))


def calcular_rota_otimizada(pontos: List[dict], origem: dict) -> List[dict]:
    """Heurística do vizinho mais próximo: a cada passo, visita o ponto não
    visitado mais próximo da posição atual. Simples e eficiente o bastante
    pra um número pequeno de lixeiras, que é o caso aqui."""
    restantes = pontos.copy()
    rota = []
    atual = origem
    while restantes:
        proximo = min(restantes, key=lambda p: distancia_haversine_km(atual, p))
        rota.append(proximo)
        restantes.remove(proximo)
        atual = proximo
    return rota


# --- Simulação das lixeiras fictícias (a real só muda via POST do ESP32) ---
#
# No Vercel não existe processo rodando em segundo plano, então a simulação
# avança sob demanda: quando o dashboard pede dados e já passou o intervalo,
# as lixeiras simuladas ganham uma nova leitura. O UPDATE condicional garante
# que só uma requisição por intervalo faça isso, mesmo com várias instâncias.

def simular_se_necessario():
    agora = datetime.now(timezone.utc)
    corte_intervalo = (agora - timedelta(seconds=INTERVALO_SIMULACAO_S)).isoformat(timespec="seconds")
    try:
        with conexao() as conn:
            vez = conn.execute(
                "UPDATE configuracoes SET valor = %s WHERE chave = 'ultima_simulacao' AND valor < %s",
                (agora.isoformat(timespec="seconds"), corte_intervalo),
            )
            if vez.rowcount == 0:
                return
            simuladas = conn.execute(
                """
                SELECT l.dispositivo_id,
                       (SELECT nivel_ocupacao FROM leituras WHERE dispositivo_id = l.dispositivo_id
                        ORDER BY registrado_em DESC, id DESC LIMIT 1) AS nivel
                FROM lixeiras l WHERE l.tipo = 'simulada'
                """
            ).fetchall()
            for lixeira in simuladas:
                atual = lixeira["nivel"]
                if atual is None:
                    novo_nivel = random.uniform(10, 60)
                elif atual >= 95:
                    novo_nivel = random.uniform(0, 10)  # simula que o caminhão coletou
                else:
                    novo_nivel = min(100, atual + random.uniform(1, 5))
                conn.execute(
                    "INSERT INTO leituras (dispositivo_id, distancia_cm, nivel_ocupacao, registrado_em) "
                    "VALUES (%s, NULL, %s, %s)",
                    (lixeira["dispositivo_id"], round(novo_nivel, 2), agora_iso()),
                )

            corte = (agora - timedelta(days=DIAS_RETENCAO_HISTORICO)).isoformat(timespec="seconds")
            conn.execute("DELETE FROM leituras WHERE registrado_em < %s", (corte,))
    except Exception as erro:  # a simulação não pode derrubar o dashboard
        print(f"[simulacao] erro: {erro}")


# --- Inicialização do banco (uma vez por instância, na primeira requisição) ---

NO_VERCEL = bool(os.environ.get("VERCEL"))
_banco_pronto = False


def garantir_banco():
    global _banco_pronto
    if _banco_pronto:
        return
    admin_email = os.environ.get("COLETA_ADMIN_EMAIL", "admin@coleta.local")
    # Sem senha padrão: o banco (Neon) é o mesmo no Vercel e no computador local
    admin_senha = os.environ.get("COLETA_ADMIN_SENHA")
    senha_hash = auth.gerar_hash_senha(admin_senha) if admin_senha else None
    if inicializar_banco(admin_email, senha_hash):
        print(f"[coleta] usuário inicial criado: {admin_email}")
    _banco_pronto = True


app = FastAPI(title="Coleta Inteligente", docs_url=None, redoc_url=None, openapi_url=None)

# Content-Security-Policy: scripts, estilos e fontes só do próprio site (as
# bibliotecas ficam em static/vendor). De fora, só os blocos do mapa
# (OpenStreetMap) e o traçado da rota (OSRM).
CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: https://*.tile.openstreetmap.org",
    "font-src 'self'",
    "connect-src 'self' https://router.project-osrm.org",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])

CABECALHOS_SEGURANCA = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
}
if NO_VERCEL:
    CABECALHOS_SEGURANCA["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

# Arquivos estáticos: o navegador guarda por 10 min e a CDN do Vercel por 1 dia
# (a CDN é limpa a cada novo deploy, então uma alteração publicada aparece logo).
CACHE_ESTATICOS = "public, max-age=600, s-maxage=86400"


def resposta_banco_indisponivel() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"detail": "Banco de dados indisponível. Tente novamente em instantes."},
        headers={"Retry-After": "5", "Cache-Control": "no-store"},
    )


@app.middleware("http")
async def middleware_principal(request: Request, call_next):
    caminho = request.url.path
    if caminho.startswith("/api/") and caminho != "/api/saude":
        try:
            await asyncio.to_thread(garantir_banco)
        except BancoIndisponivel:
            return resposta_banco_indisponivel()

    resposta = await call_next(request)

    for nome, valor in CABECALHOS_SEGURANCA.items():
        resposta.headers.setdefault(nome, valor)
    if caminho.startswith("/static/") and resposta.status_code == 200:
        resposta.headers["Cache-Control"] = CACHE_ESTATICOS
    elif caminho.startswith("/api/") and "cache-control" not in resposta.headers:
        resposta.headers["Cache-Control"] = "no-store"
    return resposta


@app.exception_handler(BancoIndisponivel)
async def tratar_banco_indisponivel(request: Request, erro: BancoIndisponivel):
    return resposta_banco_indisponivel()


app.mount("/static", StaticFiles(directory=PASTA_STATIC), name="static")


# --- Modelos ---

class LeituraSensor(BaseModel):
    dispositivo_id: str = Field(default="lixeira-01", min_length=1, max_length=60)
    distancia_cm: Optional[float] = Field(default=None, ge=0, le=1000)
    nivel_ocupacao: Optional[float] = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def exige_distancia_ou_nivel(self):
        if self.distancia_cm is None and self.nivel_ocupacao is None:
            raise ValueError("Informe distancia_cm ou nivel_ocupacao")
        return self


class Credenciais(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    senha: str = Field(min_length=1, max_length=200)


class LixeiraEntrada(BaseModel):
    nome: str = Field(min_length=1, max_length=120)
    endereco: str = Field(default="", max_length=200)
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    altura_cm: float = Field(default=100, gt=0, le=1000)
    tipo: Literal["real", "simulada"] = "real"


class LixeiraNova(LixeiraEntrada):
    dispositivo_id: str = Field(min_length=1, max_length=60, pattern=r"^[A-Za-z0-9_\-]+$")


class Configuracoes(BaseModel):
    limite_alerta: float = Field(ge=1, le=100)


# --- Leituras do ESP32 (RF02 / RF03) ---

def token_dispositivo_valido(token_recebido: Optional[str]) -> bool:
    """Se COLETA_TOKEN_DISPOSITIVO estiver definido, o ESP32 precisa enviar o mesmo
    valor no cabeçalho X-Token-Dispositivo. Sem a variável, o envio é aberto."""
    esperado = os.environ.get("COLETA_TOKEN_DISPOSITIVO", "")
    if not esperado:
        return True
    return hmac.compare_digest((token_recebido or "").encode(), esperado.encode())


@app.post("/api/leituras")
def receber_leitura(leitura: LeituraSensor, x_token_dispositivo: Optional[str] = Header(default=None)):
    if not token_dispositivo_valido(x_token_dispositivo):
        raise HTTPException(status_code=401, detail="Token do dispositivo inválido")

    with conexao() as conn:
        lixeira = conn.execute(
            "SELECT altura_cm FROM lixeiras WHERE dispositivo_id = %s", (leitura.dispositivo_id,)
        ).fetchone()
        if lixeira is None:
            raise HTTPException(status_code=404, detail="Lixeira não cadastrada. Cadastre o dispositivo em Configurações.")

        # O firmware atual já envia o nível calculado com a calibração dele;
        # a altura cadastrada só é usada quando chega apenas a distância.
        nivel = leitura.nivel_ocupacao
        if nivel is None:
            nivel = calcular_nivel(leitura.distancia_cm, lixeira["altura_cm"])

        conn.execute(
            "INSERT INTO leituras (dispositivo_id, distancia_cm, nivel_ocupacao, registrado_em) VALUES (%s, %s, %s, %s)",
            (leitura.dispositivo_id, leitura.distancia_cm, nivel, agora_iso()),
        )
    return {"status": "ok", "nivel_ocupacao": nivel}


@app.get("/api/leituras")
def listar_leituras(_=Depends(auth.usuario_atual)):
    return [
        {k: l[k] for k in ("dispositivo_id", "distancia_cm", "nivel_ocupacao", "atualizado_em")}
        for l in listar_lixeiras_com_status() if l["atualizado_em"]
    ]


# --- Autenticação (RF04 / RNF04) ---

@app.post("/api/login")
def login(credenciais: Credenciais, request: Request, response: Response):
    ip = auth.ip_do_cliente(request)
    if auth.login_bloqueado(credenciais.email, ip):
        raise HTTPException(
            status_code=429,
            detail="Muitas tentativas de login. Aguarde 15 minutos e tente novamente.",
        )
    usuario = auth.autenticar(credenciais.email, credenciais.senha)
    if usuario is None:
        auth.registrar_falha_login(credenciais.email, ip)
        raise HTTPException(status_code=401, detail="E-mail ou senha inválidos")
    token = auth.criar_sessao(usuario["id"])
    response.set_cookie(
        auth.COOKIE_SESSAO, token,
        httponly=True, samesite="lax", secure=NO_VERCEL,
        max_age=int(auth.DURACAO_SESSAO.total_seconds()),
    )
    return {"nome": usuario["nome"], "email": usuario["email"]}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(auth.COOKIE_SESSAO)
    if token:
        auth.encerrar_sessao(token)
    response.delete_cookie(auth.COOKIE_SESSAO)
    return {"status": "ok"}


@app.get("/api/me")
def quem_sou_eu(usuario=Depends(auth.usuario_atual)):
    return usuario


# --- Dashboard (RF06) ---

def lixeiras_e_limite():
    """Lixeiras com status e o limite de alerta, lidos na mesma conexão."""
    with conexao() as conn:
        return listar_lixeiras_com_status(conn), obter_limite_alerta(conn)


@app.get("/api/resumo")
def resumo(_=Depends(auth.usuario_atual)):
    lixeiras, limite = lixeiras_e_limite()
    return {
        "total": len(lixeiras),
        "cheias": sum(1 for l in lixeiras if l["status"] == "cheia"),
        "parciais": sum(1 for l in lixeiras if l["status"] == "parcial"),
        "vazias": sum(1 for l in lixeiras if l["status"] == "vazia"),
        "offline": sum(1 for l in lixeiras if not l["online"]),
        "alertas": sum(1 for l in lixeiras if l["nivel_ocupacao"] >= limite),
        "limite_alerta": limite,
        "lixeiras": lixeiras,
    }


# --- Lixeiras: consulta e cadastro (RF05) ---

@app.get("/api/lixeiras")
def listar_lixeiras(_=Depends(auth.usuario_atual)):
    return listar_lixeiras_com_status()


@app.get("/api/lixeiras/{dispositivo_id}")
def detalhar_lixeira(dispositivo_id: str, _=Depends(auth.usuario_atual)):
    return buscar_lixeira(dispositivo_id)


@app.post("/api/lixeiras", status_code=201)
def cadastrar_lixeira(dados: LixeiraNova, _=Depends(auth.usuario_atual)):
    with conexao() as conn:
        if conn.execute("SELECT 1 FROM lixeiras WHERE dispositivo_id = %s", (dados.dispositivo_id,)).fetchone():
            raise HTTPException(status_code=409, detail="Já existe uma lixeira com esse ID de dispositivo")
        conn.execute(
            "INSERT INTO lixeiras (dispositivo_id, nome, endereco, lat, lng, altura_cm, tipo, criado_em) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (dados.dispositivo_id, dados.nome.strip(), dados.endereco.strip(), dados.lat, dados.lng,
             dados.altura_cm, dados.tipo, agora_iso()),
        )
    return buscar_lixeira(dados.dispositivo_id)


@app.put("/api/lixeiras/{dispositivo_id}")
def editar_lixeira(dispositivo_id: str, dados: LixeiraEntrada, _=Depends(auth.usuario_atual)):
    with conexao() as conn:
        cursor = conn.execute(
            "UPDATE lixeiras SET nome = %s, endereco = %s, lat = %s, lng = %s, altura_cm = %s, tipo = %s "
            "WHERE dispositivo_id = %s",
            (dados.nome.strip(), dados.endereco.strip(), dados.lat, dados.lng, dados.altura_cm, dados.tipo,
             dispositivo_id),
        )
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Lixeira não encontrada")
    return buscar_lixeira(dispositivo_id)


@app.delete("/api/lixeiras/{dispositivo_id}", status_code=204)
def remover_lixeira(dispositivo_id: str, _=Depends(auth.usuario_atual)):
    with conexao() as conn:
        cursor = conn.execute("DELETE FROM lixeiras WHERE dispositivo_id = %s", (dispositivo_id,))
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Lixeira não encontrada")
    return Response(status_code=204)


# --- Histórico (RF08) ---

@app.get("/api/lixeiras/{dispositivo_id}/historico")
def obter_historico(dispositivo_id: str, horas: int = 24, _=Depends(auth.usuario_atual)):
    return historico_lixeira(dispositivo_id, horas)


def historico_lixeira(dispositivo_id: str, horas: int) -> List[dict]:
    horas = max(1, min(horas, 24 * DIAS_RETENCAO_HISTORICO))
    desde = (datetime.now(timezone.utc) - timedelta(hours=horas)).isoformat(timespec="seconds")
    with conexao() as conn:
        if not conn.execute("SELECT 1 FROM lixeiras WHERE dispositivo_id = %s", (dispositivo_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Lixeira não encontrada")
        linhas = conn.execute(
            "SELECT nivel_ocupacao, distancia_cm, registrado_em FROM leituras "
            "WHERE dispositivo_id = %s AND registrado_em >= %s ORDER BY registrado_em, id",
            (dispositivo_id, desde),
        ).fetchall()

    # Reduz a quantidade de pontos pra o gráfico continuar leve
    passo = max(1, math.ceil(len(linhas) / MAX_PONTOS_HISTORICO))
    amostra = linhas[::passo]
    if linhas and amostra[-1] is not linhas[-1]:
        amostra.append(linhas[-1])
    return [dict(l) for l in amostra]


# --- Alertas (RF07 / RN02) ---

@app.get("/api/alertas")
def listar_alertas(_=Depends(auth.usuario_atual)):
    lixeiras, limite = lixeiras_e_limite()
    alertas = [l for l in lixeiras if l["nivel_ocupacao"] >= limite]
    alertas.sort(key=lambda l: l["nivel_ocupacao"], reverse=True)  # mais cheia = maior prioridade
    return {"limite_alerta": limite, "alertas": alertas}


# --- Configurações (RN02) ---

@app.get("/api/configuracoes")
def ler_configuracoes(_=Depends(auth.usuario_atual)):
    return {"limite_alerta": obter_limite_alerta()}


@app.put("/api/configuracoes")
def salvar_configuracoes(config: Configuracoes, _=Depends(auth.usuario_atual)):
    definir_limite_alerta(config.limite_alerta)
    return {"limite_alerta": obter_limite_alerta()}


# --- Mapa e rota ---

@app.get("/api/rota")
def obter_rota(_=Depends(auth.usuario_atual)):
    lixeiras, limite = lixeiras_e_limite()
    precisam_coleta = [l for l in lixeiras if l["nivel_ocupacao"] >= limite]

    origem = {"lat": DEPOSITO["lat"], "lng": DEPOSITO["lng"]}
    rota_ordenada = calcular_rota_otimizada(precisam_coleta, origem)

    distancia_total_km = 0.0
    anterior = origem
    for parada in rota_ordenada:
        distancia_total_km += distancia_haversine_km(anterior, parada)
        anterior = parada

    return {
        "deposito": DEPOSITO,
        "limite_coleta": limite,
        "paradas": rota_ordenada,
        "distancia_total_km": round(distancia_total_km, 2),
    }


# --- Acesso público (landing page da feira) ---
# Somente leitura e sem login: mostra o status das lixeiras para qualquer visitante.
# Cadastro, configurações, alertas e rota continuam exigindo login.

CAMPOS_PUBLICOS = (
    "dispositivo_id", "nome", "endereco", "lat", "lng", "tipo",
    "nivel_ocupacao", "distancia_cm", "status", "online", "atualizado_em",
)
# A CDN do Vercel reaproveita a resposta por 3 s: muitos celulares na feira geram uma consulta ao banco
CACHE_PUBLICO = "public, max-age=0, s-maxage=3, stale-while-revalidate=10"


@app.get("/api/publico/lixeiras")
def lixeiras_publicas(response: Response):
    response.headers["Cache-Control"] = CACHE_PUBLICO
    lixeiras, limite = lixeiras_e_limite()
    return {
        "limite_alerta": limite,
        "lixeiras": [{k: l[k] for k in CAMPOS_PUBLICOS} for l in lixeiras],
    }


@app.get("/api/publico/lixeiras/{dispositivo_id}/historico")
def historico_publico(dispositivo_id: str, response: Response, horas: int = 1):
    response.headers["Cache-Control"] = CACHE_PUBLICO
    return historico_lixeira(dispositivo_id, min(horas, 24))


# --- Diagnóstico ---

@app.get("/api/saude")
def saude():
    """Verificação rápida para a feira: o servidor e o banco estão respondendo?
    Não exige login e não mostra dados sensíveis."""
    resultado = {"servidor": "ok", "banco": "erro", "tipo_banco": None, "ultima_leitura_sensor_real": None}
    try:
        resultado["tipo_banco"] = tipo_do_banco()
        garantir_banco()
        with conexao() as conn:
            linha = conn.execute(
                "SELECT MAX(u.registrado_em) AS ultima FROM leituras u "
                "JOIN lixeiras l ON l.dispositivo_id = u.dispositivo_id WHERE l.tipo = 'real'"
            ).fetchone()
        resultado["banco"] = "ok"
        resultado["ultima_leitura_sensor_real"] = linha["ultima"]
    except BancoIndisponivel:
        return JSONResponse(status_code=503, content=resultado, headers={"Cache-Control": "no-store"})
    return resultado


# --- Front-end ---
#
# As páginas citam CSS/JS com "?v=__VERSAO__". A versão muda a cada deploy (ou
# quando um arquivo de static/ muda), então o navegador nunca usa um arquivo
# antigo guardado em cache depois de uma atualização.

def _versao_estaticos() -> str:
    deploy = os.environ.get("VERCEL_DEPLOYMENT_ID") or os.environ.get("VERCEL_GIT_COMMIT_SHA")
    if deploy:
        return deploy[-12:]
    mais_recente = max(
        os.path.getmtime(os.path.join(raiz, nome))
        for raiz, _, arquivos in os.walk(PASTA_STATIC) for nome in arquivos
    )
    return str(int(mais_recente))


VERSAO_ESTATICOS = _versao_estaticos()
_paginas = {}


def pagina(arquivo: str) -> HTMLResponse:
    if arquivo not in _paginas:
        with open(os.path.join(PASTA_STATIC, arquivo), encoding="utf-8") as f:
            _paginas[arquivo] = f.read().replace("__VERSAO__", VERSAO_ESTATICOS)
    return HTMLResponse(_paginas[arquivo], headers={"Cache-Control": "no-cache"})


@app.get("/")
def landing():
    return pagina("landing.html")


@app.get("/painel")
def dashboard():
    return pagina("index.html")
