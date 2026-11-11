// Vigia da certidao do FGTS (CRF) no Portal Sebrae de credenciados.
// Roda 1x por dia (timer 11h50 BRT, depois que o Claudinho emitiu e entregou a
// CRF no WhatsApp): le no banco do Portal Linski a CRF regular vigente mais nova
// da Linski; se a validade dela e MAIOR que a ja cadastrada no portal do Sebrae,
// cadastra pela rota /crf-incluir do scraper (a mesma do playbook
// sebrae-cadastrar-certidao-negativa-fgts) e avisa o Rafael no WhatsApp.
// Sem certidao nova = so loga. Falha = UMA tentativa so (decisao do Rafael,
// 11/11/2026): avisa o motivo, manda o PDF no WhatsApp para ele anexar na mao e
// nao tenta de novo aquela certidao. DRY_RUN=1 mostra o que faria, sem tocar no
// portal; CRF_TESTE_AVISO=1 manda o aviso de falha + PDF marcado como TESTE.

const fs = require("fs");
const { Client } = require("pg");
const comum = require("./comum");

const {
  CRF_CNPJ = "61062750000182",
  CRF_CONTA = "rafael", // login do Sebrae em que a certidao e cadastrada
  // Os segredos ficam onde ja estao; o vigia le so a variavel de que precisa.
  CRF_ENV_PORTAL = "/opt/das-mei-worker/.env", // SUPABASE_DB_URL (banco do Portal Linski)
  CRF_ENV_TOKEN = "/opt/ia-hub/.env", // NF_TOKEN da rota do scraper
  SCRAPER_URL = "http://127.0.0.1:8001",
  DRY_RUN = "",
  CRF_IGNORAR_ESTADO = "", // teste: age como se o portal nao tivesse nada
  CRF_TESTE_AVISO = "", // teste: so manda o aviso de falha + PDF (marcado TESTE)
  EVO_URL,
  EVO_KEY,
  EVO_INSTANCE = "claudinho",
  DESTINO_TECNICO,
  DESTINOS,
} = process.env;

// Aviso da certidao vai so para o Rafael (mesmo destino do alerta tecnico).
const destinoRafael = (DESTINO_TECNICO || "").trim() || (DESTINOS || "").split(",")[0].trim();

const v = comum.criar({ nome: "vigia-crf", prefixoEstado: "crf", minutosPorRodada: 1440 });
const { log } = v;

// Le UMA variavel de um .env sem carregar o resto (as senhas do Sebrae so ficam la).
function lerVar(arquivo, nome) {
  const linha = fs.readFileSync(arquivo, "utf8").split(/\r?\n/).find((l) => l.startsWith(`${nome}=`));
  if (!linha) throw new Error(`${nome} nao esta em ${arquivo}`);
  return linha.slice(nome.length + 1).trim().replace(/^["']|["']$/g, "");
}

const br = (iso) => iso.split("-").reverse().join("/"); // 2026-12-09 -> 09/12/2026

async function certidaoMaisNova() {
  const db = new Client({
    connectionString: lerVar(CRF_ENV_PORTAL, "SUPABASE_DB_URL"),
    ssl: { rejectUnauthorized: false },
  });
  await db.connect();
  try {
    const r = await db.query(
      `select crf_numero,
              to_char(validade_inicio, 'YYYY-MM-DD') as ini,
              to_char(validade_fim, 'YYYY-MM-DD') as fim,
              pdf_base64
         from fgts_crf_certidoes
        where cnpj = $1 and situacao = 'REGULAR' and pdf_base64 is not null
          and validade_inicio <= (now() at time zone 'America/Sao_Paulo')::date
          and validade_fim >= (now() at time zone 'America/Sao_Paulo')::date
        order by validade_fim desc, gerada_em desc
        limit 1`,
      [CRF_CNPJ]
    );
    return r.rows[0] || null;
  } finally {
    await db.end();
  }
}

// POST /crf-incluir com ate=fim. A rota confere tipo, datas, vinculo Socio e
// arquivo lidos da tela antes de salvar, e so devolve gravou=true se o
// "Enviada em" da linha mudou. 9 min: a rota espera a fila da sessao do Sebrae.
async function cadastrar(crf) {
  const corpo = {
    tipo: "FGTS",
    data_emissao: br(crf.ini),
    data_validade: br(crf.fim),
    vinculo: "socio",
    conta: CRF_CONTA,
    ate: "fim",
    permitir_duplicada: false,
    nome_arquivo: `crf_${CRF_CNPJ}_${crf.ini.replace(/-/g, "")}.pdf`,
    pdf_base64: crf.pdf_base64.split(",").pop(),
  };
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 540000);
  try {
    const r = await fetch(`${SCRAPER_URL}/crf-incluir`, {
      method: "POST",
      signal: ctrl.signal,
      headers: { "x-nf-token": lerVar(CRF_ENV_TOKEN, "NF_TOKEN"), "Content-Type": "application/json" },
      body: JSON.stringify(corpo),
    });
    return await r.json().catch(() => ({ sucesso: false, erro: `HTTP ${r.status} sem JSON` }));
  } finally {
    clearTimeout(timer);
  }
}

function textoOk(crf, linha) {
  return (
    `✅ *Certidão do FGTS atualizada no portal do Sebrae*\n` +
    `CRF nº ${crf.crf_numero}\n` +
    `Validade: ${br(crf.ini)} a ${br(crf.fim)}\n` +
    `Status no portal: ${linha.status || "?"} (enviada em ${linha.enviada_em || "?"})\n` +
    `O Sebrae ainda vai validar; não precisa fazer nada.`
  );
}

function textoFalha(crf, erro, teste = false) {
  return (
    (teste ? "🧪 *TESTE — nada aconteceu no portal*\n" : "") +
    `⚠️ *Certidão do FGTS — não consegui cadastrar no portal do Sebrae*\n` +
    `CRF nº ${crf.crf_numero} (válida de ${br(crf.ini)} a ${br(crf.fim)})\n` +
    `Motivo: ${String(erro).slice(0, 220)}\n\n` +
    `Não vou tentar de novo. O PDF vem logo abaixo para você anexar em ` +
    `*Financeiro › Certidão Negativa › Incluir*: tipo FGTS, emissão ${br(crf.ini)}, ` +
    `validade ${br(crf.fim)}, vínculo Sócio.\n` +
    `Se o portal disser que essa validade já existe, é porque chegou a gravar — aí é só conferir.`
  );
}

async function enviarPdf(crf) {
  if (!EVO_URL || !EVO_KEY || !destinoRafael) {
    log("sem credencial Evolution/destino; PDF não enviado");
    return false;
  }
  try {
    const r = await fetch(`${EVO_URL}/message/sendMedia/${EVO_INSTANCE}`, {
      method: "POST",
      headers: { apikey: EVO_KEY, "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify({
        number: destinoRafael,
        mediatype: "document",
        mimetype: "application/pdf",
        fileName: `CRF FGTS - 61.062.750 LTDA - valida ate ${br(crf.fim).replace(/\//g, "-")}.pdf`,
        media: crf.pdf_base64.split(",").pop(),
      }),
    });
    if (!r.ok) {
      log(`envio do PDF falhou HTTP ${r.status}: ${(await r.text().catch(() => "")).slice(0, 200)}`);
      return false;
    }
    return true;
  } catch (e) {
    log("erro ao enviar o PDF:", e.message || e);
    return false;
  }
}

// Aviso de falha = texto + PDF. O que nao sair fica em "aviso_falha_pendente"
// ("tudo" ou "pdf") e e retentado na rodada seguinte -- o portal, nao.
async function avisarFalha(db, crf, erro, falta = "tudo", teste = false) {
  let okTexto = falta === "pdf";
  if (!okTexto) okTexto = await v.enviarWhatsApp(textoFalha(crf, erro, teste), { tecnico: true });
  const okPdf = okTexto ? await enviarPdf(crf) : false;
  const pendente = !okTexto ? "tudo" : !okPdf ? "pdf" : "";
  if (!teste) await v.estadoGravar(db, "aviso_falha_pendente", pendente);
  log(`aviso de falha: texto ${okTexto ? "ok" : "falhou"}, PDF ${okPdf ? "ok" : "falhou"}`);
  return !pendente;
}

async function main() {
  await v.comBanco(async (db) => {
    // Aviso de sucesso que nao saiu na rodada anterior: tenta antes de tudo.
    const pendente = await v.estadoLer(db, "aviso_pendente");
    if (pendente && !DRY_RUN) {
      if (await v.enviarWhatsApp(pendente, { tecnico: true })) {
        await v.estadoGravar(db, "aviso_pendente", "");
        log("aviso pendente enviado");
      }
    }

    const crf = await certidaoMaisNova();
    if (!crf) {
      log("nenhuma CRF regular vigente no Portal; nada a fazer (irregularidade o Claudinho avisa)");
      return;
    }

    if (CRF_TESTE_AVISO) {
      await avisarFalha(db, crf, "isto é um teste do aviso; o portal não foi acessado", "tudo", true);
      return;
    }

    // Ja tentou ESTA certidao e falhou: nao tenta de novo (o Rafael anexa na mao).
    // So completa o aviso que nao tiver saido. A proxima certidao (validade maior)
    // volta a ser tentada normalmente.
    const falhou = await v.estadoLer(db, "falhou_validade");
    if (falhou && crf.fim <= falhou) {
      const falta = await v.estadoLer(db, "aviso_falha_pendente");
      if (falta === "tudo" || falta === "pdf") {
        await avisarFalha(db, crf, (await v.estadoLer(db, "ultimo_erro")) || "?", falta);
      }
      log(`CRF ${crf.crf_numero} falhou antes; aguardando o cadastro manual, não tento de novo`);
      return;
    }

    const noPortal = CRF_IGNORAR_ESTADO ? null : await v.estadoLer(db, "validade_portal");
    if (noPortal && crf.fim <= noPortal) {
      log(`CRF ${crf.crf_numero} (até ${br(crf.fim)}) já está no portal; nada a fazer`);
      return;
    }

    if (DRY_RUN) {
      log(`[ensaio] cadastraria CRF ${crf.crf_numero} ${br(crf.ini)} a ${br(crf.fim)} ` +
          `(no portal: ${noPortal ? br(noPortal) : "desconhecido"}); PDF ${crf.pdf_base64.length} chars`);
      log(`[ensaio] aviso que sairia:\n${textoOk(crf, { status: "Pendente de Verificação Sebrae", enviada_em: "(agora)" })}`);
      return;
    }

    log(`cadastrando CRF ${crf.crf_numero} ${br(crf.ini)} a ${br(crf.fim)} (no portal: ${noPortal || "?"})`);
    let res;
    try {
      res = await cadastrar(crf);
    } catch (e) {
      res = { sucesso: false, erro: e.name === "AbortError" ? "a rota não respondeu em 9 min" : (e.message || String(e)) };
    }

    if (res.sucesso && res.gravou) {
      await v.estadoGravar(db, "validade_portal", crf.fim);
      const texto = textoOk(crf, res.linha_nova || {});
      const ok = await v.enviarWhatsApp(texto, { tecnico: true });
      if (!ok) await v.estadoGravar(db, "aviso_pendente", texto);
      log(`gravou (enviada em ${res.linha_nova?.enviada_em}); aviso ${ok ? "enviado" : "falhou, fica pendente"}`);
      return;
    }

    // O portal ja tem essa validade (ex.: cadastrada na mao): so registra.
    if (res.duplicada && res.duplicada.length) {
      await v.estadoGravar(db, "validade_portal", crf.fim);
      const d = res.duplicada[0];
      log(`portal já tinha a CRF até ${br(crf.fim)} (${d.status}, enviada em ${d.enviada_em}); estado atualizado, sem aviso`);
      return;
    }

    const erro = res.erro || res.detail || "resposta sem sucesso";
    log(`falhou: ${erro} -- não vou tentar de novo esta certidão`);
    await v.estadoGravar(db, "falhou_validade", crf.fim);
    await v.estadoGravar(db, "ultimo_erro", String(erro).slice(0, 500));
    await avisarFalha(db, crf, erro);
  });
}

main().catch((e) => {
  log("erro fatal:", e.message || e);
  process.exit(1);
});
