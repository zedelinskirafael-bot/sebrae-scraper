// Vigia de O.S. do Portal Sebrae de credenciados.
// Roda a cada 15 min (systemd timer na KVM8), pede ao scraper a lista de
// Contratacao > Consultar Os, compara com o que ja viu (tabela
// sebrae_os_vistas, banco apollo) e avisa no WhatsApp (Evolution, instancia
// claudinho) quando aparece O.S. nova ou quando o "Fluxo atual" muda.
// Primeira rodada so grava (baseline), nao avisa.

const { Client } = require("pg");

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
  JANELA_INICIO = "7",
  JANELA_FIM = "21",
  FALHAS_PARA_AVISAR = "3",
} = process.env;

const PORTAL = "https://app2.pr.sebrae.com.br/SebraePR/login.do";
const TZ = "America/Sao_Paulo";

function agoraBR() {
  const partes = new Intl.DateTimeFormat("pt-BR", {
    timeZone: TZ, hour: "numeric", weekday: "short", hour12: false,
  }).formatToParts(new Date());
  const pegar = (t) => partes.find((p) => p.type === t)?.value || "";
  return { hora: parseInt(pegar("hour"), 10), dia: pegar("weekday").replace(".", "") };
}

function dentroDaJanela() {
  const { hora, dia } = agoraBR();
  if (dia === "dom") return false;
  return hora >= parseInt(JANELA_INICIO, 10) && hora <= parseInt(JANELA_FIM, 10);
}

function log(...a) {
  console.log(new Date().toISOString(), "[vigia-os]", ...a);
}

// Quem recebe: lista separada por virgula (Rafael primeiro, depois Geovana).
const LISTA_DESTINOS = (DESTINOS || "").split(",").map((s) => s.trim()).filter(Boolean);

const dormir = (ms) => new Promise((r) => setTimeout(r, ms));

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
  if (!EVO_URL || !EVO_KEY || LISTA_DESTINOS.length === 0) {
    log("sem credencial Evolution/DESTINOS; mensagem nao enviada:", texto);
    return false;
  }
  const alvos = apenasPrimeiro ? LISTA_DESTINOS.slice(0, 1) : LISTA_DESTINOS;
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

async function buscarOS() {
  const ctrl = new AbortController();
  // 9 min: o scraper serializa a sessao do Sebrae, entao esta chamada pode
  // ficar na fila atras do worker da Maquina de Vendas. Esperar nao e falha.
  const timer = setTimeout(() => ctrl.abort(), 540000);
  try {
    const r = await fetch(`${SCRAPER_URL}/os-credenciado`, { signal: ctrl.signal });
    const corpo = await r.json().catch(() => ({}));
    if (!r.ok || !corpo.sucesso) {
      throw new Error(corpo.detail || `HTTP ${r.status}`);
    }
    return corpo.os || [];
  } finally {
    clearTimeout(timer);
  }
}

function dataBR(d) {
  // "27/08/2026" -> "2026-08-27"; qualquer outra coisa -> null
  const m = /^(\d{2})\/(\d{2})\/(\d{4})$/.exec(d || "");
  return m ? `${m[3]}-${m[2]}-${m[1]}` : null;
}

function textoNova(o) {
  return (
    `🟠 *Nova O.S. Sebrae*\n` +
    `*${o.os}* · ${o.data}\n` +
    `Fluxo: *${o.fluxo}*\n` +
    `Empresa: ${o.empresa}\n` +
    `Solicitante: ${o.solicitante}\n` +
    `Valor: R$ ${o.valor}\n` +
    (o.objeto ? `Objeto: ${o.objeto}\n` : "") +
    `\nAceite é seu: ${PORTAL}`
  );
}

function textoMudou(o, antes) {
  return (
    `🔵 *O.S. Sebrae mudou de fluxo*\n` +
    `*${o.os}* · ${o.data} · R$ ${o.valor}\n` +
    `${antes} → *${o.fluxo}*`
  );
}

async function estadoLer(db, chave) {
  const r = await db.query("select valor from sebrae_os_vigia_estado where chave=$1", [chave]);
  return r.rows[0]?.valor ?? null;
}

async function estadoGravar(db, chave, valor) {
  await db.query(
    `insert into sebrae_os_vigia_estado (chave, valor, atualizado_em) values ($1,$2,now())
     on conflict (chave) do update set valor=excluded.valor, atualizado_em=now()`,
    [chave, String(valor)]
  );
}

async function tratarFalha(db, erro) {
  const falhas = parseInt((await estadoLer(db, "falhas_seguidas")) || "0", 10) + 1;
  await estadoGravar(db, "falhas_seguidas", falhas);
  await estadoGravar(db, "ultimo_erro", String(erro).slice(0, 500));
  log(`falha ${falhas}: ${erro}`);
  const limite = parseInt(FALHAS_PARA_AVISAR, 10);
  const hoje = new Date().toLocaleDateString("pt-BR", { timeZone: TZ });
  const avisadoEm = await estadoLer(db, "falha_avisada_em");
  if (falhas >= limite && avisadoEm !== hoje) {
    const motivo = /login|senha|usuario|Entrar/i.test(String(erro))
      ? "login recusado (senha mudou?)"
      : String(erro).slice(0, 160);
    const ok = await enviarWhatsApp(
      `⚠️ *Vigia de O.S. Sebrae*\nNão consigo acessar o portal há ${falhas} rodadas (~${(falhas * 30) / 60}h).\nMotivo: ${motivo}`,
      { apenasPrimeiro: true } // problema técnico é só do Rafael
    );
    if (ok) await estadoGravar(db, "falha_avisada_em", hoje);
  }
}

async function main() {
  if (!dentroDaJanela()) {
    log("fora da janela (seg–sáb, " + JANELA_INICIO + "h–" + JANELA_FIM + "h BRT); nada a fazer");
    return;
  }
  const db = new Client({ connectionString: PG_URL });
  await db.connect();
  try {
    let lista;
    try {
      lista = await buscarOS();
    } catch (e) {
      await tratarFalha(db, e.message || e);
      return;
    }
    await estadoGravar(db, "falhas_seguidas", 0);
    await estadoGravar(db, "ultima_leitura_ok", new Date().toISOString());

    const vistas = await db.query(
      "select os, fluxo from sebrae_os_vistas where conta=$1", [CONTA]
    );
    const baseline = vistas.rows.length === 0;
    const mapa = new Map(vistas.rows.map((r) => [r.os, r.fluxo]));
    let novas = 0, mudadas = 0;

    for (const o of lista) {
      if (!o.os) continue;
      const fluxoAntes = mapa.get(o.os);
      if (fluxoAntes === undefined) {
        const avisar = !baseline;
        const enviado = avisar ? await enviarWhatsApp(textoNova(o)) : false;
        if (avisar && !enviado) {
          // Nao grava: na proxima rodada ela aparece como nova de novo e o aviso e retentado.
          log(`aviso de ${o.os} falhou; fica para a proxima rodada`);
          continue;
        }
        await db.query(
          `insert into sebrae_os_vistas
             (conta, os, data_os, solicitante, equipe, empresa, objeto, fluxo, valor, avisado_em)
           values ($1,$2,$3,$4,$5,$6,$7,$8,$9, case when $10 then now() end)
           on conflict (conta, os) do nothing`,
          [CONTA, o.os, dataBR(o.data), o.solicitante, o.equipe, o.empresa, o.objeto,
           o.fluxo, o.valor, enviado]
        );
        novas++;
      } else if (fluxoAntes !== o.fluxo) {
        const enviado = await enviarWhatsApp(textoMudou(o, fluxoAntes));
        if (!enviado) {
          log(`aviso de mudança de ${o.os} falhou; fica para a proxima rodada`);
          continue;
        }
        await db.query(
          `update sebrae_os_vistas set fluxo=$3, valor=$4, atualizado_em=now(),
             avisado_em = case when $5 then now() else avisado_em end
           where conta=$1 and os=$2`,
          [CONTA, o.os, o.fluxo, o.valor, enviado]
        );
        mudadas++;
      }
    }
    log(`lidas=${lista.length} novas=${novas} mudadas=${mudadas}${baseline ? " (baseline, sem aviso)" : ""}`);
  } finally {
    await db.end();
  }
}

main().catch((e) => {
  log("erro fatal:", e.message || e);
  process.exit(1);
});
