// Landing page pública: mostra as lixeiras ao vivo usando a API pública (somente leitura).

const ID_REAL = 'lixeira-01';
const INTERVALO_LIXEIRAS_MS = 5000;
const INTERVALO_HISTORICO_MS = 30000;

const CORES = { vazia: '#2e9e5b', parcial: '#e0a100', cheia: '#d64541', sem_dados: '#98a39d' };
const ROTULOS = { vazia: 'Vazia', parcial: 'Parcial', cheia: 'Cheia', sem_dados: 'Sem dados' };

let ultimaResposta = null;
let grafico = null;
let mapa = null;
let mapaEnquadrado = false;
let marcadores = {};

const $ = (id) => document.getElementById(id);

function esc(texto) {
  return String(texto ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function tempoRelativo(iso) {
  if (!iso) return 'sem leituras';
  const seg = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (seg < 5) return 'agora';
  if (seg < 60) return `há ${seg}s`;
  const min = Math.round(seg / 60);
  if (min < 60) return `há ${min} min`;
  const h = Math.round(min / 60);
  if (h < 24) return `há ${h} h`;
  return `há ${Math.round(h / 24)} dias`;
}

function statusVisivel(l) {
  return l.atualizado_em ? l.status : 'sem_dados';
}

async function buscar(caminho) {
  const resp = await fetch(caminho, { cache: 'no-store' });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

// ---------- Card da lixeira real ----------

function renderizarReal(real, limite) {
  const card = $('card-real');
  if (!real) {
    $('real-nome').textContent = 'Lixeira com sensor';
    $('real-detalhe').textContent = 'A lixeira do sensor real não está cadastrada.';
    return;
  }
  const status = statusVisivel(real);
  const nivel = Math.round(real.nivel_ocupacao);

  card.classList.toggle('offline', !real.online);
  $('real-selo').textContent = real.online ? 'Sensor real (ESP32) · ao vivo' : 'Sensor real (ESP32) · desligado';
  $('real-percentual').textContent = real.atualizado_em ? nivel : '--';
  $('real-nome').textContent = real.nome;

  const chip = $('real-status');
  chip.className = `chip ${status}`;
  chip.textContent = real.online ? ROTULOS[status] : 'Sensor offline';

  const barra = $('real-nivel-barra');
  barra.className = `nivel ${status}`;
  barra.style.height = `${real.atualizado_em ? Math.min(100, Math.max(0, real.nivel_ocupacao)) : 0}%`;
  $('real-linha-limite').style.bottom = `${limite}%`;

  let detalhe;
  if (!real.atualizado_em) {
    detalhe = 'Aguardando a primeira leitura do sensor.';
  } else if (!real.online) {
    detalhe = `O sensor está desligado. Última leitura ${tempoRelativo(real.atualizado_em)}.`;
  } else if (real.distancia_cm != null) {
    detalhe = `O sensor está a ${real.distancia_cm.toFixed(1).replace('.', ',')} cm do lixo.`;
  } else {
    detalhe = real.endereco;
  }
  $('real-detalhe').textContent = detalhe;
}

function atualizarRelogio() {
  if (!ultimaResposta) return;
  const real = ultimaResposta.lixeiras.find((l) => l.dispositivo_id === ID_REAL);
  $('real-atualizado').textContent = real && real.atualizado_em
    ? `última leitura ${tempoRelativo(real.atualizado_em)}`
    : 'sem leituras';
}

// ---------- Grade de lixeiras ----------

function cardLixeira(l) {
  const status = statusVisivel(l);
  const nivel = Math.round(l.nivel_ocupacao);
  const real = l.tipo === 'real';
  return `
    <article class="lixeira ${real ? 'real' : 'simulada'}">
      <div class="lixeira-topo">
        <span class="id">${esc(l.dispositivo_id)}</span>
        <span class="tag ${real ? 'tag-real' : 'tag-simulada'}">${real ? 'SENSOR REAL' : 'SIMULADA'}</span>
      </div>
      <h3>${esc(l.nome)}</h3>
      <p class="origem">${real ? 'Dados do sensor HC-SR04 via ESP32' : 'Dados gerados pelo sistema'}</p>
      <div class="barra"><div class="${status}" style="width:${l.atualizado_em ? nivel : 0}%"></div></div>
      <div class="lixeira-rodape">
        <strong>${l.atualizado_em ? nivel + '%' : '--'}</strong>
        <span>${l.online ? esc(ROTULOS[status]) : 'offline'} · ${esc(tempoRelativo(l.atualizado_em))}</span>
      </div>
    </article>`;
}

function renderizarGrade(lixeiras) {
  const reais = lixeiras.filter((l) => l.tipo === 'real');
  const simuladas = lixeiras.filter((l) => l.tipo !== 'real').sort((a, b) => b.nivel_ocupacao - a.nivel_ocupacao);
  $('grade-real').innerHTML = reais.map(cardLixeira).join('') || '<div class="carregando">Nenhuma lixeira com sensor cadastrada.</div>';
  $('grade-simuladas').innerHTML = simuladas.map(cardLixeira).join('') || '<div class="carregando">Nenhuma lixeira simulada.</div>';

  const contagem = { vazia: 0, parcial: 0, cheia: 0 };
  lixeiras.forEach((l) => { if (l.atualizado_em && contagem[l.status] != null) contagem[l.status]++; });
  $('qtd-vazia').textContent = contagem.vazia;
  $('qtd-parcial').textContent = contagem.parcial;
  $('qtd-cheia').textContent = contagem.cheia;
}

// ---------- Mapa ----------

function renderizarMapa(lixeiras) {
  if (!window.L) return;
  if (!mapa) {
    mapa = L.map('mapa', { scrollWheelZoom: false, dragging: !L.Browser.mobile, tap: false });
    const fundo = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution: '&copy; OpenStreetMap',
    }).addTo(mapa);
    // Sem internet (modo offline) o fundo não carrega, mas os marcadores continuam
    fundo.once('tileerror', () => { $('mapa-aviso').hidden = false; });
  }
  // Enquadra as lixeiras só quando o mapa já tem tamanho (com a aba em segundo plano ele pode estar com 0 px)
  const caixa = $('mapa');
  if (!mapaEnquadrado && caixa.clientWidth > 0 && caixa.clientHeight > 0 && lixeiras.length) {
    mapa.invalidateSize();
    mapa.fitBounds(L.latLngBounds(lixeiras.map((l) => [l.lat, l.lng])), { padding: [30, 30], maxZoom: 16 });
    mapaEnquadrado = true;
  }
  lixeiras.forEach((l) => {
    const status = statusVisivel(l);
    const cor = l.online ? CORES[status] : CORES.sem_dados;
    const real = l.tipo === 'real';
    const icone = L.divIcon({
      className: '',
      html: `<div class="marcador ${real ? 'marcador-real' : 'marcador-simulada'}" style="background:${cor}"></div>`,
      iconSize: real ? [26, 26] : [16, 16],
      iconAnchor: real ? [13, 13] : [8, 8],
    });
    const texto = `<strong>${esc(l.nome)}</strong><br>${l.atualizado_em ? Math.round(l.nivel_ocupacao) + '% ocupada' : 'sem leituras'}`
      + `<br><small>${real ? 'SENSOR REAL: dados do ESP32' : 'SIMULADA: dados gerados pelo sistema'}</small>`;
    if (marcadores[l.dispositivo_id]) {
      marcadores[l.dispositivo_id].setLatLng([l.lat, l.lng]).setIcon(icone).setPopupContent(texto);
    } else {
      marcadores[l.dispositivo_id] = L.marker([l.lat, l.lng], { icon: icone, zIndexOffset: real ? 1000 : 0 })
        .addTo(mapa).bindPopup(texto);
    }
  });
}

// ---------- Gráfico da lixeira real ----------

async function atualizarGrafico() {
  if (!window.Chart) return;
  let pontos;
  try {
    pontos = await buscar(`/api/publico/lixeiras/${ID_REAL}/historico?horas=1`);
  } catch (e) {
    return;
  }
  const limite = ultimaResposta ? ultimaResposta.limite_alerta : 80;
  const rotulos = pontos.map((p) => new Date(p.registrado_em).toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' }));
  const valores = pontos.map((p) => p.nivel_ocupacao);
  $('grafico-legenda').textContent = pontos.length ? `${pontos.length} leituras` : 'sem leituras na última hora';

  if (!grafico) {
    grafico = new Chart($('grafico-real'), {
      type: 'line',
      data: {
        labels: rotulos,
        datasets: [
          {
            label: 'Nível (%)',
            data: valores,
            borderColor: '#1e8449',
            backgroundColor: 'rgba(30, 132, 73, 0.12)',
            fill: true,
            tension: 0.3,
            pointRadius: 0,
            borderWidth: 2,
          },
          {
            label: 'Limite de alerta',
            data: valores.map(() => limite),
            borderColor: '#d64541',
            borderDash: [6, 5],
            borderWidth: 1.5,
            pointRadius: 0,
            fill: false,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        interaction: { mode: 'index', intersect: false },
        scales: {
          y: { min: 0, max: 100, ticks: { callback: (v) => `${v}%` }, grid: { color: '#edf1ee' } },
          x: { ticks: { maxTicksLimit: 6, autoSkip: true }, grid: { display: false } },
        },
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${Math.round(c.parsed.y)}%` } },
        },
      },
    });
  } else {
    grafico.data.labels = rotulos;
    grafico.data.datasets[0].data = valores;
    grafico.data.datasets[1].data = valores.map(() => limite);
    grafico.update('none');
  }
}

// ---------- Ciclo de atualização ----------

async function atualizarLixeiras() {
  try {
    ultimaResposta = await buscar('/api/publico/lixeiras');
  } catch (e) {
    $('real-atualizado').textContent = 'sem conexão, tentando de novo...';
    return;
  }
  const { lixeiras, limite_alerta: limite } = ultimaResposta;
  renderizarReal(lixeiras.find((l) => l.dispositivo_id === ID_REAL), limite);
  renderizarGrade(lixeiras);
  renderizarMapa(lixeiras);
  atualizarRelogio();
}

function iniciar() {
  atualizarLixeiras().then(atualizarGrafico);
  setInterval(() => { if (!document.hidden) atualizarLixeiras(); }, INTERVALO_LIXEIRAS_MS);
  setInterval(() => { if (!document.hidden) atualizarGrafico(); }, INTERVALO_HISTORICO_MS);
  setInterval(atualizarRelogio, 1000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) atualizarLixeiras(); });

  const botao = $('botao-compartilhar');
  if (navigator.share) {
    botao.hidden = false;
    botao.addEventListener('click', () => {
      navigator.share({
        title: 'Coleta Inteligente',
        text: 'Veja ao vivo o nível da lixeira do nosso projeto',
        url: location.origin,
      }).catch(() => { /* compartilhamento cancelado */ });
    });
  }
}

iniciar();
