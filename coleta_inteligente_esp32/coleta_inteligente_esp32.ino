/*
  Projeto: Coleta Inteligente (PPI - UNIMAX)
  Leitura de distância com HC-SR04 + envio via WiFi para o backend/dashboard

  Conectividade via WiFiManager: ao ligar, o ESP32 tenta conectar na última
  rede salva. Se não conseguir (rede diferente: casa / faculdade / apresentação),
  ele cria um ponto de acesso "ColetaInteligente-Config" pra você configurar o
  WiFi e o endereço do servidor pelo celular, sem precisar recompilar nem
  plugar no computador.

  Para reconfigurar manualmente (ex: trocar de rede, ou só mudar o IP do
  servidor), segure o botão BOOT por uns 3 segundos com o ESP32 JÁ LIGADO e
  rodando normalmente (não precisa resetar nem religar). O Serial Monitor
  avisa quando o portal está prestes a abrir.

  Servidor: no portal, informe o IP:porta do computador (ex: 192.168.0.105:8000)
  para usar o backend local via HTTP, ou o endereço do site publicado no Vercel
  (ex: coleta-inteligente.vercel.app) para enviar via HTTPS pela internet.

  Token do dispositivo (opcional): se o servidor tiver a variável
  COLETA_TOKEN_DISPOSITIVO, informe o mesmo valor no portal. Ele vai no
  cabeçalho X-Token-Dispositivo e impede que outra pessoa envie leituras
  falsas. Com o campo vazio, o envio funciona como antes.

  Pinos do sensor:
    TRIG -> GPIO 32
    ECHO -> GPIO 27 (via divisor de tensão 5V -> 3.3V)
*/

#include <WiFi.h>
#include <WiFiManager.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
#include <Preferences.h>

const int PINO_BOTAO_BOOT = 0; // botão BOOT, usado para forçar reconfiguração

const int PINO_TRIG = 32;
const int PINO_ECHO = 27;

// Ajuste depois de calibrar com a lixeira real
const float DISTANCIA_LIXEIRA_VAZIA_CM = 40.0;
const float DISTANCIA_LIXEIRA_CHEIA_CM = 5.0;

const char* DISPOSITIVO_ID = "lixeira-01";
const unsigned long INTERVALO_ENVIO_MS = 5000;
unsigned long ultimoEnvio = 0;

// Quantas leituras seguidas do botão pressionado (o loop roda ~1x/segundo,
// então 3 equivale a ~3 segundos segurando)
const int LEITURAS_PARA_RECONFIGURAR = 3;
int contadorBotaoPressionado = 0;

Preferences preferencias;
String enderecoServidor; // "192.168.0.105:8000" (local) ou "coleta-inteligente.vercel.app"
String tokenDispositivo; // vazio = servidor sem token
bool precisaSalvarConfig = false;

void aoSalvarConfig() {
  precisaSalvarConfig = true;
}

void setup() {
  Serial.begin(115200);
  pinMode(PINO_TRIG, OUTPUT);
  pinMode(PINO_ECHO, INPUT);
  digitalWrite(PINO_TRIG, LOW);
  pinMode(PINO_BOTAO_BOOT, INPUT_PULLUP);

  preferencias.begin("coleta", false);
  enderecoServidor = preferencias.getString("servidor", "192.168.0.105:8000");
  tokenDispositivo = preferencias.getString("token", "");

  conectarOuAbrirPortal(false);
}

void loop() {
  verificarBotaoDeReconfiguracao();

  float distancia = medirDistanciaCm();

  if (distancia >= 0) {
    float nivel = calcularNivelOcupacao(distancia);

    Serial.print("Distancia: ");
    Serial.print(distancia);
    Serial.print(" cm | Ocupacao: ");
    Serial.print(nivel);
    Serial.println(" %");

    if (millis() - ultimoEnvio >= INTERVALO_ENVIO_MS) {
      enviarLeitura(distancia, nivel);
      ultimoEnvio = millis();
    }
  } else {
    Serial.println("Sem eco (fora de alcance ou erro de leitura)");
  }

  delay(1000);
}

// Detecta o botão BOOT segurado por alguns segundos ENQUANTO o app já está
// rodando (evita a janela instável de detectar o botão só no boot/reset)
void verificarBotaoDeReconfiguracao() {
  if (digitalRead(PINO_BOTAO_BOOT) == LOW) {
    contadorBotaoPressionado++;

    if (contadorBotaoPressionado == 1) {
      Serial.println("Botao BOOT segurado... continue segurando por ~3s para abrir o portal de configuracao.");
    }

    if (contadorBotaoPressionado >= LEITURAS_PARA_RECONFIGURAR) {
      Serial.println("Abrindo portal de configuracao...");
      contadorBotaoPressionado = 0;
      conectarOuAbrirPortal(true);
    }
  } else {
    contadorBotaoPressionado = 0;
  }
}

void conectarOuAbrirPortal(bool forcarPortal) {
  WiFiManager wm;

  // Campo extra no portal pra digitar o servidor: IP:porta local ou o site no Vercel
  WiFiManagerParameter parametroServidor(
    "servidor", "Servidor (ex: 192.168.0.105:8000 ou coleta-inteligente.vercel.app)",
    enderecoServidor.c_str(), 80
  );
  wm.addParameter(&parametroServidor);
  WiFiManagerParameter parametroToken(
    "token", "Token do dispositivo (opcional)", tokenDispositivo.c_str(), 64
  );
  wm.addParameter(&parametroToken);
  wm.setSaveConfigCallback(aoSalvarConfig);
  wm.setConfigPortalTimeout(180); // 3 min tentando configurar antes de desistir

  bool conectado;
  if (forcarPortal) {
    conectado = wm.startConfigPortal("ColetaInteligente-Config");
  } else {
    conectado = wm.autoConnect("ColetaInteligente-Config");
  }

  if (!conectado) {
    Serial.println("Nao foi possivel conectar. Reiniciando...");
    delay(2000);
    ESP.restart();
  }

  if (precisaSalvarConfig) {
    enderecoServidor = String(parametroServidor.getValue());
    preferencias.putString("servidor", enderecoServidor);
    tokenDispositivo = String(parametroToken.getValue());
    tokenDispositivo.trim();
    preferencias.putString("token", tokenDispositivo);
    precisaSalvarConfig = false;
    Serial.println("Endereco do servidor salvo: " + enderecoServidor);
  }

  Serial.println("WiFi conectado! IP do ESP32: " + WiFi.localIP().toString());
  Serial.println("Enviando dados para: " + urlDeEnvio());
  Serial.println(tokenDispositivo.length() > 0 ? "Token do dispositivo: configurado" : "Token do dispositivo: nao usado");
}

// IP local (ex: 192.168.0.105:8000) usa HTTP; endereço de site (Vercel) usa HTTPS
bool servidorUsaHttps() {
  if (enderecoServidor.startsWith("https://")) return true;
  if (enderecoServidor.startsWith("http://")) return false;
  return !isDigit(enderecoServidor.charAt(0));
}

String urlDeEnvio() {
  String endereco = enderecoServidor;
  endereco.replace("https://", "");
  endereco.replace("http://", "");
  if (endereco.endsWith("/")) endereco.remove(endereco.length() - 1);
  return String(servidorUsaHttps() ? "https://" : "http://") + endereco + "/api/leituras";
}

void enviarLeitura(float distanciaCm, float nivelOcupacao) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi desconectado, tentando reconectar...");
    WiFi.reconnect();
    return;
  }

  HTTPClient http;
  WiFiClient clienteHttp;
  WiFiClientSecure clienteHttps;
  // Sem validar o certificado: suficiente para o protótipo, mas a conexão
  // continua criptografada. Em produção, use setCACert com o certificado raiz.
  clienteHttps.setInsecure();

  String url = urlDeEnvio();
  if (servidorUsaHttps()) {
    http.begin(clienteHttps, url);
  } else {
    http.begin(clienteHttp, url);
  }
  http.setTimeout(10000);
  http.addHeader("Content-Type", "application/json");
  if (tokenDispositivo.length() > 0) {
    http.addHeader("X-Token-Dispositivo", tokenDispositivo);
  }

  String corpoJson = "{";
  corpoJson += "\"dispositivo_id\":\"" + String(DISPOSITIVO_ID) + "\",";
  corpoJson += "\"distancia_cm\":" + String(distanciaCm, 2) + ",";
  corpoJson += "\"nivel_ocupacao\":" + String(nivelOcupacao, 2);
  corpoJson += "}";

  int codigoResposta = http.POST(corpoJson);

  if (codigoResposta >= 200 && codigoResposta < 300) {
    Serial.print("Envio OK, codigo HTTP: ");
    Serial.println(codigoResposta);
  } else if (codigoResposta == 401) {
    Serial.println("Servidor recusou: token do dispositivo ausente ou errado (configure no portal)");
  } else if (codigoResposta == 404) {
    Serial.println("Servidor recusou: lixeira nao cadastrada (confira o DISPOSITIVO_ID)");
  } else if (codigoResposta > 0) {
    Serial.print("Servidor respondeu com erro, codigo HTTP: ");
    Serial.println(codigoResposta);
  } else {
    Serial.print("Erro no envio: ");
    Serial.println(http.errorToString(codigoResposta));
  }

  http.end();
}

// Dispara o pulso ultrassônico e retorna a distância em cm (-1 se não houver eco)
float medirDistanciaCm() {
  digitalWrite(PINO_TRIG, LOW);
  delayMicroseconds(2);
  digitalWrite(PINO_TRIG, HIGH);
  delayMicroseconds(10);
  digitalWrite(PINO_TRIG, LOW);

  long duracao = pulseIn(PINO_ECHO, HIGH, 30000); // timeout de 30ms (~5m)

  if (duracao == 0) {
    return -1;
  }

  float distanciaCm = (duracao * 0.0343) / 2;
  return distanciaCm;
}

// Converte a distância lida em um percentual estimado de ocupação da lixeira
float calcularNivelOcupacao(float distanciaCm) {
  float nivel = (DISTANCIA_LIXEIRA_VAZIA_CM - distanciaCm) /
                (DISTANCIA_LIXEIRA_VAZIA_CM - DISTANCIA_LIXEIRA_CHEIA_CM) * 100.0;

  if (nivel < 0) nivel = 0;
  if (nivel > 100) nivel = 100;

  return nivel;
}
