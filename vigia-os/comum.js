// Pecas compartilhadas dos vigias do Portal Sebrae de credenciados
// (vigia.js = O.S., vigia-nf.js = notas fiscais): envio pelo WhatsApp,
// chamada ao scraper, estado/contador de falhas por conta e utilidades.

const { Client } = require("pg");

const TZ = "America/Sao_Paulo";
const PORTAL = "https://app2.pr.sebrae.com.br/SebraePR/login.do";

function agoraBR() {
  const partes = new Intl.DateTimeFormat("pt-BR", {
    timeZone: TZ, hour: "numeric", month: "numeric", weekday: "short", hour12: false,
  }).formatToParts(new Date());
  const pegar = (t) => partes.find((p) => p.type === t)?.value || "";
  return {
    hora: parseInt(pegar("hour"), 10) % 24, // Intl pode devolver "24" para meia-noite
    mes: parseInt(pegar("month"), 10),
    dia: pegar("weekday").replace(".", ""),
  };
}

function dataBR(d) {
  // "27/08/2026" -> "2026-08-27"; qualquer outra coisa -> null
  const m = /^(\d{2})\/(\d{2})\/(\d{4})$/.exec(d || "");
  return m ? `${m[3]}-${m[2]}-${m[1]}` : null;
}

const dormir = (ms) => new Promise((r) => setTimeout(r, ms));

// Monta o conjunto de funcoes de um vigia. `nome` aparece no log; `prefixoEstado`
// separa as chaves de estado ("os", "nf"); `minutosPorRodada` so serve para
// dizer ha quantas horas o portal esta fora no alerta tecnico.
function criar({ nome, prefixoEstado, minutosPorRodada, env = process.env }) {
  const {
    PG_URL,
    SCRAPER_URL = "http://127.0.0.1:8001",
    EVO_URL,
    EVO_KEY,
    EVO_INSTANCE = "claudinho",
    DESTINOS,
    CONTA = "rafael",
    INTERVALO_MIN_S = "30",
    INTERVALO_MAX_S = "120",
    FALHAS_PARA_AVISAR = "6",
  } = env;

  const log = (...a) => console.log(new Date().toISOString(), `[${nome}]`, ...a);

  // "rafael" -> "Rafael": entra no titulo de todo aviso para dizer de quem e a conta.
  const nomeConta = CONTA.charAt(0).toUpperCase() + CONTA.slice(1).toLowerCase();

  // Quem recebe: lista separada por virgula, dono da conta primeiro.
  const destinos = (DESTINOS || "").split(",").map((s) => s.trim()).filter(Boolean);

  function pausaAleatoriaMs() {
    const min = parseInt(INTERVALO_MIN_S, 10) * 1000;
    const max = parseInt(INTERVALO_MAX_S, 10) * 1000;
    return min + Math.floor(Math.random() * (max - min + 1));
  }

  async function enviarPara(numero, texto) {
    const r = await fetch(`${EVO_URL}/message/sendText/${EVO_INSTANCE}`, {
      method: "POST",
      headers: { apikey: EVO_KEY, "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify({ number: numero, text: texto }),
    });
    if (!r.ok) {
      const corpo = await r.text().catch(() => "");
      log(`envio para ${numero} falhou HTTP ${r.status}: ${corpo.slice(0, 200)}`);
      return false;
    }
    return true;
  }

  // Manda para todos os destinos, com pausa aleatoria entre um e outro para a
  // instancia nao disparar duas mensagens iguais no mesmo segundo. Devolve true
  // se pelo menos um recebeu (ai nao retenta, para nao duplicar no que recebeu).
  async function enviarWhatsApp(texto, { apenasPrimeiro = false } = {}) {
    if (!EVO_URL || !EVO_KEY || destinos.length === 0) {
      log("sem credencial Evolution/DESTINOS; mensagem nao enviada:", texto);
      return false;
    }
    const alvos = apenasPrimeiro ? destinos.slice(0, 1) : destinos;
    let algumOk = false;
    for (let i = 0; i < alvos.length; i++) {
      if (i > 0) {
        const ms = pausaAleatoriaMs();
        log(`aguardando ${Math.round(ms / 1000)}s antes do próximo destino`);
        await dormir(ms);
      }
      try {
        if (await enviarPara(alvos[i], texto)) algumOk = true;
      } catch (e) {
        log(`erro ao enviar para ${alvos[i]}:`, e.message || e);
      }
    }
    return algumOk;
  }

  // GET no scraper. 9 min de espera: o scraper serializa a sessao do Sebrae,
  // entao a chamada pode ficar na fila atras do worker da Maquina de Vendas.
  // Esperar nao e falha.
  async function chamarScraper(caminho, { timeoutMs = 540000 } = {}) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const r = await fetch(`${SCRAPER_URL}${caminho}`, { signal: ctrl.signal });
      const corpo = await r.json().catch(() => ({}));
      if (!r.ok || !corpo.sucesso) throw new Error(corpo.detail || `HTTP ${r.status}`);
      return corpo;
    } finally {
      clearTimeout(timer);
    }
  }

  // Estado por vigia E por conta ("os:geovana:falhas_seguidas"). Antes as duas
  // contas do vigia de O.S. dividiam o mesmo contador.
  const chave = (k) => `${prefixoEstado}:${CONTA}:${k}`;

  async function estadoLer(db, k) {
    const r = await db.query("select valor from sebrae_os_vigia_estado where chave=$1", [chave(k)]);
    return r.rows[0]?.valor ?? null;
  }

  async function estadoGravar(db, k, valor) {
    await db.query(
      `insert into sebrae_os_vigia_estado (chave, valor, atualizado_em) values ($1,$2,now())
       on conflict (chave) do update set valor=excluded.valor, atualizado_em=now()`,
      [chave(k), String(valor)]
    );
  }

  // Falha de leitura (portal fora, senha recusada): conta e, a partir de
  // FALHAS_PARA_AVISAR falhas seguidas, avisa so o 1o destino (problema
  // tecnico e do Rafael), uma vez por dia. Guarda quando a queda comecou e se
  // o alerta saiu, para leituraOk avisar a volta.
  async function tratarFalha(db, erro, titulo) {
    const falhas = parseInt((await estadoLer(db, "falhas_seguidas")) || "0", 10) + 1;
    await estadoGravar(db, "falhas_seguidas", falhas);
    if (falhas === 1) await estadoGravar(db, "fora_desde", new Date().toISOString());
    await estadoGravar(db, "ultimo_erro", String(erro).slice(0, 500));
    log(`falha ${falhas}: ${erro}`);
    const limite = parseInt(FALHAS_PARA_AVISAR, 10);
    const hoje = new Date().toLocaleDateString("pt-BR", { timeZone: TZ });
    const avisadoEm = await estadoLer(db, "falha_avisada_em");
    if (falhas >= limite && avisadoEm !== hoje) {
      const motivo = /login|senha|usuario|Entrar/i.test(String(erro))
        ? "login recusado (senha mudou?)"
        : String(erro).slice(0, 160);
      const horas = Math.round((falhas * minutosPorRodada) / 6) / 10;
      const ok = await enviarWhatsApp(
        `⚠️ *${titulo} — ${nomeConta}*\nNão consigo acessar o portal há ${falhas} rodadas (~${horas}h).\nMotivo: ${motivo}`,
        { apenasPrimeiro: true }
      );
      if (ok) {
        await estadoGravar(db, "falha_avisada_em", hoje);
        await estadoGravar(db, "queda_avisada", "sim");
      }
    }
  }

  // "24/10 17:00" no horario de Brasilia.
  const quandoBR = (iso) =>
    new Date(iso).toLocaleString("pt-BR", {
      timeZone: TZ, day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
    }).replace(",", "");

  function textoVoltou(titulo, foraDesde) {
    let periodo = "";
    if (foraDesde && !isNaN(new Date(foraDesde))) {
      const min = Math.max(0, Math.round((Date.now() - new Date(foraDesde)) / 60000));
      const dur = min < 60 ? `${min} min` : `${Math.floor(min / 60)}h${String(min % 60).padStart(2, "0")}`;
      periodo = `\nFicou fora de ${quandoBR(foraDesde)} até ${quandoBR(new Date().toISOString())} (${dur}).`;
    }
    return (
      `✅ *${titulo} — ${nomeConta}*\nO portal voltou e estou lendo de novo.${periodo}\n` +
      `Se algo mudou nesse período, o aviso chega em seguida; se não chegar nada, não mudou nada.`
    );
  }

  // Leitura deu certo. Se o alerta de queda saiu, avisa a volta para a mesma
  // pessoa: sem isso, quem recebeu "nao consigo acessar" fica sem saber se o
  // portal voltou. Envio que falhou mantem a marca e retenta na proxima rodada.
  async function leituraOk(db, titulo) {
    if ((await estadoLer(db, "queda_avisada")) === "sim") {
      const foraDesde = await estadoLer(db, "fora_desde");
      const ok = await enviarWhatsApp(textoVoltou(titulo, foraDesde), { apenasPrimeiro: true });
      if (ok) await estadoGravar(db, "queda_avisada", "");
      else log("aviso de volta do portal falhou; fica para a proxima rodada");
    }
    await estadoGravar(db, "falhas_seguidas", 0);
    await estadoGravar(db, "ultima_leitura_ok", new Date().toISOString());
  }

  // Abre conexao com o banco apollo, roda fn(db) e fecha sempre.
  async function comBanco(fn) {
    const db = new Client({ connectionString: PG_URL });
    await db.connect();
    try {
      return await fn(db);
    } finally {
      await db.end();
    }
  }

  return {
    log, nomeConta, CONTA, destinos, enviarWhatsApp, chamarScraper,
    estadoLer, estadoGravar, tratarFalha, leituraOk, comBanco, textoVoltou,
  };
}

module.exports = { criar, agoraBR, dataBR, dormir, TZ, PORTAL };
