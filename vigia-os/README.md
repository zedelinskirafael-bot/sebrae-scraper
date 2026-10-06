# Vigia de O.S. — Portal Sebrae de credenciados

O portal do Sebrae não avisa quando cai uma O.S. nova para aceite. Este vigia olha
**Contratação › Consultar Os** a cada 15 min e manda WhatsApp quando aparece O.S.
nova ou quando o "Fluxo atual" de uma O.S. muda (ex.: Pendente → Aprovada). O prazo de
aceite é de **3 horas**; depois a O.S. vai para outra empresa, por isso a urgência.

Avisa o **Rafael e a Geovana** (WhatsApp pessoal de cada um), com pausa aleatória de
30 s a 2 min entre os dois envios para não estressar a instância. Alerta técnico
(portal fora, senha) vai só para o Rafael.

Só a conta do Rafael (05/10/2026). A da Geovana entra quando ele decidir guardar a
senha dela na VPS, igual à dele já está (`/opt/ia-hub/.env`, usada pelo scraper).

## Peças (KVM8)

| Peça | Onde | Papel |
|------|------|-------|
| `GET /os-credenciado` | contêiner `sebrae-scraper` (porta 127.0.0.1:8001) | Login → botão Portal do Credenciado (aba nova) → Consultar Os → devolve as linhas da tabela |
| `vigia.js` | `/opt/sebrae-os-vigia` | Compara com `sebrae_os_vistas`, avisa pela Evolution (`claudinho`) |
| `sebrae-os-vigia.timer` | systemd | A cada 15 min (prazo de aceite 3h, pior caso sobra 2h45); o script só trabalha seg–sáb 7h–21h BRT |
| `sebrae_os_vistas`, `sebrae_os_vigia_estado` | banco `apollo` | O que já foi visto + contador de falhas |

Log: `/var/log/sebrae-os-vigia.log`.

## Regras

- **Primeira rodada = baseline**: grava tudo e não avisa.
- **Falha** (portal fora, login recusado): tenta na próxima rodada; avisa só na 6ª
  falha seguida (1h30 sem acesso), uma vez por dia.
- **Sessão única do Sebrae**: o scraper serializa toda rota que abre navegador
  (`SMART_LOCK`). O vigia espera o worker da Máquina de Vendas terminar, nunca
  derruba a sessão dele.
- A lista vem ordenada da mais recente para a mais antiga, 10 por página — O.S.
  nova sempre está na 1ª página, por isso não há paginação.
- Ir direto na URL `/credenciado/Home.do` **não funciona** sem passar pelo botão
  `RedirecionaPCR.do` do Menu Geral (cai no login do SAS).

## Operar

```bash
systemctl list-timers sebrae-os-vigia.timer     # próxima rodada
systemctl start sebrae-os-vigia.service         # rodar agora
tail -20 /var/log/sebrae-os-vigia.log
curl -s 127.0.0.1:8001/os-credenciado | jq      # testar só a leitura (~40s)
```

Desligar: `systemctl disable --now sebrae-os-vigia.timer`.
Zerar baseline (vai avisar tudo de novo na próxima rodada, cuidado):
`delete from sebrae_os_vistas where conta='rafael';`

## Deploy

Scraper: igual ao resto do repo (`scp main.py` → `docker build` → `docker compose up -d --no-deps --force-recreate sebrae-scraper`).
Vigia: `scp vigia-os/{vigia.js,package.json} root@kvm8:/opt/sebrae-os-vigia/` + `npm i --omit=dev`; units em `/etc/systemd/system/`.
