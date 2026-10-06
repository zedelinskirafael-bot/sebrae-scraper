// Vigia de O.S. do Portal Sebrae de credenciados.
// Roda a cada 15 min (systemd timer na KVM8), pede ao scraper a lista de
// Contratacao > Consultar Os, compara com o que ja viu (tabela
// sebrae_os_vistas, banco apollo) e avisa no WhatsApp (Evolution, instancia
// claudinho) quando aparece O.S. nova ou quando o "Fluxo atual" muda.
// Primeira rodada so grava (baseline), nao avisa. Pecas comuns (envio,
// scraper, falhas, banco) em comum.js, divididas com o vigia de notas.

const comum = require("./comum");

const {
  CONTA = "rafael",
  // Janela de funcionamento em horas BRT (inclusive). Padrao 0-23 = 24h/7d,
  // decisao do Rafael em 06/10/2026: O.S. pode cair a qualquer hora e o prazo
  // de aceite (3h) corre mesmo de madrugada e no domingo.
  JANELA_INICIO = "0",
  JANELA_FIM = "23",
} = process.env;

const v = comum.criar({ nome: "vigia-os", prefixoEstado: "os", minutosPorRodada: 15 });
const { log, nomeConta } = v;

function dentroDaJanela() {
  const { hora } = comum.agoraBR();
  return hora >= parseInt(JANELA_INICIO, 10) && hora <= parseInt(JANELA_FIM, 10);
}

async function buscarOS() {
  const corpo = await v.chamarScraper(`/os-credenciado?conta=${encodeURIComponent(CONTA)}`);
  return corpo.os || [];
}

// Todo aviso diz de quem e a conta (Rafael ou Geovana) no titulo.
function textoNova(o) {
  return (
    `🟠 *Nova O.S. Sebrae — ${nomeConta}*\n` +
    `*${o.os}* · ${o.data}\n` +
    `Fluxo: *${o.fluxo}*\n` +
    `Empresa: ${o.empresa}\n` +
    `Solicitante: ${o.solicitante}\n` +
    `Valor: R$ ${o.valor}\n` +
    (o.objeto ? `Objeto: ${o.objeto}\n` : "") +
    `\nAceite é seu: ${comum.PORTAL}`
  );
}

function textoMudou(o, antes) {
  return (
    `🔵 *O.S. Sebrae mudou de fluxo — ${nomeConta}*\n` +
    `*${o.os}* · ${o.data} · R$ ${o.valor}\n` +
    `${antes} → *${o.fluxo}*`
  );
}

async function main() {
  if (!dentroDaJanela()) {
    log("fora da janela (" + JANELA_INICIO + "h–" + JANELA_FIM + "h BRT); nada a fazer");
    return;
  }
  await v.comBanco(async (db) => {
    let lista;
    try {
      lista = await buscarOS();
    } catch (e) {
      await v.tratarFalha(db, e.message || e, "Vigia de O.S. Sebrae");
      return;
    }
    await v.leituraOk(db);

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
        const enviado = avisar ? await v.enviarWhatsApp(textoNova(o)) : false;
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
          [CONTA, o.os, comum.dataBR(o.data), o.solicitante, o.equipe, o.empresa, o.objeto,
           o.fluxo, o.valor, enviado]
        );
        novas++;
      } else if (fluxoAntes !== o.fluxo) {
        const enviado = await v.enviarWhatsApp(textoMudou(o, fluxoAntes));
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
  });
}

module.exports = { textoNova, textoMudou };

if (require.main === module) {
  main().catch((e) => {
    log("erro fatal:", e.message || e);
    process.exit(1);
  });
}
