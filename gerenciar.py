"""
Ferramentas de linha de comando do Coleta Inteligente.

    python gerenciar.py verificar [--offline]        confere se está tudo pronto para rodar
    python gerenciar.py iniciar [--offline] [--porta 8000]
                                                     inicia o servidor (acessível na rede local)
    python gerenciar.py gestor --email EMAIL [--offline]
                                                     cria o gestor ou troca a senha dele
    python gerenciar.py demo [--offline] [--sim]     reinicia os dados das lixeiras simuladas
    python gerenciar.py preparar-offline [--sim]     copia o banco online (Neon) para o arquivo local
    python gerenciar.py backup [--offline]           exporta lixeiras, configurações e leituras em JSON

--offline usa o banco SQLite local (coleta_local.db) em vez do Neon.
Detalhes e exemplos no README.md.
"""

import argparse
import getpass
import importlib
import json
import os
import random
import socket
import sys
from datetime import datetime, timedelta, timezone

PASTA = os.path.dirname(os.path.abspath(__file__))
ARQUIVO_OFFLINE = "coleta_local.db"
URL_OFFLINE = f"sqlite:///{ARQUIVO_OFFLINE}"
VERSAO_MINIMA_PYTHON = (3, 10)
DEPENDENCIAS = ["fastapi", "uvicorn", "psycopg", "psycopg_pool", "dotenv", "pydantic"]
ARQUIVOS_OBRIGATORIOS = [
    "static/landing.html", "static/index.html", "static/app.js", "static/landing.js",
    "static/vendor/leaflet/leaflet.js", "static/vendor/chart.umd.min.js", "static/vendor/fonts/fonts.css",
]

sys.path.insert(0, PASTA)
os.chdir(PASTA)


def ok(msg):
    print(f"  [OK]    {msg}")


def aviso(msg):
    print(f"  [AVISO] {msg}")


def erro(msg):
    print(f"  [ERRO]  {msg}")


def usar_offline():
    # Precisa ser definido antes de importar database.py (que lê os arquivos .env)
    os.environ["DATABASE_URL"] = URL_OFFLINE


def confirmar(pergunta: str, ja_confirmado: bool) -> bool:
    if ja_confirmado:
        return True
    resposta = input(f"{pergunta} Digite 'sim' para continuar: ").strip().lower()
    return resposta == "sim"


def ips_da_rede():
    """IPs deste computador; o primeiro é o da interface usada para sair da rede
    (normalmente o Wi-Fi). Os demais costumam ser adaptadores virtuais (WSL, VPN)."""
    ips = []
    try:
        # Não envia nada: só descobre qual interface seria usada para sair da rede
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ips.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return ips


def descrever_banco():
    import database
    tipo = database.tipo_do_banco()
    if tipo == "sqlite":
        return f"SQLite local ({database.caminho_sqlite(database.url_do_banco())})"
    return "PostgreSQL online (Neon)"


# --- verificar ---

def cmd_verificar(args):
    falhas = 0
    print("\nPython e dependências")
    if sys.version_info >= VERSAO_MINIMA_PYTHON:
        ok(f"Python {sys.version.split()[0]}")
    else:
        erro(f"Python {sys.version.split()[0]}: é preciso 3.10 ou mais novo")
        falhas += 1
    for modulo in DEPENDENCIAS:
        try:
            importlib.import_module(modulo)
            ok(f"pacote {modulo}")
        except ImportError:
            erro(f"pacote {modulo} não instalado: rode  pip install -r requirements.txt")
            falhas += 1

    print("\nArquivos do site")
    for caminho in ARQUIVOS_OBRIGATORIOS:
        if os.path.exists(os.path.join(PASTA, caminho)):
            ok(caminho)
        else:
            erro(f"{caminho} não encontrado")
            falhas += 1

    print("\nBanco de dados")
    if args.offline:
        usar_offline()
    try:
        import database
        print(f"  usando: {descrever_banco()}")
        if database.tipo_do_banco() == "sqlite" and not os.path.exists(database.caminho_sqlite(database.url_do_banco())):
            aviso("o arquivo do banco local ainda não existe; ele é criado ao iniciar o servidor "
                  "(ou rode  python gerenciar.py preparar-offline  para copiar os dados do Neon)")
        with database.conexao() as conn:
            database.criar_estrutura(conn)
            total_lixeiras = conn.execute("SELECT COUNT(*) AS n FROM lixeiras").fetchone()["n"]
            total_gestores = conn.execute("SELECT COUNT(*) AS n FROM usuarios").fetchone()["n"]
            ultima = conn.execute(
                "SELECT MAX(u.registrado_em) AS ultima FROM leituras u "
                "JOIN lixeiras l ON l.dispositivo_id = u.dispositivo_id WHERE l.tipo = 'real'"
            ).fetchone()["ultima"]
        ok("conexão com o banco")
        (ok if total_lixeiras else aviso)(f"{total_lixeiras} lixeira(s) cadastrada(s)"
                                          + ("" if total_lixeiras else " (são criadas ao iniciar o servidor)"))
        if total_gestores:
            ok(f"{total_gestores} gestor(es) cadastrado(s)")
        else:
            aviso("nenhum gestor cadastrado: rode  python gerenciar.py gestor --email SEU_EMAIL"
                  + (" --offline" if args.offline else ""))
        if ultima:
            idade = datetime.now(timezone.utc) - datetime.fromisoformat(ultima)
            minutos = int(idade.total_seconds() // 60)
            (ok if minutos <= 10 else aviso)(f"última leitura do sensor real: há {minutos} min"
                                             + ("" if minutos <= 10 else " (ESP32 desligado ou sem rede?)"))
        else:
            aviso("o sensor real ainda não enviou nenhuma leitura para este banco")
    except Exception as e:  # noqa: BLE001 - o objetivo é relatar qualquer falha
        erro(f"{type(e).__name__}: {e}")
        falhas += 1

    print("\nRede")
    ips = ips_da_rede()
    if ips:
        ok(f"IP principal deste computador: {ips[0]}"
           + (f"  (outros: {', '.join(ips[1:])})" if ips[1:] else ""))
    else:
        aviso("sem rede local detectada")

    print("\nResultado: " + ("tudo pronto." if not falhas else f"{falhas} problema(s) encontrado(s)."))
    return 1 if falhas else 0


# --- iniciar ---

def cmd_iniciar(args):
    if args.offline:
        usar_offline()
    import uvicorn
    import database

    try:
        banco = descrever_banco()
    except database.BancoIndisponivel as e:
        erro(str(e))
        return 1

    print("\nColeta Inteligente")
    print(f"  Banco: {banco}")
    print(f"  Neste computador:    http://localhost:{args.porta}")
    print(f"  Painel do gestor:    http://localhost:{args.porta}/painel")
    ips = ips_da_rede()
    if ips:
        print(f"  Na mesma rede Wi-Fi: http://{ips[0]}:{args.porta}")
        print(f"  Servidor no portal do ESP32: {ips[0]}:{args.porta}")
        if ips[1:]:
            print(f"  (outros IPs, se o principal não funcionar: {', '.join(ips[1:])})")
    print("  Para parar: Ctrl+C\n")
    uvicorn.run("main:app", host="0.0.0.0", port=args.porta, log_level="info")
    return 0


# --- gestor ---

def cmd_gestor(args):
    if args.offline:
        usar_offline()
    import auth
    import main  # noqa: F401 - garante que as tabelas existem

    main.garantir_banco()
    senha = getpass.getpass("Nova senha (mínimo 8 caracteres): ")
    if len(senha) < 8:
        erro("a senha precisa ter pelo menos 8 caracteres")
        return 1
    if getpass.getpass("Repita a senha: ") != senha:
        erro("as senhas não conferem")
        return 1
    resultado = auth.definir_gestor(args.email, senha)
    ok(f"{resultado}: {args.email.strip().lower()} ({descrever_banco()})")
    return 0


# --- demo ---

def cmd_demo(args):
    if args.offline:
        usar_offline()
    import database
    import main

    banco = descrever_banco()
    print(f"Banco: {banco}")
    print("Isto apaga as leituras das lixeiras SIMULADAS e gera um histórico novo de 2 horas.")
    print("Não mexe no sensor real, no cadastro das lixeiras nem nos gestores. O limite de alerta volta a 80%.")
    if not confirmar("Continuar?", args.sim):
        print("Cancelado.")
        return 1

    main.garantir_banco()
    agora = datetime.now(timezone.utc)
    with database.conexao() as conn:
        simuladas = [l["dispositivo_id"] for l in conn.execute(
            "SELECT dispositivo_id FROM lixeiras WHERE tipo = 'simulada' ORDER BY dispositivo_id").fetchall()]
        leituras = []
        for dispositivo in simuladas:
            conn.execute("DELETE FROM leituras WHERE dispositivo_id = %s", (dispositivo,))
            nivel = random.uniform(5, 45)
            for minutos_atras in range(120, -1, -5):
                momento = (agora - timedelta(minutes=minutos_atras)).isoformat(timespec="seconds")
                leituras.append((dispositivo, None, round(nivel, 2), momento))
                nivel = random.uniform(0, 10) if nivel >= 95 else min(100, nivel + random.uniform(1, 6))
        conn.executemany(
            "INSERT INTO leituras (dispositivo_id, distancia_cm, nivel_ocupacao, registrado_em) VALUES (%s, %s, %s, %s)",
            leituras,
        )
        conn.execute("UPDATE configuracoes SET valor = %s WHERE chave = 'limite_alerta'",
                     (str(database.LIMITE_ALERTA_PADRAO),))
        conn.execute("UPDATE configuracoes SET valor = %s WHERE chave = 'ultima_simulacao'",
                     (agora.isoformat(timespec="seconds"),))
    ok(f"{len(simuladas)} lixeira(s) simulada(s) com histórico novo ({len(leituras)} leituras)")
    return 0


# --- preparar-offline ---

TABELAS_COPIA = ["usuarios", "lixeiras", "configuracoes"]


def cmd_preparar_offline(args):
    import database

    try:
        if database.tipo_do_banco() != "postgres":
            raise database.BancoIndisponivel("DATABASE_URL não aponta para o Neon")
    except database.BancoIndisponivel as e:
        erro(f"{e}. Rode  vercel env pull  para baixar a configuração do banco online.")
        return 1

    destino = os.path.join(PASTA, ARQUIVO_OFFLINE)
    print(f"Copia gestores, lixeiras, configurações e as leituras das últimas 24 h do Neon para {ARQUIVO_OFFLINE}.")
    if os.path.exists(destino):
        print(f"O arquivo {ARQUIVO_OFFLINE} já existe e será substituído (o banco online não é alterado).")
    if not confirmar("Continuar?", args.sim):
        print("Cancelado.")
        return 1

    desde = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    with database.conexao() as conn:
        dados = {t: conn.execute(f"SELECT * FROM {t}").fetchall() for t in TABELAS_COPIA}
        dados["leituras"] = conn.execute(
            "SELECT dispositivo_id, distancia_cm, nivel_ocupacao, registrado_em FROM leituras "
            "WHERE registrado_em >= %s ORDER BY registrado_em", (desde,)).fetchall()

    if os.path.exists(destino):
        os.replace(destino, destino + ".anterior")
    url_online = os.environ["DATABASE_URL"]
    usar_offline()
    try:
        with database.conexao() as conn:
            database.criar_estrutura(conn)
            for tabela in TABELAS_COPIA + ["leituras"]:
                linhas = dados[tabela]
                if not linhas:
                    continue
                colunas = list(linhas[0].keys())
                conn.executemany(
                    f"INSERT INTO {tabela} ({', '.join(colunas)}) VALUES ({', '.join(['%s'] * len(colunas))})",
                    [tuple(l[c] for c in colunas) for l in linhas],
                )
    finally:
        os.environ["DATABASE_URL"] = url_online
    for tabela in TABELAS_COPIA + ["leituras"]:
        ok(f"{tabela}: {len(dados[tabela])} registro(s)")
    print("\nBanco offline pronto. Inicie com:  python gerenciar.py iniciar --offline")
    return 0


# --- backup ---

def cmd_backup(args):
    if args.offline:
        usar_offline()
    import database

    with database.conexao() as conn:
        dados = {
            "gerado_em": database.agora_iso(),
            "banco": database.tipo_do_banco(),
            "lixeiras": conn.execute("SELECT * FROM lixeiras ORDER BY dispositivo_id").fetchall(),
            "configuracoes": conn.execute("SELECT * FROM configuracoes").fetchall(),
            "leituras": conn.execute(
                "SELECT dispositivo_id, distancia_cm, nivel_ocupacao, registrado_em FROM leituras "
                "ORDER BY registrado_em").fetchall(),
        }
    os.makedirs(os.path.join(PASTA, "backups"), exist_ok=True)
    nome = f"backups/backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    with open(os.path.join(PASTA, nome), "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=1)
    ok(f"{nome}: {len(dados['lixeiras'])} lixeiras, {len(dados['leituras'])} leituras "
       "(gestores e sessões não são exportados)")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Ferramentas do Coleta Inteligente")
    sub = parser.add_subparsers(dest="comando", required=True)

    p = sub.add_parser("verificar", help="confere dependências, arquivos e banco")
    p.add_argument("--offline", action="store_true", help="usar o banco SQLite local")
    p.set_defaults(func=cmd_verificar)

    p = sub.add_parser("iniciar", help="inicia o servidor")
    p.add_argument("--offline", action="store_true", help="usar o banco SQLite local (sem internet)")
    p.add_argument("--porta", type=int, default=8000)
    p.set_defaults(func=cmd_iniciar)

    p = sub.add_parser("gestor", help="cria o gestor ou troca a senha")
    p.add_argument("--email", required=True)
    p.add_argument("--offline", action="store_true")
    p.set_defaults(func=cmd_gestor)

    p = sub.add_parser("demo", help="reinicia os dados das lixeiras simuladas")
    p.add_argument("--offline", action="store_true")
    p.add_argument("--sim", action="store_true", help="não pedir confirmação")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("preparar-offline", help="copia o banco online para o arquivo local")
    p.add_argument("--sim", action="store_true", help="não pedir confirmação")
    p.set_defaults(func=cmd_preparar_offline)

    p = sub.add_parser("backup", help="exporta os dados em JSON")
    p.add_argument("--offline", action="store_true")
    p.set_defaults(func=cmd_backup)

    args = parser.parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrompido.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
