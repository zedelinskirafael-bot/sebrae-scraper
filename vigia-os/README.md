# Vigias do Portal Sebrae de credenciados — O.S. e notas fiscais

O portal do Sebrae não avisa nada: nem quando cai uma O.S. para aceite, nem quando uma
nota fiscal muda de status. Dois vigias na KVM8 fazem isso com o mesmo scraper, o mesmo
`.env` por conta e o mesmo canal: WhatsApp pessoal do **Rafael e da Geovana** pela Evolution
(`claudinho`), com pausa aleatória de 30 s a 2 min entre os dois envios. Todo aviso diz de
quem é a conta no título ("— Rafael" / "— Geovana"). Alerta técnico (portal fora, senha) vai
só para o Rafael.

| Vigia | Tela do portal | Rodada | Script | Tabela |
|-------|----------------|--------|--------|--------|
| O.S. (desde 05/10/2026) | Contratação › Consultar Os | 15 min (Rafael) / 20 min (Geovana), 24h/7d | `vigia.js` | `sebrae_os_vistas` |
| Notas fiscais (desde 06/10/2026) | Financeiro › Consultar Nota Fiscal | de hora em hora, 7h–20h BRT | `vigia-nf.js` | `sebrae_nf_vistas` |

Peças comuns (envio, pausa entre destinos, chamada ao scraper, contador de falhas por
conta, banco) em `comum.js`. Estado em `sebrae_os_vigia_estado`, chaves `os:<conta>:*` e
`nf:<conta>:*`.

**Duas contas, mesmo script, `.env` diferentes.** As senhas do Sebrae ficam em
`/opt/ia-hub/.env` (`SEBRAE_USER`/`SEBRAE_PASS` e `SEBRAE_USER_GEOVANA`/`SEBRAE_PASS_GEOVANA`),
entregues ao contêiner pelo compose; o scraper escolhe pela query `?conta=rafael|geovana`.
Quando dois timers caem juntos, o `SMART_LOCK` do scraper enfileira um atrás do outro.

| Conta lida | Timer de O.S. | Timer de notas | Ordem do aviso | Env |
|-----------|---------------|----------------|----------------|-----|
| Rafael | `sebrae-os-vigia.timer` (:00 :15 :30 :45) | `sebrae-nf-vigia.timer` (:10) | Rafael → Geovana | `/opt/sebrae-os-vigia/.env` |
| Geovana | `sebrae-os-vigia-geovana.timer` (:05 :25 :45) | `sebrae-nf-vigia-geovana.timer` (:40) | Geovana → Rafael | `/opt/sebrae-os-vigia/geovana.env` |

## Vigia de O.S.

Olha **Contratação › Consultar Os** e avisa quando aparece O.S. nova ou quando o "Fluxo atual"
muda (ex.: Pendente → Aprovada). O prazo de aceite é de **3 horas**; depois a O.S. vai para
outra empresa, por isso 15 min e 24h/7d (decisão do Rafael, 06/10/2026).

- Rota do scraper: `GET /os-credenciado?conta=` (~22 s). Lista ordenada da mais recente para a
  mais antiga, 10 por página — O.S. nova sempre está na 1ª página, por isso não há paginação.
- Falha: avisa só o Rafael na 6ª seguida (1h30), uma vez por dia.
- Log `/var/log/sebrae-os-vigia.log`.

## Vigia de notas fiscais

Olha **Financeiro › Consultar Nota Fiscal** (filtro Status = Todas, ano corrente). Status
possíveis no portal: Em cadastramento → Enviada ao Sebrae → Recebida no Sebrae → Enviada para
UCF → Pré-apropriada → Paga; ou Cancelada / Reprovada Importador / Reprovada Sebrae.

Avisos:
- 🧾 nota apareceu no portal (ou saiu de "Em cadastramento");
- 🔵 mudou de status (Recebida no Sebrae, Enviada para UCF, Pré-apropriada);
- 📅 ganhou data com o mesmo status — "pagamento previsto para DD/MM/AAAA" quando a data de
  pagamento aparece em uma nota ainda não paga;
- ✅ Paga, com a data;
- 🔴 Cancelada ou Reprovada — "Precisa emitir uma nova nota".

Mudança só em "Última interação" (nome do analista) ou no valor grava em silêncio, não avisa.

- Rota do scraper: `GET /nf-credenciado?conta=rafael|geovana[&ano_anterior=true]` (~20 s).
  Pede 99 registros por página (sem paginar; se o portal ignorar, anda com "Próximo"). Em
  janeiro e fevereiro o script manda ler também o ano anterior na mesma sessão (nota de
  dezembro é paga em janeiro).
- Chave da nota = "Código Nota Fiscal" interno do portal (fica no `title` da célula de status);
  sem ele, `nf:<número>|os:<O.S.>`.
- Janela própria `NF_JANELA_INICIO`/`NF_JANELA_FIM` (padrão 7–20 BRT), porque o `.env` é
  dividido com o vigia de O.S., que roda 0–23.
- Falha: avisa só o Rafael na 6ª seguida (6h), uma vez por dia.
- Log `/var/log/sebrae-nf-vigia.log`.

## Regras comuns

- **Primeira rodada = baseline**: grava tudo e não avisa.
- **Aviso que falhou não grava**: na próxima rodada a mudança é detectada de novo e o envio
  é retentado (sem duplicar para quem já recebeu).
- **Layout mudou** (portal diz que há linhas/páginas e o leitor não lê nenhuma): falha alta,
  nunca `[]` como sucesso.
- **Sessão única do Sebrae**: o scraper serializa toda rota que abre navegador
  (`SMART_LOCK`). Os vigias esperam o worker da Máquina de Vendas terminar, nunca derrubam a
  sessão dele.
- Ir direto na URL `/credenciado/Home.do` **não funciona** sem passar pelo botão
  `RedirecionaPCR.do` do Menu Geral (cai no login do SAS).

## Operar

```bash
systemctl list-timers | grep sebrae                # próximas rodadas (4 timers)
systemctl start sebrae-nf-vigia.service            # rodar agora (idem -geovana, sebrae-os-vigia)
tail -20 /var/log/sebrae-os-vigia.log /var/log/sebrae-nf-vigia.log
curl -s "127.0.0.1:8001/nf-credenciado?conta=rafael" | jq   # só a leitura (~20 s)
```

Desligar: `systemctl disable --now sebrae-os-vigia.timer sebrae-os-vigia-geovana.timer sebrae-nf-vigia.timer sebrae-nf-vigia-geovana.timer`.

Zerar baseline (vai avisar tudo de novo na próxima rodada, cuidado):
`delete from sebrae_os_vistas where conta='rafael';` / `delete from sebrae_nf_vistas where conta='rafael';`

Testar o aviso de notas sem esperar o Sebrae (feito em 06/10/2026): voltar o status de UMA nota
no banco, pelo `codigo`, e rodar o script com `DESTINOS` só do Rafael:

```bash
psql "$PG" -c "update sebrae_nf_vistas set status='Enviada ao Sebrae', data_recebimento=null where conta='rafael' and codigo='296720'"
cd /opt/sebrae-os-vigia && set -a && . ./.env && set +a && DESTINOS=55DDDNUMERO node vigia-nf.js
```

## Deploy

Scraper: igual ao resto do repo (`scp main.py root@kvm8:/root/sebrae-scraper-build/` →
`docker tag ...:latest ...:bak-AAAAMMDD` → `docker build -t easypanel/pap/sebrae-scraper:latest .`
→ `cd /opt/ia-hub && docker compose up -d --no-deps --force-recreate sebrae-scraper`). Fazer
fora dos minutos dos timers para não derrubar uma leitura em andamento.

Vigias: `scp vigia-os/{comum.js,vigia.js,vigia-nf.js,package.json} root@kvm8:/opt/sebrae-os-vigia/`
(+ `npm i --omit=dev` se mudar dependência); units em `/etc/systemd/system/` + `systemctl daemon-reload`.

Tabela nova no banco `apollo`: criar como `postgres` (`apollo_fn` não cria no schema public),
`owner postgres` + `grant insert, select, update, delete on ... to apollo_fn` — igual às de O.S.
