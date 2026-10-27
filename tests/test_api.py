"""Testes da API: leituras do ESP32, login, permissões, cadastro e página pública."""

import pytest

import auth
import database
from conftest import EMAIL, SENHA

ROTAS_PROTEGIDAS = [
    ("get", "/api/me"), ("get", "/api/resumo"), ("get", "/api/lixeiras"),
    ("get", "/api/lixeiras/lixeira-01"), ("get", "/api/lixeiras/lixeira-01/historico"),
    ("get", "/api/alertas"), ("get", "/api/configuracoes"), ("put", "/api/configuracoes"),
    ("get", "/api/rota"), ("get", "/api/leituras"), ("post", "/api/lixeiras"),
    ("put", "/api/lixeiras/lixeira-01"), ("delete", "/api/lixeiras/lixeira-01"),
]


# --- Leituras do ESP32 (RF02 / RF03) ---

def test_esp32_envia_leitura_e_ela_aparece_no_publico(cliente):
    r = cliente.post("/api/leituras", json={"dispositivo_id": "lixeira-01", "distancia_cm": 12.5, "nivel_ocupacao": 82})
    assert r.status_code == 200
    assert r.json()["nivel_ocupacao"] == 82

    real = next(l for l in cliente.get("/api/publico/lixeiras").json()["lixeiras"] if l["dispositivo_id"] == "lixeira-01")
    assert real["nivel_ocupacao"] == 82
    assert real["status"] == "cheia"
    assert real["online"] is True
    assert real["tipo"] == "real"


def test_servidor_calcula_o_nivel_quando_chega_so_a_distancia(cliente):
    r = cliente.post("/api/leituras", json={"dispositivo_id": "lixeira-01", "distancia_cm": 30})
    assert r.json()["nivel_ocupacao"] == 70.0  # altura cadastrada: 100 cm


@pytest.mark.parametrize("corpo", [
    {"dispositivo_id": "lixeira-01", "distancia_cm": -1},
    {"dispositivo_id": "lixeira-01", "nivel_ocupacao": 120},
    {"dispositivo_id": "lixeira-01"},
    {"dispositivo_id": "x" * 61, "distancia_cm": 10},
])
def test_leituras_invalidas_sao_recusadas(cliente, corpo):
    assert cliente.post("/api/leituras", json=corpo).status_code == 422


def test_dispositivo_nao_cadastrado_e_recusado(cliente):
    r = cliente.post("/api/leituras", json={"dispositivo_id": "nao-existe", "distancia_cm": 10})
    assert r.status_code == 404


def test_token_do_dispositivo_quando_configurado(cliente, monkeypatch):
    monkeypatch.setenv("COLETA_TOKEN_DISPOSITIVO", "token-secreto-de-teste")
    corpo = {"dispositivo_id": "lixeira-01", "distancia_cm": 10}
    assert cliente.post("/api/leituras", json=corpo).status_code == 401
    assert cliente.post("/api/leituras", json=corpo, headers={"X-Token-Dispositivo": "errado"}).status_code == 401
    assert cliente.post("/api/leituras", json=corpo, headers={"X-Token-Dispositivo": "token-secreto-de-teste"}).status_code == 200


# --- Login e permissões (RF04 / RNF04) ---

@pytest.mark.parametrize("metodo,rota", ROTAS_PROTEGIDAS)
def test_rotas_do_gestor_exigem_login(cliente, metodo, rota):
    assert getattr(cliente, metodo)(rota).status_code == 401


def test_login_com_senha_errada(cliente):
    r = cliente.post("/api/login", json={"email": EMAIL, "senha": "errada"})
    assert r.status_code == 401
    assert "sessao" not in r.cookies


def test_login_ok_cria_cookie_httponly_e_libera_o_painel(cliente):
    r = cliente.post("/api/login", json={"email": EMAIL.upper(), "senha": SENHA})
    assert r.status_code == 200
    assert "httponly" in r.headers["set-cookie"].lower()
    assert "samesite=lax" in r.headers["set-cookie"].lower()
    assert cliente.get("/api/me").json()["email"] == EMAIL


def test_banco_guarda_so_o_hash_do_token(logado):
    token = logado.cookies.get("sessao")
    with database.conexao() as conn:
        tokens = [l["token"] for l in conn.execute("SELECT token FROM sessoes").fetchall()]
    assert token not in tokens
    assert len(tokens) == 1


def test_logout_encerra_a_sessao(logado):
    assert logado.post("/api/logout").status_code == 200
    assert logado.get("/api/me").status_code == 401


def test_login_bloqueado_depois_de_muitas_tentativas(cliente):
    for _ in range(auth.MAX_TENTATIVAS_IP):
        assert cliente.post("/api/login", json={"email": EMAIL, "senha": "errada"}).status_code == 401
    # Nem a senha certa entra enquanto o bloqueio durar
    assert cliente.post("/api/login", json={"email": EMAIL, "senha": SENHA}).status_code == 429


def test_trocar_senha_do_gestor_encerra_sessoes(logado):
    assert auth.definir_gestor(EMAIL, "nova-senha-456") == "senha atualizada"
    assert logado.get("/api/me").status_code == 401
    assert logado.post("/api/login", json={"email": EMAIL, "senha": "nova-senha-456"}).status_code == 200


# --- Cadastro de lixeiras (RF05) e configurações (RN02) ---

def test_cadastrar_editar_e_remover_lixeira(logado):
    nova = {"dispositivo_id": "lixeira-99", "nome": "Teste", "endereco": "Rua A", "lat": -23.1, "lng": -47.2,
            "altura_cm": 80, "tipo": "real"}
    assert logado.post("/api/lixeiras", json=nova).status_code == 201
    assert logado.post("/api/lixeiras", json=nova).status_code == 409  # ID repetido

    editada = {**nova, "nome": "Teste editada"}
    editada.pop("dispositivo_id")
    assert logado.put("/api/lixeiras/lixeira-99", json=editada).json()["nome"] == "Teste editada"

    assert logado.delete("/api/lixeiras/lixeira-99").status_code == 204
    assert logado.get("/api/lixeiras/lixeira-99").status_code == 404


def test_id_de_dispositivo_com_caracteres_invalidos(logado):
    nova = {"dispositivo_id": "<script>", "nome": "X", "lat": 0, "lng": 0}
    assert logado.post("/api/lixeiras", json=nova).status_code == 422


def test_limite_de_alerta_gera_alertas_rn02(logado):
    logado.post("/api/leituras", json={"dispositivo_id": "lixeira-01", "nivel_ocupacao": 75})
    assert logado.put("/api/configuracoes", json={"limite_alerta": 70}).json()["limite_alerta"] == 70
    alertas = logado.get("/api/alertas").json()
    assert "lixeira-01" in [a["dispositivo_id"] for a in alertas["alertas"]]
    assert logado.put("/api/configuracoes", json={"limite_alerta": 0}).status_code == 422


def test_resumo_rota_e_historico(logado):
    logado.post("/api/leituras", json={"dispositivo_id": "lixeira-01", "nivel_ocupacao": 90})
    resumo = logado.get("/api/resumo").json()
    assert resumo["total"] == 5
    assert resumo["cheias"] >= 1
    rota = logado.get("/api/rota").json()
    assert "lixeira-01" in [p["dispositivo_id"] for p in rota["paradas"]]
    historico = logado.get("/api/lixeiras/lixeira-01/historico?horas=1").json()
    assert historico[-1]["nivel_ocupacao"] == 90


# --- Página pública, diagnóstico e cabeçalhos ---

def test_api_publica_nao_expoe_dados_internos(cliente):
    dados = cliente.get("/api/publico/lixeiras").json()
    assert set(dados) == {"limite_alerta", "lixeiras"}
    assert len(dados["lixeiras"]) == 5
    for lixeira in dados["lixeiras"]:
        assert "altura_cm" not in lixeira and "criado_em" not in lixeira


def test_simulacao_gera_leituras_para_as_simuladas(cliente):
    lixeiras = cliente.get("/api/publico/lixeiras").json()["lixeiras"]
    simuladas = [l for l in lixeiras if l["tipo"] == "simulada"]
    assert len(simuladas) == 4
    assert all(l["atualizado_em"] for l in simuladas)


def test_historico_publico_de_lixeira_inexistente(cliente):
    assert cliente.get("/api/publico/lixeiras/nao-existe/historico").status_code == 404


def test_saude(cliente):
    r = cliente.get("/api/saude")
    assert r.status_code == 200
    assert r.json()["banco"] == "ok"
    assert r.json()["tipo_banco"] == "sqlite"


def test_paginas_e_cabecalhos_de_seguranca(cliente):
    for caminho in ("/", "/painel"):
        r = cliente.get(caminho)
        assert r.status_code == 200
        assert "__VERSAO__" not in r.text
        assert "default-src 'self'" in r.headers["content-security-policy"]
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["x-content-type-options"] == "nosniff"
    assert cliente.get("/api/me").headers["cache-control"] == "no-store"
    assert "max-age" in cliente.get("/static/landing.css").headers["cache-control"]


def test_csrf_formulario_de_outro_site_nao_altera_dados(logado):
    # Um formulário HTML de outro site só consegue enviar text/plain ou form-urlencoded;
    # a API exige JSON, e o cookie SameSite=Lax nem é enviado nesse caso.
    r = logado.put("/api/configuracoes", content='{"limite_alerta": 10}',
                   headers={"Content-Type": "text/plain"})
    assert r.status_code == 422
    assert logado.get("/api/configuracoes").json()["limite_alerta"] == 80


def test_sem_documentacao_automatica_publica(cliente):
    assert cliente.get("/docs").status_code == 404
    assert cliente.get("/openapi.json").status_code == 404


def test_banco_fora_do_ar_responde_503(cliente, monkeypatch):
    cliente.get("/api/saude")  # inicializa o banco

    def banco_fora_do_ar():
        raise database.BancoIndisponivel("teste")

    monkeypatch.setattr("main.conexao", banco_fora_do_ar)
    r = cliente.get("/api/publico/lixeiras")
    assert r.status_code == 503
    assert "indisponível" in r.json()["detail"]
