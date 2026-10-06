// Vigia de notas fiscais do Portal Sebrae de credenciados.
// Roda de hora em hora (systemd timer na KVM8; janela 7h-20h BRT no script),
// pede ao scraper a lista de Financeiro > Consultar Nota Fiscal, compara com
// o que ja viu (tabela sebrae_nf_vistas, banco apollo) e avisa no WhatsApp
// (Evolution, instancia claudinho) quando uma nota aparece, muda de status ou
// ganha data (recebimento, apropriacao, pagamento). Primeira rodada so grava
// (baseline), nao avisa. Pecas comuns (envio, scraper, falhas, banco) em comum.js.

const comum = require("./comum");

const {
  CONTA = "rafael",
  // Janela em horas BRT (inclusive). Nota nao tem prazo de aceite e de madrugada
  // ninguem do Sebrae mexe nela. Nomes com NF_ para nao herdar a janela 0-23 do
  // vigia de O.S., que divide o mesmo .env.
  NF_JANELA_INICIO = "7",
  NF_JANELA_FIM = "20",
} = process.env;

const v = comum.criar({ nome: "vigia-nf", prefixoEstado: "nf", minutosPorRodada: 60 });
const { log, nomeConta } = v;

const CAMPOS_DATA = ["data_recebimento", "data_apropriacao", "data_pagamento"];
const EM_CADASTRAMENTO = /em cadastramento/i;
const RUIM = /cancelada|reprovada/i;
const PAGA = /^paga$/i;

function dentroDaJanela() {
  const { hora } = comum.agoraBR();
  return hora >= parseInt(NF_JANELA_INICIO, 10) && hora <= parseInt(NF_JANELA_FIM, 10);
}

async function buscarNotas() {
  // Janeiro e fevereiro: le tambem o ano anterior (nota de dezembro paga em janeiro).
  const anoAnterior = comum.agoraBR().mes <= 2;
  const corpo = await v.chamarScraper(
    `/nf-credenciado?conta=${encodeURIComponent(CONTA)}&ano_anterior=${anoAnterior}`
  );
  return corpo.notas || [];
}

// Chave da nota: o codigo interno do portal; sem ele, numero da NF + O.S.
const chaveDe = (n) => n.codigo || `nf:${n.nf}|os:${n.os}`;

const cabecalho = (n) => `NF *${n.nf}* · O.S. ${n.os} · R$ ${n.valor}`;

function linhaDatas(n) {
  const partes = [];
  if (n.data_recebimento) partes.push(`Recebida em ${n.data_recebimento}`);
  if (n.data_apropriacao) partes.push(`Apropriada em ${n.data_apropriacao}`);
  if (n.data_pagamento) partes.push(`Pagamento: ${n.data_pagamento}`);
  return partes.join(" · ");
}

// Nota que apareceu no portal (ou saiu de "Em cadastramento").
function textoNova(n) {
  const datas = linhaDatas(n);
  return `🧾 *Nota fiscal Sebrae — ${nomeConta}*\n${cabecalho(n)}\nStatus: *${n.status}*` +
    (datas ? `\n${datas}` : "");
}

// O que mudou entre a leitura anterior (antes) e a de agora (n).
// "Ultima interacao" (nome do analista) e valor mudam em silencio.
function detectarMudancas(antes, n) {
  const m = [];
  if ((antes.status || "") !== (n.status || "")) m.push("status");
  for (const c of CAMPOS_DATA) if ((antes[c] || "") !== (n[c] || "")) m.push(c);
  return m;
}

function textoMudou(n, antes, mudancas) {
  const cab = cabecalho(n);
  const datas = linhaDatas(n);
  if (mudancas.includes("status")) {
    if (EM_CADASTRAMENTO.test(antes.status || "")) return textoNova(n);
    if (PAGA.test(n.status)) {
      return `✅ *Nota paga — ${nomeConta}*\n${cab}` +
        (n.data_pagamento ? `\nPaga em ${n.data_pagamento}` : "");
    }
    if (RUIM.test(n.status)) {
      const tipo = /cancelada/i.test(n.status) ? "cancelada" : "reprovada";
      return `🔴 *Nota ${tipo} — ${nomeConta}*\n${cab}\n${antes.status} → *${n.status}*\n` +
        `Precisa emitir uma nova nota.`;
    }
    return `🔵 *Nota fiscal Sebrae — ${nomeConta}*\n${cab}\n${antes.status} → *${n.status}*` +
      (datas ? `\n${datas}` : "");
  }
  // So data mudou, status igual.
  let detalhe;
  if (mudancas.includes("data_pagamento") && n.data_pagamento) {
    detalhe = PAGA.test(n.status)
      ? `paga em *${n.data_pagamento}*`
      : `pagamento previsto para *${n.data_pagamento}*`;
  } else if (mudancas.includes("data_apropriacao") && n.data_apropriacao) {
    detalhe = `apropriada em *${n.data_apropriacao}*`;
  } else if (mudancas.includes("data_recebimento") && n.data_recebimento) {
    detalhe = `recebida em *${n.data_recebimento}*`;
  } else {
    detalhe = `datas atualizadas: ${datas || "nenhuma"}`;
  }
  return `📅 *Nota fiscal Sebrae — ${nomeConta}*\n${cab}\n${n.status} · ${detalhe}`;
}

async function main() {
  if (!dentroDaJanela()) {
    log(`fora da janela (${NF_JANELA_INICIO}h–${NF_JANELA_FIM}h BRT); nada a fazer`);
    return;
  }
  await v.comBanco(async (db) => {
    let lista;
    try {
      lista = await buscarNotas();
    } catch (e) {
      await v.tratarFalha(db, e.message || e, "Vigia de notas Sebrae");
      return;
    }
    await v.leituraOk(db);

    const vistas = await db.query(
      `select codigo, status, valor, ultima_interacao,
              to_char(data_recebimento, 'DD/MM/YYYY') as data_recebimento,
              to_char(data_apropriacao, 'DD/MM/YYYY') as data_apropriacao,
              to_char(data_pagamento,   'DD/MM/YYYY') as data_pagamento
         from sebrae_nf_vistas where conta=$1`,
      [CONTA]
    );
    const baseline = vistas.rows.length === 0;
    const mapa = new Map(vistas.rows.map((r) => [r.codigo, r]));
    let novas = 0, mudadas = 0;

    for (const n of lista) {
      if (!n.nf) continue;
      const chave = chaveDe(n);
      const antes = mapa.get(chave);

      if (!antes) {
        // "Em cadastramento" = ainda sendo preenchida; o aviso sai quando for enviada.
        const avisar = !baseline && !EM_CADASTRAMENTO.test(n.status || "");
        const enviado = avisar ? await v.enviarWhatsApp(textoNova(n)) : false;
        if (avisar && !enviado) {
          // Nao grava: na proxima rodada ela aparece como nova de novo e o aviso e retentado.
          log(`aviso da NF ${n.nf} falhou; fica para a proxima rodada`);
          continue;
        }
        await db.query(
          `insert into sebrae_nf_vistas
             (conta, codigo, nf, os, empresa, credenciado, valor, optante_simples, status,
              data_recebimento, data_apropriacao, data_pagamento, ultima_interacao, avisado_em)
           values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13, case when $14 then now() end)
           on conflict (conta, codigo) do nothing`,
          [CONTA, chave, n.nf, n.os, n.empresa, n.credenciado, n.valor, !!n.optante_simples, n.status,
           comum.dataBR(n.data_recebimento), comum.dataBR(n.data_apropriacao), comum.dataBR(n.data_pagamento),
           n.ultima_interacao, enviado]
        );
        novas++;
        continue;
      }

      const mudancas = detectarMudancas(antes, n);
      let enviado = false;
      if (mudancas.length) {
        enviado = await v.enviarWhatsApp(textoMudou(n, antes, mudancas));
        if (!enviado) {
          log(`aviso de mudança da NF ${n.nf} falhou; fica para a proxima rodada`);
          continue;
        }
        mudadas++;
      } else if ((antes.ultima_interacao || "") === (n.ultima_interacao || "") &&
                 (antes.valor || "") === (n.valor || "")) {
        continue; // nada mudou
      }
      await db.query(
        `update sebrae_nf_vistas
            set status=$3, valor=$4, data_recebimento=$5, data_apropriacao=$6, data_pagamento=$7,
                ultima_interacao=$8, atualizado_em=now(),
                avisado_em = case when $9 then now() else avisado_em end
          where conta=$1 and codigo=$2`,
        [CONTA, chave, n.status, n.valor, comum.dataBR(n.data_recebimento), comum.dataBR(n.data_apropriacao),
         comum.dataBR(n.data_pagamento), n.ultima_interacao, enviado]
      );
    }
    log(`lidas=${lista.length} novas=${novas} mudadas=${mudadas}${baseline ? " (baseline, sem aviso)" : ""}`);
  });
}

module.exports = { textoNova, textoMudou, detectarMudancas };

if (require.main === module) {
  main().catch((e) => {
    log("erro fatal:", e.message || e);
    process.exit(1);
  });
}
