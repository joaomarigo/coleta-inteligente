"""Regras de negócio puras (RF01, RN01, RN03) e funções auxiliares."""

from datetime import datetime, timedelta, timezone

import auth
import main


def test_calculo_do_nivel_rf01():
    assert main.calcular_nivel(100, 100) == 0.0      # lixo no fundo: vazia
    assert main.calcular_nivel(30, 100) == 70.0      # exemplo do README
    assert main.calcular_nivel(0, 100) == 100.0
    assert main.calcular_nivel(150, 100) == 0.0      # leitura maior que a lixeira não fica negativa
    assert main.calcular_nivel(10, 0) == 0.0         # altura inválida não divide por zero


def test_faixas_de_status_rn01():
    assert main.status_por_nivel(0) == "vazia"
    assert main.status_por_nivel(50) == "vazia"
    assert main.status_por_nivel(50.1) == "parcial"
    assert main.status_por_nivel(79.9) == "parcial"
    assert main.status_por_nivel(80) == "cheia"
    assert main.status_por_nivel(100) == "cheia"


def test_sensor_offline_depois_de_10_minutos_rn03():
    agora = datetime.now(timezone.utc)
    assert main.sensor_online((agora - timedelta(minutes=9)).isoformat()) is True
    assert main.sensor_online((agora - timedelta(minutes=11)).isoformat()) is False
    assert main.sensor_online(None) is False


def test_rota_visita_o_ponto_mais_proximo_primeiro():
    origem = {"lat": 0.0, "lng": 0.0}
    longe = {"lat": 0.0, "lng": 0.2, "nome": "longe"}
    perto = {"lat": 0.0, "lng": 0.1, "nome": "perto"}
    assert [p["nome"] for p in main.calcular_rota_otimizada([longe, perto], origem)] == ["perto", "longe"]


def test_hash_de_senha():
    h = auth.gerar_hash_senha("minha-senha")
    assert "minha-senha" not in h
    assert auth.verificar_senha("minha-senha", h)
    assert not auth.verificar_senha("outra", h)
    assert not auth.verificar_senha("x", "formato-invalido")
    assert not auth.verificar_senha("x", "pbkdf2_sha256$1$zz$zz")  # hex inválido não derruba o login
