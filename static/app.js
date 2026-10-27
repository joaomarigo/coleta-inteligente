// Coleta Inteligente - front-end (SPA sem framework)
// Fluxo: Login -> Dashboard -> Detalhes / Alertas / Configurações (+ Mapa)

const CORES = { vazia: '#2e9e5b', parcial: '#e0a100', cheia: '#d64541', sem_dados: '#98a39d' };
const ROTULO_STATUS = { vazia: 'Vazia', parcial: 'Parcial', cheia: 'Cheia', sem_dados: 'Sem dados' };
const INTERVALO_ATUALIZACAO_MS = 10000; // RNF01: dashboard atualizado em até 1 minuto
const CENTRO_MAPA = [-23.0990, -47.2200];  // Indaiatuba/SP

let usuario = null;
let telaAtual = null;
let timerAtualizacao = null;
let limiteAlerta = 80;

// ---------- utilitários ----------

class ErroApi extends Error {
  constructor(mensagem, status) {
    super(mensagem);
    this.status = status;
  }
}

function esc(valor) {
  return String(valor ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

function mensagemDeErro(detalhe, status) {
  if (Array.isArray(detalhe)) {
    return detalhe.map(d => `${(d.loc || []).slice(-1)[0] || 'campo'}: ${d.msg}`).join(' · ');
  }
  return detalhe || `Erro ${status}`;
}

async function api(caminho, opcoes = {}) {
  const config = { method: opcoes.method || 'GET', credentials: 'same-origin', headers: {} };
  if (opcoes.body !== undefined) {
    config.headers['Content-Type'] = 'application/json';
    config.body = JSON.stringify(opcoes.body);
  }
  const resp = await fetch(caminho, config);
  if (resp.status === 401 && caminho !== '/api/login') {
    mostrarLogin();
    throw new ErroApi('Sessão expirada', 401);
  }
  if (!resp.ok) {
    let detalhe;
    try { detalhe = (await resp.json()).detail; } catch (e) { /* sem corpo */ }
    throw new ErroApi(mensagemDeErro(detalhe, resp.status), resp.status);
  }
  return resp.status === 204 ? null : resp.json();
}

function tempoRelativo(iso) {
  if (!iso) return 'sem leituras';
  const seg = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (seg < 5) return 'agora';
  if (seg < 60) return `há ${seg}s`;
  const min = Math.round(seg / 60);
  if (min < 60) return `há ${min} min`;
  const horas = Math.round(min / 60);
  if (horas < 48) return `há ${horas} h`;
  return `há ${Math.round(horas / 24)} dias`;
}

function dataHora(iso) {
  return iso ? new Date(iso).toLocaleString('pt-BR') : '—';
}

function chipStatus(status) {
  const classe = status === 'sem_dados' ? 'neutro' : status;
  return `<span class="chip ${classe}">${ROTULO_STATUS[status] || status}</span>`;
}

function statusSensorHtml(l) {
  return `<span class="status-sensor ${l.online ? '' : 'offline'}"><span class="dot"></span>${l.online ? 'online' : 'offline'}</span>`;
}

function barraHtml(nivel, status) {
  const largura = Math.max(0, Math.min(100, nivel));
  return `<div class="barra" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(nivel)}"><div style="width:${largura}%;background:${CORES[status]}"></div></div>`;
}

function gaugeSvg(nivel, status) {
  const alturaTotal = 84;
  const alturaPreenchida = Math.max(2, (nivel / 100) * (alturaTotal - 6));
  return `
    <svg width="46" height="98" viewBox="0 0 42 98" aria-hidden="true">
      <rect x="3" y="3" width="36" height="${alturaTotal}" rx="4" fill="none" stroke="#dbe4de" stroke-width="2" />
      <rect x="6" y="${3 + (alturaTotal - 3 - alturaPreenchida)}" width="30" height="${alturaPreenchida}" rx="2" fill="${CORES[status]}" />
    </svg>`;
}

function marcarAtualizacao(ok) {
  const el = document.getElementById('indicador-atualizacao');
  el.classList.toggle('erro-conexao', !ok);
  el.lastElementChild.textContent = ok
    ? `atualizado às ${new Date().toLocaleTimeString('pt-BR')}`
    : 'sem conexão com o servidor';
}

function novoMapa(idElemento, centro, zoom) {
  const mapa = L.map(idElemento).setView(centro, zoom);
  avisarMapaSemInternet(L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; OpenStreetMap',
    maxZoom: 19
  }).addTo(mapa), mapa);
  return mapa;
}

// Sem internet (modo offline) o fundo do mapa não carrega, mas os marcadores continuam
function avisarMapaSemInternet(camada, mapa) {
  camada.once('tileerror', () => {
    const aviso = L.control({ position: 'topright' });
    aviso.onAdd = () => {
      const div = L.DomUtil.create('div', 'aviso-mapa');
      div.textContent = 'Fundo do mapa indisponível sem internet';
      return div;
    };
    aviso.addTo(mapa);
  });
}

// ---------- telas ----------

const TELAS = {};

// Dashboard (RF06): resumo cheias/parciais/vazias + lista com barra colorida
TELAS.dashboard = {
  async carregar() {
    let r;
    try {
      r = await api('/api/resumo');
    } catch (e) {
      if (e.status !== 401) marcarAtualizacao(false);
      return;
    }
    marcarAtualizacao(true);
    limiteAlerta = r.limite_alerta;

    document.getElementById('qtd-cheias').textContent = r.cheias;
    document.getElementById('qtd-parciais').textContent = r.parciais;
    document.getElementById('qtd-vazias').textContent = r.vazias;
    document.getElementById('qtd-offline').textContent = r.offline;

    const banner = document.getElementById('banner-alerta');
    banner.hidden = r.alertas === 0;
    document.getElementById('banner-texto').textContent =
      `${r.alertas} lixeira(s) com ocupação a partir de ${r.limite_alerta}% precisam de coleta`;

    const lista = document.getElementById('lista-lixeiras');
    if (r.lixeiras.length === 0) {
      lista.innerHTML = `<div class="vazio"><strong>Nenhuma lixeira cadastrada</strong>Cadastre a primeira em <a href="#/configuracoes">Configurações</a>.</div>`;
      return;
    }
    const ordenadas = [...r.lixeiras].sort((a, b) => b.nivel_ocupacao - a.nivel_ocupacao);
    lista.innerHTML = ordenadas.map(l => `
      <a class="linha-lixeira" href="#/lixeira/${encodeURIComponent(l.dispositivo_id)}">
        <div>
          <div class="nome">${esc(l.nome)}</div>
          <div class="meta">${statusSensorHtml(l)} · atualizado ${tempoRelativo(l.atualizado_em)}${l.tipo === 'simulada' ? ' · simulada' : ''}</div>
        </div>
        ${barraHtml(l.nivel_ocupacao, l.status)}
        <div class="pct" style="color:${CORES[l.status]}">${l.atualizado_em ? Math.round(l.nivel_ocupacao) + '%' : '—'}</div>
        ${chipStatus(l.status)}
      </a>`).join('');
  }
};

// Detalhes da lixeira (RF08): nível, localização, status do sensor e histórico
let mapaDetalhe = null;
let marcadorDetalhe = null;
let grafico = null;
let horasHistorico = 24;

TELAS.detalhes = {
  async carregar(id) {
    let l;
    try {
      l = await api(`/api/lixeiras/${encodeURIComponent(id)}`);
    } catch (e) {
      if (e.status === 404) {
        document.getElementById('det-nome').textContent = 'Lixeira não encontrada';
        document.getElementById('det-endereco').textContent = 'Ela pode ter sido removida.';
      }
      return;
    }

    document.getElementById('det-nome').textContent = l.nome;
    document.getElementById('det-endereco').textContent = l.endereco || '';
    document.getElementById('det-chips').innerHTML = chipStatus(l.status);
    document.getElementById('det-gauge').innerHTML = gaugeSvg(l.nivel_ocupacao, l.status);
    document.getElementById('det-nivel').innerHTML = l.atualizado_em
      ? `${Math.round(l.nivel_ocupacao)}<span>%</span>` : '—';
    document.getElementById('det-status').innerHTML = l.nivel_ocupacao >= limiteAlerta
      ? `<span class="chip cheia">Acima do limite de alerta (${limiteAlerta}%)</span>`
      : `<span class="dica">Limite de alerta: ${limiteAlerta}%</span>`;

    document.getElementById('det-sensor').innerHTML = `
      <dt>Situação</dt><dd>${statusSensorHtml(l)}</dd>
      <dt>Última leitura</dt><dd>${dataHora(l.atualizado_em)}<br><span class="dica">${tempoRelativo(l.atualizado_em)}</span></dd>
      <dt>Distância medida</dt><dd>${l.distancia_cm != null ? l.distancia_cm.toFixed(1) + ' cm' : '—'}</dd>
      <dt>Altura útil</dt><dd>${l.altura_cm} cm</dd>
      <dt>Dispositivo</dt><dd><code>${esc(l.dispositivo_id)}</code> · ${l.tipo === 'real' ? 'ESP32 + HC-SR04' : 'simulada'}</dd>`;

    document.getElementById('det-local').innerHTML = `
      <dt>Endereço</dt><dd>${esc(l.endereco || '—')}</dd>
      <dt>Coordenadas</dt><dd>${l.lat.toFixed(5)}, ${l.lng.toFixed(5)}</dd>`;

    if (window.L) {
      if (!mapaDetalhe) {
        mapaDetalhe = novoMapa('mapa-detalhe', [l.lat, l.lng], 16);
        marcadorDetalhe = L.circleMarker([l.lat, l.lng], { radius: 9, color: '#fff', weight: 2, fillOpacity: 0.95 }).addTo(mapaDetalhe);
      }
      mapaDetalhe.invalidateSize();
      if (!marcadorDetalhe.getLatLng().equals([l.lat, l.lng])) mapaDetalhe.setView([l.lat, l.lng], 16);
      marcadorDetalhe.setLatLng([l.lat, l.lng]).setStyle({ fillColor: CORES[l.status] });
    }

    await carregarHistorico(id);
  }
};

function rotuloTempo(iso, horas) {
  const d = new Date(iso);
  const hora = d.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
  return horas > 24 ? `${d.toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit' })} ${hora}` : hora;
}

function corPorNivel(nivel) {
  if (nivel >= 80) return CORES.cheia;
  if (nivel > 50) return CORES.parcial;
  return CORES.vazia;
}

async function carregarHistorico(id) {
  let pontos;
  try {
    pontos = await api(`/api/lixeiras/${encodeURIComponent(id)}/historico?horas=${horasHistorico}`);
  } catch (e) {
    return;
  }
  const canvas = document.getElementById('grafico-historico');
  document.getElementById('historico-vazio').hidden = pontos.length > 0;
  canvas.hidden = pontos.length === 0;
  if (typeof Chart === 'undefined' || pontos.length === 0) return;

  const labels = pontos.map(p => rotuloTempo(p.registrado_em, horasHistorico));
  const niveis = pontos.map(p => p.nivel_ocupacao);
  const linhaLimite = pontos.map(() => limiteAlerta);

  if (grafico) {
    grafico.data.labels = labels;
    grafico.data.datasets[0].data = niveis;
    grafico.data.datasets[1].data = linhaLimite;
    grafico.data.datasets[1].label = `Limite de alerta (${limiteAlerta}%)`;
    grafico.update('none');
    return;
  }

  grafico = new Chart(canvas, {
    type: 'line',
    data: {
      labels,
      datasets: [
        {
          label: 'Ocupação (%)',
          data: niveis,
          borderWidth: 2.5,
          borderColor: CORES.vazia,
          segment: { borderColor: ctx => corPorNivel(ctx.p1.parsed.y) },
          pointRadius: 0,
          pointHoverRadius: 4,
          tension: 0.25
        },
        {
          label: `Limite de alerta (${limiteAlerta}%)`,
          data: linhaLimite,
          borderColor: CORES.cheia,
          borderDash: [6, 4],
          borderWidth: 1.5,
          pointRadius: 0,
          pointHoverRadius: 0
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        y: { min: 0, max: 100, ticks: { callback: v => v + '%' } },
        x: { ticks: { maxTicksLimit: 8, autoSkip: true, maxRotation: 0 } }
      },
      plugins: {
        legend: { labels: { boxWidth: 12 } },
        tooltip: { callbacks: { label: c => `${c.dataset.label}: ${Math.round(c.parsed.y)}%` } }
      }
    }
  });
}

document.getElementById('periodos').addEventListener('click', e => {
  const botao = e.target.closest('button[data-horas]');
  if (!botao) return;
  horasHistorico = Number(botao.dataset.horas);
  document.querySelectorAll('#periodos button').forEach(b => b.classList.toggle('ativo', b === botao));
  carregarHistorico(telaAtual.param);
});

// Alertas (RF07): lixeiras acima do limite, ordenadas por prioridade
TELAS.alertas = {
  async carregar() {
    let r;
    try {
      r = await api('/api/alertas');
    } catch (e) {
      return;
    }
    limiteAlerta = r.limite_alerta;
    document.getElementById('alertas-subtitulo').textContent =
      `Lixeiras com ocupação a partir de ${r.limite_alerta}%, ordenadas por prioridade (mais cheia primeiro)`;

    const lista = document.getElementById('lista-alertas');
    if (r.alertas.length === 0) {
      lista.innerHTML = `<div class="vazio"><strong>Nenhum alerta no momento</strong>Todas as lixeiras estão abaixo de ${r.limite_alerta}%.</div>`;
      return;
    }
    lista.innerHTML = r.alertas.map((l, i) => `
      <div class="alerta-item">
        <div class="prioridade" title="Prioridade ${i + 1}">${i + 1}</div>
        <div>
          <div class="nome">${esc(l.nome)} · ${Math.round(l.nivel_ocupacao)}%</div>
          <div class="meta">${esc(l.endereco || l.dispositivo_id)} · ${statusSensorHtml(l)} · ${tempoRelativo(l.atualizado_em)}</div>
        </div>
        ${barraHtml(l.nivel_ocupacao, l.status)}
        <div class="acoes">
          <a class="btn secundario pequeno" href="#/lixeira/${encodeURIComponent(l.dispositivo_id)}">Detalhes</a>
        </div>
      </div>`).join('');
  }
};

// Configurações (RF05 / RN02): limite de alerta e cadastro de lixeiras
let mapaForm = null;
let marcadorForm = null;
let lixeiraEmEdicao = null;
let lixeirasCadastradas = [];

TELAS.configuracoes = {
  semAtualizacaoAutomatica: true,
  async carregar() {
    const [config, lixeiras] = await Promise.all([api('/api/configuracoes'), api('/api/lixeiras')]);
    limiteAlerta = config.limite_alerta;
    document.getElementById('limite-range').value = config.limite_alerta;
    document.getElementById('limite-numero').value = config.limite_alerta;
    document.getElementById('limite-msg').textContent = '';
    lixeirasCadastradas = lixeiras;
    renderizarTabelaLixeiras();
  }
};

function renderizarTabelaLixeiras() {
  const corpo = document.getElementById('tabela-lixeiras');
  if (lixeirasCadastradas.length === 0) {
    corpo.innerHTML = `<tr><td colspan="5" class="vazio">Nenhuma lixeira cadastrada.</td></tr>`;
    return;
  }
  corpo.innerHTML = lixeirasCadastradas.map(l => `
    <tr>
      <td><code>${esc(l.dispositivo_id)}</code></td>
      <td>${esc(l.nome)}<br><span class="dica">${esc(l.endereco)}</span></td>
      <td>${l.altura_cm} cm</td>
      <td><span class="chip neutro">${l.tipo === 'real' ? 'sensor real' : 'simulada'}</span></td>
      <td class="acoes">
        <button class="btn secundario pequeno" data-editar="${esc(l.dispositivo_id)}">Editar</button>
        <button class="btn perigo pequeno" data-remover="${esc(l.dispositivo_id)}">Remover</button>
      </td>
    </tr>`).join('');
}

document.getElementById('tabela-lixeiras').addEventListener('click', async e => {
  const editar = e.target.closest('[data-editar]');
  if (editar) {
    abrirFormLixeira(lixeirasCadastradas.find(l => l.dispositivo_id === editar.dataset.editar));
    return;
  }
  const remover = e.target.closest('[data-remover]');
  if (!remover) return;

  // Confirmação em dois cliques, sem diálogo nativo do navegador
  if (!remover.classList.contains('confirmar')) {
    remover.classList.add('confirmar');
    remover.textContent = 'Confirmar remoção';
    setTimeout(() => {
      remover.classList.remove('confirmar');
      remover.textContent = 'Remover';
    }, 4000);
    return;
  }
  remover.disabled = true;
  try {
    await api(`/api/lixeiras/${encodeURIComponent(remover.dataset.remover)}`, { method: 'DELETE' });
    if (lixeiraEmEdicao && lixeiraEmEdicao.dispositivo_id === remover.dataset.remover) fecharFormLixeira();
    await TELAS.configuracoes.carregar();
  } catch (erro) {
    remover.disabled = false;
    const msg = document.getElementById('limite-msg');
    msg.className = 'erro';
    msg.textContent = `Não foi possível remover: ${erro.message}`;
  }
});

function abrirFormLixeira(lixeira) {
  lixeiraEmEdicao = lixeira || null;
  const form = document.getElementById('form-lixeira');
  form.reset();
  document.getElementById('form-lixeira-erro').textContent = '';
  document.getElementById('form-lixeira-titulo').textContent = lixeira ? `Editar: ${lixeira.nome}` : 'Nova lixeira';

  const dados = lixeira || { lat: CENTRO_MAPA[0], lng: CENTRO_MAPA[1], altura_cm: 100, tipo: 'real' };
  for (const campo of ['dispositivo_id', 'nome', 'endereco', 'altura_cm', 'tipo', 'lat', 'lng']) {
    if (dados[campo] !== undefined) form.elements[campo].value = dados[campo];
  }
  form.elements.dispositivo_id.disabled = !!lixeira;

  const painel = document.getElementById('painel-form-lixeira');
  painel.hidden = false;

  if (window.L) {
    if (!mapaForm) {
      mapaForm = novoMapa('mapa-form', [dados.lat, dados.lng], 15);
      marcadorForm = L.marker([dados.lat, dados.lng]).addTo(mapaForm);
      mapaForm.on('click', ev => {
        marcadorForm.setLatLng(ev.latlng);
        form.elements.lat.value = ev.latlng.lat.toFixed(6);
        form.elements.lng.value = ev.latlng.lng.toFixed(6);
      });
    }
    mapaForm.invalidateSize();
    mapaForm.setView([dados.lat, dados.lng], 15);
    marcadorForm.setLatLng([dados.lat, dados.lng]);
  }
  painel.scrollIntoView({ behavior: 'smooth', block: 'start' });
  (lixeira ? form.elements.nome : form.elements.dispositivo_id).focus({ preventScroll: true });
}

function fecharFormLixeira() {
  lixeiraEmEdicao = null;
  document.getElementById('painel-form-lixeira').hidden = true;
}

document.getElementById('btn-nova-lixeira').addEventListener('click', () => abrirFormLixeira(null));
document.getElementById('btn-cancelar-form').addEventListener('click', fecharFormLixeira);

// Atualiza o marcador quando o gestor digita as coordenadas
['lat', 'lng'].forEach(campo => {
  document.getElementById('form-lixeira').elements[campo].addEventListener('change', () => {
    const form = document.getElementById('form-lixeira');
    const lat = parseFloat(form.elements.lat.value);
    const lng = parseFloat(form.elements.lng.value);
    if (marcadorForm && !isNaN(lat) && !isNaN(lng)) {
      marcadorForm.setLatLng([lat, lng]);
      mapaForm.panTo([lat, lng]);
    }
  });
});

function validarFormLixeira(form) {
  const v = nome => form.elements[nome].value.trim();
  if (!lixeiraEmEdicao) {
    if (!v('dispositivo_id')) return 'Informe o ID do dispositivo.';
    if (!/^[A-Za-z0-9_-]+$/.test(v('dispositivo_id'))) return 'O ID do dispositivo só pode ter letras, números, "-" e "_".';
  }
  if (!v('nome')) return 'Informe o nome da lixeira.';
  const altura = parseFloat(v('altura_cm'));
  if (isNaN(altura) || altura <= 0 || altura > 1000) return 'A altura deve estar entre 1 e 1000 cm.';
  const lat = parseFloat(v('lat'));
  const lng = parseFloat(v('lng'));
  if (isNaN(lat) || lat < -90 || lat > 90) return 'Latitude inválida.';
  if (isNaN(lng) || lng < -180 || lng > 180) return 'Longitude inválida.';
  return null;
}

document.getElementById('form-lixeira').addEventListener('submit', async e => {
  e.preventDefault();
  const form = e.target;
  const erroEl = document.getElementById('form-lixeira-erro');
  const erro = validarFormLixeira(form);
  if (erro) {
    erroEl.textContent = erro;
    return;
  }

  const corpo = {
    nome: form.elements.nome.value.trim(),
    endereco: form.elements.endereco.value.trim(),
    altura_cm: parseFloat(form.elements.altura_cm.value),
    tipo: form.elements.tipo.value,
    lat: parseFloat(form.elements.lat.value),
    lng: parseFloat(form.elements.lng.value)
  };

  const botao = form.querySelector('button[type=submit]');
  botao.disabled = true;
  try {
    if (lixeiraEmEdicao) {
      await api(`/api/lixeiras/${encodeURIComponent(lixeiraEmEdicao.dispositivo_id)}`, { method: 'PUT', body: corpo });
    } else {
      await api('/api/lixeiras', { method: 'POST', body: { ...corpo, dispositivo_id: form.elements.dispositivo_id.value.trim() } });
    }
    fecharFormLixeira();
    await TELAS.configuracoes.carregar();
    const msg = document.getElementById('limite-msg');
    msg.className = 'sucesso';
    msg.textContent = 'Lixeira salva.';
  } catch (erroApi) {
    erroEl.textContent = erroApi.message;
  } finally {
    botao.disabled = false;
  }
});

// Limite de alerta (RN02)
const limiteRange = document.getElementById('limite-range');
const limiteNumero = document.getElementById('limite-numero');
limiteRange.addEventListener('input', () => { limiteNumero.value = limiteRange.value; });
limiteNumero.addEventListener('input', () => { limiteRange.value = limiteNumero.value; });
document.getElementById('limite-padrao').addEventListener('click', () => {
  limiteRange.value = limiteNumero.value = 80;
});

document.getElementById('form-limite').addEventListener('submit', async e => {
  e.preventDefault();
  const msg = document.getElementById('limite-msg');
  const valor = Number(limiteNumero.value);
  if (!Number.isFinite(valor) || valor < 1 || valor > 100) {
    msg.className = 'erro';
    msg.textContent = 'O limite deve estar entre 1% e 100%.';
    return;
  }
  try {
    const r = await api('/api/configuracoes', { method: 'PUT', body: { limite_alerta: valor } });
    limiteAlerta = r.limite_alerta;
    msg.className = 'sucesso';
    msg.textContent = `Limite de alerta salvo: ${r.limite_alerta}%.`;
    atualizarBadgeAlertas();
  } catch (erro) {
    msg.className = 'erro';
    msg.textContent = erro.message;
  }
});

// Mapa e rota (prévia de evolução futura)
let mapaRota = null;
let mapaRotaEnquadrado = false;
let camadaLixeiras = null;
let camadaRota = null;

async function tracarRotaComOSRM(pontos) {
  const coordsStr = pontos.map(p => `${p.lng},${p.lat}`).join(';');
  const url = `https://router.project-osrm.org/route/v1/driving/${coordsStr}?overview=full&geometries=geojson`;
  const resposta = await Promise.race([
    fetch(url),
    new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), 4000))
  ]);
  if (!resposta.ok) throw new Error('OSRM indisponível');
  const dados = await resposta.json();
  if (!dados.routes || dados.routes.length === 0) throw new Error('sem rota');
  return dados.routes[0].geometry.coordinates.map(c => [c[1], c[0]]);
}

TELAS.mapa = {
  async carregar() {
    if (!window.L) return;
    if (!mapaRota) {
      mapaRota = novoMapa('mapa', CENTRO_MAPA, 14);
      camadaLixeiras = L.layerGroup().addTo(mapaRota);
      camadaRota = L.layerGroup().addTo(mapaRota);
    }
    mapaRota.invalidateSize();

    let lixeiras, rota;
    try {
      [lixeiras, rota] = await Promise.all([api('/api/lixeiras'), api('/api/rota')]);
    } catch (e) {
      return;
    }

    if (!mapaRotaEnquadrado && lixeiras.length) {
      const pontos = [...lixeiras.map(l => [l.lat, l.lng]), [rota.deposito.lat, rota.deposito.lng]];
      mapaRota.fitBounds(L.latLngBounds(pontos), { padding: [30, 30], maxZoom: 16 });
      mapaRotaEnquadrado = true;
    }

    camadaLixeiras.clearLayers();
    lixeiras.forEach(l => {
      L.circleMarker([l.lat, l.lng], {
        radius: 9, color: '#fff', weight: 2, fillColor: CORES[l.status], fillOpacity: 0.95
      })
        .bindPopup(`<strong>${esc(l.nome)}</strong><br>${Math.round(l.nivel_ocupacao)}% ocupada<br><a href="#/lixeira/${encodeURIComponent(l.dispositivo_id)}">ver detalhes</a>`)
        .addTo(camadaLixeiras);
    });

    camadaRota.clearLayers();
    L.circleMarker([rota.deposito.lat, rota.deposito.lng], {
      radius: 8, color: '#fff', weight: 2, fillColor: '#5b6b62', fillOpacity: 0.95
    }).bindPopup(`<strong>${esc(rota.deposito.nome)}</strong><br>Base do caminhão`).addTo(camadaRota);

    document.getElementById('subtitulo-rota').textContent =
      `Lixeiras a partir de ${rota.limite_coleta}% (limite de alerta) entram na rota sugerida.`;
    const lista = document.getElementById('lista-paradas');
    const distanciaEl = document.getElementById('rota-distancia');
    const legendaEl = document.getElementById('rota-legenda');

    if (rota.paradas.length === 0) {
      distanciaEl.textContent = '0 km';
      legendaEl.textContent = `nenhuma lixeira atingiu ${rota.limite_coleta}% ainda`;
      lista.innerHTML = '';
      return;
    }

    distanciaEl.textContent = `${rota.distancia_total_km} km`;
    legendaEl.textContent = `distância estimada · ${rota.paradas.length} parada(s)`;
    lista.innerHTML = rota.paradas.map((p, i) => `
      <li class="parada-item">
        <span class="parada-numero">${i + 1}</span>
        <span>${esc(p.nome)}<br><span class="parada-nivel">${Math.round(p.nivel_ocupacao)}% ocupada</span></span>
      </li>`).join('');

    const pontosRota = [{ lat: rota.deposito.lat, lng: rota.deposito.lng }, ...rota.paradas];
    let coordenadasLinha;
    try {
      coordenadasLinha = await tracarRotaComOSRM(pontosRota);
    } catch (e) {
      coordenadasLinha = pontosRota.map(p => [p.lat, p.lng]);
    }
    L.polyline(coordenadasLinha, { color: '#1e8449', weight: 4, opacity: 0.85 }).addTo(camadaRota);

    rota.paradas.forEach((p, i) => {
      const icone = L.divIcon({
        className: '',
        html: `<div class="numero-marcador">${i + 1}</div>`,
        iconSize: [24, 24],
        iconAnchor: [12, 12]
      });
      L.marker([p.lat, p.lng], { icon: icone }).addTo(camadaRota);
    });
  }
};

// ---------- navegação ----------

function rotaAtual() {
  const partes = location.hash.replace(/^#\/?/, '').split('/');
  const tela = partes[0] || 'dashboard';
  return {
    nome: tela === 'lixeira' ? 'detalhes' : tela,
    param: decodeURIComponent(partes.slice(1).join('/'))
  };
}

async function atualizarBadgeAlertas() {
  try {
    const r = await api('/api/alertas');
    limiteAlerta = r.limite_alerta;
    const badge = document.getElementById('badge-alertas');
    badge.hidden = r.alertas.length === 0;
    badge.textContent = r.alertas.length;
  } catch (e) { /* tratado em api() */ }
}

async function navegar() {
  if (!usuario) return;
  const rota = rotaAtual();
  if (!TELAS[rota.nome]) {
    location.hash = '#/dashboard';
    return;
  }
  if (telaAtual && telaAtual.nome === 'detalhes' && (rota.nome !== 'detalhes' || rota.param !== telaAtual.param) && grafico) {
    grafico.destroy();
    grafico = null;
  }
  telaAtual = rota;

  document.querySelectorAll('[data-tela]').forEach(s => { s.hidden = s.dataset.tela !== rota.nome; });
  const menuAtivo = rota.nome === 'detalhes' ? 'dashboard' : rota.nome;
  document.querySelectorAll('[data-menu]').forEach(a => a.classList.toggle('ativo', a.dataset.menu === menuAtivo));
  window.scrollTo(0, 0);

  clearInterval(timerAtualizacao);
  atualizarBadgeAlertas();
  try {
    await TELAS[rota.nome].carregar(rota.param);
  } catch (e) { /* erros de rede: a próxima atualização tenta de novo */ }

  if (!TELAS[rota.nome].semAtualizacaoAutomatica) {
    timerAtualizacao = setInterval(() => {
      if (document.hidden || telaAtual !== rota) return;
      TELAS[rota.nome].carregar(rota.param).catch(() => {});
      atualizarBadgeAlertas();
    }, INTERVALO_ATUALIZACAO_MS);
  }
}

window.addEventListener('hashchange', navegar);

// ---------- login / sessão ----------

function mostrarLogin() {
  usuario = null;
  telaAtual = null;
  clearInterval(timerAtualizacao);
  document.getElementById('app').hidden = true;
  document.getElementById('tela-login').hidden = false;
  document.getElementById('login-email').focus();
}

function mostrarApp() {
  document.getElementById('tela-login').hidden = true;
  document.getElementById('app').hidden = false;
  document.getElementById('usuario-email').textContent = usuario.email;
  if (!location.hash.startsWith('#/')) history.replaceState(null, '', '#/dashboard');
  navegar();
}

document.getElementById('form-login').addEventListener('submit', async e => {
  e.preventDefault();
  const email = document.getElementById('login-email').value.trim();
  const senha = document.getElementById('login-senha').value;
  const erroEl = document.getElementById('login-erro');

  if (!email || !senha) {
    erroEl.textContent = 'Preencha e-mail e senha.';
    return;
  }
  if (!/^[^\s@]+@[^\s@]+$/.test(email)) {
    erroEl.textContent = 'Informe um e-mail válido.';
    return;
  }

  const botao = document.getElementById('login-botao');
  botao.disabled = true;
  erroEl.textContent = '';
  try {
    usuario = await api('/api/login', { method: 'POST', body: { email, senha } });
    document.getElementById('login-senha').value = '';
    mostrarApp();
  } catch (erro) {
    erroEl.textContent = erro.status === 401 ? 'E-mail ou senha inválidos.' : 'Não foi possível conectar ao servidor.';
  } finally {
    botao.disabled = false;
  }
});

document.querySelectorAll('[data-acao="sair"]').forEach(botao => {
  botao.addEventListener('click', async () => {
    try { await api('/api/logout', { method: 'POST' }); } catch (e) { /* já deslogado */ }
    mostrarLogin();
  });
});

document.querySelectorAll('.logo-svg').forEach(el => {
  el.outerHTML = '<img src="/static/logo.svg" alt="" width="36" height="36">';
});

(async function iniciar() {
  try {
    usuario = await api('/api/me');
    mostrarApp();
  } catch (e) {
    mostrarLogin();
  }
})();
